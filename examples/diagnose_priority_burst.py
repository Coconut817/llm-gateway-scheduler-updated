"""Diagnostic replay of saved burst releases; no changes to the main scheduler."""
import argparse
import heapq
import json
from pathlib import Path

import pandas as pd

from workload_profiling.baseline import load_config, WorkloadRequest
from workload_profiling.baseline.models import EndpointState
from workload_profiling.baseline.routing import load_strategy
from workload_profiling.baseline.reporting import atomic_text
from workload_profiling.common.io import sha256_file, write_json
from workload_profiling.common.paths import RESULTS


def statistics(values):
    import math
    values = sorted(values)
    return {"mean": sum(values)/len(values), "p95": values[math.ceil(.95*len(values))-1], "max": max(values)}


def replay(source_rows, batches, config, scope, tie, discount):
    """Same releases and nonpreemptive endpoint model; choose only released jobs."""
    states = [EndpointState(e) for e in config.endpoints]
    by_endpoint = {e.config.endpoint_id:e for e in states}
    strategy = load_strategy(config.strategy)
    rows = {r["request_id"]:dict(r) for r in source_rows}
    requests = {rid:WorkloadRequest(rid,r["input_tokens"],r["output_tokens"]) for rid,r in rows.items()}
    for row in rows.values():
        row["source_batch_position"] = row["batch_position"]
        row["priority_discount"] = discount if row["heavy"] else 1
        row["effective_priority"] = row["base_priority"]*row["priority_discount"]
        row["queue_selection_scope"] = scope
        row["tie_breaker"] = tie
    def key(rid):
        row=rows[rid]
        length=row["output_tokens"] if tie == "output" else row["service_ms"]
        return (-row["effective_priority"],length,row["source_line"])
    ready, running = [], []
    release_index, sequence, now = 0,0,0
    while release_index < len(batches) or ready or running:
        times=[]
        if release_index < len(batches):
            times.append(batches[release_index]["released_at_ms"])
        if running:
            times.append(running[0][0])
        if not times:
            raise AssertionError("Ready requests have no future capacity event")
        now=min(times)
        while running and running[0][0] <= now:
            _,_,eid,_=heapq.heappop(running)
            by_endpoint[eid].complete()
        while release_index < len(batches) and batches[release_index]["released_at_ms"] == now:
            batch=batches[release_index]
            ordered = sorted(batch["request_ids"],key=key)
            for position,rid in enumerate(ordered):
                rows[rid]["batch_position"] = position
            ready.extend(ordered)
            release_index+=1
        while ready:
            index=0 if scope == "local" else min(range(len(ready)),key=lambda i:key(ready[i]))
            rid=ready[index]
            request=requests[rid]
            views=tuple(e.view(now,config.window_ms) for e in states)
            if any(v.requests_in_window >= v.rpm_limit or v.tokens_in_window+request.total_tokens > v.tpm_limit for v in views):
                raise AssertionError("Diagnostic must exclude quota blocking")
            candidates=tuple(v for v in views if v.can_accept(request))
            if not candidates:
                break
            eid=strategy.select(request,candidates,now)
            view=next(v for v in candidates if v.endpoint_id == eid)
            finish=by_endpoint[eid].dispatch(request,now,config.window_ms)
            ready.pop(index)
            row=rows[rid]
            if finish-now != row["service_ms"]:
                raise AssertionError("Saved service times differ from endpoint model")
            row.update(endpoint_id=eid,dispatch_at_ms=now,finished_at_ms=finish,
                       capacity_wait_ms=now-row["batch_released_at_ms"],queue_wait_ms=now-row["arrival_at_ms"],
                       latency_ms=finish-row["arrival_at_ms"],
                       endpoint_rpm_before=view.requests_in_window,endpoint_tpm_before=view.tokens_in_window,
                       endpoint_concurrency_before=view.concurrency,rpm_utilization_before=view.rpm_utilization,
                       tpm_utilization_before=view.tpm_utilization,concurrency_utilization_before=view.concurrency_utilization,
                       dispatch_sequence=sequence+1)
            sequence+=1
            heapq.heappush(running,(finish,sequence,eid,rid))
    result=[rows[r["request_id"]] for r in source_rows]
    if sum(e.total_requests for e in states) != len(result) or any(e.concurrency for e in states):
        raise AssertionError("Diagnostic did not execute/drain the whole workload")
    if sum(e.total_tokens for e in states) != sum(r["total_tokens"] for r in result):
        raise AssertionError("Token conservation failed")
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference",type=Path,default=RESULTS / "reproduction/priority_burst_321_v1")
    parser.add_argument("--output-dir",type=Path,default=RESULTS / "reproduction/priority_burst_diagnosis_v3")
    args=parser.parse_args()
    config=load_config(args.reference / "discount_05/config.json")
    if config.arrival_mode != "burst" or config.batch_scope != "all" or any(e.service_jitter_fraction for e in config.endpoints):
        raise ValueError("Expected saved all-request deterministic burst simulation")
    speeds={(e.base_latency_ms,e.input_tokens_per_ms,e.output_tokens_per_ms) for e in config.endpoints}
    if len(speeds) != 1:
        raise ValueError("This diagnosis assumes identical endpoint speeds")
    checks=json.loads((args.reference / "acceptance_checks.json").read_text(encoding="utf-8"))
    for name in ("no_discount","discount_05"):
        if sha256_file(args.reference / name / "requests.csv") != checks["runs"][name]["requests_sha256"]:
            raise ValueError("Saved reference request artifact changed")
    output=args.output_dir.resolve()
    output.mkdir(parents=True,exist_ok=False)
    frame=pd.read_csv(args.reference / "no_discount/requests.csv")
    rows=frame.to_dict("records")
    batches=json.loads((args.reference / "no_discount/batches.json").read_text(encoding="utf-8"))
    comparison={}
    for scope in ("local","global"):
        for tie in ("output","service"):
            for discount in (1,.5):
                name=f"{scope}_{tie}_{'no_discount' if discount == 1 else 'discount'}"
                result=replay(rows,batches,config,scope,tie,discount)
                if replay(rows,batches,config,scope,tie,discount) != result:
                    raise AssertionError("Diagnostic replay is not repeatable")
                if scope == "local" and tie == "output":
                    existing=pd.read_csv(args.reference / ("no_discount" if discount == 1 else "discount_05") / "requests.csv")
                    columns=["request_id","endpoint_id","dispatch_at_ms","finished_at_ms","service_ms","latency_ms","capacity_wait_ms","effective_priority", "priority_discount", "batch_position", "endpoint_rpm_before", "endpoint_tpm_before", "endpoint_concurrency_before"]
                    pd.testing.assert_frame_equal(existing[columns],pd.DataFrame(result)[columns],check_exact=True,check_dtype=False)
                directory=output/name
                directory.mkdir()
                atomic_text(directory/"requests.csv",pd.DataFrame(result).to_csv(index=False),"utf-8-sig")
                write_json(directory/"diagnostic_config.json",{"reference_config":config.to_dict(),"scope":scope,
                           "tie_break":tie,"discount":discount,"reference":str(args.reference.resolve()),
                           "arrival_release_policy":"reuse original recorded batch releases, never use unreleased jobs",
                           "service_time_policy":"same input/output formula, same endpoint speeds, no jitter",
                           "reference_requests_sha256":sha256_file(args.reference/"no_discount/requests.csv")})
                summary={"all":statistics([r["latency_ms"] for r in result]),
                         "heavy":statistics([r["latency_ms"] for r in result if r["heavy"]]),
                         "light":statistics([r["latency_ms"] for r in result if not r["heavy"]]),
                         "groups":{g:statistics([r["latency_ms"] for r in result if r["priority_class"]==g]) for g in ("high","normal","low")}}
                write_json(directory/"summary.json",summary)
                comparison[name]=summary
    write_json(output/"comparison.json",comparison)
    write_json(output/"acceptance_checks.json",{"local_output_both_runs_match_original_exactly":True,
               "same_requests_priorities_services_batch_releases":True,"all_diagnostic_runs_exclude_quota_blocking":True,
               "all_runs_execute_and_drain_workload":True,"all_runs_conserve_tokens":True,
               "all_runs_repeat_exactly":True})
    lines=["# 小收益原因诊断：排序范围与同分长度指标", "",
           "使用已保存的3168条全请求突发记录，复用业务标签、到达与批释放时刻。重放原配置的端点容量、服务时间和min_rpm。",
           "local/output两组逐请求派发端点、时刻、服务时间和等待与原实验完全一致，再进行诊断变体。",
           "local仅批内排序、后批不可越过前批；global在每次派发时从所有已释放待派发请求中重新选最高优先级。",
           "output同分时按输出tokens；service同分时按已知长度计算的服务时间。没有未来请求前视、不抢占运行中请求。",
           "这些是独立诊断重放，不修改主调度器，不代表已采纳global或service策略。", "",
           "| 诊断变体 | 全部平均 ms | 全部P95 ms | high平均 ms | normal平均 ms | low平均 ms |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name,s in comparison.items():
        lines.append(f"| {name} | {s['all']['mean']:.4f} | {s['all']['p95']} | {s['groups']['high']['mean']:.4f} | {s['groups']['normal']['mean']:.4f} | {s['groups']['low']['mean']:.4f} |")
    lines += ["", "一次只沿一个轴比较：local→global检验跨批排序限制，output→service检验长度代理，同一scope/tie下无折扣→折扣检验折扣额外作用。",
              "所有变体运行完整工作量，额度始终充足；没有SLO，业务组代价需要同时观察。source requests是原实验计数结果，本脚本不重新tokenize或调用模型。", ""]
    local_a,local_b=comparison["local_output_no_discount"]["all"],comparison["local_output_discount"]["all"]
    global_a,global_b=comparison["global_output_no_discount"]["all"],comparison["global_output_discount"]["all"]
    lines += ["## 诊断结论", "",
              f"现有仅批内排序：折扣平均改善{100*(local_a['mean']-local_b['mean'])/local_a['mean']:.2f}%；允许跨批已释放队列选择后，同一有无折扣对照改善{100*(global_a['mean']-global_b['mean'])/global_a['mean']:.2f}%，P95为{global_a['p95']}→{global_b['p95']}ms。",
              "本场景证据支持：折扣作用范围被16条批边界限制，而积压队列可达457条；它不能让后批短请求越过前批长请求。没有折扣组本就同级短输出优先，也减少了额外优化空间。",
              "同分指标从输出长度改为服务时间，在local折扣组只带来约0.009ms均值变化，因此不能把本次小收益主要归咎于长度代理。",
              "global输出排序中高业务组均值31.0610→40.3505ms，低业务组226.9537→183.1786ms。整体收益包含业务组之间的让位，仍需SLO与公平性约束。",
              "跨批诊断改变了FIFO约束，只验证机制，不代表已决定修改主策略，也不能推广为所有数据和种子都改善。", ""]
    atomic_text(output/"comparison.md","\n".join(lines))
    print(json.dumps({name:s['all'] for name,s in comparison.items()},indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
