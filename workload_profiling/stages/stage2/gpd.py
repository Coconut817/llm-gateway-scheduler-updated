"""GPD 拟合与重新拟合的参数 bootstrap GOF；不使用普通 KS p-value。"""
from __future__ import annotations

import warnings
import numpy as np
from scipy.optimize import fmin, minimize
from scipy.stats import genpareto


def _mean_nll_gradient(parameters, data):
    """固定 loc=0 的平均负对数似然及解析梯度；参数为 (xi, log(sigma))。"""
    xi, log_scale = parameters
    if not np.isfinite(parameters).all() or abs(log_scale) > 700:
        return np.inf, np.zeros(2)
    scaled = data * np.exp(-log_scale)
    support = 1 + xi * scaled
    if np.any(support <= 0):
        return np.inf, np.zeros(2)
    if abs(xi) < 1e-5 and np.max(np.abs(xi * scaled)) < 1e-3:
        # xi 接近零时直接相减会丢失精度，使用 log(1+xi*x) 的展开和指数极限。
        m1, m2, m3, m4 = (np.mean(scaled ** power) for power in (1, 2, 3, 4))
        value = log_scale + m1 + xi * (m1 - m2 / 2) + xi ** 2 * (m3 / 3 - m2 / 2) + xi ** 3 * (m3 / 3 - m4 / 4)
        shape_gradient = m1 - m2 / 2 + xi * (2 * m3 / 3 - m2) + xi ** 2 * (m3 - 3 * m4 / 4)
        scale_gradient = 1 - m1 + xi * (m2 - m1) + xi ** 2 * (m2 - m3) + xi ** 3 * (m4 - m3)
    else:
        log_mean = np.mean(np.log1p(xi * scaled))
        fraction_mean = np.mean(scaled / support)
        value = log_scale + (1 + 1 / xi) * log_mean
        shape_gradient = -log_mean / xi ** 2 + (1 + 1 / xi) * fraction_mean
        scale_gradient = 1 - (1 + xi) * fraction_mean
    return value, np.array([shape_gradient, scale_gradient])


def _refit_optimizer(function, initial, args=(), disp=0):
    """SciPy fit 支持的 optimizer：同一似然、解析梯度 BFGS，失败回退默认 Nelder-Mead。"""
    data = args[0]
    with warnings.catch_warnings():
        # 线搜索可能试探支持域外点，返回 inf 后继续搜索；不输出每次试探的警告。
        warnings.simplefilter("ignore", RuntimeWarning)
        solution = minimize(_mean_nll_gradient, [initial[0], np.log(initial[1])],
                            args=(data,), jac=True, method="BFGS", options={"gtol": 1e-7, "maxiter": 150})
    if np.isfinite(solution.fun) and np.isfinite(solution.jac).all() and np.linalg.norm(solution.jac, np.inf) < 1e-5:
        parameters = np.array([solution.x[0], np.exp(solution.x[1])])
        # 用 SciPy 原似然再检查一次；不接受比生成分布初值更差的“优化”结果。
        if np.isfinite(function(parameters, *args)) and function(parameters, *args) <= function(initial, *args) + len(data) * 1e-9:
            return parameters
    return fmin(function, initial, args=args, disp=disp)


def fit_gpd(exceedances, start=None) -> dict:
    """loc 固定为 0；start 只提供优化初值，xi/sigma 仍需重新估计。"""
    y = np.asarray(exceedances, dtype=float)
    if len(y) < 2 or not np.isfinite(y).all() or np.any(y <= 0):
        return {"fit_ok": False, "fit_error": "invalid_exceedances"}
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            if start is None:
                xi, loc, sigma = genpareto.fit(y, floc=0)
            else:
                # synthetic data 用生成分布作初值以减少优化时间，并非固定未知参数。
                xi, loc, sigma = genpareto.fit(y, start[0], floc=0, scale=start[1], optimizer=_refit_optimizer)
        if not np.isfinite([xi, sigma]).all() or sigma <= 0 or loc != 0:
            raise ValueError("nonfinite_or_invalid_parameters")
        if not np.all(1 + xi * y / sigma > 0):
            raise ValueError("observations_outside_gpd_support")
        log_likelihood = float(genpareto.logpdf(y, xi, loc=0, scale=sigma).sum())
        if not np.isfinite(log_likelihood):
            raise ValueError("nonfinite_log_likelihood")
        return {"fit_ok": True, "fit_error": None, "xi": float(xi), "sigma": float(sigma),
                "log_likelihood": log_likelihood, "fit_warning_count": len(caught)}
    except (ValueError, RuntimeError, FloatingPointError) as error:
        return {"fit_ok": False, "fit_error": type(error).__name__ + ":" + str(error)}


def ks_statistic(exceedances, xi, sigma) -> float:
    """两侧经验 CDF 差的最大值；只返回 D，不计算假定参数已知的 KS p。"""
    y = np.sort(np.asarray(exceedances, dtype=float))
    cdf = genpareto.cdf(y, xi, loc=0, scale=sigma)
    n = len(y)
    return float(max(np.max(np.arange(1, n + 1) / n - cdf),
                     np.max(cdf - np.arange(n) / n)))


def parametric_bootstrap_gof(exceedances, fit, repetitions: int, seed) -> dict:
    """每个 synthetic sample 都重新拟合，再用 (1 + #D_b>=D_obs)/(B+1)。"""
    y = np.asarray(exceedances, dtype=float)
    xi, sigma = fit["xi"], fit["sigma"]
    observed = ks_statistic(y, xi, sigma)
    rng = np.random.default_rng(seed)
    extreme = 0
    failures = 0
    statistics = []
    for _ in range(repetitions):
        synthetic = genpareto.rvs(xi, loc=0, scale=sigma, size=len(y), random_state=rng)
        refit = fit_gpd(synthetic, start=(xi, sigma))
        if not refit["fit_ok"]:
            # 失败不从 B 中静默删除，也不补 p-value；该 candidate 的 GOF 作失败处理。
            failures += 1
            continue
        statistic = ks_statistic(synthetic, refit["xi"], refit["sigma"])
        statistics.append(statistic)
        extreme += statistic >= observed
    return {
        "ks_statistic": observed, "bootstrap_gof_p": (1 + int(extreme)) / (repetitions + 1) if not failures else None,
        "gof_repetitions": repetitions, "gof_refit_successes": repetitions - failures,
        "gof_refit_failures": failures, "gof_extreme_count": int(extreme),
        "gof_status": "OK" if not failures else "BOOTSTRAP_REFIT_FAILED",
        "null_ks_median": float(np.median(statistics)) if statistics else None,
    }
