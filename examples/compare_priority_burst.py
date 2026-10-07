"""Direct 3/2/1 discount ablation under audited concurrency pressure."""
import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from examples.concurrency_audit import audit_concurrency_waits
from workload_profiling.baseline import BaselineRunner, load_config
from workload_profiling.baseline.arrivals import burst_arrival_times
from workload_profiling.baseline.priority import assign_priority
from workload_profiling.baseline.reporting import atomic_text, write_outputs
from workload_profiling.baseline.source import read_prompt_requests
from workload_profiling.common.io import sha256_file, write_json
from workload_profiling.common.paths import PACKAGE, RESULTS
from workload_profiling.common.tokenizer import load_tokenizer


def verify_run(result, requests, times, config):
    if result.summary["completed_requests"] != len(requests) or result.summary["rejected_requests"]:
        raise AssertionError("All requests must complete")
    for row, request, arrival in zip(result.requests, requests, times):
        if (row["request_id"], row["input_tokens"], row["output_tokens"], row["base_priority"], row["priority_class"], row["arrival_at_ms"]) != (
                request.request_id, request.input_tokens, request.output_tokens, request.base_priority, request.priority_class, arrival):
            raise AssertionError("Workload, priority or arrival changed")
        if row["batch_id"] is None or row["endpoint_id"] is None:
            raise AssertionError("Request bypassed batching/execution")
        expected = row["base_priority"] * (config.heavy_priority_discount if row["heavy"] else 1)
        if (row["effective_priority"] != expected or
                row["queue_wait_ms"] != row["batch_wait_ms"] + row["capacity_wait_ms"] or
                row["latency_ms"] != row["queue_wait_ms"] + row["service_ms"]):
            raise AssertionError("Score or time accounting mismatch")
    lookup = {r["request_id"]: r for r in result.requests}
    for batch in result.batches:
        expected = sorted(batch["request_ids"], key=lambda rid: (-lookup[rid]["effective_priority"], lookup[rid]["output_tokens"]))
        if batch["dispatch_order"] != expected:
            raise AssertionError("Incorrect priority ordering")
    if sum(e["total_tokens"] for e in result.endpoints) != sum(r.total_tokens for r in requests):
        raise AssertionError("Tokens not conserved")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PACKAGE / "config/baseline_priority_burst_321.json")
    parser.add_argument("--reference", type=Path, default=RESULTS / "reproduction/priority_weights_321_v1")
    parser.add_argument("--output-dir", type=Path, default=RESULTS / "reproduction/priority_burst_321_v1")
    args = parser.parse_args()
    config = load_config(args.config)
    previous_dir = args.reference / "ample_quota/priority_discount"
    previous_config = load_config(previous_dir / "config.json")
    if replace(config, arrival_mode="fixed", burst_size=previous_config.burst_size,
               burst_span_ms=previous_config.burst_span_ms) != previous_config or config.arrival_mode != "burst":
        raise ValueError("Only arrival mode may change from the 3/2/1 ample reference")
    if (config.batch_scope != "all" or config.batch_order != "effective_priority" or
            config.heavy_priority_discount != .5 or any(e.service_jitter_fraction for e in config.endpoints)):
        raise ValueError("Expected all-request priority ordering, .5 discount, no jitter")
    previous = json.loads((previous_dir / "summary.json").read_text(encoding="utf-8"))["provenance"]
    source = Path(previous["source"])
    source_hash = sha256_file(source)
    if previous["source_format"] != "prompt" or previous["limit"] is not None or source_hash != previous["source_sha256"]:
        raise ValueError("Source mismatch")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    tokenizer, metadata = load_tokenizer()
    if previous["tokenizer_id"] != metadata["tokenizer_id"] or previous["tokenizer_revision"] != metadata["revision"]:
        raise ValueError("Tokenizer mismatch")
    raw = tuple(read_prompt_requests(source, tokenizer,
                progress=lambda n: print(f"Profiled {n} original requests", flush=True)))
    if sha256_file(source) != source_hash:
        raise ValueError("Source changed")
    requests = tuple(assign_priority(r, config) for r in raw)
    labels = pd.DataFrame([{"request_id": r.request_id, "source_line": r.source_line, "base_priority": r.base_priority,
                           "priority_class": r.priority_class, "priority_source": r.priority_source} for r in requests])
    pd.testing.assert_frame_equal(pd.read_csv(args.reference / "priority_labels.csv"), labels, check_dtype=False, check_exact=True)
    atomic_text(output / "priority_labels.csv", labels.to_csv(index=False), "utf-8-sig")
    times = burst_arrival_times(len(requests), config)
    atomic_text(output / "arrival_schedule.csv", pd.DataFrame({"request_id": [r.request_id for r in requests],
                "arrival_burst_ms": times, "arrival_fixed_ms": [i*config.arrival_interval_ms for i in range(len(requests))]}).to_csv(index=False), "utf-8-sig")
    settings = {"no_discount": replace(config, heavy_priority_discount=1), "discount_05": config}
    results, audits, acceptance = {}, {}, {}
    for name, run_config in settings.items():
        result = BaselineRunner(run_config).run(requests)
        verify_run(result, requests, times, run_config)
        audit, snapshots = audit_concurrency_waits(result, run_config)
        if not snapshots or not audit["saturated_with_ready_queue_ms"]:
            raise AssertionError("No actual concurrency pressure generated")
        audits[name] = audit
        results[name] = result
        provenance = {**previous, "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                      "comparison_method": "same 3/2/1 business labels, same burst arrivals and ample quotas; only heavy discount changes",
                      "reference_fixed_arrivals": str(previous_dir.resolve()), "source_sha256": source_hash,
                      "priority_labels_sha256": sha256_file(output / "priority_labels.csv"),
                      "arrival_schedule_sha256": sha256_file(output / "arrival_schedule.csv"),
                      "tokenizer_metadata": metadata}
        write_outputs(result, run_config, output / name, provenance=provenance)
        atomic_text(output / name / "concurrency_wait_snapshots.csv", pd.DataFrame(snapshots).to_csv(index=False), "utf-8-sig")
        if BaselineRunner(run_config).run(requests) != result:
            raise AssertionError("Replay is not repeatable")
        acceptance[name] = {"full_workload_executed": True, "scores_order_and_timing_verified": True,
                            "repeat_exact": True, "tokens_conserved": True, "waits_concurrency_only": True,
                            "requests_sha256": sha256_file(output / name / "requests.csv"),
                            "events_sha256": sha256_file(output / name / "events.jsonl")}
    before, after = results["no_discount"], results["discount_05"]
    if [b["request_ids"] for b in before.batches] != [b["request_ids"] for b in after.batches]:
        raise AssertionError("Batch membership differs")
    differences = []
    for a, b in zip(before.requests, after.requests):
        differences.append({"request_id": a["request_id"], "priority_class": a["priority_class"], "heavy": a["heavy"],
                            "input_tokens": a["input_tokens"], "output_tokens": a["output_tokens"],
                            "base_priority": a["base_priority"], "effective_priority_before": a["effective_priority"],
                            "effective_priority_after": b["effective_priority"],
                            "batch_position_before": a["batch_position"], "batch_position_after": b["batch_position"],
                            "latency_before_ms": a["latency_ms"], "latency_after_ms": b["latency_ms"],
                            "capacity_wait_before_ms": a["capacity_wait_ms"], "capacity_wait_after_ms": b["capacity_wait_ms"],
                            "latency_delta_ms": b["latency_ms"]-a["latency_ms"]})
    atomic_text(output / "request_differences.csv", pd.DataFrame(differences).to_csv(index=False), "utf-8-sig")
    delta = [r["latency_delta_ms"] for r in differences]
    effect = {"changed_batches": sum(a["dispatch_order"] != b["dispatch_order"] for a,b in zip(before.batches,after.batches)),
              "changed_positions": sum(r["batch_position_before"] != r["batch_position_after"] for r in differences),
              "changed_latency_requests": sum(d != 0 for d in delta), "improved_requests": sum(d < 0 for d in delta),
              "worsened_requests": sum(d > 0 for d in delta), "mean_delta_ms": sum(delta)/len(delta),
              "delta_min_ms": min(delta), "delta_max_ms": max(delta)}
    comparison = {name: result.summary for name,result in results.items()}
    write_json(output / "comparison.json", {"runs": comparison, "concurrency_audits": audits, "discount_effect": effect})
    write_json(output / "acceptance_checks.json", {"source_sha256": source_hash,
               "same_321_labels_as_previous": True, "same_burst_schedule_and_batches": True,
               "only_discount_differs": True, "runs": acceptance})
    lines = ["# 并发竞争下3/2/1有无折扣：直接对照", "",
             f"同一份{len(requests)}条请求，同一份高/普通/低标签639/1902/627，权重3/2/1、seed20261006。",
             "所有请求入窗执行；每组最多512条集中在约20ms后归一化的时间表内到达，首末到达仍为0/3167ms。",
             "RPM/TPM为原配置100倍，速度20/20/20 tokens/ms，波动关闭，并发仍为8/12/16（合计36）。",
             "两组都按业务有效优先级排序，同分短输出优先；唯一差别是重型折扣1与0.5。输入/输出固定阈值40342.5/578，后批不能越过前批。",
             "逐事件重建端点状态与待派发队列：每个capacity_wait事件均确认三个端点并发全部占满，且每个端点的RPM/TPM均足以接收队首请求。没有额度耗尽等待。", "",
             "| 指标 | 无折扣 | 折扣0.5 |", "| --- | ---: | ---: |"]
    for label, a, b in (
        ("完成请求数", before.summary["completed_requests"], after.summary["completed_requests"]),
        ("拒绝数", before.summary["rejected_requests"], after.summary["rejected_requests"]),
        ("全部平均延迟 ms", before.summary["all_latency_ms"]["mean"], after.summary["all_latency_ms"]["mean"]),
        ("全部P95延迟 ms", before.summary["all_latency_ms"]["p95"], after.summary["all_latency_ms"]["p95"]),
        ("发生容量等待的请求数", before.summary["capacity_wait_requests"], after.summary["capacity_wait_requests"]),
        ("平均容量等待 ms", before.summary["all_capacity_wait_ms"]["mean"], after.summary["all_capacity_wait_ms"]["mean"]),
        ("最大容量等待 ms", before.summary["all_capacity_wait_ms"]["max"], after.summary["all_capacity_wait_ms"]["max"]),
        ("并发全满且队列积压的累计时间 ms", audits["no_discount"]["saturated_with_ready_queue_ms"], audits["discount_05"]["saturated_with_ready_queue_ms"]),
        ("待派发队列峰值", audits["no_discount"]["ready_queue_peak"], audits["discount_05"]["ready_queue_peak"])):
        lines.append(f"| {label} | {a if isinstance(a,int) else f'{a:.4f}'} | {b if isinstance(b,int) else f'{b:.4f}'} |")
    lines += ["", "| 固定业务组 | 人数 | 无折扣平均/P95 ms | 折扣平均/P95 ms | 平均变化 ms |",
              "| --- | ---: | --- | --- | ---: |"]
    for label in ("high","normal","low"):
        a,b=before.summary["priority_groups"][label],after.summary["priority_groups"][label]
        lines.append(f"| {label} | {a['requests']} | {a['latency_ms']['mean']:.4f}/{a['latency_ms']['p95']} | {b['latency_ms']['mean']:.4f}/{b['latency_ms']['p95']} | {b['latency_ms']['mean']-a['latency_ms']['mean']:.4f} |")
    lines += ["", "| 固定工作量组 | 人数 | 无折扣平均/P95 ms | 折扣平均/P95 ms |", "| --- | ---: | --- | --- |"]
    for label, heavy in (("heavy",True),("light",False),("high_heavy",True)):
        aa=[r for r in before.requests if r["heavy"] == heavy and (label != "high_heavy" or r["priority_class"] == "high")]
        bb=[r for r in after.requests if r["heavy"] == heavy and (label != "high_heavy" or r["priority_class"] == "high")]
        def stats(rows):
            values=sorted(r["latency_ms"] for r in rows)
            import math
            return sum(values)/len(values),values[math.ceil(.95*len(values))-1]
        am,ap=stats(aa);bm,bp=stats(bb)
        lines.append(f"| {label} | {len(aa)} | {am:.4f}/{ap} | {bm:.4f}/{bp} |")
    lines += ["", f"折扣改变{effect['changed_batches']}个窗口、{effect['changed_positions']}条批内位置；{effect['improved_requests']}条延迟改善、{effect['worsened_requests']}条变差，平均变化{effect['mean_delta_ms']:.6f}ms。",
              "并发等待的直接证据在各子目录concurrency_wait_snapshots.csv，累计饱和时间按相邻事件间隔计算，不是capacity_wait事件次数。",
              "所有请求、类别、批成员和到达表一致，两组重复回放一致；这是单数据单seed的模拟对照，未设置SLO，不能直接声称满足业务要求。",
              "日志与汇总分别在no_discount/和discount_05/；逐请求差异为request_differences.csv，时间表与标签独立保存。", ""]
    atomic_text(output / "comparison.md", "\n".join(lines))
    print(json.dumps({"all_latency": {name:r.summary["all_latency_ms"] for name,r in results.items()},
                      "concurrency_audits": audits, "discount_effect":effect},indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
