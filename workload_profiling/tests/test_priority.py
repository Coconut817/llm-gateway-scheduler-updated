from dataclasses import replace
import json
import unittest

from ..baseline import BaselineRunner, WorkloadRequest
from ..baseline.ordering import EffectivePriorityOrderStrategy
from ..baseline.priority import assign_priority, effective_priority
from ..baseline.source import read_length_requests, read_prompt_requests
from .test_baseline import config, req, endpoint, temporary_directory
from .test_stream import FakeTokenizer


class PriorityTests(unittest.TestCase):
    def test_seeded_labels_repeat_without_length_or_order_dependency(self):
        settings = config(priority_assignment="synthetic")
        requests = [req(i) for i in range(200)]
        labels = {r.request_id: assign_priority(r, settings).base_priority for r in requests}
        self.assertEqual(labels, {r.request_id: assign_priority(replace(r, output_tokens=1000), settings).base_priority
                                  for r in reversed(requests)})
        self.assertEqual(set(labels.values()), {1, 3, 10})
        different = {r.request_id: assign_priority(r, replace(settings, priority_seed=1)).base_priority for r in requests}
        self.assertNotEqual(labels, different)
        explicit = WorkloadRequest("explicit", 10, 1, base_priority=7, priority_class="custom")
        self.assertEqual(assign_priority(explicit, settings), explicit)

    def test_effective_priority_discount_and_stable_ties(self):
        settings = config(heavy_priority_discount=.5)
        requests = (
            WorkloadRequest("high_heavy", 10, 50, base_priority=10, priority_class="high"),
            WorkloadRequest("normal_light", 1, 2, base_priority=3),
            WorkloadRequest("normal_heavy", 10, 2, base_priority=3),
            WorkloadRequest("tie_first", 1, 1, base_priority=3),
            WorkloadRequest("tie_second", 1, 1, base_priority=3),
        )
        self.assertEqual(EffectivePriorityOrderStrategy(settings).order_batch(requests, (), 0),
                         ["high_heavy", "tie_first", "tie_second", "normal_light", "normal_heavy"])
        self.assertEqual(effective_priority(requests[0], settings), 5)
        self.assertEqual(effective_priority(requests[1], settings), 3)

    def test_logged_scores_match_actual_order_and_capacity(self):
        settings = config(batch_scope="all", batch_size=3, batch_order="effective_priority",
                          heavy_priority_discount=.5, endpoints=(endpoint(concurrency=1),))
        requests = [WorkloadRequest("heavy", 10, 1, base_priority=3),
                    WorkloadRequest("light", 1, 2, base_priority=3),
                    WorkloadRequest("urgent", 10, 30, base_priority=10, priority_class="high")]
        result = BaselineRunner(settings).run(requests)
        self.assertEqual(result.batches[0]["dispatch_order"], ["urgent", "light", "heavy"])
        self.assertEqual([e["request_id"] for e in result.events if e["event"] == "dispatched"], ["urgent", "light", "heavy"])
        self.assertEqual([r["effective_priority"] for r in result.requests], [1.5, 3, 5])
        self.assertEqual(result.summary["endpoint_executed_requests"], 3)
        self.assertEqual(result.endpoints[0]["peak_concurrency"], 1)
        self.assertEqual(result.summary["priority_groups"]["high"]["requests"], 1)

    def test_later_batch_does_not_overtake_prior_batch(self):
        settings = config(batch_scope="all", batch_size=1, batch_order="effective_priority",
                          endpoints=(endpoint(concurrency=1, latency=9),))
        result = BaselineRunner(settings).run([WorkloadRequest("low0", 1, 1), WorkloadRequest("low1", 1, 1),
                                              WorkloadRequest("high", 1, 1, base_priority=10, priority_class="high")])
        self.assertEqual([e["request_id"] for e in result.events if e["event"] == "dispatched"], ["low0", "low1", "high"])

    def test_readers_preserve_explicit_priority(self):
        with temporary_directory() as directory:
            path = directory / "lengths.jsonl"
            path.write_text(json.dumps({"input_tokens": 1, "output_tokens": 1, "base_priority": 10}), encoding="utf-8")
            request = next(read_length_requests(path))
            self.assertEqual((request.base_priority, request.priority_class, request.priority_source), (10, "high", "recorded"))
            prompt = directory / "prompt.jsonl"
            prompt.write_text(json.dumps({"prompt": {"messages": [{"role": "user", "content": "hello"}]},
                                          "response": "hi", "base_priority": 7}), encoding="utf-8")
            request = next(read_prompt_requests(prompt, FakeTokenizer()))
            self.assertEqual((request.base_priority, request.priority_class), (7, "custom"))

    def test_disabled_discount_and_invalid_inputs(self):
        self.assertEqual(effective_priority(WorkloadRequest("a", 10, 1, base_priority=3), config()), 3)
        for changes in ({"heavy_priority_discount": 0}, {"heavy_priority_discount": 1.1},
                        {"heavy_priority_discount": float("nan")}, {"priority_seed": -1},
                        {"priority_assignment": "wrong"}):
            with self.assertRaises(ValueError):
                config(**changes)
        for value in (0, -1, True, float("inf")):
            with self.assertRaises(ValueError):
                WorkloadRequest("a", 1, 1, base_priority=value)

    def test_weight_change_preserves_class_and_changes_only_score(self):
        original = config(priority_assignment="synthetic")
        new = replace(original, priority_high_weight=3, priority_normal_weight=2)
        for i in range(200):
            before, after = assign_priority(req(i), original), assign_priority(req(i), new)
            self.assertEqual(before.priority_class, after.priority_class)
            self.assertEqual(after.base_priority, {"high": 3, "normal": 2, "low": 1}[after.priority_class])
        for changes in ({"priority_high_weight": 2}, {"priority_low_weight": 0},
                        {"priority_normal_weight": float("nan")}, {"priority_high_weight": True}):
            with self.assertRaises(ValueError):
                replace(original, **changes)

    def test_321_discount_can_cross_business_classes(self):
        settings = config(heavy_priority_discount=.5, priority_high_weight=3, priority_normal_weight=2)
        requests = (WorkloadRequest("high_heavy", 10, 8, base_priority=3, priority_class="high"),
                    WorkloadRequest("normal_light", 1, 2, base_priority=2),
                    WorkloadRequest("normal_heavy", 10, 4, base_priority=2),
                    WorkloadRequest("low_light", 1, 1, base_priority=1, priority_class="low"))
        self.assertEqual(EffectivePriorityOrderStrategy(settings).order_batch(requests, (), 0),
                         ["normal_light", "high_heavy", "low_light", "normal_heavy"])
