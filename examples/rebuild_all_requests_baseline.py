"""Establish full-execution baseline and verify compatibility with handoff_v1."""
import argparse
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
from examples.comparison_common import assert_reference_requests_match


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=RESULTS / "reproduction/handoff_v1")
    parser.add_argument("--config", type=Path, default=PACKAGE / "config/baseline_all_requests.json")
    parser.add_argument("--output-dir", type=Path, default=RESULTS / "reproduction/all_requests_v1")
    args = parser.parse_args()
    old_config, all_config = load_config(args.reference / "config.json"), load_config(args.config)
    if old_config.batch_scope != "heavy_only" or all_config.batch_scope != "all" or replace(all_config, batch_scope="heavy_only") != old_config:
        raise ValueError("Only batch_scope may change compared with the handoff reference")
    reference = json.loads((args.reference / "summary.json").read_text(encoding="utf-8"))
    previous = reference["provenance"]
    if previous["source_format"] != "prompt" or previous["limit"] is not None:
        raise ValueError("Reference must be a complete prompt replay")
    source = Path(previous["source"])
    source_hash = sha256_file(source)
    if source_hash != previous["source_sha256"]:
        raise ValueError("Original source changed")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    tokenizer, metadata = load_tokenizer()
    if previous["tokenizer_id"] != metadata["tokenizer_id"] or previous["tokenizer_revision"] != metadata["revision"]:
        raise ValueError("Tokenizer differs from reference")
    requests = tuple(read_prompt_requests(source, tokenizer,
                     progress=lambda n: print(f"Profiled {n} original requests", flush=True)))
    if sha256_file(source) != source_hash:
        raise ValueError("Source changed during tokenization")
    legacy = BaselineRunner(old_config).run(requests)
    full = BaselineRunner(all_config).run(requests)
    if full.summary["rejected_requests"] != 0 or full.summary["endpoint_executed_requests"] != len(requests):
        raise AssertionError("Expected all source requests to execute without rejection")
    for old, row in zip(legacy.requests, full.requests):
        for key in ("request_id", "input_tokens", "output_tokens", "source_line", "arrival_at_ms", "heavy"):
            if row[key] != old[key]:
                raise AssertionError("Workload identity or classification changed")
        if row["batch_id"] is None or row["endpoint_id"] is None or row["service_ms"] < 1:
            raise AssertionError("Request bypassed batching or execution")
        if row["queue_wait_ms"] != row["batch_wait_ms"] + row["capacity_wait_ms"] or row["latency_ms"] != row["queue_wait_ms"] + row["service_ms"]:
            raise AssertionError("Timing accounting mismatch")
    if sum(e["total_tokens"] for e in full.endpoints) != sum(r.total_tokens for r in requests):
        raise AssertionError("Token accounting mismatch")
    for endpoint in full.endpoints:
        if (endpoint["concurrency"] != 0 or endpoint["peak_concurrency"] > endpoint["concurrency_limit"] or
                endpoint["peak_requests_in_window"] > endpoint["rpm_limit"] or
                endpoint["peak_tokens_in_window"] > endpoint["tpm_limit"]):
            raise AssertionError("Endpoint capacity accounting mismatch")
    if any(event["event"] == "light_completed" for event in full.events):
        raise AssertionError("Light request bypassed execution")
    provenance = {**previous, "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                  "comparison_reference": str(args.reference.resolve()),
                  "comparison_method": "same full prompt requests; only batch_scope changes; not a strategy performance comparison",
                  "scenario_config": str(args.config.resolve()), "scenario_config_sha256": sha256_file(args.config),
                  "tokenizer_metadata": metadata}
    for name, result, settings in (("legacy_heavy_only", legacy, old_config), ("all_requests", full, all_config)):
        write_outputs(result, settings, output / name, provenance=provenance)
    assert_reference_requests_match(args.reference / "requests.csv", output / "legacy_heavy_only/requests.csv")
    acceptance = {"legacy_request_records_match_handoff": True, "only_batch_scope_changed": True,
                  "same_requests_arrivals_and_labels": True, "all_requests_batched_and_executed": True,
                  "endpoint_tokens_conserved": True, "capacity_limits_respected": True,
                  "timing_decomposition_valid": True, "source_sha256": source_hash,
                  "config_sha256": sha256_file(output / "all_requests/config.json"),
                  "requests_sha256": sha256_file(output / "all_requests/requests.csv")}
    write_json(output / "acceptance_checks.json", acceptance)
    comparison = {"legacy_heavy_only": legacy.summary, "all_requests": full.summary,
                  "note": "Workload execution model changes: old light requests were zero-time and consumed no capacity."}
    write_json(output / "comparison.json", comparison)
    lines = ["# 所有请求入窗并执行：新基线", "",
             "同一份完整原始数据；唯一行为配置变化为 batch_scope：heavy_only → all。",
             "旧模式逐请求结果与 handoff_v1 完全一致。新模式所有轻重请求均入窗并模拟执行，轻重标签不变。",
             "固定到达 1ms、批大小 16、最长收集等待 20ms、输出最短优先、min_rpm、原容量和速度、波动关闭。",
             "输入/输出仍使用固定 40342.5/578 tokens 阈值；尚未接入 EVT、动态百分位或业务优先级。", "",
             "| 指标 | 旧模式：仅重型执行 | 新模式：所有请求执行 |", "| --- | ---: | ---: |"]
    for label, a, b in (
        ("完成请求数", legacy.summary["completed_requests"], full.summary["completed_requests"]),
        ("端点实际执行请求数", legacy.summary["endpoint_executed_requests"], full.summary["endpoint_executed_requests"]),
        ("端点实际执行轻型数", legacy.summary["endpoint_executed_light_requests"], full.summary["endpoint_executed_light_requests"]),
        ("拒绝数", legacy.summary["rejected_requests"], full.summary["rejected_requests"]),
        ("全部完成请求平均延迟 ms", legacy.summary["all_latency_ms"]["mean"], full.summary["all_latency_ms"]["mean"]),
        ("全部完成请求 P95 延迟 ms", legacy.summary["all_latency_ms"]["p95"], full.summary["all_latency_ms"]["p95"]),
        ("轻型平均延迟 ms", legacy.summary["light_latency_ms"]["mean"], full.summary["light_latency_ms"]["mean"]),
        ("重型平均延迟 ms", legacy.summary["heavy_latency_ms"]["mean"], full.summary["heavy_latency_ms"]["mean"]),
        ("发生容量等待的完成请求数", legacy.summary["capacity_wait_requests"], full.summary["capacity_wait_requests"]),
        ("最大容量等待 ms", legacy.summary["all_capacity_wait_ms"]["max"], full.summary["all_capacity_wait_ms"]["max"]),
        ("批次数", legacy.summary["batch_count"], full.summary["batch_count"]),
        ("模拟结束时刻 ms", legacy.summary["simulation_end_ms"], full.summary["simulation_end_ms"])):
        lines.append(f"| {label} | {a if isinstance(a, int) else f'{a:.2f}'} | {b if isinstance(b, int) else f'{b:.2f}'} |")
    lines += ["", "不能用这两组延迟判断算法优劣：端点执行工作量从重型子集变为全部请求。后续策略必须与新全请求基线比较。",
              "总 RPM 上限为 2700/分钟，而本次 3168 条全部执行；即使速度足够，也必然有请求等到一分钟额度恢复，TPM 与并发也可能形成瓶颈。",
              "延迟统计仅包含 completed 请求，拒绝数单列；轻重标签和计数不是新的优先级规则。",
              "全部请求 CSV 和事件日志在 all_requests/，旧模式兼容性回放在 legacy_heavy_only/，验收与哈希在 acceptance_checks.json。", ""]
    atomic_text(output / "comparison.md", "\n".join(lines))
    print(json.dumps(comparison, ensure_ascii=False, indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
