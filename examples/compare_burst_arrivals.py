"""Full prompt replay: fixed arrival vs deterministic bursts, same time span."""
import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from workload_profiling.baseline import BaselineRunner, load_config
from workload_profiling.baseline.reporting import atomic_text, write_outputs
from workload_profiling.baseline.source import read_prompt_requests
from workload_profiling.common.io import sha256_file, write_json
from workload_profiling.common.paths import PACKAGE, RESULTS
from workload_profiling.common.tokenizer import load_tokenizer
from examples.compare_endpoint_speed import metrics
from examples.comparison_common import assert_reference_requests_match


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=RESULTS / "reproduction/handoff_v1")
    parser.add_argument("--config", type=Path, default=PACKAGE / "config/baseline_burst.json")
    parser.add_argument("--output-dir", type=Path, default=RESULTS / "reproduction/burst_arrivals_v1")
    args = parser.parse_args()
    control_config, burst_config = load_config(args.reference / "config.json"), load_config(args.config)
    restored = replace(burst_config, arrival_mode=control_config.arrival_mode,
                       burst_size=control_config.burst_size, burst_span_ms=control_config.burst_span_ms)
    if restored != control_config or control_config.arrival_mode != "fixed" or burst_config.arrival_mode != "burst":
        raise ValueError("Only arrival mode and burst parameters may change")
    if any(e.service_jitter_fraction != 0 for e in burst_config.endpoints):
        raise ValueError("Jitter must be disabled")
    reference = json.loads((args.reference / "summary.json").read_text(encoding="utf-8"))
    previous = reference["provenance"]
    if previous["source_format"] != "prompt" or previous["limit"] is not None:
        raise ValueError("Reference must be a full prompt replay")
    source = Path(previous["source"])
    source_hash = sha256_file(source)
    if source_hash != previous["source_sha256"]:
        raise ValueError("Original source has changed")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    tokenizer, metadata = load_tokenizer()
    if (previous["tokenizer_id"] != metadata["tokenizer_id"] or
            previous["tokenizer_revision"] != metadata["revision"]):
        raise ValueError("Tokenizer differs from reference")
    requests = tuple(read_prompt_requests(source, tokenizer,
                     progress=lambda n: print(f"Profiled {n} original requests", flush=True)))
    if sha256_file(source) != source_hash:
        raise ValueError("Source changed during tokenization")
    control = BaselineRunner(control_config).run(requests)
    burst = BaselineRunner(burst_config).run(requests)
    repeated = BaselineRunner(burst_config).run(requests)
    if burst != repeated:
        raise AssertionError("Burst replay is not repeatable")
    if len(control.requests) != len(burst.requests) or control.summary["last_arrival_ms"] != burst.summary["last_arrival_ms"]:
        raise AssertionError("Request count or observation span changed")
    original_rows = {r["request_id"]: r for r in control.requests}
    for row in burst.requests:
        old = original_rows[row["request_id"]]
        for key in ("input_tokens", "output_tokens", "heavy", "source_line"):
            if row[key] != old[key]:
                raise AssertionError("Workload identity changed")
        if row["queue_wait_ms"] != row["batch_wait_ms"] + row["capacity_wait_ms"]:
            raise AssertionError("Wait time decomposition failed")
    provenance = {**previous, "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                  "comparison_reference": str(args.reference.resolve()),
                  "comparison_method": "same full prompt requests; only arrival timing changes; same first/last arrival",
                  "scenario_config": str(args.config.resolve()),
                  "scenario_config_sha256": sha256_file(args.config), "tokenizer_metadata": metadata}
    for name, result, config in (("fixed_arrivals", control, control_config), ("burst_arrivals", burst, burst_config)):
        write_outputs(result, config, output / name, provenance=provenance)
    assert_reference_requests_match(args.reference / "requests.csv", output / "fixed_arrivals/requests.csv")
    schedule, differences = [], []
    for row in burst.requests:
        old = original_rows[row["request_id"]]
        schedule.append({"request_id": row["request_id"], "heavy": row["heavy"],
                         "arrival_fixed_ms": old["arrival_at_ms"], "arrival_burst_ms": row["arrival_at_ms"]})
        if row["heavy"]:
            differences.append({"request_id": row["request_id"],
                                **{f"{key}_{suffix}": item[key] for key in
                                   ("arrival_at_ms", "endpoint_id", "batch_wait_ms", "capacity_wait_ms", "queue_wait_ms", "latency_ms")
                                   for suffix, item in (("before", old), ("after", row))},
                                "latency_delta_ms": row["latency_ms"] - old["latency_ms"]})
    for filename, records in (("arrival_schedule.csv", schedule), ("request_differences.csv", differences)):
        atomic_text(output / filename, pd.DataFrame(records).to_csv(index=False), "utf-8-sig")
    bucket_size = 100
    bucket_rows = []
    fixed_counts = Counter(r["arrival_at_ms"] // bucket_size for r in control.requests)
    burst_counts = Counter(r["arrival_at_ms"] // bucket_size for r in burst.requests)
    for bucket in range(control.summary["last_arrival_ms"] // bucket_size + 1):
        bucket_rows.append({"start_ms": bucket * bucket_size, "end_ms": (bucket + 1) * bucket_size,
                            "fixed_requests": fixed_counts[bucket], "burst_requests": burst_counts[bucket]})
    atomic_text(output / "arrival_counts_100ms.csv", pd.DataFrame(bucket_rows).to_csv(index=False), "utf-8-sig")
    comparison = {"matches_handoff_request_records": True, "burst_repeat_exact": True,
                  "same_workload_and_first_last_arrival": True, "only_arrival_parameters_changed": True,
                  "source_sha256": source_hash, "fixed_arrivals": metrics(control), "burst_arrivals": metrics(burst),
                  "max_requests_per_100ms_fixed": max(fixed_counts.values()),
                  "max_requests_per_100ms_burst": max(burst_counts.values())}
    write_json(output / "comparison.json", comparison)
    lines = ["# 突发到达：全量对照", "",
             f"同一份 {len(requests)} 条原始请求，顺序与输入/输出长度保持一致。排序为输出 token 最短优先，路由为 min_rpm。",
             "端点速度均为 20 tokens/ms，波动关闭，容量与批参数保持原值。",
             f"每组最多 {burst_config.burst_size} 条请求在原始 {burst_config.burst_span_ms}ms 跨度内到达，组间空闲；时间表整体归一化保持首末到达时刻。",
             "毫秒取整允许同时到达。轻型仍不消耗端点；突发组需要预读取请求数量以构建完整时间表。",
             "固定组 requests.csv 与 handoff_v1 完全一致；突发组重复两次，全部结果与事件一致。", "",
             "| 指标 | 固定到达 | 突发到达 |", "| --- | ---: | ---: |"]
    for label, a, b in (
        ("完成请求数", control.summary["completed_requests"], burst.summary["completed_requests"]),
        ("拒绝请求数", control.summary["rejected_requests"], burst.summary["rejected_requests"]),
        ("最后到达时刻 ms", control.summary["last_arrival_ms"], burst.summary["last_arrival_ms"]),
        ("重型平均延迟 ms", control.summary["heavy_latency_ms"]["mean"], burst.summary["heavy_latency_ms"]["mean"]),
        ("重型 P95 延迟 ms", control.summary["heavy_latency_ms"]["p95"], burst.summary["heavy_latency_ms"]["p95"]),
        ("重型最大延迟 ms", control.summary["heavy_latency_ms"]["max"], burst.summary["heavy_latency_ms"]["max"]),
        ("重型平均批收集等待 ms", sum(r["batch_wait_ms"] for r in control.requests) / control.summary["heavy_requests"],
         sum(r["batch_wait_ms"] for r in burst.requests) / burst.summary["heavy_requests"]),
        ("容量等待请求数", comparison["fixed_arrivals"]["capacity_wait_requests"], comparison["burst_arrivals"]["capacity_wait_requests"]),
        ("平均容量等待 ms（全部重型）", comparison["fixed_arrivals"]["capacity_wait_mean_ms"], comparison["burst_arrivals"]["capacity_wait_mean_ms"]),
        ("最大容量等待 ms", comparison["fixed_arrivals"]["capacity_wait_max_ms"], comparison["burst_arrivals"]["capacity_wait_max_ms"]),
        ("单个 100ms 桶最多请求数", comparison["max_requests_per_100ms_fixed"], comparison["max_requests_per_100ms_burst"])):
        lines.append(f"| {label} | {a if isinstance(a, int) else f'{a:.2f}'} | {b if isinstance(b, int) else f'{b:.2f}'} |")
    lines += ["", f"固定批触发：{control.summary['batch_triggers']}；突发批触发：{burst.summary['batch_triggers']}。",
              "", "突发既增加容量等待，也让批更快凑满、减少收集等待；平均延迟与 P95 可能向不同方向变化，不能仅用一个指标描述整体变化。",
              "这是人为构造的压力场景，不是实际流量拟合，也没有更换策略；结果只说明到达时间变化的影响。",
              "arrival_schedule.csv 保存全部请求的两组到达时刻，arrival_counts_100ms.csv 保存流量集中程度。",
              "request_differences.csv 保存重型请求差异，各子目录保存完整配置、日志与结果。", ""]
    atomic_text(output / "comparison.md", "\n".join(lines))
    print(json.dumps(comparison, ensure_ascii=False, indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
