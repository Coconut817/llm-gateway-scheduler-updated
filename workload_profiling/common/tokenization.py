"""离线展开和在线逐条输入共用同一计数口径，不加载模型权重。"""
from collections import Counter
from copy import deepcopy

from .conversation import StructureError, content_text, normalize_message
from .tokenizer import CHAT_KWARGS


def normalize_prompt(prompt):
    if not isinstance(prompt, dict) or not isinstance(prompt.get("messages"), list) or not prompt["messages"]:
        raise StructureError("invalid_prompt_messages")
    result = deepcopy(prompt)
    messages = [normalize_message(message) for message in prompt["messages"]]
    if not any(message["role"] == "user" for message in messages) or messages[-1]["role"] not in {"user", "tool"}:
        raise StructureError("context_does_not_end_in_user_or_tool")
    call_ids = set()
    for message in messages:
        if message["role"] == "tool" and (not isinstance(message.get("tool_call_id"), str) or message["tool_call_id"] not in call_ids):
            raise StructureError("orphan_tool_message")
        call_ids.update(call["id"] for call in (message.get("tool_calls") or []) if isinstance(call.get("id"), str))
    tools = prompt.get("tools")
    if tools is not None and (not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools)):
        raise StructureError("invalid_tools")
    result["messages"] = messages
    return result


class LengthCounter:
    """一次加载 tokenizer，多条调用分别计数。input 是完整上下文，output 仅是当前文本。"""
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def input_lengths(self, context, *, tools=None, prompt_controls=None):
        ids = self.tokenizer.apply_chat_template(context, tools=tools, **CHAT_KWARGS, **(prompt_controls or {}))
        roles = Counter(message["role"] for message in context)
        return {"input_tokens": len(ids), "message_count": len(context),
                **{role + "_message_count": roles[role] for role in ("user", "assistant", "system", "tool")}}

    def output_length(self, response_text):
        return len(self.tokenizer.encode(content_text(response_text), add_special_tokens=False, truncation=False))

    def completed_request(self, request):
        lengths = self.input_lengths(request["context"], tools=request["tools"], prompt_controls=request["prompt_controls"])
        output = self.output_length(request["response_text"])
        return {**{key: request[key] for key in ("conversation_id", "request_index", "source_message_index", "response_origin", "response_has_tool_calls")},
                **lengths, "output_tokens": output, "total_tokens": lengths["input_tokens"] + output,
                "tokenization_status": "ok"}
