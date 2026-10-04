"""Run only Stage 1: expansion, offline token lengths, descriptive summaries."""
# Stage 1 总入口：检查源数据 → 展开并计算长度 → 保存 Parquet → 统计、绘图和报告。
# --smoke 覆盖少量真实数据和展开测试；不带此参数时处理全量数据。
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from .inspect_data import DEFAULT_SOURCE, ROOT, inspect_source, read_rows
from ...common.conversation import Audit, COLUMNS, expand_conversation
from ...common.tokenization import LengthCounter
from ...common.tokenizer import load_tokenizer
from ...common.io import sha256_file, write_json, write_parquet
from ...common.paths import CACHE, processed_dir, stage_results
from ...common.datasets import validate_lengths
from .reporting import describe_lengths, make_plots, make_report

# 将库的缓存留在项目内；本流程只需要 tokenizer，不需要模型权重。
os.environ.setdefault("MPLCONFIGDIR", str(CACHE / "matplotlib"))
os.environ.setdefault("HF_HOME", str(CACHE / "huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="Run expansion checks and 12 diverse source conversations")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args()
    started = time.perf_counter()
    source_digest = sha256_file(args.source)
    # 先检查全量源文件，了解真实结构；smoke 的选样也据此覆盖不同特征。
    inspection = inspect_source(args.source)
    # 前几行加上每种结构的首个示例行，去重排序；保留原始行号，不重新编号。
    # 当前数据会选中 12 行，换用其他源数据时选中数量可能不同。
    selected = sorted(set(range(min(8, inspection["physical_row_count"]))) |
                      set(inspection["feature_example_rows"].values())) if args.smoke else None
    if args.smoke:
        import unittest
        from ...tests import test_stage1
        # 先验证展开边界规则，通过后才使用真实数据执行 tokenization 和产物检查。
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_stage1)
        if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
            raise SystemExit("Expansion checks failed")
    processed = processed_dir(args.smoke, stage="stage1")
    results = stage_results("stage1", args.smoke)
    for directory in (processed, results):
        directory.mkdir(parents=True, exist_ok=True)
    write_json(results / "raw_inspection.json", inspection)
    print(f"Source rows: {inspection['physical_row_count']}; mode: {'smoke' if args.smoke else 'full'}", flush=True)
    tokenizer, tokenizer_metadata = load_tokenizer()
    counter = LengthCounter(tokenizer)
    print(f"Tokenizer ready: {tokenizer_metadata['tokenizer_id']} @ {tokenizer_metadata['revision']}", flush=True)
    audit = Audit()
    records = []
    for conversation_id, row, error in read_rows(args.source):
        if selected is not None and conversation_id not in selected:
            continue
        # 用本行前后的成功样本数量，统计没有产生任何可用 request 的 conversation。
        before = len(records)
        if error:
            audit.counters["processed_conversations"] += 1
            audit.counters["invalid_conversations"] += 1
            audit.events.append({"conversation_id": conversation_id, "stage": "conversation", "reason": error})
        else:
            for request in expand_conversation(row, conversation_id, audit):
                try:
                    records.append(counter.completed_request(request))
                except (ValueError, TypeError, KeyError, IndexError) as exc:
                    # tokenization 失败只写审计原因，不补 0/估计长度，也不保存异常中的原文。
                    audit.counters["tokenization_failures"] += 1
                    audit.skip(conversation_id, request["request_index"], request["source_message_index"],
                               "tokenization_error:" + type(exc).__name__, stage="tokenization")
        if len(records) == before:
            audit.counters["conversations_without_successful_requests"] += 1
        completed = audit.counters["processed_conversations"]
        if completed % 100 == 0 or args.smoke:
            print(f"Processed {completed} conversations; {len(records)} successful requests", flush=True)
    with (results / "skipped_events.jsonl").open("w", encoding="utf-8") as stream:
        # 保存逐条异常定位；即使后面没有可用数据，也能据此排查跳过原因。
        for event in audit.events:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    if not records:
        write_json(results / "audit_summary.json", audit.summary())
        raise SystemExit("No successfully tokenized requests; inspect audit_summary.json")
    import pandas as pd
    # records 已经只含长度、计数和定位键；固定整数类型，保证 Parquet 列类型稳定。
    frame = pd.DataFrame.from_records(records, columns=COLUMNS)
    integer_columns = [column for column in COLUMNS if column.endswith("_tokens") or column.endswith("_count") or column in {"conversation_id", "request_index", "source_message_index"}]
    frame = frame.astype({column: "int64" for column in integer_columns})
    if frame.duplicated(["conversation_id", "request_index"]).any():
        raise ValueError("Duplicate conversation/request key")
    # 写出前核对关键不变量：长度求和、角色计数、非负值以及成功/跳过候选总数。
    validate_lengths(frame)
    if not (frame["message_count"] == frame[["user_message_count", "assistant_message_count", "system_message_count", "tool_message_count"]].sum(axis=1)).all():
        raise ValueError("Message counts do not reconcile")
    if not (frame[integer_columns] >= 0).all().all():
        raise ValueError("Negative request identifiers or message counts")
    if len(frame) + audit.counters["skipped_requests"] != audit.counters["assistant_candidates"]:
        raise ValueError("Assistant candidate counts do not reconcile")
    if sha256_file(args.source) != source_digest:
        raise ValueError("Source changed while Stage 1 was running")
    path = processed / "request_lengths.parquet"
    write_parquet(path, frame)
    # 回读验证列类型和值均一致，防止只检查文件存在而忽略写出问题。
    pd.testing.assert_frame_equal(frame, pd.read_parquet(path))
    stats = describe_lengths(frame)
    write_json(results / "length_statistics.json", stats)
    make_plots(frame, results)
    # metadata 记录本次运行的来源、处理口径、审计、依赖和文件哈希；不保存原文。
    metadata = {
        "stage": 1, "mode": "smoke" if args.smoke else "full",
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "source": str(args.source.resolve().relative_to(ROOT)) if args.source.resolve().is_relative_to(ROOT) else str(args.source.resolve()),
        "source_sha256": source_digest, "source_row_count": inspection["physical_row_count"],
        "source_rows_ending_in_assistant": inspection["counts"]["last_roles"].get("assistant", 0),
        "selected_source_rows": selected, "conversation_id_base": 0,
        "request_index_policy": "zero-based assistant ordinal including skipped candidates",
        "successful_requests": len(frame), "audit": audit.summary(),
        "response_origin_counts": dict(Counter(frame["response_origin"])),
        "response_has_tool_calls_count": int(frame["response_has_tool_calls"].sum()),
        "input_exceeds_tokenizer_model_max_length_count": int((frame["input_tokens"] > tokenizer.model_max_length).sum()),
        "columns": list(frame.columns), "message_counts_scope": "input context only",
        "tokenizer": tokenizer_metadata, "parquet_sha256": sha256_file(path),
        "contains_prompt_response_text": False, "dataset_split": None,
        "checks": {"unique_keys": True, "nonnegative_lengths": True, "total_sum": True,
                   "role_count_sum": True, "candidate_reconciliation": True, "parquet_roundtrip": True,
                   "smoke_expansion_tests": args.smoke},
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    review_path = results / "visual_review.json"
    if review_path.exists():
        review = json.loads(review_path.read_text(encoding="utf-8"))
        # 只有源文件、Parquet 和记录中的图表哈希均匹配时，才复用已有人工观察结论。
        if (review.get("source_sha256") == metadata["source_sha256"]
                and review.get("parquet_sha256") == metadata["parquet_sha256"]
                and all(sha256_file(results / name) == digest
                        for name, digest in review.get("plots_sha256", {}).items())):
            metadata["visual_review"] = review
    write_json(results / "stage1_metadata.json", metadata)
    make_report(metadata, stats, results)
    print(f"Saved {len(frame)} requests to {path.relative_to(ROOT)}", flush=True)
    print(f"Skipped requests: {audit.counters['skipped_requests']}; tokenization failures: {audit.counters['tokenization_failures']}", flush=True)
    print(f"Report: {(results / 'stage1_report.md').relative_to(ROOT)}", flush=True)
    if audit.counters["tokenization_failures"] or audit.counters["invalid_conversations"]:
        # 异常运行仍保存报告供排查，但返回失败状态，防止自动流程把它当作完整验收。
        raise SystemExit("Stage 1 has failures; review the audit before accepting the dataset")


if __name__ == "__main__":
    main()
