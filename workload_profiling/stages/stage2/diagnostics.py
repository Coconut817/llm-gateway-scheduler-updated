"""候选阈值、参数稳定性及 MRL 诊断；不把辅助 MRL 当作选择门槛。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import numpy as np
from scipy.stats import linregress

from .gpd import fit_gpd


@dataclass(frozen=True)
class TailConfig:
    # Input/Output 以及每次外层 bootstrap 都使用同一套选择标准。
    quantile_min: int = 70
    quantile_max: int = 98
    minimum_tail_count: int = 100
    minimum_tail_fraction: float = .02
    window_size: int = 4
    xi_range_limit: float = .15
    xi_cv_limit: float = .20
    xi_near_zero: float = .05
    modified_scale_cv_limit: float = .20
    cv_zero_epsilon: float = 1e-12
    gof_alpha: float = .05
    mrl_r2_support: float = .90
    gof_repetitions: int = 500
    threshold_bootstrap_repetitions: int = 200
    seed: int = 20261003
    std_ddof: int = 0

    def metadata(self):
        return {**asdict(self), "quantile_method": "linear", "exceedance_rule": "X > u",
                "empirical_percentile_rule": "100 * count(X <= u) / n",
                "cv_denominator": "abs(mean)",
                "xi_near_zero_rule": "abs(window xi mean) < .05: record CV, use xi range only",
                "modified_scale_near_zero_rule": "abs(mean) <= 1e-12: CV undefined, window fails",
                "mrl_policy": "auxiliary only; never veto selection",
                "gof_policy": "continuous GPD parametric bootstrap with synthetic-data refitting; no ordinary KS p-value",
                "outer_bootstrap_policy": "iid resampling of request length observations; same full selection rules and same GOF B",
                "synthetic_fit_initialization": "parent fitted xi/sigma are starting values only",
                "synthetic_fit_optimizer": "SciPy genpareto.fit optimizer hook: analytic-gradient BFGS in xi/log(sigma), same likelihood; convergence/objective checked, default Nelder-Mead fallback"}


def unique_thresholds(values, config: TailConfig) -> list[dict]:
    """按实际浮点 threshold 去重，保留该阈值对应的所有网格 quantile。"""
    values = np.asarray(values, dtype=float)
    groups = {}
    for integer in range(config.quantile_min, config.quantile_max + 1):
        q = integer / 100
        u = float(np.quantile(values, q, method="linear"))
        groups.setdefault(u, []).append(q)
    records = []
    for index, (threshold, quantiles) in enumerate(sorted(groups.items())):
        records.append({"candidate_index": index, "quantile": quantiles[0],
                        "quantile_min": min(quantiles), "quantile_max": max(quantiles),
                        "quantile_count": len(quantiles), "quantiles": quantiles, "threshold": threshold})
    return records


def fit_candidates(values, config: TailConfig) -> list[dict]:
    values = np.asarray(values, dtype=float)
    records = unique_thresholds(values, config)
    for record in records:
        u = record["threshold"]
        # 严格大于 u，等于阈值的 token 长度不进入尾部。
        excess = values[values > u] - u
        n = len(excess)
        record.update({"tail_count": n, "tail_fraction": n / len(values),
                       "empirical_percentile": float(np.mean(values <= u) * 100),
                       "mean_excess": float(np.mean(excess)) if n else None,
                       "xi": None, "sigma": None, "modified_scale": None,
                       "ks_statistic": None, "bootstrap_gof_p": None,
                       "gof_status": "NOT_EVALUATED", "gof_repetitions": 0,
                       "gof_refit_failures": 0, "gof_refit_successes": 0})
        record["sample_valid"] = n >= config.minimum_tail_count and n / len(values) >= config.minimum_tail_fraction
        if not record["sample_valid"]:
            record.update({"fit_ok": False, "fit_error": "insufficient_tail_sample", "candidate_valid": False})
            continue
        record.update(fit_gpd(excess))
        record["candidate_valid"] = record["fit_ok"]
        if record["fit_ok"]:
            # 原始 sigma 会随 u 变化，应检查 threshold-invariant 的 modified scale。
            record["modified_scale"] = record["sigma"] - record["xi"] * u
    return records


def window_diagnostics(records, config: TailConfig) -> list[dict]:
    """在原始 unique threshold 序列上滑窗，不跨过无效候选拼接窗口。"""
    windows = []
    for start in range(len(records) - config.window_size + 1):
        segment = records[start:start + config.window_size]
        basic_valid = all(r["candidate_valid"] for r in segment)
        window = {"window_index": start, "start_candidate_index": start,
                  "end_candidate_index": start + config.window_size - 1,
                  "start_threshold": segment[0]["threshold"], "end_threshold": segment[-1]["threshold"],
                  "basic_valid": basic_valid, "shape_stable": False,
                  "modified_scale_stable": False, "parameter_stable": False,
                  "gof_pass": False, "sustained_valid": False,
                  "xi_mean": None, "xi_std": None, "xi_range": None, "xi_cv": None,
                  "xi_cv_bypassed": False, "modified_scale_mean": None,
                  "modified_scale_std": None, "modified_scale_range": None,
                  "modified_scale_cv": None, "mrl_slope": None, "mrl_r2": None,
                  "mrl_support": False}
        if basic_valid:
            shape = np.array([r["xi"] for r in segment])
            scale = np.array([r["modified_scale"] for r in segment])
            xi_mean = float(shape.mean())
            xi_std = float(shape.std(ddof=config.std_ddof))
            xi_range = float(np.ptp(shape))
            xi_cv = xi_std / abs(xi_mean) if abs(xi_mean) > config.cv_zero_epsilon else None
            xi_near_zero = abs(xi_mean) < config.xi_near_zero
            modified_mean = float(scale.mean())
            modified_std = float(scale.std(ddof=config.std_ddof))
            modified_cv = modified_std / abs(modified_mean) if abs(modified_mean) > config.cv_zero_epsilon else None
            shape_stable = xi_range <= config.xi_range_limit and (xi_near_zero or (xi_cv is not None and xi_cv <= config.xi_cv_limit))
            scale_stable = modified_cv is not None and modified_cv <= config.modified_scale_cv_limit
            regression = linregress([r["threshold"] for r in segment], [r["mean_excess"] for r in segment])
            r2 = float(regression.rvalue ** 2) if np.isfinite(regression.rvalue) else None
            window.update({"xi_mean": xi_mean, "xi_std": xi_std, "xi_range": xi_range,
                           "xi_cv": xi_cv, "xi_cv_bypassed": xi_near_zero,
                           "shape_stable": bool(shape_stable), "modified_scale_mean": modified_mean,
                           "modified_scale_std": modified_std, "modified_scale_range": float(np.ptp(scale)),
                           "modified_scale_cv": modified_cv, "modified_scale_stable": bool(scale_stable),
                           "parameter_stable": bool(shape_stable and scale_stable),
                           "mrl_slope": float(regression.slope), "mrl_r2": r2,
                           "mrl_support": r2 is not None and r2 >= config.mrl_r2_support})
        windows.append(window)
    return windows


def attach_gof_to_windows(records, windows, config):
    for window in windows:
        segment = records[window["start_candidate_index"]:window["end_candidate_index"] + 1]
        window["gof_pass"] = all(r["bootstrap_gof_p"] is not None and r["bootstrap_gof_p"] >= config.gof_alpha for r in segment)
        window["sustained_valid"] = window["parameter_stable"] and window["gof_pass"]
