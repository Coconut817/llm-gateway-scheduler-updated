"""Fixed thresholds: shortest output vs business priority vs heavy discount."""
import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from examples.comparison_common import assert_reference_requests_match
from workload_profiling.baseline import BaselineRunner, load_config
from workload_profiling.baseline.priority import assign_priority
from workload_profiling.baseline.reporting import atomic_text, write_outputs
from workload_profiling.baseline.source import read_prompt_requests
from workload_profiling.common.io import sha256_file, write_json
from workload_profiling.common.paths import PACKAGE, RESULTS
from workload_profiling.common.tokenizer import load_tokenizer


def check_run(result, requests, config):
    if result.summary["completed_requests"] != len(requests) or result.summary["rejected_requests"] != 0:
        raise AssertionError("Incomplete workload execution")
    if sum(e["total_requests"] for e in result.endpoints) != len(requests):
        raise AssertionError("Not all requests executed")
    if sum(e["total_tokens"] for e in result.endpoints) != sum(r.total_tokens for r in requests):
        raise AssertionError("Token conservation failed")
    for row, request in zip(result.requests, requests):
        if (row["request_id"], row["input_tokens"], row["output_tokens"], row["base_priority"], row["priority_class"]) != (
                request.request_id, request.input_tokens, request.output_tokens, request.base_priority, request.priority_class):
            raise AssertionError("Request or priority identity changed")
        if row["arrival_at_ms"] != row["source_line"] * config.arrival_interval_ms:
            raise AssertionError("Arrival schedule changed")
        expected = request.base_priority * (config.heavy_priority_discount if row["heavy"] else 1)
        if row["effective_priority"] != expected or row["queue_wait_ms"] != row["batch_wait_ms"] + row["capacity_wait_ms"]:
            raise AssertionError("Score or timing accounting failed")
    by_id = {r["request_id"]: r for r in result.requests}
    if config.batch_order == "effective_priority":
        for batch in result.batches:
            expected = sorted(batch["request_ids"], key=lambda rid: (-by_id[rid]["effective_priority"], by_id[rid]["output_tokens"]))
            if batch["dispatch_order"] != expected:
                raise AssertionError("Incorrect priority order")
    for endpoint in result.endpoints:
        if (endpoint["concurrency"] != 0 or endpoint["peak_concurrency"] > endpoint["concurrency_limit"] or
                endpoint["peak_requests_in_window"] > endpoint["rpm_limit"] or
                endpoint["peak_tokens_in_window"] > endpoint["tpm_limit"]):
            raise AssertionError("Capacity invariant failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=RESULTS / "reproduction/all_requests_v1/all_requests")
    parser.add_argument("--output-dir", type=Path, default=RESULTS / "reproduction/priority_v1")
    parser.add_argument("--high-weight", type=float)
    parser.add_argument("--normal-weight", type=float)
    parser.add_argument("--low-weight", type=float)
    parser.add_argument("--compare-results", type=Path, help="Previous weight experiment with identical labels/workload")
    args = parser.parse_args()
    reference_config = load_config(args.reference / "config.json")
    original = load_config(PACKAGE / "config/baseline_priority.json")
    ample = load_config(PACKAGE / "config/baseline_priority_ample_quota.json")
    overrides = {f"priority_{label}_weight": getattr(args, f"{label}_weight") for label in ("high", "normal", "low")
                 if getattr(args, f"{label}_weight") is not None}
    original, ample = replace(original, **overrides), replace(ample, **overrides)
    restored = replace(original, batch_order=reference_config.batch_order,
                       priority_assignment=reference_config.priority_assignment,
                       priority_seed=reference_config.priority_seed,
                       heavy_priority_discount=reference_config.heavy_priority_discount,
                       priority_high_weight=reference_config.priority_high_weight,
                       priority_normal_weight=reference_config.priority_normal_weight,
                       priority_low_weight=reference_config.priority_low_weight)
    if restored != reference_config or original.batch_scope != "all":
        raise ValueError("Original scenario must match established all-request baseline")
    expected_ample = replace(original, endpoints=tuple(replace(e, rpm_limit=e.rpm_limit*100,
                             tpm_limit=e.tpm_limit*100) for e in original.endpoints))
    if ample != expected_ample:
        raise ValueError("Ample scenario may only multiply RPM/TPM by 100")
    previous = json.loads((args.reference / "summary.json").read_text(encoding="utf-8"))["provenance"]
    source = Path(previous["source"])
    source_hash = sha256_file(source)
    if previous["source_format"] != "prompt" or previous["limit"] is not None or source_hash != previous["source_sha256"]:
        raise ValueError("Reference source mismatch")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    tokenizer, metadata = load_tokenizer()
    if previous["tokenizer_id"] != metadata["tokenizer_id"] or previous["tokenizer_revision"] != metadata["revision"]:
        raise ValueError("Tokenizer differs from reference")
    raw = tuple(read_prompt_requests(source, tokenizer,
                progress=lambda n: print(f"Profiled {n} original requests", flush=True)))
    if sha256_file(source) != source_hash:
        raise ValueError("Source changed during tokenization")
    requests = tuple(assign_priority(r, original) for r in raw)
    labels = [{"request_id": r.request_id, "source_line": r.source_line, "base_priority": r.base_priority,
               "priority_class": r.priority_class, "priority_source": r.priority_source} for r in requests]
    atomic_text(output / "priority_labels.csv", pd.DataFrame(labels).to_csv(index=False), "utf-8-sig")
    label_hash = sha256_file(output / "priority_labels.csv")
    if args.compare_results is not None:
        previous_labels = pd.read_csv(args.compare_results / "priority_labels.csv")
        new_labels = pd.read_csv(output / "priority_labels.csv")
        pd.testing.assert_frame_equal(previous_labels.drop(columns="base_priority"),
                                      new_labels.drop(columns="base_priority"), check_exact=True)
    comparison, acceptance, changes, ordering_changes, discount_effect = {}, {}, [], {}, {}
    for environment, config in (("original_quota", original), ("ample_quota", ample)):
        runs = {}
        settings = {
            "output_shortest": replace(config, batch_order="examples.output_shortest_first:OutputShortestFirstOrder", heavy_priority_discount=1),
            "priority_no_discount": replace(config, heavy_priority_discount=1),
            "priority_discount": config,
        }
        for name, run_config in settings.items():
            result = BaselineRunner(run_config).run(requests)
            check_run(result, requests, run_config)
            provenance = {**previous, "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                          "comparison_reference": str(args.reference.resolve()), "environment": environment,
                          "comparison_method": "same full requests and synthetic labels; compare ordering within each environment",
                          "priority_labels_sha256": label_hash, "priority_assignment_method": "SHA256 JSON [seed,request_id]; modulo100; high<20 normal<80 low otherwise",
                          "tokenizer_metadata": metadata}
            write_outputs(result, run_config, output / environment / name, provenance=provenance)
            runs[name] = result
            acceptance[f"{environment}/{name}"] = {"all_requests_executed": True, "same_workload_and_labels": True,
                                                    "tokens_conserved": True, "capacity_limits_respected": True,
                                                    "priority_order_checked": name != "output_shortest",
                                                    "requests_sha256": sha256_file(output / environment / name / "requests.csv")}
        if environment == "original_quota":
            assert_reference_requests_match(args.reference / "requests.csv", output / environment / "output_shortest/requests.csv")
        repeated = BaselineRunner(config).run(requests)
        if repeated != runs["priority_discount"]:
            raise AssertionError("Priority replay is not repeatable")
        if any(runs[name].batches[i]["request_ids"] != runs["output_shortest"].batches[i]["request_ids"]
               for name in runs for i in range(len(runs["output_shortest"].batches))):
            raise AssertionError("Batch membership changed")
        comparison[environment] = {name: result.summary for name, result in runs.items()}
        ordering_changes[environment] = {
            "priority_vs_output_changed_batches": sum(a["dispatch_order"] != b["dispatch_order"] for a, b in
                zip(runs["output_shortest"].batches, runs["priority_no_discount"].batches)),
            "discount_vs_no_discount_changed_batches": sum(a["dispatch_order"] != b["dispatch_order"] for a, b in
                zip(runs["priority_no_discount"].batches, runs["priority_discount"].batches))}
        undiscounted = runs["priority_no_discount"].requests
        discounted = runs["priority_discount"].requests
        deltas = [b["latency_ms"] - a["latency_ms"] for a, b in zip(undiscounted, discounted)]
        discount_effect[environment] = {
            "changed_positions": sum(a["batch_position"] != b["batch_position"] for a, b in zip(undiscounted, discounted)),
            "changed_dispatch_times": sum(a["dispatch_at_ms"] != b["dispatch_at_ms"] for a, b in zip(undiscounted, discounted)),
            "changed_latency": sum(delta != 0 for delta in deltas),
            "improved_requests": sum(delta < 0 for delta in deltas), "worsened_requests": sum(delta > 0 for delta in deltas),
            "mean_delta_ms": sum(deltas) / len(deltas), "min_delta_ms": min(deltas), "max_delta_ms": max(deltas)}
        reference_rows = {r["request_id"]: r for r in runs["output_shortest"].requests}
        for name in ("priority_no_discount", "priority_discount"):
            for row in runs[name].requests:
                before = reference_rows[row["request_id"]]
                changes.append({"environment": environment, "ordering": name, "request_id": row["request_id"],
                                "priority_class": row["priority_class"], "base_priority": row["base_priority"],
                                "heavy": row["heavy"], "effective_priority": row["effective_priority"],
                                "batch_position_before": before["batch_position"], "batch_position_after": row["batch_position"],
                                "latency_before_ms": before["latency_ms"], "latency_after_ms": row["latency_ms"],
                                "latency_delta_ms": row["latency_ms"]-before["latency_ms"]})
        acceptance[f"{environment}_discount_repeat_exact"] = True
    write_json(output / "comparison.json", comparison)
    write_json(output / "ordering_changes.json", ordering_changes)
    write_json(output / "discount_effect.json", discount_effect)
    write_json(output / "acceptance_checks.json", {"source_sha256": source_hash, "priority_labels_sha256": label_hash,
               "original_order_legacy_fields_match_all_requests_v1": True, "runs": acceptance})
    atomic_text(output / "request_differences.csv", pd.DataFrame(changes).to_csv(index=False), "utf-8-sig")
    weights = "/".join(f"{getattr(original, f'priority_{label}_weight'):g}" for label in ("high", "normal", "low"))
    lines = ["# 业务优先级与重型折扣：全量对照", "",
             f"同一份 {len(requests)} 条请求和合成业务标签，固定种子 {original.priority_seed}。高/普通/低权重为{weights}，目标比例20%/60%/20%，实际人数见分组表。",
             "所有请求入窗执行；输入/输出固定阈值40342.5/578，固定1ms到达，批16/等待20ms，min_rpm，速度20/20/20，波动关闭。",
             "三组：输出最短优先；业务优先级排序且无折扣；有效优先级排序且重型折扣0.5。同分输出短优先，再按到达顺序。",
             "仅窗口内排序，后批不能越过前批；合成标签不代表真实业务需求。原排序组只记录标签，不使用它决策。",
             "原额度场景保持已有基线；额度充足场景仅将RPM/TPM乘100，并发与速度保持一致。只在同一场景内部比较排序。", ""]
    for environment, runs in comparison.items():
        lines += [f"## {environment}", "",
                  f"业务优先级使 {ordering_changes[environment]['priority_vs_output_changed_batches']} 个窗口顺序改变；增加折扣又使 {ordering_changes[environment]['discount_vs_no_discount_changed_batches']} 个窗口顺序改变。",
                  "", "| 排序 | 全部平均 ms | 全部 P95 ms | 容量等待数 | 完成/拒绝 |",
                  "| --- | ---: | ---: | ---: | --- |"]
        for name, summary in runs.items():
            lines.append(f"| {name} | {summary['all_latency_ms']['mean']:.2f} | {summary['all_latency_ms']['p95']} | {summary['capacity_wait_requests']} | {summary['completed_requests']}/{summary['rejected_requests']} |")
        lines += ["", "| 业务组 | 人数 | 原排序均值/P95 ms | 无折扣均值/P95 ms | 折扣0.5均值/P95 ms |", "| --- | ---: | --- | --- | --- |"]
        for label in ("high", "normal", "low"):
            group = runs["output_shortest"]["priority_groups"][label]
            values = [runs[name]["priority_groups"][label]["latency_ms"] for name in settings]
            cells = " | ".join(f"{v['mean']:.2f}/{v['p95']}" for v in values)
            lines.append(f"| {label} | {group['requests']} | {cells} |")
    if args.compare_results is not None:
        weight_comparison = {}
        lines += ["", "## 与上一组权重的折扣排序对照", "",
                  "业务类别、请求内容、到达、容量、折扣均相同，仅改变基础权重。",
                  "| 场景/业务组 | 旧权重平均 ms | 新权重平均 ms | 新减旧 ms | 旧/新 P95 ms |",
                  "| --- | ---: | ---: | ---: | --- |"]
        for env, env_config in (("original_quota", original), ("ample_quota", ample)):
            old_dir = args.compare_results / env / "priority_discount"
            old_config = load_config(old_dir / "config.json")
            if replace(env_config, priority_high_weight=old_config.priority_high_weight,
                       priority_normal_weight=old_config.priority_normal_weight,
                       priority_low_weight=old_config.priority_low_weight) != old_config:
                raise AssertionError("Previous experiment differs in more than weights")
            old_summary = json.loads((old_dir / "summary.json").read_text(encoding="utf-8"))
            if old_summary["provenance"]["source_sha256"] != source_hash:
                raise AssertionError("Previous experiment has different source")
            old_rows = pd.read_csv(old_dir / "requests.csv")
            new_rows = pd.read_csv(output / env / "priority_discount/requests.csv")
            identity = ["request_id", "input_tokens", "output_tokens", "source_line", "arrival_at_ms", "heavy", "priority_class", "priority_source"]
            pd.testing.assert_frame_equal(old_rows[identity], new_rows[identity], check_exact=True)
            difference = new_rows.latency_ms - old_rows.latency_ms
            weight_comparison[env] = {
                "changed_positions": int((old_rows.batch_position != new_rows.batch_position).sum()),
                "changed_dispatch_times": int((old_rows.dispatch_at_ms != new_rows.dispatch_at_ms).sum()),
                "changed_latency_requests": int((difference != 0).sum()),
                "improved_requests": int((difference < 0).sum()), "worsened_requests": int((difference > 0).sum()),
                "mean_delta_ms": float(difference.mean()), "delta_min_ms": float(difference.min()), "delta_max_ms": float(difference.max()),
                "previous_requests_sha256": sha256_file(old_dir / "requests.csv"),
                "new_requests_sha256": sha256_file(output / env / "priority_discount/requests.csv"),
                "groups": {}}
            for group in ("all", "high", "normal", "low"):
                before = old_summary["all_latency_ms"] if group == "all" else old_summary["priority_groups"][group]["latency_ms"]
                after = comparison[env]["priority_discount"]["all_latency_ms"] if group == "all" else comparison[env]["priority_discount"]["priority_groups"][group]["latency_ms"]
                weight_comparison[env]["groups"][group] = {"before": before, "after": after, "mean_delta_ms": after["mean"]-before["mean"]}
                lines.append(f"| {env}/{group} | {before['mean']:.6f} | {after['mean']:.6f} | {after['mean']-before['mean']:.6f} | {before['p95']}/{after['p95']} |")
        write_json(output / "weight_comparison.json", {"previous_results": str(args.compare_results.resolve()),
                   "same_business_labels_verified": True, "only_weights_changed_verified": True, "scenarios": weight_comparison})
    lines += ["", "## 折扣相对于同权重无折扣的逐请求作用", "",
              "| 场景 | 批内位置改变数 | 延迟改变数 | 改善/变差请求数 | 平均延迟变化 ms |",
              "| --- | ---: | ---: | --- | ---: |"]
    for env, effect in discount_effect.items():
        lines.append(f"| {env} | {effect['changed_positions']} | {effect['changed_latency']} | {effect['improved_requests']}/{effect['worsened_requests']} | {effect['mean_delta_ms']:.6f} |")
    if original.priority_high_weight == 10 and original.priority_normal_weight == 3 and original.priority_low_weight == 1:
        weight_note = "10/3/1配0.5不会改变业务等级顺序；同级输出重型与短输出优先重叠。"
    else:
        weight_note = f"本次权重{weights}配0.5折扣允许重型请求落到较低业务等级轻型请求之后；改善需同时检查各组代价，不能只看整体均值。"
    lines += ["", "这验证功能与本次场景的指标，不保证整体或各组都改善；还未设置SLO、动态阈值、跨批抢占或防饥饿机制。",
              weight_note,
              "原额度约一分钟的等待会主导延迟；额度充足场景用于单独观察排序，不代表生产容量。",
              "priority_labels.csv保存全部标签，request_differences.csv保存逐请求比较，acceptance_checks.json保存验收与文件哈希。各场景的三个子目录均有完整配置、请求、事件与分组报告。", ""]
    atomic_text(output / "comparison.md", "\n".join(lines))
    print("\n".join(lines))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
