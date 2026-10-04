"""验证逐条、惰性、只发送一次及未知 output 语义；不访问真实服务。"""
from copy import deepcopy
import unittest

from ..common.tokenization import LengthCounter
from ..runtime import OutputHeavyPolicy, PercentileReference, RequestProcessor
from ..runtime.congestion_interface import StaticCongestionProvider, CongestionState
from ..runtime.stream import RequestProcessingError


class FakeTokenizer:
    """测试只需一个确定性 tokenizer，生产路径使用固定 Qwen tokenizer。"""
    def __init__(self):
        self.templates = []

    def apply_chat_template(self, messages, **kwargs):
        self.templates.append((deepcopy(messages), deepcopy(kwargs)))
        return list(range(5 + sum(len(message["content"]) for message in messages)))

    def encode(self, text, **kwargs):
        return list(range(len(text)))


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tokenizer = FakeTokenizer()
        self.reference = PercentileReference.from_lengths([1, 2, 2, 10])
        self.provider = StaticCongestionProvider(CongestionState.NORMAL)
        self.policy = OutputHeavyPolicy(self.reference, self.provider)

    def test_input_only_has_no_invented_output(self):
        result = RequestProcessor(self.tokenizer, output_policy=self.policy).process("hello")
        self.assertEqual(result["input_tokens"], 10)
        self.assertEqual(result["tokenization_status"], "input_only")
        for field in ["output_tokens", "total_tokens", "output_percentile", "output_heavy", "output_heavy_threshold"]:
            self.assertIsNone(result[field])

    def test_history_is_context_not_extra_requests(self):
        prompt = {"messages": [{"role": role, "content": text} for role, text in
                              [("user", "A"), ("assistant", "B"), ("user", "C")]]}
        records = [{"prompt": prompt, "response": "DD"}]
        result = list(RequestProcessor(self.tokenizer, output_policy=self.policy).process_stream(records))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["input_tokens"], 8)
        self.assertEqual(result[0]["output_tokens"], 2)
        self.assertEqual(result[0]["output_percentile"], 0.75)
        self.assertEqual(result[0]["assistant_message_count"], 1)

    def test_sender_is_lazy_and_next_request_waits(self):
        events = []

        def source():
            events.append("read0")
            yield "one"
            events.append("read1")
            yield "two"

        def sender(prompt):
            events.append("send:" + prompt["messages"][0]["content"])
            return "ok"

        iterator = RequestProcessor(self.tokenizer, sender=sender).process_stream(source())
        self.assertEqual(events, [])
        first = next(iterator)
        self.assertEqual(first["stream_index"], 0)
        self.assertEqual(events, ["read0", "send:one"])
        next(iterator)
        self.assertEqual(events, ["read0", "send:one", "read1", "send:two"])
        with self.assertRaises(StopIteration):
            next(iterator)

    def test_sender_mutation_does_not_modify_source_or_counts(self):
        record = {"messages": [{"role": "user", "content": "A"}], "temperature": 0}
        original = deepcopy(record)

        def sender(prompt):
            self.assertEqual(prompt["temperature"], 0)
            prompt["messages"][0]["content"] = "changed"
            return {"role": "assistant", "content": "B"}

        result = RequestProcessor(self.tokenizer, sender=sender).process(record)
        self.assertEqual(record, original)
        self.assertEqual(result["input_tokens"], 6)

    def test_tools_and_controls_are_preserved(self):
        calls = [{"id": "call1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        prompt = {"messages": [{"role": "user", "content": "A"},
                               {"role": "assistant", "content": None, "tool_calls": calls},
                               {"role": "tool", "content": "T", "tool_call_id": "call1"}],
                  "tools": [{"type": "function", "function": {"name": "f"}}], "tool_choice": "auto"}
        RequestProcessor(self.tokenizer).process({"prompt": prompt, "response": "B"})
        messages, kwargs = self.tokenizer.templates[-1]
        self.assertEqual(messages[1]["tool_calls"], calls)
        self.assertEqual(kwargs["tools"], prompt["tools"])
        self.assertEqual(kwargs["tool_choice"], "auto")
        self.assertTrue(kwargs["add_generation_prompt"])
        self.assertFalse(kwargs["enable_thinking"])

    def test_list_and_empty_responses(self):
        processor = RequestProcessor(self.tokenizer, output_policy=self.policy)
        record = {"prompt": {"messages": [{"role": "user", "content": [{"type": "text", "text": "A"}]}]},
                  "response": [{"type": "text", "text": "B"}, {"type": "text", "text": "C"}]}
        self.assertEqual(processor.process(record)["output_tokens"], 2)
        record["response"] = ""
        result = processor.process(record)
        self.assertEqual(result["output_tokens"], 0)
        self.assertEqual(result["output_percentile"], 0)

    def test_response_not_saved_by_default(self):
        record = {"messages": [{"role": "user", "content": "A"}], "response": "private"}
        self.assertNotIn("response", RequestProcessor(self.tokenizer).process(record))
        self.assertEqual(RequestProcessor(self.tokenizer, include_response=True).process(record)["response"], "private")

    def test_sender_failure_is_not_retried_or_logged_as_text(self):
        calls = []

        def sender(prompt):
            calls.append(1)
            raise RuntimeError("private response and credentials")

        result = next(RequestProcessor(self.tokenizer, sender=sender).process_stream(["A"], on_error="yield"))
        self.assertEqual(calls, [1])
        self.assertEqual(result["error_stage"], "send")
        self.assertNotIn("private", str(result))

    def test_invalid_request_yield_then_continue(self):
        result = list(RequestProcessor(self.tokenizer).process_stream([{}, "A"], on_error="yield"))
        self.assertEqual(result[0]["error_code"], "invalid_prompt_messages")
        self.assertEqual(result[1]["tokenization_status"], "input_only")
        self.assertEqual(result[1]["stream_index"], 1)

    def test_sender_cannot_double_send_recorded_response(self):
        calls = []
        processor = RequestProcessor(self.tokenizer, sender=lambda prompt: calls.append(1))
        with self.assertRaises(RequestProcessingError):
            processor.process({"messages": [{"role": "user", "content": "A"}], "response": "B"})
        self.assertEqual(calls, [])

    def test_shared_counter_matches_stream(self):
        context = [{"role": "user", "content": "A"}]
        offline = LengthCounter(self.tokenizer).completed_request({"context": context, "tools": None,
            "prompt_controls": {}, "response_text": "BC", "conversation_id": 12, "request_index": 0,
            "source_message_index": 1, "response_origin": "top_level_response", "response_has_tool_calls": False})
        online = RequestProcessor(self.tokenizer).process({"prompt": {"messages": context}, "response": "BC",
                                                           "conversation_id": 12, "request_index": 0})
        for key in ["input_tokens", "output_tokens", "total_tokens", "message_count", "user_message_count"]:
            self.assertEqual(offline[key], online[key])

    def test_dynamic_policy_used_after_each_response(self):
        processor = RequestProcessor(self.tokenizer, output_policy=self.policy)
        record = {"messages": [{"role": "user", "content": "A"}], "response": "BB"}
        normal = processor.process(record)
        self.policy.set_threshold(0.70)
        manual = processor.process(record)
        self.assertFalse(normal["output_heavy"])
        self.assertTrue(manual["output_heavy"])
        self.assertEqual(manual["threshold_source"], "MANUAL")
        self.assertEqual(normal["output_percentile"], manual["output_percentile"])

    def test_orphan_tool_and_async_sender_rejected(self):
        processor = RequestProcessor(self.tokenizer)
        with self.assertRaises(RequestProcessingError):
            processor.process({"messages": [{"role": "user", "content": "A"},
                                            {"role": "tool", "content": "T", "tool_call_id": []}]})

        async def async_sender(prompt):
            return "B"

        with self.assertRaisesRegex(TypeError, "synchronous"):
            RequestProcessor(self.tokenizer, sender=async_sender)
