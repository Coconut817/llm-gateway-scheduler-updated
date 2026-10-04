"""逐条请求接口：计算 input → 可选同步发送 → 收到 response 后计算 output。

没有批量预读、并发、队列或自动重试。sender 由调用方注入，模块不决定端点。
"""
from copy import deepcopy
from dataclasses import dataclass
import inspect
from typing import Callable, Iterable, Iterator

from ..common.conversation import StructureError, normalize_message
from ..common.tokenization import LengthCounter, normalize_prompt


@dataclass
class PreparedRequest:
    prompt: dict
    lengths: dict
    identifiers: dict


class RequestProcessingError(RuntimeError):
    """只暴露处理阶段和稳定错误码；JSONL 日志不包含异常里的 prompt 原文。"""
    def __init__(self, stage, code):
        self.stage, self.code = stage, code
        super().__init__(f"{stage}:{code}")


class RequestProcessor:
    def __init__(self, tokenizer, *, output_policy=None, sender: Callable | None = None,
                 include_response=False):
        if sender is not None and not callable(sender):
            raise TypeError("sender must be a synchronous callable")
        if sender is not None and inspect.iscoroutinefunction(sender):
            raise TypeError("sender must be synchronous; await it in your own adapter")
        if output_policy is not None and output_policy.reference is None:
            raise ValueError("Stream output policy requires a frozen percentile reference")
        self.counter = LengthCounter(tokenizer)
        self.output_policy = output_policy
        self.sender = sender
        self.include_response = include_response

    def prepare(self, record) -> PreparedRequest:
        if isinstance(record, str):
            prompt = {"messages": [{"role": "user", "content": record}]}
            identifiers = {}
        elif isinstance(record, dict):
            prompt = record.get("prompt", record)
            # 定位字段不是模型参数；完整 prompt 中的其它 API 参数保留给 sender。
            prompt = {key: value for key, value in prompt.items() if key not in
                      {"response", "conversation_id", "request_index"}} if isinstance(prompt, dict) else prompt
            identifiers = {key: record[key] for key in ("conversation_id", "request_index") if key in record}
            for value in identifiers.values():
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise StructureError("invalid_request_identifier")
        else:
            raise StructureError("request_not_string_or_object")
        prompt = normalize_prompt(prompt)
        controls = {key: prompt[key] for key in ("tool_choice", "parallel_tool_calls") if key in prompt}
        lengths = self.counter.input_lengths(prompt["messages"], tools=prompt.get("tools"), prompt_controls=controls)
        return PreparedRequest(prompt, lengths, identifiers)

    def complete(self, prepared: PreparedRequest, response) -> dict:
        # 发送适配器可返回文本、text content 列表或标准 assistant message。
        message = response if isinstance(response, dict) else {"role": "assistant", "content": response}
        if message.get("role") != "assistant":
            raise StructureError("response_must_be_assistant")
        normalized = normalize_message(message)
        output_tokens = self.counter.output_length(normalized["content"])
        result = {**prepared.identifiers, **prepared.lengths, "output_tokens": output_tokens,
                  "total_tokens": prepared.lengths["input_tokens"] + output_tokens,
                  "response_has_tool_calls": bool(normalized.get("tool_calls")),
                  "tokenization_status": "ok"}
        if self.output_policy is not None:
            result.update(self.output_policy.evaluate(output_tokens))
        if self.include_response:
            result["response"] = deepcopy(response)
        return result

    def process(self, record) -> dict:
        try:
            if self.sender is not None and isinstance(record, dict) and "response" in record:
                raise StructureError("sender_and_recorded_response_are_mutually_exclusive")
            prepared = self.prepare(record)
        except Exception as error:
            code = str(error) if isinstance(error, StructureError) else "invalid_prompt_or_tokenization_failed"
            raise RequestProcessingError("input", code) from error

        if self.sender is not None:
            try:
                # 只发送当前请求一次。传副本，防止适配器修改已计算的上下文。
                response = self.sender(deepcopy(prepared.prompt))
                if inspect.isawaitable(response):
                    if hasattr(response, "close"):
                        response.close()
                    raise TypeError("sender returned an awaitable instead of a completed response")
            except Exception as error:
                # 不自动重试：发送失败可能发生在服务已接受请求之后。
                raise RequestProcessingError("send", "sender_failed") from error
        elif isinstance(record, dict) and "response" in record:
            response = record["response"]
        else:
            # 尚无 response 时不填 0、不算 Output-Heavy；所有未知量明确为 null。
            result = {**prepared.identifiers, **prepared.lengths, "output_tokens": None, "total_tokens": None,
                      "response_has_tool_calls": None, "tokenization_status": "input_only"}
            if self.output_policy is not None:
                result.update({"output_percentile": None, "output_heavy": None,
                               "output_heavy_threshold": None, "congestion_state": None, "threshold_source": None})
            return result
        try:
            return self.complete(prepared, response)
        except Exception as error:
            code = str(error) if isinstance(error, StructureError) else "invalid_response_or_policy_failed"
            raise RequestProcessingError("output", code) from error

    def process_stream(self, records: Iterable, *, on_error="raise") -> Iterator[dict]:
        if on_error not in {"raise", "yield"}:
            raise ValueError("on_error must be 'raise' or 'yield'")
        # 必须 yield 当前结果后再读取下一条，接收方的消费速度就是背压。
        for index, record in enumerate(records):
            try:
                yield {"stream_index": index, **self.process(record)}
            except RequestProcessingError as error:
                if on_error == "raise":
                    raise
                yield {"stream_index": index, "tokenization_status": "error",
                       "error_stage": error.stage, "error_code": error.code}
