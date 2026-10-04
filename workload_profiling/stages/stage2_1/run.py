"""离线构建冻结 reference 和默认标签快照；不采集或估计运行时拥塞。"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
import unittest

import numpy as np
import pandas as pd

from ...tests import test_runtime_policy
from ...runtime.output_heavy_policy import CONFIG_PATH, OutputHeavyPolicy, load_config
from ...runtime.percentile_reference import PERCENTILE_DEFINITION, PercentileReference, file_sha256

from ...common.paths import PROCESSED, ARTIFACTS, relative, stage_results
from ...common.io import write_json
from ...common.datasets import load_dataset, save_labels, OUTPUT_COLUMNS
from .reporting import write_report

RESULTS = stage_results("stage2_1")


class RecordingResult(unittest.TextTestResult):
    """将实际执行的测试名称保存到 metadata，避免只记录一个笼统的 PASS。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_names = []

    def startTest(self, test):
        self.test_names.append(test.id())
        super().startTest(test)


def run_tests():
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_runtime_policy)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    summary = {"tests_run": result.testsRun, "successful": result.wasSuccessful(),
               "test_names": result.test_names,
               "failures": [{"test": test.id(), "details": detail} for test, detail in result.failures],
               "errors": [{"test": test.id(), "details": detail} for test, detail in result.errors],
               "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped]}
    write_json(RESULTS / "runtime_policy_test_results.json", summary)
    if not result.wasSuccessful():
        raise RuntimeError(stream.getvalue())
    return summary


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    test_results = run_tests()
    stage1_path = PROCESSED / "request_lengths.parquet"
    stage2_path = PROCESSED / "tail_labels.parquet"
    source_hashes = {relative(path): file_sha256(path) for path in (stage1_path, stage2_path)}
    stage1 = pd.read_parquet(stage1_path)
    stage2 = load_dataset("stage2")
    # 在同一批请求上扩展字段，避免静默丢行、错位或用其它数据覆盖历史 reference。
    if stage1.empty or not stage1.tokenization_status.eq("ok").all():
        raise ValueError("Stage 1 must contain nonempty, successfully tokenized requests")
    pd.testing.assert_frame_equal(stage2[list(stage1.columns)], stage1)
    if stage1.duplicated(["conversation_id", "request_index"]).any():
        raise ValueError("Stage 1 conversation_id/request_index keys must be unique")

    config = load_config()
    reference = PercentileReference.from_lengths(stage1.output_tokens.to_numpy())
    artifact_path = ARTIFACTS / "output_percentile_reference.parquet"
    metadata_path = RESULTS / "percentile_reference_metadata.json"
    reference.save(artifact_path)
    metadata = {"schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_dataset": relative(stage1_path), "source_sha256": source_hashes[relative(stage1_path)],
                "source_column": "output_tokens", "reference_sample_count": reference.sample_count,
                "unique_length_count": len(reference.values),
                "percentile_definition": PERCENTILE_DEFINITION,
                "percentile_formula": "count(reference.output_tokens <= x) / reference_sample_count",
                "ties_policy": "All equal lengths share the same right ECDF value; each request has unit weight",
                "below_minimum": 0.0, "at_or_above_maximum": 1.0,
                "reference_update_policy": "FIXED_OFFLINE_REFERENCE; no runtime updates",
                "meaning": "Historical rank; not an EVT tail probability or an output-length prediction",
                "length_semantics": "Inherited Stage 1 Qwen/Qwen3-8B offline tokenizer proxy lengths",
                "reference_artifact": relative(artifact_path), "artifact_sha256": file_sha256(artifact_path)}
    write_json(metadata_path, metadata)

    # 配置副本记录本次 reference 的 N；运行时使用该副本可验证与 artifact 的匹配。
    config.update({"reference_sample_count": reference.sample_count,
                   "reference_artifact": relative(artifact_path), "reference_metadata": relative(metadata_path),
                   "config_source": relative(CONFIG_PATH), "config_source_sha256": file_sha256(CONFIG_PATH)})
    config_path = RESULTS / "runtime_policy_config.json"
    write_json(config_path, config)
    loaded = PercentileReference.load(artifact_path, metadata_path)
    policy = OutputHeavyPolicy(loaded, config_path=config_path)
    percentiles = loaded.percentiles(stage1.output_tokens.to_numpy())
    threshold = policy.get_threshold()

    dataset = stage2.copy()
    dataset["output_percentile"] = percentiles
    dataset["output_heavy_threshold"] = threshold
    dataset["output_heavy"] = percentiles >= threshold
    dataset["threshold_source"] = "DEFAULT"
    dataset["congestion_state"] = pd.Series(pd.NA, index=dataset.index, dtype="string")
    dataset_path = PROCESSED / "output_labels.parquet"
    save_labels(dataset_path, stage2, dataset, OUTPUT_COLUMNS)
    metadata.update({"labels_artifact": relative(dataset_path), "labels_sha256": file_sha256(dataset_path)})
    write_json(metadata_path, metadata)
    restored = load_dataset("stage2_1")

    # 独立核对：对每个不同长度直接数 <=x 的原始样本，不复用搜索实现。
    raw = stage1.output_tokens.to_numpy()
    expected = {int(length): float(np.count_nonzero(raw <= length) / len(raw)) for length in np.unique(raw)}
    np.testing.assert_array_equal(restored.output_percentile.to_numpy(), stage1.output_tokens.map(expected).to_numpy())
    pd.testing.assert_frame_equal(restored, dataset)
    pd.testing.assert_frame_equal(restored[list(stage2.columns)], stage2)
    if not np.array_equal(restored.output_heavy, restored.output_percentile >= restored.output_heavy_threshold):
        raise AssertionError("Saved Output-Heavy labels disagree with the inclusive threshold rule")
    for path in (stage1_path, stage2_path):
        if file_sha256(path) != source_hashes[relative(path)]:
            raise AssertionError("Historical source dataset changed during construction")
    # 复核完整运行时接口和批量离线构建对每个 unique length 的判断一致。
    for length, percentile in expected.items():
        decision = policy.evaluate(length)
        if decision["output_percentile"] != percentile or decision["output_heavy"] != (percentile >= threshold):
            raise AssertionError("Runtime and batch construction disagree")

    acceptance = {"reference_artifact_roundtrip_and_checksum": True,
                  "full_ecdf_matches_direct_counts_including_ties": True,
                  "stage2_1_parquet_roundtrip": True,
                  "all_stage2_columns_preserved_including_nullable_evt": True,
                  "inclusive_output_heavy_labels": True,
                  "stage1_and_stage2_source_hashes_unchanged": True,
                  "runtime_decisions_match_offline_percentiles_and_labels": True}
    test_results.update({"artifact_checks": acceptance, "source_hashes": source_hashes,
                         "reference_sample_count": reference.sample_count, "dataset_rows": len(restored),
                         "dataset_columns": list(pd.read_parquet(dataset_path).columns),
                         "complete_view_columns": list(restored.columns), "dataset_path": relative(dataset_path),
                         "dataset_sha256": file_sha256(dataset_path), "default_threshold": threshold,
                         "default_output_heavy_count": int(restored.output_heavy.sum()),
                         "default_output_heavy_fraction": float(restored.output_heavy.mean()),
                         "preserved_output_tail_evt_null_count": int(restored.output_tail_evt.isna().sum())})
    write_json(RESULTS / "runtime_policy_test_results.json", test_results)
    write_report(loaded, restored, config, metadata, test_results, acceptance, RESULTS)
    print(json.dumps({"tests_passed": test_results["tests_run"], "artifact_checks_passed": len(acceptance),
                      "reference_requests": loaded.sample_count, "dataset": relative(dataset_path),
                      "default_heavy_count": int(restored.output_heavy.sum()),
                      "report": relative(RESULTS / "runtime_policy_report.md")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
