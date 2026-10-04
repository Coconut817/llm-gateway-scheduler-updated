"""核心规则测试：去重、连续窗口、GOF 重拟合、失败标签和原始字段保留。"""
from dataclasses import replace
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from scipy.stats import genpareto, kstest
from scipy.optimize import check_grad

from ..stages.stage2.diagnostics import TailConfig, unique_thresholds, window_diagnostics, fit_candidates
from ..stages.stage2.gpd import ks_statistic, parametric_bootstrap_gof, fit_gpd, _mean_nll_gradient
from ..stages.stage2.threshold_selection import select_threshold, selection_result, bootstrap_summary, NO_THRESHOLD
from ..stages.stage2.run import label_and_overlap, compare_p95_evt


def candidate(index, xi=.2, scale=100, p=.5, valid=True):
    # 使用手工参数隔离选择规则，避免把随机 GOF 的波动误当作单元测试失败。
    return {"candidate_index": index, "quantile_min": .70 + index * .01, "quantile_max": .70 + index * .01,
            "threshold": 100 + index * 20, "xi": xi, "sigma": scale + xi * (100 + index * 20),
            "modified_scale": scale, "mean_excess": 40 + index * 4,
            "candidate_valid": valid, "bootstrap_gof_p": p, "gof_status": "OK",
            "gof_repetitions": 500, "tail_count": 200, "tail_fraction": .2, "empirical_percentile": 80.0}


class TailTests(unittest.TestCase):
    def test_analytic_gradient_including_zero_shape(self):
        values = np.linspace(.1, 20, 200)
        for xi in (-.1, 0., 1e-6, .2, 1.):
            point = np.array([xi, np.log(10)])
            difference = check_grad(lambda p: _mean_nll_gradient(p, values)[0],
                                    lambda p: _mean_nll_gradient(p, values)[1], point)
            self.assertLess(difference, 1e-5)

    def test_fast_refit_matches_default_scipy_likelihood(self):
        rng = np.random.default_rng(73)
        for xi, sigma in [(.2, 20000), (.85, 150), (-.2, 10), (.01, 10)]:
            values = genpareto.rvs(xi, scale=sigma, size=1000, random_state=rng)
            reference = genpareto.fit(values, xi, floc=0, scale=sigma)
            fast = fit_gpd(values, start=(xi, sigma))
            self.assertTrue(fast["fit_ok"])
            np.testing.assert_allclose([fast["xi"], fast["sigma"]], [reference[0], reference[2]], rtol=1e-3, atol=1e-4)
            expected = genpareto.logpdf(values, reference[0], scale=reference[2]).sum()
            self.assertGreaterEqual(fast["log_likelihood"], expected - 1e-4)

    def test_duplicate_thresholds_keep_quantile_range(self):
        rows = unique_thresholds(np.zeros(1000), TailConfig())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantile_count"], 29)
        self.assertEqual((rows[0]["quantile_min"], rows[0]["quantile_max"]), (.7, .98))

    def test_selects_lowest_sustained_window(self):
        rows = [candidate(i, p=.05 if i == 0 else .9) for i in range(6)]
        config = TailConfig()
        result = selection_result(np.arange(1000), rows, window_diagnostics(rows, config), config)
        self.assertEqual(result["selected_threshold"], 100)

    def test_invalid_candidate_is_not_skipped_when_forming_windows(self):
        rows = [candidate(i, valid=i != 2) for i in range(6)]
        result = selection_result(np.arange(1000), rows, window_diagnostics(rows, TailConfig()), TailConfig())
        self.assertEqual(result["status"], NO_THRESHOLD)

    def test_all_four_candidates_need_gof(self):
        rows = [candidate(i, p=.049 if i == 2 else .9) for i in range(6)]
        result = selection_result(np.arange(1000), rows, window_diagnostics(rows, TailConfig()), TailConfig())
        self.assertEqual(result["status"], NO_THRESHOLD)

    def test_near_zero_xi_uses_range_and_records_cv(self):
        rows = [candidate(i, xi=[-.01, .01, -.01, .01][i]) for i in range(4)]
        window = window_diagnostics(rows, TailConfig())[0]
        self.assertTrue(window["xi_cv_bypassed"])
        self.assertTrue(window["shape_stable"])

    def test_negative_modified_scale_is_allowed(self):
        rows = [candidate(i, scale=-10) for i in range(4)]
        self.assertTrue(all(r["sigma"] > 0 for r in rows))
        self.assertTrue(window_diagnostics(rows, TailConfig())[0]["modified_scale_stable"])

    def test_mrl_never_vetoes_selection(self):
        rows = [candidate(i) for i in range(4)]
        for row, mean in zip(rows, [1, 100, 2, 10]):
            row["mean_excess"] = mean
        windows = window_diagnostics(rows, TailConfig())
        self.assertFalse(windows[0]["mrl_support"])
        self.assertEqual(selection_result(np.arange(1000), rows, windows, TailConfig())["status"], "OK")

    def test_ks_statistic_matches_scipy_statistic_only(self):
        values = np.array([1, 1, 2, 3, 8, 13], dtype=float)
        expected = kstest(values, genpareto.cdf, args=(.2, 0, 4)).statistic
        self.assertAlmostEqual(ks_statistic(values, .2, 4), expected)

    def test_parametric_bootstrap_refits_every_sample(self):
        values = genpareto.rvs(.2, scale=10, size=150, random_state=np.random.default_rng(8))
        fit = fit_gpd(values)
        with patch("workload_profiling.stages.stage2.gpd.fit_gpd", wraps=fit_gpd) as refit:
            result = parametric_bootstrap_gof(values, fit, 5, 33)
            self.assertEqual(refit.call_count, 5)
        self.assertEqual(result["bootstrap_gof_p"], (1 + result["gof_extreme_count"]) / 6)

    def test_minimum_count_and_fraction_both_enforced(self):
        # 103 个超越值达到 count>=100，但 103/5186<2%，仍不得进行拟合。
        values = np.concatenate([np.zeros(5083), np.ones(103)])
        with patch("workload_profiling.stages.stage2.diagnostics.fit_gpd") as fitter:
            records = fit_candidates(values, TailConfig())
            self.assertEqual(fitter.call_count, 0)
        self.assertFalse(records[0]["sample_valid"])
        self.assertEqual(TailConfig().minimum_tail_count, 100)

    def test_failed_synthetic_refits_do_not_create_p_value(self):
        values = np.arange(1, 151, dtype=float)
        with patch("workload_profiling.stages.stage2.gpd.fit_gpd", return_value={"fit_ok": False}):
            result = parametric_bootstrap_gof(values, {"xi": .2, "sigma": 10}, 5, 33)
        self.assertIsNone(result["bootstrap_gof_p"])
        self.assertEqual(result["gof_refit_failures"], 5)

    def test_short_circuit_selection_equals_complete_gof(self):
        values = genpareto.rvs(.2, scale=100, size=1500, random_state=np.random.default_rng(82))
        config = replace(TailConfig(), gof_repetitions=4)
        def fake_gof(data, row, config, axis, trial):
            return {**row, "gof_status": "OK", "gof_repetitions": 4,
                    "bootstrap_gof_p": .01 if row["candidate_index"] < 3 else .5}
        with patch("workload_profiling.stages.stage2.threshold_selection.evaluate_candidate_gof", side_effect=fake_gof):
            complete, _, _ = select_threshold(values, config, all_gof=True)
            lazy, _, _ = select_threshold(values, config, all_gof=False)
        self.assertEqual((complete["status"], complete["selected_threshold"]), (lazy["status"], lazy["selected_threshold"]))

    def test_unavailable_evt_labels_are_null_and_lengths_preserved(self):
        frame = pd.DataFrame({"conversation_id": [0, 0], "request_index": [0, 1],
                              "input_tokens": [1, 2], "output_tokens": [4, 8], "total_tokens": [5, 10]})
        failed = {axis: {"status": NO_THRESHOLD, "selected_threshold": None} for axis in ["input", "output"]}
        labeled, quadrants, overlap = label_and_overlap(frame, failed)
        pd.testing.assert_frame_equal(labeled[list(frame.columns)], frame)
        self.assertTrue(labeled.input_tail_evt.isna().all())
        self.assertEqual(overlap["status"], "EVT_UNAVAILABLE")
        self.assertIsNone(quadrants[0]["count"])

    def test_strict_exceedance_and_comparison(self):
        result = {"status": "OK", "selected_threshold": 10, "empirical_percentile": 50}
        c = compare_p95_evt(np.array([5, 10, 20, 30]), result, "input")
        self.assertEqual(c["evt_tail_count"], 2)
        self.assertEqual(c["both"], 1)

    def test_failed_bootstraps_not_zero_thresholds_in_interval(self):
        rows = [{"repetition": 1, "status": "OK", "selected_threshold": 100, "empirical_percentile": 80, "gof_total_refits": 500},
                {"repetition": 2, "status": NO_THRESHOLD, "selected_threshold": None, "empirical_percentile": None, "gof_total_refits": 500}]
        result = bootstrap_summary(rows, TailConfig(), {"status": "OK"})
        self.assertEqual(result["selection_success_rate"], .5)
        self.assertEqual(result["threshold"]["median"], 100)


if __name__ == "__main__":
    unittest.main()
