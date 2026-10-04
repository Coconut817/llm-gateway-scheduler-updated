"""从 Stage 1 Parquet 运行全局 Input/Output POT，不读取 Prompt 或调用 tokenizer。"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime
from functools import partial
from importlib.metadata import version
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

# 多进程已经并行执行拟合，避免每个进程再启动大量数值计算线程。
for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[variable] = "1"
from ...common.paths import PACKAGE, CACHE, PROCESSED, stage_results, processed_dir
os.environ.setdefault("MPLCONFIGDIR", str(CACHE / "matplotlib"))

import numpy as np
import pandas as pd

from ...common.io import sha256_file as file_sha256, write_json as _write_json
from ...common.datasets import validate_lengths, save_labels, TAIL_COLUMNS
from .reporting import make_report

from .diagnostics import TailConfig, fit_candidates, window_diagnostics
from .threshold_selection import evaluate_candidate_gof, selection_result, bootstrap_selection_task, bootstrap_summary

SOURCE = PROCESSED / "request_lengths.parquet"
AXES = ("input", "output")
PLOTS = ("shape_stability", "modified_scale", "mean_excess", "gof")


write_json = partial(_write_json, nonfinite_to_none=True)


def checkpoint_read(path, signature, resume):
    if not resume or not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["result"] if data.get("signature") == signature else None


def checkpoint_write(path, signature, result):
    # 公共 write_json 已原子替换，避免长实验中断后读到半个 checkpoint。
    write_json(path, {"signature": signature, "result": result})


def primary_diagnostics(lengths, config, pool, checkpoints, signature, resume):
    tables, windows, results = {}, {}, {}
    tasks = {}
    for axis_index, axis in enumerate(AXES):
        cached = checkpoint_read(checkpoints / f"primary_{axis}.json", signature, resume)
        if cached is not None:
            tables[axis], windows[axis], results[axis] = cached["records"], cached["windows"], cached["selection"]
            print(f"{axis}: restored complete primary diagnostics", flush=True)
            continue
        tables[axis] = fit_candidates(lengths[axis], config)
        windows[axis] = window_diagnostics(tables[axis], config)
        # 主实验对每个有效 unique threshold 都完整执行 B 次 GOF，不只评估选中窗口。
        for record in tables[axis]:
            if record["candidate_valid"]:
                future = pool.submit(evaluate_candidate_gof, lengths[axis], record, config, axis_index, 0)
                tasks[future] = (axis, record["candidate_index"])
    completed = 0
    for future in as_completed(tasks):
        axis, index = tasks[future]
        tables[axis][index] = future.result()
        completed += 1
        if completed % 5 == 0 or completed == len(tasks):
            print(f"Primary bootstrap GOF: {completed}/{len(tasks)} candidates complete", flush=True)
    for axis in AXES:
        if axis not in results:
            results[axis] = selection_result(lengths[axis], tables[axis], windows[axis], config)
            checkpoint_write(checkpoints / f"primary_{axis}.json", signature,
                             {"records": tables[axis], "windows": windows[axis], "selection": results[axis]})
        print(f"{axis}: {results[axis]['status']}; selected u={results[axis]['selected_threshold']}", flush=True)
    return tables, windows, results


def outer_bootstrap(lengths, config, pool, checkpoints, signature, resume):
    records = {axis: [] for axis in AXES}
    tasks = {}
    for axis_index, axis in enumerate(AXES):
        for repetition in range(1, config.threshold_bootstrap_repetitions + 1):
            path = checkpoints / f"bootstrap_{axis}_{repetition:04d}.json"
            cached = checkpoint_read(path, signature, resume)
            if cached is not None:
                records[axis].append(cached)
            else:
                future = pool.submit(bootstrap_selection_task, lengths[axis], config, axis_index, repetition)
                tasks[future] = (axis, path)
    print(f"Threshold bootstrap: restored {sum(map(len, records.values()))}; scheduled {len(tasks)}", flush=True)
    last_update = time.perf_counter()
    for future in as_completed(tasks):
        axis, path = tasks[future]
        result = future.result()
        records[axis].append(result)
        checkpoint_write(path, signature, result)
        completed = sum(map(len, records.values()))
        if completed % 10 == 0 or time.perf_counter() - last_update >= 25:
            successes = {name: sum(r["status"] == "OK" for r in items) for name, items in records.items()}
            print(f"Threshold bootstrap complete: input {len(records['input'])}/{config.threshold_bootstrap_repetitions}, output {len(records['output'])}/{config.threshold_bootstrap_repetitions}; successes {successes}", flush=True)
            last_update = time.perf_counter()
    return records


def make_diagnostic_plots(tables, selections, results_directory):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"figure.dpi": 140, "font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    specification = {
        "shape_stability": ("xi", "GPD shape xi"),
        "modified_scale": ("modified_scale", "Modified scale (sigma - xi * u)"),
        "mean_excess": ("mean_excess", "Empirical mean exceedance"),
        "gof": ("bootstrap_gof_p", "Parametric bootstrap GOF p-value"),
    }
    for axis in AXES:
        table = pd.DataFrame(tables[axis])
        selected = selections[axis]["selected_threshold"]
        for plot, (column, ylabel) in specification.items():
            fig, ax = plt.subplots(figsize=(8, 4.8), layout="constrained")
            valid = table[column].notna()
            ax.plot(table.loc[valid, "threshold"], table.loc[valid, column], "o-", color="#356a99", markersize=4, linewidth=1.3)
            if plot == "gof":
                ax.axhline(.05, color="#bd6b22", linestyle="--", label="GOF requirement: p >= .05")
                ax.set_ylim(-.02, 1.02)
            if selected is not None:
                ax.axvline(selected, color="#a52732", linestyle="--", label=f"Selected u = {selected:g}")
                segment = selections[axis]["selected_window"]
                ax.axvspan(segment["start_threshold"], segment["end_threshold"], color="#a52732", alpha=.08)
            if ax.get_legend_handles_labels()[0]:
                ax.legend(fontsize=9)
            ax.set(title=f"{axis.title()} POT: {plot.replace('_', ' ')}", xlabel="Threshold u (tokens)", ylabel=ylabel)
            ax.grid(alpha=.2)
            fig.savefig(results_directory / f"{axis}_{plot}.png")
            plt.close(fig)


def compare_p95_evt(values, selection, axis):
    values = np.asarray(values, dtype=float)
    p95 = float(np.quantile(values, .95, method="linear"))
    baseline = values > p95
    u = selection["selected_threshold"]
    result = {"axis": axis, "evt_status": selection["status"], "p95_threshold": p95,
              "evt_threshold": u, "evt_empirical_percentile": selection["empirical_percentile"],
              "p95_tail_count": int(baseline.sum()), "p95_tail_fraction": float(baseline.mean()),
              "evt_tail_count": None, "evt_tail_fraction": None, "both": None,
              "p95_only": None, "evt_only": None, "agreement": None, "jaccard": None}
    if u is not None:
        tail = values > u
        both, union = int((baseline & tail).sum()), int((baseline | tail).sum())
        result.update({"evt_tail_count": int(tail.sum()), "evt_tail_fraction": float(tail.mean()),
                       "both": both, "p95_only": int((baseline & ~tail).sum()),
                       "evt_only": int((tail & ~baseline).sum()),
                       "agreement": float((baseline == tail).mean()), "jaccard": both / union if union else 1.0})
    return result


def workload_share(values, threshold):
    if threshold is None:
        return {"threshold": None, "status": "UNAVAILABLE", "tail_count": None,
                "tail_fraction": None, "tail_token_sum": None, "total_token_sum": int(np.sum(values)), "token_workload_share": None}
    values = np.asarray(values)
    tail = values > threshold
    denominator = int(values.sum())
    numerator = int(values[tail].sum())
    return {"threshold": threshold, "status": "OK", "tail_count": int(tail.sum()),
            "tail_fraction": float(tail.mean()), "tail_token_sum": numerator, "total_token_sum": denominator,
            "token_workload_share": numerator / denominator if denominator else None}


def label_and_overlap(frame, selections):
    labeled = frame.copy()
    available = all(selections[axis]["status"] == "OK" for axis in AXES)
    for axis in AXES:
        values = frame[axis + "_tokens"].to_numpy()
        baseline = float(np.quantile(values, .95, method="linear"))
        threshold = selections[axis]["selected_threshold"]
        labeled[axis + "_tail_p95"] = pd.Series(values > baseline, index=frame.index, dtype="boolean")
        # 无 EVT 阈值时使用可空布尔列，不能把“无法判定”误写成 False。
        labeled[axis + "_tail_evt"] = pd.Series(values > threshold if threshold is not None else pd.NA,
                                                index=frame.index, dtype="boolean")
    categories = ["Normal", "Input-tail / Prefill-heavy", "Output-tail / Decode-heavy", "Both-heavy"]
    if not available:
        quadrants = [{"workload": name, "count": None, "fraction": None, "status": "EVT_UNAVAILABLE"} for name in categories]
        overlap = {"status": "EVT_UNAVAILABLE", "intersection_count": None, "union_count": None,
                   "jaccard": None, "p_output_given_input": None, "p_input_given_output": None}
        return labeled, quadrants, overlap
    a = labeled.input_tail_evt.to_numpy(dtype=bool)
    b = labeled.output_tail_evt.to_numpy(dtype=bool)
    codes = a.astype(int) + 2 * b.astype(int)
    labeled["workload_quadrant"] = np.array(categories)[codes]
    quadrants = [{"workload": name, "count": int((codes == i).sum()),
                  "fraction": float((codes == i).mean()), "status": "OK"} for i, name in enumerate(categories)]
    intersection, union = int((a & b).sum()), int((a | b).sum())
    overlap = {"status": "OK", "input_tail_count": int(a.sum()), "output_tail_count": int(b.sum()),
               "intersection_count": intersection, "intersection_fraction": intersection / len(frame),
               "union_count": union, "union_fraction": union / len(frame),
               "jaccard": intersection / union if union else 1.0,
               "p_output_given_input": intersection / int(a.sum()) if a.sum() else None,
               "p_input_given_output": intersection / int(b.sum()) if b.sum() else None}
    return labeled, quadrants, overlap


def refresh_report(frame, source, directory, processed, config):
    """只重建已有统计的存储/展示，拒绝在数据或配置变化后复用旧拟合。"""
    metadata_path = directory / "stage2_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("source_sha256") != file_sha256(source) or metadata.get("config") != config.metadata():
        raise ValueError("Saved diagnostics do not match source/config; run the full Stage 2 pipeline")
    selections = {}
    bootstrap = {}
    for axis in AXES:
        result = json.loads((directory / f"{axis}_tail_result.json").read_text(encoding="utf-8"))
        if result.get("config") != config.metadata():
            raise ValueError("Tail result config does not match metadata")
        selections[axis] = result
        bootstrap[axis] = json.loads((directory / f"threshold_bootstrap_{axis}.json").read_text(encoding="utf-8"))
        if bootstrap[axis]["repetitions"] != config.threshold_bootstrap_repetitions:
            raise ValueError("Incomplete saved threshold bootstrap")
    lengths = {axis: frame[axis + "_tokens"].to_numpy(dtype=float) for axis in AXES}
    comparisons = [compare_p95_evt(lengths[axis], selections[axis], axis) for axis in AXES]
    shares = {axis: {"p95": workload_share(lengths[axis], comparisons[i]["p95_threshold"]),
                     "evt": workload_share(lengths[axis], selections[axis]["selected_threshold"])}
              for i, axis in enumerate(AXES)}
    labeled, quadrants, overlap = label_and_overlap(frame, selections)
    dataset_path = processed / "tail_labels.parquet"
    columns = TAIL_COLUMNS + (["workload_quadrant"] if "workload_quadrant" in labeled else [])
    save_labels(dataset_path, frame, labeled, columns)
    metadata.update({"output_dataset": str(dataset_path), "output_sha256": file_sha256(dataset_path),
                     "label_columns": columns, "last_report_refresh": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()})
    write_json(metadata_path, metadata)
    make_report(directory, metadata, selections, bootstrap, comparisons, shares, quadrants, overlap)
    print(f"Stage 2 stored diagnostics verified; refreshed labels/report at {directory}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="完整逻辑；GOF B=100、外层 bootstrap=20，保留完整长度样本")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--workers", type=int, default=min(20, max(1, (os.cpu_count() or 2) - 4)))
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--refresh-report", action="store_true", help="校验来源/config 后，用已完成的结果重建精简标签和报告，不重新拟合")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    config = TailConfig()
    if args.smoke:
        config = replace(config, gof_repetitions=100, threshold_bootstrap_repetitions=20)
        import unittest
        from ...tests import test_stage2
        if not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(test_stage2)).wasSuccessful():
            raise SystemExit("Stage 2 regression checks failed")
    started = time.perf_counter()
    source_digest = file_sha256(args.source)
    frame = pd.read_parquet(args.source)
    validate_lengths(frame)
    lengths = {axis: frame[axis + "_tokens"].to_numpy(dtype=float) for axis in AXES}
    if any(not np.isfinite(values).all() or np.any(values < 0) or np.any(values != np.floor(values)) for values in lengths.values()):
        raise ValueError("Token lengths must be finite nonnegative integers")
    directory = stage_results("stage2", args.smoke)
    processed = processed_dir(args.smoke, stage="stage2")
    checkpoints = directory / ".checkpoints"
    for path in (directory, processed, checkpoints):
        path.mkdir(parents=True, exist_ok=True)
    if args.refresh_report:
        refresh_report(frame, args.source, directory, processed, config)
        return
    # 算法或依赖版本改变时不复用旧 checkpoint；workers 不参与 signature，随机流与它无关。
    core_files = [Path(__file__), Path(__file__).with_name("gpd.py"),
                  Path(__file__).with_name("diagnostics.py"), Path(__file__).with_name("threshold_selection.py")]
    dependencies = {package: version(package) for package in ("numpy", "pandas", "scipy", "pyarrow", "matplotlib")}
    signature = {"stage1_sha256": source_digest, "config": asdict(config),
                 "algorithm_files_sha256": {path.name: file_sha256(path) for path in core_files},
                 "dependencies": dependencies}
    print(f"Stage 2 {'smoke' if args.smoke else 'full'}: {len(frame)} request lengths; GOF B={config.gof_repetitions}; outer bootstrap={config.threshold_bootstrap_repetitions}/axis; workers={args.workers}", flush=True)
    # 两个轴共用进程池以利用 CPU；不按模型、端点或 conversation 分组。
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        tables, windows, selections = primary_diagnostics(lengths, config, pool, checkpoints, signature, not args.no_resume)
        for axis in AXES:
            rows = []
            by_start = {w["start_candidate_index"]: w for w in windows[axis]}
            for record in tables[axis]:
                window = by_start.get(record["candidate_index"], {})
                rows.append({**record, "quantiles": json.dumps(record["quantiles"]),
                             "gof_pass": record["bootstrap_gof_p"] is not None and record["bootstrap_gof_p"] >= config.gof_alpha,
                             **{"window_" + key: value for key, value in window.items()}})
            pd.DataFrame(rows).to_csv(directory / f"{axis}_threshold_diagnostics.csv", index=False, encoding="utf-8")
            pd.DataFrame(windows[axis]).to_csv(directory / f"{axis}_window_diagnostics.csv", index=False, encoding="utf-8")
            write_json(directory / f"{axis}_tail_result.json", {"axis": axis, **selections[axis], "config": config.metadata()})
        make_diagnostic_plots(tables, selections, directory)
        boot_records = outer_bootstrap(lengths, config, pool, checkpoints, signature, not args.no_resume)
    bootstrap = {axis: bootstrap_summary(boot_records[axis], config, selections[axis]) for axis in AXES}
    for axis in AXES:
        assert len(boot_records[axis]) == config.threshold_bootstrap_repetitions
        write_json(directory / f"threshold_bootstrap_{axis}.json", bootstrap[axis])
    comparisons = [compare_p95_evt(lengths[axis], selections[axis], axis) for axis in AXES]
    pd.DataFrame(comparisons).to_csv(directory / "p95_vs_evt.csv", index=False, encoding="utf-8")
    shares = {axis: {"p95": workload_share(lengths[axis], comparisons[i]["p95_threshold"]),
                     "evt": workload_share(lengths[axis], selections[axis]["selected_threshold"])} for i, axis in enumerate(AXES)}
    write_json(directory / "tail_workload_share.json", shares)
    labeled, quadrants, overlap = label_and_overlap(frame, selections)
    pd.DataFrame(quadrants).to_csv(directory / "workload_quadrants.csv", index=False, encoding="utf-8")
    write_json(directory / "input_output_tail_overlap.json", overlap)
    # 原始行顺序、长度、conversation/request 定位键及其他 Stage 1 字段全部保持。
    pd.testing.assert_frame_equal(frame, labeled[list(frame.columns)])
    dataset_path = processed / "tail_labels.parquet"
    label_columns = TAIL_COLUMNS + (["workload_quadrant"] if "workload_quadrant" in labeled else [])
    save_labels(dataset_path, frame, labeled, label_columns)
    assert source_digest == file_sha256(args.source), "Stage 1 source was changed"
    metadata = {"stage": 2, "mode": "smoke" if args.smoke else "full", "request_count": len(frame),
                "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "source": str(args.source.resolve()), "source_sha256": source_digest,
                "output_dataset": str(dataset_path), "output_sha256": file_sha256(dataset_path),
                "config": config.metadata(), "workers": args.workers, "signature": signature,
                "source_text_read": False, "tokenization_performed": False,
                "checks": {"source_unchanged": True, "stage1_columns_preserved": True,
                           "parquet_roundtrip": True, "outer_replicate_count": True},
                "elapsed_seconds": round(time.perf_counter() - started, 3)}
    write_json(directory / "stage2_metadata.json", metadata)
    make_report(directory, metadata, selections, bootstrap, comparisons, shares, quadrants, overlap)
    print(f"Stage 2 complete: {dataset_path}; report: {directory / 'stage2_report.md'}", flush=True)


if __name__ == "__main__":
    main()
