"""长度只存一次，阶段标签按稳定 request 键关联，拒绝错位和旧基线。"""
from pathlib import Path
import json

import pandas as pd
from pandas.api.types import is_integer_dtype

from .io import sha256_file, write_parquet
from .paths import PROCESSED, stage_results

KEYS = ["conversation_id", "request_index"]
TAIL_COLUMNS = ["input_tail_p95", "input_tail_evt", "output_tail_p95", "output_tail_evt"]
OUTPUT_COLUMNS = ["output_percentile", "output_heavy_threshold", "output_heavy", "threshold_source", "congestion_state"]


def validate_keys(frame):
    if not set(KEYS).issubset(frame.columns) or frame.empty:
        raise ValueError("Dataset needs nonempty conversation_id/request_index keys")
    for key in KEYS:
        if not is_integer_dtype(frame[key].dtype) or frame[key].isna().any() or (frame[key] < 0).any():
            raise ValueError(f"{key} must contain nonnegative integer identifiers")
    if frame.duplicated(KEYS).any():
        raise ValueError("Duplicate conversation_id/request_index keys")


def validate_lengths(frame):
    validate_keys(frame)
    for column in ("input_tokens", "output_tokens", "total_tokens"):
        if column not in frame or not is_integer_dtype(frame[column].dtype) or frame[column].isna().any() or (frame[column] < 0).any():
            raise ValueError(f"{column} must contain nonnegative integer lengths")
    # Python 整数相加，避免 int64 溢出使非法 total 恰好通过检查。
    if any(int(total) != int(left) + int(right) for total, left, right in
           zip(frame.total_tokens, frame.input_tokens, frame.output_tokens)):
        raise ValueError("total_tokens must equal input_tokens + output_tokens")
    if "tokenization_status" in frame and not frame.tokenization_status.eq("ok").all():
        raise ValueError("Dataset contains unsuccessful tokenization rows")


def join_labels(base, labels, required_columns):
    validate_keys(labels)
    if set(labels.columns) != set(KEYS + required_columns):
        raise ValueError("Label artifact contains missing or unexpected columns")
    if len(labels) != len(base) or set(map(tuple, labels[KEYS].to_numpy())) != set(map(tuple, base[KEYS].to_numpy())):
        raise ValueError("Label keys do not exactly match the length dataset")
    if set(required_columns).intersection(base.columns):
        raise ValueError("Labels would overwrite existing dataset columns")
    # merge 保持 base 顺序，labels 即使换序也不会被位置拼接而错配。
    return base.merge(labels, on=KEYS, how="left", sort=False, validate="one_to_one")


def save_labels(path, base, labeled, columns):
    validate_lengths(base)
    pd.testing.assert_frame_equal(labeled[list(base.columns)], base)
    labels = labeled[KEYS + columns].copy()
    join_labels(base, labels, columns)
    write_parquet(path, labels)
    pd.testing.assert_frame_equal(pd.read_parquet(path), labels)
    return labels


def _verify_source(metadata_path, source_path, labels_path, digest_field):
    metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
    expected = metadata.get("source_sha256")
    if not expected or expected != sha256_file(source_path):
        raise ValueError("Stage labels were constructed against a different length dataset; rebuild that stage")
    expected_labels = metadata.get(digest_field)
    if not expected_labels or expected_labels != sha256_file(labels_path):
        raise ValueError("Label artifact checksum does not match metadata; rebuild that stage")


def load_dataset(stage="stage1", *, directory=PROCESSED, results_directory=None):
    """返回完整内存视图；磁盘只存一个基表和两份精简标签。

    results_directory 供独立测试/自定义实验指定根 results 目录。
    smoke 输出仅作为 cache 下的流程检查产物，不作为正式关联数据入口。
    """
    if stage not in {"stage1", "stage2", "stage2_1"}:
        raise ValueError(f"Unknown stage: {stage}")
    directory = Path(directory)
    source_path = directory / "request_lengths.parquet"
    frame = pd.read_parquet(source_path)
    validate_lengths(frame)
    if stage == "stage1":
        return frame
    results = Path(results_directory) if results_directory is not None else stage_results("stage2").parent
    tail = pd.read_parquet(directory / "tail_labels.parquet")
    _verify_source(results / "stage2/stage2_metadata.json", source_path, directory / "tail_labels.parquet", "output_sha256")
    columns = TAIL_COLUMNS + (["workload_quadrant"] if "workload_quadrant" in tail else [])
    frame = join_labels(frame, tail, columns)
    if stage == "stage2_1":
        _verify_source(results / "stage2_1/percentile_reference_metadata.json", source_path, directory / "output_labels.parquet", "labels_sha256")
        frame = join_labels(frame, pd.read_parquet(directory / "output_labels.parquet"), OUTPUT_COLUMNS)
    return frame
