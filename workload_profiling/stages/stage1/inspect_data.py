"""Inspect the observed prompt/messages + response schema without storing text."""
# 结构检查入口：统计实际出现的字段、角色和 content 类型，供解析与 smoke 选样使用。
# 这里只记录结构和示例行号，不保存 Prompt/Response 原文。
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from ...common.paths import DEFAULT_SOURCE, ROOT


def read_rows(source: Path):
    """Indices are zero-based physical JSONL line numbers, including bad lines."""
    # utf-8-sig 同时兼容带 BOM 和不带 BOM 的 UTF-8 文件。
    # 无效 JSON 行仍占一个索引，确保 conversation_id 始终对应原文件的物理行号。
    with source.open(encoding="utf-8-sig") as stream:
        for index, line in enumerate(stream):
            try:
                yield index, json.loads(line), None
            except json.JSONDecodeError:
                yield index, None, "invalid_json"


def inspect_source(source: Path) -> dict:
    # 不预先限定字段组合，而是用 Counter 汇总真实 schema；计数不包含补接的 response。
    counts = {key: Counter() for key in (
        "row_keys", "prompt_keys", "roles", "message_keys", "content_types",
        "content_part_types", "response_types", "last_roles", "adjacent_roles",
    )}
    # 每种特征只保留首次出现的行号，让 smoke test 覆盖工具、列表、连续角色等结构。
    features: dict[str, int] = {}
    errors = Counter()
    row_count = 0
    for row_id, row, error in read_rows(source):
        row_count += 1
        if error or not isinstance(row, dict):
            errors[error or "row_not_object"] += 1
            continue
        counts["row_keys"][",".join(sorted(row))] += 1
        counts["response_types"][type(row.get("response")).__name__] += 1
        prompt = row.get("prompt")
        if not isinstance(prompt, dict) or not isinstance(prompt.get("messages"), list):
            errors["invalid_prompt_messages"] += 1
            continue
        counts["prompt_keys"][",".join(sorted(prompt))] += 1
        messages = prompt["messages"]
        if "tools" in prompt:
            features.setdefault("tools", row_id)
        if messages and isinstance(messages[-1], dict):
            role = str(messages[-1].get("role"))
            counts["last_roles"][role] += 1
            features.setdefault("ends_" + role, row_id)
        previous_role = None
        for message in messages:
            if not isinstance(message, dict):
                errors["message_not_object"] += 1
                continue
            role = str(message.get("role"))
            counts["roles"][role] += 1
            counts["message_keys"][",".join(sorted(message))] += 1
            content_type = type(message.get("content")).__name__
            counts["content_types"][content_type] += 1
            features.setdefault("role_" + role, row_id)
            features.setdefault("content_" + content_type, row_id)
            if previous_role is not None:
                # 相邻角色组合有助于识别连续 user/assistant，以及工具调用的消息顺序。
                counts["adjacent_roles"][previous_role + "->" + role] += 1
                if previous_role == role:
                    features.setdefault("consecutive_" + role, row_id)
            previous_role = role
            if message.get("tool_calls"):
                features.setdefault("tool_calls", row_id)
            if isinstance(message.get("content"), list):
                for part in message["content"]:
                    part_type = str(part.get("type")) if isinstance(part, dict) else type(part).__name__
                    counts["content_part_types"][part_type] += 1
    return {
        # 排序后输出统计字段，方便不同次运行之间对照检查。
        "source": str(source.resolve()), "physical_row_count": row_count,
        "counts": {key: dict(sorted(value.items())) for key, value in counts.items()},
        "inspection_errors": dict(errors), "feature_example_rows": features,
        "schema": "Each row has prompt.messages and a separate response text; response is appended as an assistant message.",
    }


def main():
    # 可单独执行结构检查；指定 --output 时还会保存同一份 JSON 结果。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = inspect_source(args.source)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
