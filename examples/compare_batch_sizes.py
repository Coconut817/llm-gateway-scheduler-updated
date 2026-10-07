"""Batch size sweep at fixed 20ms timeout; same burst workload and priorities."""
import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from examples.compare_priority_burst import verify_run
from examples.concurrency_audit import audit_concurrency_waits
from workload_profiling.baseline import BaselineConfig, BaselineRunner, WorkloadRequest, load_config
from workload_profiling.baseline.arrivals import burst_arrival_times
from workload_profiling.baseline.reporting import atomic_text, write_outputs
from workload_profiling.common.io import sha256_file, write_json
from workload_profiling.common.paths import RESULTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=RESULTS / "reproduction/priority_burst_321_v1")
    parser.add_argument("--cross-batch-reference", type=Path, default=RESULTS / "reproduction/priority_burst_diagnosis_v3")
    parser.add_argument("--output-dir", type=Path, default=RESULTS / "reproduction/batch_size_v2")
    args = parser.parse_args()
    config = load_config(args.reference / "discount_05/config.json")
    if config.batch_size != 16 or config.batch_wait_ms != 20 or config.arrival_mode != "burst" or config.batch_scope != "all":
        raise ValueError("Expected established all-request burst baseline, batch16, timeout20")
    if any(e.service_jitter_fraction for e in config.endpoints):
        raise ValueError("Expected jitter disabled")
    acceptance = json.loads((args.reference / "acceptance_checks.json").read_text(encoding="utf-8"))
    reference_csv = args.reference / "discount_05/requests.csv"
    reference_hash = sha256_file(reference_csv)
    if reference_hash != acceptance["runs"]["discount_05"]["requests_sha256"]:
        raise ValueError("Saved request artifact changed")
    frame = pd.read_csv(reference_csv)
    requests = tuple(WorkloadRequest(r["request_id"], r["input_tokens"], r["output_tokens"], r["source_line"],
                     r["base_priority"], r["priority_class"], r["priority_source"]) for r in frame.to_dict("records"))
    times = burst_arrival_times(len(requests), config)
    if list(times) != frame.arrival_at_ms.tolist():
        raise AssertionError("Saved burst timeline cannot be reproduced")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    atomic_text(output / "arrival_schedule.csv", pd.DataFrame({"request_id": [r.request_id for r in requests],
                "arrival_at_ms": times}).to_csv(index=False), "utf-8-sig")
    atomic_text(output / "priority_labels.csv", frame[["request_id","source_line","base_priority","priority_class","priority_source"]].to_csv(index=False), "utf-8-sig")
    records, summaries, checks, diagnostics = [], {}, {}, {}
    for size in (16,64,128,256,512):
        print(f"Running batch size {size}", flush=True)
        members = None
        for name, discount in (("no_discount",1),("discount_05",.5)):
            settings = replace(config, batch_size=size, heavy_priority_discount=discount)
            result = BaselineRunner(settings).run(requests)
            verify_run(result, requests, times, settings)
            if any(r["batch_wait_ms"] > 20 for r in result.requests):
                raise AssertionError("Collection timeout exceeded")
            current_members = [b["request_ids"] for b in result.batches]
            if members is not None and members != current_members:
                raise AssertionError("Discount changes batch membership")
            members = current_members
            audit, snapshots = audit_concurrency_waits(result, settings)
            if not snapshots:
                raise AssertionError("No concurrency pressure")
            if BaselineRunner(settings).run(requests) != result:
                raise AssertionError("Not repeatable")
            directory = output / f"batch_{size}" / name
            provenance = {"created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                          "source": str(reference_csv.resolve()), "source_sha256": reference_hash,
                          "source_format": "saved_request_lengths_and_business_priorities",
                          "original_prompt_source_sha256": acceptance["source_sha256"],
                          "comparison_method": "fixed burst schedule, timeout20, same priorities/endpoints; vary batch size and discount",
                          "network_model_called": False}
            write_outputs(result, settings, directory, provenance=provenance)
            atomic_text(directory / "concurrency_wait_snapshots.csv", pd.DataFrame(snapshots).to_csv(index=False), "utf-8-sig")
            if size == 16:
                old_path = args.reference / name / "requests.csv"
                if sha256_file(old_path) != acceptance["runs"][name]["requests_sha256"]:
                    raise ValueError("Original batch16 artifact changed")
                pd.testing.assert_frame_equal(pd.read_csv(old_path), pd.read_csv(directory / "requests.csv"), check_exact=True)
            key = f"batch_{size}/{name}"
            s = result.summary
            summaries[key] = s
            diagnostics[key] = audit
            checks[key] = {"full_workload_executed": True, "same_arrivals_labels_and_endpoints": True,
                           "only_batch_size_and_discount_change": True, "timeout_respected": True,
                           "repeated_exact": True, "quota_blocked_wait_events": audit["quota_blocked_wait_events"],
                           "requests_sha256": sha256_file(directory / "requests.csv")}
            records.append({"batch_size": size, "discount": discount,
                            "mean_latency_ms": s["all_latency_ms"]["mean"], "p95_latency_ms": s["all_latency_ms"]["p95"],
                            "mean_batch_wait_ms": s["all_batch_wait_ms"]["mean"],
                            "mean_capacity_wait_ms": s["all_capacity_wait_ms"]["mean"],
                            "capacity_wait_requests": s["capacity_wait_requests"], "batch_count": s["batch_count"],
                            "batch_size_triggers": s["batch_triggers"].get("batch_size",0),
                            "timeout_triggers": s["batch_triggers"].get("timeout",0),
                            "eof_triggers": s["batch_triggers"].get("end_of_input",0),
                            "high_mean_latency_ms": s["priority_groups"]["high"]["latency_ms"]["mean"],
                            "normal_mean_latency_ms": s["priority_groups"]["normal"]["latency_ms"]["mean"],
                            "low_mean_latency_ms": s["priority_groups"]["low"]["latency_ms"]["mean"]})
    write_json(output / "comparison.json", {"runs": summaries, "concurrency_audits": diagnostics})
    write_json(output / "acceptance_checks.json", {"source_request_csv_sha256": reference_hash,
               "batch16_both_csvs_match_original_exactly": True, "runs": checks})
    atomic_text(output / "metrics.csv", pd.DataFrame(records).to_csv(index=False), "utf-8-sig")
    cross = json.loads((args.cross_batch_reference / "comparison.json").read_text(encoding="utf-8"))
    cross_settings = json.loads((args.cross_batch_reference / "global_output_discount/diagnostic_config.json").read_text(encoding="utf-8"))
    if (cross_settings["reference_requests_sha256"] != acceptance["runs"]["no_discount"]["requests_sha256"] or
            config != BaselineConfig.from_dict(cross_settings["reference_config"])):
        raise AssertionError("Cross-batch comparison uses different reference")
    write_json(output / "cross_batch_reference.json", {"path": str(args.cross_batch_reference.resolve()),
               "config_and_workload_reference_match": True,
               "global_output_no_discount": cross["global_output_no_discount"],
               "global_output_discount": cross["global_output_discount"]})
    lines = ["# 增大批大小是否能替代跨批排序", "",
             "同一份3168条已计数请求、固定业务标签及3/2/1权重，沿用突发时间表、额度充足、36个并发名额、相同速度和无波动。",
             "batch_size取16/64/128/256/512，batch_wait_ms始终20。每个大小都直接对照无折扣和0.5折扣，主引擎仍批间FIFO。",
             "读取已验证requests.csv长度和标签，不重新tokenize、不调用模型；16条两组逐请求CSV与原实验完全一致。",
             "各组均全部执行无拒绝、重复一致，容量等待逐事件确认仅由并发满造成。没有SLO设定。", "",
             "| 批大小 | 折扣 | 全部平均延迟 ms | P95 ms | 平均收集等待 ms | 平均容量等待 ms | 批数 | 大小/超时/EOF触发 |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    for r in records:
        lines.append(f"| {r['batch_size']} | {r['discount']} | {r['mean_latency_ms']:.4f} | {r['p95_latency_ms']} | {r['mean_batch_wait_ms']:.4f} | {r['mean_capacity_wait_ms']:.4f} | {r['batch_count']} | {r['batch_size_triggers']}/{r['timeout_triggers']}/{r['eof_triggers']} |")
    lines += ["", "## 同为折扣0.5：业务组与跨批诊断", "",
              "| 方案 | 全部平均 ms | P95 ms | high平均 ms | normal平均 ms | low平均 ms |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for size in (16,64,128,256,512):
        s = summaries[f"batch_{size}/discount_05"]
        lines.append(f"| 批间FIFO、批{size} | {s['all_latency_ms']['mean']:.4f} | {s['all_latency_ms']['p95']} | {s['priority_groups']['high']['latency_ms']['mean']:.4f} | {s['priority_groups']['normal']['latency_ms']['mean']:.4f} | {s['priority_groups']['low']['latency_ms']['mean']:.4f} |")
    s = cross["global_output_discount"]
    lines.append(f"| 批16、跨批已释放队列（诊断） | {s['all']['mean']:.4f} | {s['all']['p95']} | {s['groups']['high']['mean']:.4f} | {s['groups']['normal']['mean']:.4f} | {s['groups']['low']['mean']:.4f} |")
    lines += ["", "增大批大小同时改变收集时间和排序范围；跨批诊断保留16条批的原释放时间。两者是不同方案的比较，不能把差异全部归因于排序范围。",
              "批size触发优先于同刻超时；达不到大批上限时仍按20ms释放，所以实际批可能小于配置。增大上限不保证延迟单调改善。",
              "本次同为0.5折扣时，批16→512均值134.9324→121.2292ms，平均收集等待0.3280→10.3570ms；批512的无折扣→折扣均值132.8074→121.2292ms（改善8.72%）。",
              "因此大批是有意义的简单候选，但不是跨批的等价实现；跨批16条诊断均值104.7500ms、P95为266ms，大批512折扣为121.2292ms、P95为284ms。尚无SLO，也没有比较真实排序计算开销，不能据此直接确定上线方案。",
              "metrics.csv保存全部指标，各batch_N子目录保存完整配置、汇总、请求、事件、批和并发等待快照。旧配置和主策略默认不变。", ""]
    atomic_text(output / "comparison.md", "\n".join(lines))
    print(json.dumps(records, indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
