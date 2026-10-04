"""Regression checks for expansion, missing responses and data preservation."""
# 小型合成数据用于验证展开规则；运行测试不下载 tokenizer、不写正式产物。
import unittest

from ..common.conversation import Audit, expand_conversation, reconstruct_request


def msg(role, content, **extra):
    return {"role": role, "content": content, **extra}


def expand(messages, **extra):
    # 固定一个示例 conversation_id，extra 可模拟源行单独存放的顶层 response。
    audit = Audit()
    requests = list(expand_conversation({"prompt": {"messages": messages}, **extra}, 12, audit))
    return requests, audit


class ExpansionTests(unittest.TestCase):
    def test_three_turns_and_history(self):
        # A/B、C/D、E/F 三轮必须生成三个请求，输入历史分别为 A、ABC、ABCDE。
        messages = [msg(role, text) for role, text in zip(
            ["user", "assistant", "user", "assistant", "user", "assistant"], "ABCDEF")]
        requests, audit = expand(messages)
        self.assertEqual([r["response_text"] for r in requests], ["B", "D", "F"])
        self.assertEqual([[m["content"] for m in r["context"]] for r in requests],
                         [["A"], ["A", "B", "C"], ["A", "B", "C", "D", "E"]])
        self.assertEqual([(r["conversation_id"], r["request_index"]) for r in requests], [(12, 0), (12, 1), (12, 2)])

    def test_source_response_is_appended(self):
        # 顶层 response 与 messages 中的历史 assistant 都参与展开。
        requests, _ = expand([msg("user", "A"), msg("assistant", "B"), msg("user", "C")], response="D")
        self.assertEqual([r["response_text"] for r in requests], ["B", "D"])
        self.assertEqual(requests[1]["response_origin"], "top_level_response")

    def test_consecutive_assistant_preserves_stable_indices(self):
        # 连续 assistant C 不生成输出样本，但占序号并保留在后续历史里。
        messages = [msg("user", "A"), msg("assistant", "B"), msg("assistant", "C"), msg("user", "D"), msg("assistant", "E")]
        requests, audit = expand(messages)
        self.assertEqual([r["request_index"] for r in requests], [0, 2])
        self.assertEqual([m["content"] for m in requests[1]["context"]], list("ABCD"))
        self.assertEqual(audit.skipped_reasons["consecutive_assistant"], 1)
        row = {"prompt": {"messages": messages}}
        self.assertEqual(reconstruct_request(row, 12, 2)["response_text"], "E")

    def test_system_consecutive_users_and_list_content(self):
        # system 不丢失，连续 user 不合并为一条消息，列表的 text 块按顺序拼接。
        requests, audit = expand([msg("system", "S"), msg("user", [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]), msg("user", "C")], response="D")
        self.assertEqual([m["content"] for m in requests[0]["context"]], ["S", "AB", "C"])
        self.assertEqual(audit.observations["consecutive_user"], 1)

    def test_null_tool_call_preserved_for_tool_continuation(self):
        # 无文本的工具调用不作为输出样本，但其调用 ID、结果和 tools 定义必须可回溯。
        calls = [{"id": "call1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        audit = Audit()
        row = {"prompt": {"messages": [msg("user", "A"), msg("assistant", None, tool_calls=calls), msg("tool", "T", tool_call_id="call1")], "tools": tools}, "response": "B"}
        requests = list(expand_conversation(row, 12, audit))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["request_index"], 1)
        self.assertEqual(requests[0]["context"][1]["tool_calls"], calls)
        self.assertEqual(requests[0]["context"][2]["tool_call_id"], "call1")
        self.assertEqual(requests[0]["tools"], tools)
        self.assertEqual(audit.skipped_reasons["empty_or_null_assistant_content"], 1)
        self.assertIsNone(row["prompt"]["messages"][1]["content"])

    def test_trailing_user_without_response(self):
        # 未回答的 user 尾部只记录观察，不伪造新 request。
        requests, audit = expand([msg("user", "A"), msg("assistant", "B"), msg("user", "C")])
        self.assertEqual(len(requests), 1)
        self.assertEqual(audit.observations["trailing_context_without_assistant"], 1)

    def test_no_user_and_unknown_role_do_not_produce_valid_samples(self):
        # 缺少 user 无法配对；坏历史不能通过删除未知角色后继续生成“正常”样本。
        requests, audit = expand([msg("assistant", "A"), msg("user", "B"), msg("unknown", "C"), msg("assistant", "D")])
        self.assertEqual(requests, [])
        self.assertEqual(audit.skipped_reasons["no_preceding_user"], 1)
        self.assertEqual(audit.skipped_reasons["invalid_context:unsupported_role"], 1)

    def test_nontext_list_not_silently_dropped(self):
        # 当前只支持文本块；未知非文本内容必须进入异常审计，而不是当作空输入。
        requests, audit = expand([msg("user", [{"type": "image_url", "image_url": "unused"}])], response="B")
        self.assertEqual(requests, [])
        self.assertEqual(audit.skipped_reasons["invalid_context:unsupported_content_part"], 1)


if __name__ == "__main__":
    unittest.main()
