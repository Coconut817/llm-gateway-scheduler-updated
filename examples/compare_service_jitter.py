"""Full-data control: original handoff settings vs seeded +/-20% service jitter."""
import argparse
from dataclasses import replace
from pathlib import Path

import pandas as pd

from workload_profiling.baseline import BaselineRunner, load_config
from workload_profiling.baseline.reporting import write_outputs, atomic_text
from workload_profiling.baseline.source import read_prompt_requests
from workload_profiling.common.io import sha256_file, write_json
from workload_profiling.common.paths import RESULTS
from workload_profiling.common.tokenizer import load_tokenizer
import json
from examples.comparison_common import assert_reference_requests_match


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=RESULTS / "reproduction/handoff_v1")
    parser.add_argument("--output-dir", type=Path, default=RESULTS / "reproduction/service_jitter_v1")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = load_config(args.reference / "config.json")
    reference = json.loads((args.reference / "summary.json").read_text(encoding="utf-8"))
    source = Path(reference["provenance"]["source"])
    if reference["provenance"]["source_format"] != "prompt" or reference["provenance"]["limit"] is not None:
        raise ValueError("Reference must be a complete prompt replay")
    source_hash = sha256_file(source)
    if source_hash != reference["provenance"]["source_sha256"]:
        raise ValueError("Original source has changed")
    tokenizer, metadata = load_tokenizer()
    requests = tuple(read_prompt_requests(source, tokenizer,
                     progress=lambda n: print(f"Profiled {n} original requests", flush=True)))
    if sha256_file(source) != source_hash:
        raise ValueError("Source changed during tokenization")
    original = BaselineRunner(config).run(requests)
    jitter_config = replace(config, endpoints=tuple(replace(e, service_jitter_fraction=.2,
                           service_jitter_seed=20261005) for e in config.endpoints))
    jitter = BaselineRunner(jitter_config).run(requests)
    repeated = BaselineRunner(jitter_config).run(requests)
    if (jitter.requests != repeated.requests or jitter.events != repeated.events
            or jitter.batches != repeated.batches or jitter.endpoints != repeated.endpoints
            or jitter.summary != repeated.summary):
        raise AssertionError("Seeded full replay is not repeatable")
    provenance = {**reference["provenance"], "comparison_reference": str(args.reference.resolve()),
                  "tokenizer_metadata": metadata,
                  "comparison_method": "same fully tokenized original requests; only jitter changes"}
    # Reference created_at describes the old run, not these newly generated runs.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    provenance["created_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    for name, result, settings in (("no_jitter", original, config), ("jitter_20pct", jitter, jitter_config)):
        write_outputs(result, settings, output / name, provenance=provenance)
    assert_reference_requests_match(args.reference / "requests.csv", output / "no_jitter/requests.csv")
    original_rows = {r["request_id"]: r for r in original.requests}
    differences = []
    for row in jitter.requests:
        before = original_rows[row["request_id"]]
        if row["heavy"]:
            differences.append({"request_id": row["request_id"], "endpoint_before": before["endpoint_id"],
                                "endpoint_after": row["endpoint_id"],
                                **{f"{key}_{suffix}": item[key] for key in
                                   ("service_ms", "latency_ms", "capacity_wait_ms")
                                   for suffix, item in (("before", before), ("after", row))},
                                "latency_delta_ms": row["latency_ms"] - before["latency_ms"]})
    atomic_text(output / "request_differences.csv", pd.DataFrame(differences).to_csv(index=False), "utf-8-sig")
    def metrics(result):
        return {**result.summary,
                "capacity_wait_requests": sum(r["capacity_wait_ms"] > 0 for r in result.requests),
                "capacity_wait_max_ms": max(r["capacity_wait_ms"] for r in result.requests)}
    comparison = {"matches_handoff_request_records": True, "seed_repeat_exact": True,
                  "source_sha256": source_hash, "no_jitter": metrics(original), "jitter_20pct": metrics(jitter)}
    write_json(output / "comparison.json", comparison)
    lines = ["# 全量服务时间波动对照", "",
             "输入、输出长度、到达时间、排序、路由与容量保持一致；仅新增 ±20% 均匀倍率，种子 20261005。",
             "逐请求耗时波动独立于派发顺序，不是端点持续变慢的时间过程。轻型仍不消耗端点。", "",
             "无波动逐请求记录与 handoff_v1 完全一致；有波动全量回放重复两次，全部记录与事件一致。", "",
             "| 指标 | 无波动 | ±20% 波动 |", "| --- | ---: | ---: |"]
    for label, before, after in (
        ("请求数", len(original.requests), len(jitter.requests)),
        ("拒绝数", original.summary["rejected_requests"], jitter.summary["rejected_requests"]),
        ("重型平均延迟 ms", original.summary["heavy_latency_ms"]["mean"], jitter.summary["heavy_latency_ms"]["mean"]),
        ("重型 P95 延迟 ms", original.summary["heavy_latency_ms"]["p95"], jitter.summary["heavy_latency_ms"]["p95"]),
        ("重型最大延迟 ms", original.summary["heavy_latency_ms"]["max"], jitter.summary["heavy_latency_ms"]["max"]),
        ("容量等待请求数", comparison["no_jitter"]["capacity_wait_requests"], comparison["jitter_20pct"]["capacity_wait_requests"])):
        lines.append(f"| {label} | {before:.2f} | {after:.2f} |")
    lines.extend(["", "这是环境变化对照，不是新调度策略的性能验证。单个种子的结果不能代表多种子统计结论。",
                  "逐请求差异见 request_differences.csv；各子目录保存完整配置、请求、事件和汇总。", ""])
    atomic_text(output / "comparison.md", "\n".join(lines))
    print(json.dumps(comparison, ensure_ascii=False, indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
