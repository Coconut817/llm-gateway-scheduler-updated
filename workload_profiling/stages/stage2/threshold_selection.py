"""选择最低的持续稳定区域，并对完整选择算法进行外层 bootstrap。"""
from __future__ import annotations

import numpy as np
from .diagnostics import TailConfig, fit_candidates, window_diagnostics, attach_gof_to_windows
from .gpd import parametric_bootstrap_gof

NO_THRESHOLD = "NO_STABLE_EVT_THRESHOLD"


def candidate_seed(config, axis_index, trial_index, candidate_index):
    # 每个 trial/candidate 的独立随机流不依赖进程完成顺序，保证并行运行可复现。
    return np.random.SeedSequence([config.seed, axis_index, trial_index, candidate_index, 11])


def evaluate_candidate_gof(values, record, config, axis_index, trial_index):
    threshold = record["threshold"]
    values = np.asarray(values, dtype=float)
    excess = values[values > threshold] - threshold
    result = dict(record)
    result.update(parametric_bootstrap_gof(
        excess, record, config.gof_repetitions,
        candidate_seed(config, axis_index, trial_index, record["candidate_index"]),
    ))
    return result


def selection_result(values, records, windows, config):
    """候选按 u 递增，取第一个满足全部要求的四阈值窗口起点。"""
    attach_gof_to_windows(records, windows, config)
    selected = next((w for w in windows if w["sustained_valid"]), None)
    result = {"status": "OK" if selected else NO_THRESHOLD,
              "selected_threshold": None, "empirical_percentile": None,
              "tail_count": None, "tail_fraction": None, "selected_window": selected,
              "unique_candidate_count": len(records),
              "parameter_stable_window_count": sum(w["parameter_stable"] for w in windows),
              "sustained_valid_window_count": sum(w["sustained_valid"] for w in windows),
              "gof_evaluated_candidate_count": sum(r["gof_status"] != "NOT_EVALUATED" for r in records),
              "gof_total_refits": sum(r["gof_repetitions"] for r in records),
              "rule": "first/lowest sustained valid region of four consecutive unique thresholds; MRL auxiliary"}
    if selected:
        record = records[selected["start_candidate_index"]]
        result.update({"selected_threshold": record["threshold"], "empirical_percentile": record["empirical_percentile"],
                       "tail_count": record["tail_count"], "tail_fraction": record["tail_fraction"],
                       "xi": record["xi"], "sigma": record["sigma"], "modified_scale": record["modified_scale"],
                       "bootstrap_gof_p": record["bootstrap_gof_p"],
                       "quantile_min": record["quantile_min"], "quantile_max": record["quantile_max"]})
    return result


def select_threshold(values, config: TailConfig, axis_index=0, trial_index=0, all_gof=False):
    records = fit_candidates(values, config)
    windows = window_diagnostics(records, config)
    if all_gof:
        for i, record in enumerate(records):
            if record["candidate_valid"]:
                records[i] = evaluate_candidate_gof(values, record, config, axis_index, trial_index)
    else:
        # 外层 bootstrap 只计算可能影响“第一个合法窗口”的 GOF，并复用重叠候选。
        # 这是确定性的短路求值，不降低 B、不改选择规则。未用到的候选无需拟合 GOF。
        for window in windows:
            if not window["parameter_stable"]:
                continue
            good = True
            for i in range(window["start_candidate_index"], window["end_candidate_index"] + 1):
                if records[i]["gof_status"] == "NOT_EVALUATED":
                    records[i] = evaluate_candidate_gof(values, records[i], config, axis_index, trial_index)
                p = records[i]["bootstrap_gof_p"]
                if p is None or p < config.gof_alpha:
                    good = False
                    break
            if good:
                break
    result = selection_result(values, records, windows, config)
    return result, records, windows


def bootstrap_selection_task(values, config, axis_index, repetition):
    # 用户要求 request-level observation 重采样，样本量与原长度列完全相同。
    rng = np.random.default_rng(np.random.SeedSequence([config.seed, axis_index, repetition, 23]))
    sample = rng.choice(np.asarray(values, dtype=float), size=len(values), replace=True)
    result, _, _ = select_threshold(sample, config, axis_index, repetition, all_gof=False)
    percentile = result["empirical_percentile"]
    return {"repetition": repetition, **result,
            "empirical_quantile": percentile / 100 if percentile is not None else None,
            "gof_diagnostics_scope": "short_circuit evaluation of first sustained valid region"}


def bootstrap_summary(records, config, original_result):
    success = [r for r in records if r["status"] == "OK"]
    def interval(key):
        if not success:
            return {"median": None, "p2_5": None, "p97_5": None}
        values = np.array([r[key] for r in success], dtype=float)
        return {"median": float(np.median(values)), "p2_5": float(np.quantile(values, .025)),
                "p97_5": float(np.quantile(values, .975))}
    threshold = interval("selected_threshold")
    percentile = interval("empirical_percentile")
    # 区间只基于成功选出阈值的 replicates，失败率另列，不能把失败当作零阈值。
    relative_width = ((threshold["p97_5"] - threshold["p2_5"]) / abs(threshold["median"])
                      if success and threshold["median"] != 0 else None)
    return {"repetitions": len(records), "requested_repetitions": config.threshold_bootstrap_repetitions,
            "gof_repetitions_per_evaluated_candidate": config.gof_repetitions,
            "selection_success_count": len(success), "selection_success_rate": len(success) / len(records),
            "threshold": threshold, "empirical_percentile": percentile,
            "empirical_quantile": {key: value / 100 if value is not None else None for key, value in percentile.items()},
            "conditional_interval": "2.5%/97.5% percentiles among successful selections only",
            "threshold_interval_relative_width": relative_width,
            "original_selection_status": original_result["status"],
            "total_gof_refits": sum(r["gof_total_refits"] for r in records),
            "records": sorted(records, key=lambda r: r["repetition"])}
