"""真实缓存 tokenizer 与现有科学产物的集成验收；不下载、不发网络请求。"""
import json
import unittest

import pandas as pd

from ..common.conversation import reconstruct_request
from ..common.datasets import load_dataset
from ..common.paths import DEFAULT_SOURCE, TOKENIZER_DIR, PROCESSED, ARTIFACTS, stage_results, CACHE
from ..common.tokenizer import load_tokenizer
from ..runtime import PercentileReference, OutputHeavyPolicy, RequestProcessor
from ..stages.stage1.inspect_data import read_rows


@unittest.skipUnless((TOKENIZER_DIR / "stage1_tokenizer_lock.json").exists(), "Requires existing fixed tokenizer cache")
class RealTokenizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer, _ = load_tokenizer()

    def test_single_request_interface_matches_stage1_real_smoke(self):
        path = CACHE / "smoke/stage1/data/request_lengths.parquet"
        if not path.exists():
            self.skipTest("Run Stage 1 --smoke first")
        expected = pd.read_parquet(path)
        selected = set(expected.conversation_id)
        source = {index: row for index, row, error in read_rows(DEFAULT_SOURCE) if index in selected and not error}
        processor = RequestProcessor(self.tokenizer)
        for row in expected.to_dict("records"):
            request = reconstruct_request(source[row["conversation_id"]], row["conversation_id"], row["request_index"])
            prompt = {"messages": request["context"], "tools": request["tools"], **request["prompt_controls"]}
            result = processor.process({"prompt": prompt, "response": request["response_text"]})
            for field in ["input_tokens", "output_tokens", "total_tokens", "message_count",
                          "user_message_count", "assistant_message_count", "system_message_count", "tool_message_count"]:
                self.assertEqual(result[field], row[field], (row["conversation_id"], row["request_index"], field))


@unittest.skipUnless((PROCESSED / "output_labels.parquet").exists(), "Requires completed Stage 2.1")
class ArtifactTests(unittest.TestCase):
    def test_saved_labels_have_same_keys_and_default_definition(self):
        frame = load_dataset("stage2_1")
        reference = PercentileReference.load(ARTIFACTS / "output_percentile_reference.parquet",
                                             stage_results("stage2_1") / "percentile_reference_metadata.json")
        policy = OutputHeavyPolicy(reference, config_path=stage_results("stage2_1") / "runtime_policy_config.json")
        self.assertEqual(reference.sample_count, len(frame))
        for tokens, group in frame.groupby("output_tokens"):
            decision = policy.evaluate(int(tokens))
            self.assertTrue(group.output_percentile.eq(decision["output_percentile"]).all())
            self.assertTrue(group.output_heavy.eq(decision["output_heavy"]).all())
        self.assertTrue(frame.congestion_state.isna().all())
        self.assertTrue(frame.output_tail_evt.isna().all())
        # 上次迁移已经比较了旧完整视图；这里验证基表仍是那次的同一批长度。
        from ..common.io import sha256_file
        manifest = json.loads((stage_results("stage1").parent / "refactor_validation.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["base_length_dataset_sha256"], sha256_file(PROCESSED / "request_lengths.parquet"))
