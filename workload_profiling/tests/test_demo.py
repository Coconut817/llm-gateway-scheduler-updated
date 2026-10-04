"""Demo 复用逐条接口，人工阈值/模拟文本/CSV 导出都不改变正式数据。"""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import pandas as pd

from demo import DemoService, EXAMPLE_RESPONSE, export_csv
from ..common.paths import CACHE
from ..runtime import PercentileReference
from ..runtime.run_stream import threshold_argument
from .test_stream import FakeTokenizer


class DemoTests(unittest.TestCase):
    def setUp(self):
        config = {"default_threshold": 0.95, "reference_sample_count": 4,
                  "congestion_threshold_mapping": {"idle": 0.98, "normal": 0.95, "busy": 0.9, "critical": 0.8}}
        mocked = patch("workload_profiling.runtime.output_heavy_policy.load_config", side_effect=lambda *a, **k: deepcopy(config))
        mocked.start()
        self.addCleanup(mocked.stop)
        self.frame = pd.DataFrame({"conversation_id": [0, 1], "request_index": [0, 0],
            "output_percentile": [0.75, 1.0], "output_heavy": [False, True],
            "output_tail_evt": pd.Series([pd.NA, pd.NA], dtype="boolean")})
        self.service = DemoService(FakeTokenizer(), PercentileReference.from_lengths([1, 2, 2, 10]), self.frame)

    def test_prompt_only_leaves_output_unknown(self):
        data = self.service.profile({"prompt": "hello", "threshold": 0.9})
        self.assertEqual(data["result"]["input_tokens"], 10)
        self.assertIsNone(data["result"]["output_heavy"])
        self.assertEqual(data["active_threshold_source"], "MANUAL")
        self.assertFalse(data["model_called"])

    def test_threshold_changes_one_request_only(self):
        payload = {"prompt": "A", "response_mode": "typed", "response": "BB", "threshold": 0.7}
        manual = self.service.profile(payload)
        payload["threshold"] = None
        default = self.service.profile(payload)
        self.assertTrue(manual["result"]["output_heavy"])
        self.assertFalse(default["result"]["output_heavy"])
        self.assertEqual(default["active_threshold_source"], "DEFAULT")
        self.assertEqual(manual["result"]["output_percentile"], default["result"]["output_percentile"])

    def test_example_response_explicitly_not_model_generated(self):
        data = self.service.profile({"prompt": "A", "response_mode": "example"})
        self.assertEqual(data["example_response"], EXAMPLE_RESPONSE)
        self.assertEqual(data["result"]["output_tokens"], len(EXAMPLE_RESPONSE))
        self.assertFalse(data["model_called"])

    def test_preview_preserves_null_and_supports_heavy_filter(self):
        preview = self.service.preview(heavy_only=True)
        self.assertEqual(preview["count"], 1)
        self.assertIsNone(preview["records"][0]["output_tail_evt"])
        self.assertEqual(preview["records"][0]["conversation_id"], 1)
        self.assertEqual(self.service.preview(page=1, size=1)["records"][0]["conversation_id"], 1)

    def test_invalid_inputs_rejected(self):
        for payload in [{"prompt": ""}, {"prompt": "A", "threshold": 1},
                        {"prompt": "A", "response_mode": "unknown"}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.profile(payload)

    def test_csv_roundtrip_keeps_missing_booleans(self):
        directory = CACHE / ("demo_csv_test_" + uuid4().hex)
        directory.mkdir(parents=True)
        path = directory / "test.csv"
        try:
            export_csv(self.frame, path)
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            restored = pd.read_csv(path, float_precision="round_trip").astype(self.frame.dtypes.to_dict())
            pd.testing.assert_frame_equal(restored, self.frame)
        finally:
            path.unlink(missing_ok=True)
            directory.rmdir()

    def test_command_line_threshold_validation(self):
        import argparse
        self.assertEqual(threshold_argument("0.9"), 0.9)
        for value in ["0", "1", "nan", "abc"]:
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                threshold_argument(value)
