"""Replay identical full prompt data with 20/20/20 vs 30/20/10 output speeds."""
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


def validate_scenario(control, scenario):
    """Reject accidental changes to anything other than output speed."""
    restored = replace(scenario, endpoints=tuple(
        replace(endpoint, output_tokens_per_ms=before.output_tokens_per_ms)
        for endpoint, before in zip(scenario.endpoints, control.endpoints)))
    if len(scenario.endpoints) != len(control.endpoints) or restored != control:
        raise ValueError("Scenario must change only endpoint output speeds")
    if any(endpoint.service_jitter_fraction != 0 for endpoint in control.endpoints + scenario.endpoints):
        raise ValueError("This experiment requires jitter disabled")


def metrics(result):
    heavy = [r for r in result.requests if r["heavy"]]
    return {**result.summary,
            "capacity_wait_requests": sum(r["capacity_wait_ms"] > 0 for r in heavy),
            "capacity_wait_max_ms": max((r["capacity_wait_ms"] for r in heavy), default=0),
            "capacity_wait_mean_ms": sum(r["capacity_wait_ms"] for r in heavy) / max(1, len(heavy))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=RESULTS / "reproduction/handoff_v1")
    parser.add_argument("--config", type=Path, default=PACKAGE / "config/baseline_endpoint_speed.json")
    parser.add_argument("--output-dir", type=Path, default=RESULTS / "reproduction/endpoint_speed_v1")
    args = parser.parse_args()
    control_config, scenario_config = load_config(args.reference / "config.json"), load_config(args.config)
    validate_scenario(control_config, scenario_config)
    reference = json.loads((args.reference / "summary.json").read_text(encoding="utf-8"))
    original_provenance = reference["provenance"]
    if original_provenance["source_format"] != "prompt" or original_provenance["limit"] is not None:
        raise ValueError("Reference must be a complete prompt replay")
    source = Path(original_provenance["source"])
    source_hash = sha256_file(source)
    if source_hash != original_provenance["source_sha256"]:
        raise ValueError("Original source has changed")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    tokenizer, metadata = load_tokenizer()
    for field, key in (("tokenizer_id", "tokenizer_id"), ("tokenizer_revision", "revision")):
        if original_provenance[field] != metadata[key]:
            raise ValueError("Tokenizer differs from reference")
    requests = tuple(read_prompt_requests(source, tokenizer,
                     progress=lambda n: print(f"Profiled {n} original requests", flush=True)))
    if sha256_file(source) != source_hash:
        raise ValueError("Source changed during tokenization")
    control = BaselineRunner(control_config).run(requests)
    scenario = BaselineRunner(scenario_config).run(requests)
    # Both runs consume the same immutable request tuple; only endpoint speed changes.
    provenance = {**original_provenance,
                  "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                  "comparison_reference": str(args.reference.resolve()),
                  "comparison_method": "same full prompt requests; only output speed changes; jitter disabled",
                  "scenario_config": str(args.config.resolve()),
                  "scenario_config_sha256": sha256_file(args.config), "tokenizer_metadata": metadata}
    for name, result, config in (("uniform_speed", control, control_config),
                                 ("different_speeds", scenario, scenario_config)):
        write_outputs(result, config, output / name, provenance=provenance)
    assert_reference_requests_match(args.reference / "requests.csv", output / "uniform_speed/requests.csv")
    before = {row["request_id"]: row for row in control.requests}
    differences = []
    for row in scenario.requests:
        old = before[row["request_id"]]
        if row["heavy"]:
            differences.append({"request_id": row["request_id"], "input_tokens": row["input_tokens"],
                                "output_tokens": row["output_tokens"],
                                "endpoint_before": old["endpoint_id"], "endpoint_after": row["endpoint_id"],
                                **{f"{key}_{suffix}": item[key] for key in
                                   ("service_ms", "latency_ms", "capacity_wait_ms", "dispatch_at_ms")
                                   for suffix, item in (("before", old), ("after", row))},
                                "latency_delta_ms": row["latency_ms"] - old["latency_ms"]})
    atomic_text(output / "request_differences.csv", pd.DataFrame(differences).to_csv(index=False), "utf-8-sig")
    comparison = {"matches_handoff_request_records": True, "only_output_speed_changed": True,
                  "source_sha256": source_hash, "uniform_speed": metrics(control),
                  "different_speeds": metrics(scenario),
                  "changed_endpoints": sum(r["endpoint_before"] != r["endpoint_after"] for r in differences)}
    write_json(output / "comparison.json", comparison)
    lines = ["# 三个 endpoint 输出速度差异：全量对照", "",
             "原始数据 3168 条请求；按实际结果列出计数。排序为输出 token 最短优先，路由为 min_rpm。",
             "仅将 A/B/C 输出速度从 20/20/20 改为 30/20/10 tokens/ms，耗时波动关闭。",
             "到达间隔、输入速度、基础时延、容量、重型阈值与批参数保持一致。轻型仍不消耗端点。",
             "均匀速度组的逐请求记录与 handoff_v1 完全一致。", "",
             "| 指标 | 原速度 20/20/20 | 新速度 30/20/10 |", "| --- | ---: | ---: |"]
    for label, a, b in (
        ("完成请求数", control.summary["completed_requests"], scenario.summary["completed_requests"]),
        ("拒绝请求数", control.summary["rejected_requests"], scenario.summary["rejected_requests"]),
        ("重型平均延迟 ms", control.summary["heavy_latency_ms"]["mean"], scenario.summary["heavy_latency_ms"]["mean"]),
        ("重型 P95 延迟 ms", control.summary["heavy_latency_ms"]["p95"], scenario.summary["heavy_latency_ms"]["p95"]),
        ("重型最大延迟 ms", control.summary["heavy_latency_ms"]["max"], scenario.summary["heavy_latency_ms"]["max"]),
        ("容量等待请求数", comparison["uniform_speed"]["capacity_wait_requests"], comparison["different_speeds"]["capacity_wait_requests"]),
        ("平均容量等待 ms（全部重型）", comparison["uniform_speed"]["capacity_wait_mean_ms"], comparison["different_speeds"]["capacity_wait_mean_ms"]),
        ("最大容量等待 ms", comparison["uniform_speed"]["capacity_wait_max_ms"], comparison["different_speeds"]["capacity_wait_max_ms"])):
        a_text = str(a) if isinstance(a, int) else f"{a:.2f}"
        b_text = str(b) if isinstance(b, int) else f"{b:.2f}"
        lines.append(f"| {label} | {a_text} | {b_text} |")
    lines += ["", "| 端点 | 原分配数 | 新分配数 | 原峰值并发 | 新峰值并发 |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for a, b in zip(control.endpoints, scenario.endpoints):
        lines.append(f"| {a['endpoint_id']} | {a['total_requests']} | {b['total_requests']} | {a['peak_concurrency']} | {b['peak_concurrency']} |")
    lines += ["", f"{comparison['changed_endpoints']} 条重型请求选择的端点发生变化，各端点累计分配数见上表。容量等待为 0 不代表每个端点随时都有容量，只表示批释放后请求能立即被某个候选端点接收。",
              "min_rpm 不读取输出速度，只按可用候选的 RPM 占比选择；速度通过服务时间与并发占用影响运行。",
              "这里同时让 A 更快、C 更慢，没有保持总处理能力不变；这是环境变化对照，不是新策略优劣或真实服务性能验证。",
              "comparison.json 保存验收与完整指标；request_differences.csv 保存所有重型请求的前后差异。",
              "uniform_speed/、different_speeds/ 保存完整配置、summary、requests、events、batches、endpoints 和报告。", ""]
    atomic_text(output / "comparison.md", "\n".join(lines))
    print(json.dumps(comparison, ensure_ascii=False, indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
