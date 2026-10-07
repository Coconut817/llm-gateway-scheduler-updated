"""Equal-business-priority output percentile ablation with all requests executed."""
import argparse
from dataclasses import replace
from datetime import datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from examples.concurrency_audit import audit_concurrency_waits
from workload_profiling.baseline import BaselineRunner, WorkloadRequest, load_config
from workload_profiling.baseline.classification import OutputPercentileClassifier
from workload_profiling.baseline.reporting import atomic_text, write_outputs
from workload_profiling.common.io import sha256_file, write_json
from workload_profiling.common.paths import ARTIFACTS, PACKAGE, RESULTS, stage_results
from workload_profiling.runtime import PercentileReference


def stats(values):
    values = sorted(values)
    return {"mean": sum(values)/len(values), "p95": values[math.ceil(.95*len(values))-1], "max": max(values)} if values else {"mean":0,"p95":0,"max":0}


def verify(result, requests, source_rows, classifier, config):
    if result.summary["completed_requests"] != len(requests) or result.summary["rejected_requests"]:
        raise AssertionError("All requests must execute")
    by_id = {r.request_id:r for r in requests}
    trace = {s["batch_id"]:s for s in classifier.trace}
    if len(trace) != len(result.batches):
        raise AssertionError("Exactly one classification per released batch is required")
    for row, source in zip(result.requests,source_rows):
        request = by_id[row["request_id"]]
        if (row["input_tokens"],row["output_tokens"],row["arrival_at_ms"]) != (source["input_tokens"],source["output_tokens"],source["arrival_at_ms"]):
            raise AssertionError("Workload changed")
        if row["base_priority"] != 1 or row["priority_class"] != "normal" or row["priority_source"] != "default":
            raise AssertionError("Business priority is not uniform")
        threshold = trace[row["batch_id"]]["threshold"]
        p = classifier.reference.percentile(request.output_tokens)
        if (row["output_percentile"] != p or row["output_percentile_threshold"] != threshold or
                row["output_heavy"] != (p >= threshold) or
                row["input_heavy"] != (request.input_tokens >= config.input_threshold_tokens) or
                row["heavy"] != (row["input_heavy"] or row["output_heavy"])):
            raise AssertionError("Incorrect or changed classification")
        if row["classified_at_ms"] != row["batch_released_at_ms"]:
            raise AssertionError("Classification is not frozen at release")
        if row["queue_wait_ms"] != row["batch_wait_ms"]+row["capacity_wait_ms"] or row["latency_ms"] != row["queue_wait_ms"]+row["service_ms"]:
            raise AssertionError("Time accounting failed")
    rows = {r["request_id"]:r for r in result.requests}
    for batch in result.batches:
        key = (lambda rid:rows[rid]["output_tokens"]) if config.batch_order.startswith("examples.output_shortest") else (lambda rid:rows[rid]["heavy"])
        if batch["dispatch_order"] != sorted(batch["request_ids"],key=key):
            raise AssertionError("Wrong dispatch order")
    if sum(e["total_tokens"] for e in result.endpoints) != sum(r.total_tokens for r in requests):
        raise AssertionError("Token accounting failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PACKAGE / "config/baseline_dynamic_output.json")
    parser.add_argument("--source-reference", type=Path, default=RESULTS / "reproduction/priority_burst_321_v1")
    parser.add_argument("--output-dir", type=Path, default=RESULTS / "reproduction/dynamic_output_v1")
    args = parser.parse_args()
    config = load_config(args.config)
    previous_config = load_config(args.source_reference / "discount_05/config.json")
    restored = replace(config, output_classification=previous_config.output_classification,
                       output_percentile_threshold=previous_config.output_percentile_threshold,
                       priority_assignment=previous_config.priority_assignment,
                       priority_high_weight=previous_config.priority_high_weight,
                       priority_normal_weight=previous_config.priority_normal_weight,
                       priority_low_weight=previous_config.priority_low_weight,
                       batch_order=previous_config.batch_order)
    if restored != previous_config or config.batch_scope != "all" or config.priority_assignment != "uniform":
        raise ValueError("Unexpected changes to physical workload/environment")
    source_csv = args.source_reference / "no_discount/requests.csv"
    previous_checks = json.loads((args.source_reference / "acceptance_checks.json").read_text(encoding="utf-8"))
    source_hash = sha256_file(source_csv)
    if source_hash != previous_checks["runs"]["no_discount"]["requests_sha256"]:
        raise ValueError("Source artifact changed")
    source_rows = pd.read_csv(source_csv).to_dict("records")
    requests = tuple(WorkloadRequest(r["request_id"],r["input_tokens"],r["output_tokens"],r["source_line"],
                                    base_priority=1,priority_class="normal",priority_source="default") for r in source_rows)
    reference_path = ARTIFACTS / "output_percentile_reference.parquet"
    metadata_path = stage_results("stage2_1") / "percentile_reference_metadata.json"
    reference_hash = sha256_file(reference_path)
    reference = PercentileReference.load(reference_path,metadata_path)
    output = args.output_dir.resolve()
    output.mkdir(parents=True,exist_ok=False)
    atomic_text(output / "equal_priority_requests.jsonl", "".join(json.dumps({"request_id":r.request_id,
                "input_tokens":r.input_tokens,"output_tokens":r.output_tokens,"base_priority":1,"priority_class":"normal"})+"\n" for r in requests))
    atomic_text(output / "arrival_schedule.csv", pd.DataFrame({"request_id":[r.request_id for r in requests],
                "arrival_at_ms":[r["arrival_at_ms"] for r in source_rows]}).to_csv(index=False),"utf-8-sig")
    write_json(output / "reference_metadata.json",json.loads(metadata_path.read_text(encoding="utf-8")))
    cohort = {r.request_id:(r.input_tokens >= config.input_threshold_tokens or reference.percentile(r.output_tokens) >= .8) for r in requests}
    variants = {
        "output_shortest":replace(config,output_classification="percentile_fixed",batch_order="examples.output_shortest_first:OutputShortestFirstOrder"),
        "fixed_80":replace(config,output_classification="percentile_fixed"),
        "dynamic":config,
    }
    results, policies, audits, checks, cohort_metrics = {}, {}, {}, {}, {}
    for name,settings in variants.items():
        policy = OutputPercentileClassifier(settings,reference)
        result = BaselineRunner(settings,classification_policy=policy).run(requests)
        verify(result,requests,source_rows,policy,settings)
        audit,snapshots = audit_concurrency_waits(result,settings)
        repeated_policy = OutputPercentileClassifier(settings,reference)
        if BaselineRunner(settings,classification_policy=repeated_policy).run(requests) != result or repeated_policy.trace != policy.trace:
            raise AssertionError("Output policy replay not repeatable")
        results[name],policies[name],audits[name] = result,policy,audit
        provenance = {"created_at":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                      "source":str(source_csv.resolve()),"source_sha256":source_hash,
                      "source_format":"saved_lengths_with_all_business_priorities_reset_to_one",
                      "original_prompt_source_sha256":previous_checks["source_sha256"],
                      "output_reference":str(reference_path.resolve()),"output_reference_sha256":reference_hash,
                      "output_reference_metadata_sha256":sha256_file(metadata_path),
                      "reference_relationship":"historical Stage1 expanded samples from same underlying corpus; not independent held-out validation",
                      "pressure_policy_sha256":sha256_file(PACKAGE / "config/dynamic_output_policy.json"),
                      "comparison_method":"all requests execute; only fixed/dynamic percentile differs in classification arms",
                      "network_model_called":False}
        directory = output / name
        write_outputs(result,settings,directory,provenance=provenance)
        atomic_text(directory / "threshold_trace.csv",pd.DataFrame(policy.trace).to_csv(index=False),"utf-8-sig")
        atomic_text(directory / "concurrency_wait_snapshots.csv",pd.DataFrame(snapshots).to_csv(index=False),"utf-8-sig")
        cohort_metrics[name] = {}
        for label,heavy in (("fixed80_heavy",True),("fixed80_light",False)):
            members = [r for r in result.requests if cohort[r["request_id"]] == heavy]
            cohort_metrics[name][label] = {"requests":len(members),"latency_ms":stats([r["latency_ms"] for r in members]),
                                           "queue_wait_ms":stats([r["queue_wait_ms"] for r in members])}
        checks[name] = {"all_requests_execute":True,"all_business_priorities_one":True,"same_workload_arrivals_and_endpoints":True,
                       "classification_order_and_timing_verified":True,"repeat_exact":True,
                       "quota_blocked_wait_events":audit["quota_blocked_wait_events"],
                       "requests_sha256":sha256_file(directory / "requests.csv")}
    # Classifier must not alter the physical behavior of pure shortest-output ordering.
    token_config = replace(variants["output_shortest"],output_classification="tokens")
    token_result = BaselineRunner(token_config).run(requests)
    columns = ["request_id","endpoint_id","dispatch_at_ms","finished_at_ms","service_ms","queue_wait_ms","capacity_wait_ms"]
    pd.testing.assert_frame_equal(pd.DataFrame(token_result.requests)[columns],pd.DataFrame(results["output_shortest"].requests)[columns],check_exact=True)
    if sha256_file(reference_path) != reference_hash:
        raise AssertionError("Frozen output reference changed")
    fixed,dynamic = results["fixed_80"],results["dynamic"]
    if [b["request_ids"] for b in fixed.batches] != [b["request_ids"] for b in dynamic.batches]:
        raise AssertionError("Membership differs between classification arms")
    delta = []
    for a,b in zip(fixed.requests,dynamic.requests):
        delta.append({"request_id":a["request_id"],"output_tokens":a["output_tokens"],"output_percentile":a["output_percentile"],
                      "fixed80_cohort_heavy":cohort[a["request_id"]],"input_heavy":a["input_heavy"],
                      "heavy_fixed":a["heavy"],"heavy_dynamic":b["heavy"],
                      "threshold_fixed":a["output_percentile_threshold"],"threshold_dynamic":b["output_percentile_threshold"],
                      "batch_position_fixed":a["batch_position"],"batch_position_dynamic":b["batch_position"],
                      "latency_fixed_ms":a["latency_ms"],"latency_dynamic_ms":b["latency_ms"],
                      "latency_delta_ms":b["latency_ms"]-a["latency_ms"]})
    atomic_text(output / "request_differences.csv",pd.DataFrame(delta).to_csv(index=False),"utf-8-sig")
    effect = {"changed_classifications":sum(r["heavy_fixed"] != r["heavy_dynamic"] for r in delta),
              "changed_positions":sum(r["batch_position_fixed"] != r["batch_position_dynamic"] for r in delta),
              "changed_latency_requests":sum(r["latency_delta_ms"] != 0 for r in delta),
              "improved_requests":sum(r["latency_delta_ms"] < 0 for r in delta),
              "worsened_requests":sum(r["latency_delta_ms"] > 0 for r in delta),
              "mean_delta_ms":sum(r["latency_delta_ms"] for r in delta)/len(delta)}
    comparison = {"runs":{name:r.summary for name,r in results.items()},"fixed_cohorts":cohort_metrics,
                  "concurrency_audits":audits,"dynamic_effect":effect,
                  "percentile_token_cutoffs":{str(p):reference.minimum_length_at(p) for p in (.6,.7,.8,.9)}}
    write_json(output / "comparison.json",comparison)
    write_json(output / "acceptance_checks.json",{"reference_distribution_unchanged":True,
               "shortest_output_timing_compatible_with_original_token_classification":True,
               "same_membership_fixed_dynamic":True,"source_request_csv_sha256":source_hash,"runs":checks})
    lines = ["# 平级调度：固定80%与动态输出门槛", "",
             "所有3168条请求基础优先级=1，全部进入endpoint执行。固定输入阈值40342.5；输出使用同学冻结的5186样本右连续ECDF。",
             "相同突发时间表、批16/等待20ms、额度充足、并发8/12/16、速度20/20/20、波动关闭、min_rpm；仍批间FIFO，无跨批排序。",
             "output_shortest是输出最短优先参照；fixed_80和dynamic都使用轻型优先、同类FIFO，只改变百分位门槛更新规则。",
             "动态在每批释放前读当前并发、已有ready和当前已到达批的请求数；不读取未来请求。本批分类冻结，等待期间不重分类。",
             "U=在途/并发上限；E=max(0,ready数+本批数-空闲名额)。E>=总名额用60%；否则E>0或U>=85%用70%；否则U>=60%用80%；其余90%。",
             "初始状态80%，第一次采样即可变化；本版无平滑/滞回，仅验证机制。idle/normal/busy/critical是内部压力档位，不改变假定全局busy的研究范围。",
             "参考分布来自同一底层语料的历史展开样本，统计口径与3168完整行不同，不是独立测试集；本实验不能证明真实部署泛化。", "",
             "| 方案 | 全部平均 ms | P95 ms | 容量等待均值 ms | 重型数量（会变） | 门槛取值 | 门槛变化次数 |",
             "| --- | ---: | ---: | ---: | ---: | --- | ---: |"]
    for name,result in results.items():
        s = result.summary
        lines.append(f"| {name} | {s['all_latency_ms']['mean']:.4f} | {s['all_latency_ms']['p95']} | {s['all_capacity_wait_ms']['mean']:.4f} | {s['heavy_requests']} | {s['threshold_values_used']} | {s['threshold_update_count']} |")
    lines += ["", "## 固定80%定义的同一请求集合", "",
              "| 集合 | 人数 | 最短输出均值/P95 ms | 固定80%均值/P95 ms | 动态均值/P95 ms |",
              "| --- | ---: | --- | --- | --- |"]
    for label in ("fixed80_heavy","fixed80_light"):
        groups = [cohort_metrics[name][label] for name in variants]
        cells = " | ".join(f"{g['latency_ms']['mean']:.4f}/{g['latency_ms']['p95']}" for g in groups)
        lines.append(f"| {label} | {groups[0]['requests']} | {cells} |")
    lines += ["", f"动态相对固定80%：{effect['changed_classifications']}条分类改变、{effect['changed_positions']}条批内位置改变；{effect['improved_requests']}条延迟改善、{effect['worsened_requests']}条变差，平均变化{effect['mean_delta_ms']:.6f}ms。",
              "门槛降低扩大重型范围，但没有减少工作量；不应只比较两组各自的重型均值。SLO尚未定义，不报告达标率。",
              "threshold_trace.csv记录每批压力、门槛、长度界限和分类数；requests.csv记录每条当时分类及时间；输入判定未接EVT在线更新。",
              "这是首轮预设规则，不因结果好坏事后调参数；既要比较动态相对固定，也要比较它是否胜过原输出最短策略。", ""]
    atomic_text(output / "comparison.md","\n".join(lines))
    print(json.dumps({"metrics":{name:r.summary['all_latency_ms'] for name,r in results.items()},
                      "thresholds":results['dynamic'].summary['threshold_values_used'],"dynamic_effect":effect},indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
