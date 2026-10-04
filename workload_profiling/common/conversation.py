"""Expand every eligible assistant message into a context/response sample."""
# 核心数据流程：校验和规范化消息 → 逐条展开 assistant → 计算 request 长度。
# 完整历史与回答只在内存中使用，返回的长度记录不包含原文。
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field


ROLES = {"system", "user", "assistant", "tool"}
# 主数据的固定列顺序；source_message_index/response_origin 用于补充原文定位。
COLUMNS = [
    "conversation_id", "request_index", "source_message_index", "response_origin",
    "input_tokens", "output_tokens", "total_tokens", "message_count",
    "user_message_count", "assistant_message_count", "system_message_count",
    "tool_message_count", "response_has_tool_calls", "tokenization_status",
]


class StructureError(ValueError):
    # 错误消息使用稳定的原因编码，便于按原因汇总异常，而不是写入原始内容。
    pass


@dataclass
class Audit:
    # counters 是流程数量；skipped_reasons 是跳过请求原因；observations 是可重叠的结构观察。
    # events 只保存定位键、处理阶段和原因，供审计回溯。
    counters: Counter = field(default_factory=Counter)
    skipped_reasons: Counter = field(default_factory=Counter)
    observations: Counter = field(default_factory=Counter)
    events: list[dict] = field(default_factory=list)

    def skip(self, conversation_id, request_index, position, reason, stage="expansion"):
        self.counters["skipped_requests"] += 1
        self.skipped_reasons[reason] += 1
        self.events.append({"conversation_id": conversation_id, "request_index": request_index,
                            "source_message_index": position, "stage": stage, "reason": reason})

    def summary(self):
        # 即使某类异常没有发生，也显式返回 0，便于报告和下游程序读取。
        keys = ("processed_conversations", "invalid_conversations", "assistant_candidates",
                "expanded_requests", "skipped_requests", "tokenization_failures",
                "conversations_without_successful_requests")
        return {"counts": {key: self.counters[key] for key in keys}, "skipped_request_reasons": dict(self.skipped_reasons),
                "observations": dict(self.observations)}


def content_text(content) -> str:
    # Qwen 文本模板不能直接处理这里的 content 列表，所以按原序拼接 text 块。
    # 不插入额外分隔符；遇到未知块直接拒绝，避免静默遗漏内容或改变长度口径。
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                raise StructureError("unsupported_content_part")
            parts.append(part["text"])
        return "".join(parts)
    if content is None:
        raise StructureError("null_content_without_tool_calls")
    raise StructureError("unsupported_content_type")


def normalize_message(message: dict) -> dict:
    if not isinstance(message, dict):
        raise StructureError("message_not_object")
    if message.get("role") not in ROLES:
        raise StructureError("unsupported_role")
    # 修改副本，保留原始数据以及 tool_calls、reasoning_content 等附加字段。
    result = deepcopy(message)
    calls = result.get("tool_calls")
    if calls is not None:
        if result["role"] != "assistant" or not isinstance(calls, list):
            raise StructureError("invalid_tool_calls")
        for call in calls:
            if not isinstance(call, dict) or call.get("type", "function") != "function":
                raise StructureError("invalid_tool_call")
            function = call.get("function", call)
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise StructureError("invalid_tool_call_function")
            arguments = function.get("arguments")
            if not isinstance(arguments, (str, dict)):
                raise StructureError("invalid_tool_call_arguments")
            if isinstance(arguments, str):
                import json
                try:
                    # 仅验证参数字符串是合法 JSON，不重新序列化，保留源数据表达。
                    json.loads(arguments)
                except json.JSONDecodeError as error:
                    raise StructureError("invalid_tool_call_arguments_json") from error
    if result.get("content") is None and result["role"] == "assistant" and calls:
        # 工具调用消息允许没有文本。转为空字符串供模板处理，工具调用本身仍保留。
        result["content"] = ""
    else:
        result["content"] = content_text(result.get("content"))
    reasoning = result.get("reasoning_content")
    if reasoning is not None and not isinstance(reasoning, str):
        raise StructureError("invalid_reasoning_content")
    return result


def expand_conversation(row: dict, conversation_id: int, audit: Audit):
    """request_index is the zero-based assistant ordinal, including skipped candidates.

    Keep the complete prefix. Tool continuations are allowed after a tool message;
    consecutive assistants are skipped. Invalid history invalidates dependent
    prefixes instead of deleting messages and silently changing the context.
    """
    audit.counters["processed_conversations"] += 1
    prompt = row.get("prompt") if isinstance(row, dict) else None
    if not isinstance(prompt, dict) or not isinstance(prompt.get("messages"), list):
        audit.counters["invalid_conversations"] += 1
        audit.events.append({"conversation_id": conversation_id, "stage": "conversation", "reason": "invalid_prompt_messages"})
        return
    tools = prompt.get("tools")
    if tools is not None and (not isinstance(tools, list) or any(not isinstance(t, dict) for t in tools)):
        audit.counters["invalid_conversations"] += 1
        audit.events.append({"conversation_id": conversation_id, "stage": "conversation", "reason": "invalid_tools"})
        return
    messages = list(prompt["messages"])
    history_length = len(messages)
    # 源行的 response 位于 messages 之外，先补接为 assistant，再统一枚举候选。
    # history_length 同时用于区分历史回答和顶层 response 的来源。
    if "response" in row:
        messages.append({"role": "assistant", "content": row["response"]})
    else:
        audit.observations["missing_top_level_response"] += 1
    controls = {key: prompt[key] for key in ("tool_choice", "parallel_tool_calls") if key in prompt}
    # context 始终代表当前消息之前的历史；必须先生成请求，再把当前消息加入历史。
    context = []
    user_seen = False
    previous_role = None
    # 一旦历史结构损坏，后续依赖它的前缀也无效，不能靠删除坏消息继续生成样本。
    invalid_history = None
    # 用已见调用的 ID 检查 tool 消息是否有历史来源。
    tool_call_ids = set()
    request_index = 0
    for position, raw in enumerate(messages):
        role = raw.get("role") if isinstance(raw, dict) else None
        if role == previous_role and role in ROLES:
            audit.observations["consecutive_" + role] += 1
        error = None
        try:
            message = normalize_message(raw)
            if role == "tool" and (not isinstance(message.get("tool_call_id"), str)
                                   or message["tool_call_id"] not in tool_call_ids):
                raise StructureError("orphan_tool_message")
        except StructureError as exc:
            error = str(exc)
            message = None
            audit.observations["invalid_message:" + error] += 1
        if isinstance(raw, dict) and isinstance(raw.get("content"), list):
            audit.observations["list_content_messages"] += 1
        if isinstance(raw, dict) and raw.get("content") is None:
            audit.observations["null_content_messages"] += 1
        if role == "assistant":
            audit.counters["assistant_candidates"] += 1
            # 一个候选只记录首个拒绝原因，避免跳过数量重复计数。
            # 已有 user 且前一角色为 user 或 tool 才能配对；tool 允许工具执行后的续答。
            reason = None
            if error:
                reason = "invalid_response:" + error
            elif invalid_history:
                reason = "invalid_context:" + invalid_history
            elif not user_seen:
                reason = "no_preceding_user"
            elif previous_role == "assistant":
                reason = "consecutive_assistant"
            elif previous_role not in {"user", "tool"}:
                reason = "context_does_not_end_in_user_or_tool"
            elif not message["content"].strip():
                reason = "empty_or_null_assistant_content"
            if reason:
                audit.skip(conversation_id, request_index, position, reason)
            else:
                audit.counters["expanded_requests"] += 1
                # 复制历史列表，防止继续遍历时向 context 追加消息改变已产出的请求。
                yield {
                    "conversation_id": conversation_id, "request_index": request_index,
                    "source_message_index": position,
                    "response_origin": "history" if position < history_length else "top_level_response",
                    "context": list(context), "response_text": message["content"],
                    "response_has_tool_calls": bool(message.get("tool_calls")),
                    "tools": tools, "prompt_controls": controls,
                }
            # 被跳过的 assistant 也占序号，因此 request_index 可能有间隙但可稳定回溯。
            request_index += 1
        if error:
            invalid_history = invalid_history or error
        else:
            # 不生成当前输出样本并不意味着删除该消息；合法的空工具调用等仍留在历史。
            context.append(message)
            if message.get("tool_calls"):
                tool_call_ids.update(call["id"] for call in message["tool_calls"] if isinstance(call.get("id"), str))
        if role == "user":
            user_seen = True
        previous_role = role
    if previous_role in {"user", "tool"}:
        # 尾部只有上下文时记录观察，不人为补造 assistant 回答。
        audit.observations["trailing_context_without_assistant"] += 1


def reconstruct_request(row: dict, conversation_id: int, request_index: int) -> dict:
    """Reconstruct source text in memory using stable identifiers; save no text."""
    # 使用相同展开规则回溯，确保请求序号与主数据一致；审计对象是临时的。
    for request in expand_conversation(row, conversation_id, Audit()):
        if request["request_index"] == request_index:
            return request
    raise KeyError((conversation_id, request_index))
