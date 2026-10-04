"""合并按键而非行位置，缺行、重复键、非法长度不能静默通过。"""
import unittest
import pandas as pd

from ..common.datasets import join_labels, validate_lengths


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.base = pd.DataFrame({"conversation_id": [0, 0], "request_index": [0, 1],
                                  "input_tokens": [2, 4], "output_tokens": [1, 2], "total_tokens": [3, 6]})
        self.labels = pd.DataFrame({"conversation_id": [0, 0], "request_index": [1, 0],
                                    "tail": pd.Series([True, pd.NA], dtype="boolean")})

    def test_reordered_labels_join_by_key_and_preserve_null(self):
        result = join_labels(self.base, self.labels, ["tail"])
        self.assertTrue(pd.isna(result["tail"].iloc[0]))
        self.assertTrue(result["tail"].iloc[1])
        self.assertEqual(result.request_index.tolist(), [0, 1])

    def test_bad_keys_and_columns_rejected(self):
        cases = [self.labels.iloc[:1], pd.concat([self.labels.iloc[:1]] * 2),
                 self.labels.assign(request_index=[2, 0]), self.labels.assign(extra=0)]
        for labels in cases:
            with self.subTest(labels=labels.to_dict()), self.assertRaises(ValueError):
                join_labels(self.base, labels, ["tail"])

    def test_invalid_lengths_rejected(self):
        cases = [self.base.assign(total_tokens=[2, 6]), self.base.assign(output_tokens=[-1, 2]),
                 self.base.assign(input_tokens=[2.5, 4.0]), self.base.assign(tokenization_status="failed"),
                 self.base.assign(request_index=[0, 0])]
        for frame in cases:
            with self.subTest(frame=frame.to_dict()), self.assertRaises(ValueError):
                validate_lengths(frame)
