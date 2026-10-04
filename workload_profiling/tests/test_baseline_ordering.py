"""Batch permutations affect real dispatch order while preserving capacity and auditability."""
import csv
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import unittest

from examples.min_tpm import MinTpmStrategy
from ..baseline import BaselineConfig, BaselineRunner, load_batch_order, load_config
from ..baseline.dashboard import DashboardService
from ..baseline.run import execute
from .test_baseline import config, endpoint, req, temporary_directory


class ReverseOrder:
    def order_batch(self, requests, endpoints, now_ms):
        return tuple(request.request_id for request in reversed(requests))


reverse_instance = ReverseOrder()


def make_reverse():
    return ReverseOrder()


class AsyncOrder:
    async def order_batch(self, requests, endpoints, now_ms):
        return [request.request_id for request in requests]


class BatchOrderingTests(unittest.TestCase):
    def test_old_config_defaults_to_fifo_and_keeps_original_dispatch(self):
        payload = config().to_dict()
        payload.pop("batch_order")
        settings = BaselineConfig.from_dict(payload)
        result = BaselineRunner(settings).run([req(0, 30), req(1, 10)])
        self.assertEqual(settings.batch_order, "fifo")
        self.assertEqual(result.batches[0]["request_ids"], ["0", "1"])
        self.assertEqual(result.batches[0]["dispatch_order"], ["0", "1"])
        self.assertEqual([r["batch_position"] for r in result.requests], [0, 1])

    def test_shortest_first_changes_dispatch_and_waiting_with_stable_ties(self):
        settings = config(batch_size=4, batch_wait_ms=100, batch_order="shortest_first",
                          endpoints=(endpoint(concurrency=1, latency=5),))
        result = BaselineRunner(settings).run([req(0, 40), req(1, 10), req(2, 20), req(3, 10)])
        dispatches = [e for e in result.events if e["event"] == "dispatched"]
        self.assertEqual([e["request_id"] for e in dispatches], ["1", "3", "2", "0"])
        self.assertEqual([e["time_ms"] for e in dispatches], [3, 9, 15, 21])
        # Result rows retain source order even when execution order changes.
        self.assertEqual([r["request_id"] for r in result.requests], ["0", "1", "2", "3"])
        self.assertEqual([r["batch_position"] for r in result.requests], [3, 0, 2, 1])
        self.assertEqual(result.requests[0]["capacity_wait_ms"], 18)
        self.assertEqual(result.summary["completed_requests"], 4)
        self.assertEqual(result.endpoints[0]["peak_concurrency"], 1)

    def test_longest_first_uses_total_tokens_and_preserves_ties(self):
        result = BaselineRunner(config(batch_size=4, batch_order="longest_first")).run(
            [req(0, 10, 30), req(1, 30, 10), req(2, 10, 50), req(3, 10, 1)])
        self.assertEqual(result.batches[0]["dispatch_order"], ["2", "0", "1", "3"])

    def test_order_and_min_rpm_route_compose_with_fresh_state(self):
        settings = config(batch_size=4, batch_order="shortest_first",
                          endpoints=(endpoint("a", rpm=100), endpoint("b", rpm=200)))
        result = BaselineRunner(settings).run([req(0, 40), req(1, 10), req(2, 20), req(3, 10)])
        dispatches = [e for e in result.events if e["event"] == "dispatched"]
        self.assertEqual([(e["request_id"], e["endpoint_id"]) for e in dispatches],
                         [("1", "a"), ("3", "b"), ("2", "b"), ("0", "a")])
        self.assertEqual([r["endpoint_id"] for r in result.requests], ["a", "a", "b", "b"])

    def test_both_injected_policies_compose_and_route_observes_updates(self):
        class LastRoute:
            def __init__(self):
                self.counts = []

            def select(self, request, endpoints, now_ms):
                self.counts.append(endpoints[-1].requests_in_window)
                return endpoints[-1].endpoint_id

        route = LastRoute()
        settings = config(batch_size=4, endpoints=(endpoint("a"), endpoint("b")))
        result = BaselineRunner(settings, batch_order=ReverseOrder(), strategy=route).run([req(i) for i in range(4)])
        self.assertEqual([e["request_id"] for e in result.events if e["event"] == "dispatched"], ["3", "2", "1", "0"])
        self.assertEqual(route.counts, [0, 1, 2, 3])
        self.assertEqual(result.endpoints[1]["total_requests"], 4)

    def test_later_batch_does_not_overtake_and_order_sees_busy_endpoints(self):
        class ObservingOrder:
            def __init__(self):
                self.snapshots = []

            def order_batch(self, requests, endpoints, now_ms):
                self.snapshots.append((now_ms, endpoints))
                return [r.request_id for r in sorted(requests, key=lambda r: r.total_tokens)]

        order = ObservingOrder()
        settings = config(batch_size=2, batch_wait_ms=100, endpoints=(endpoint(concurrency=1, latency=10),))
        result = BaselineRunner(settings, batch_order=order).run([req(0, 40), req(1, 30), req(2, 20), req(3, 10)])
        self.assertEqual([e["request_id"] for e in result.events if e["event"] == "dispatched"], ["1", "0", "3", "2"])
        self.assertEqual([t for t, _ in order.snapshots], [1, 3])
        self.assertEqual(order.snapshots[1][1][0].concurrency, 1)
        self.assertFalse(order.snapshots[1][1][0].can_accept(req("extra")))

    def test_every_release_reason_calls_order_and_empty_input_does_not(self):
        class RecordingOrder(ReverseOrder):
            def __init__(self):
                self.calls = []

            def order_batch(self, requests, endpoints, now_ms):
                self.calls.append((requests, now_ms))
                return super().order_batch(requests, endpoints, now_ms)

        cases = [(config(), "batch_size"),
                 (config(batch_size=10, batch_wait_ms=1), "timeout"),
                 (config(batch_size=10, batch_wait_ms=100), "end_of_input")]
        for settings, reason in cases:
            with self.subTest(reason=reason):
                order = RecordingOrder()
                result = BaselineRunner(settings, batch_order=order).run([req(0, 30), req(1, 10)])
                self.assertEqual(result.batches[0]["trigger"], reason)
                self.assertEqual(len(order.calls), len(result.batches))
                self.assertEqual(result.batches[0]["dispatch_order"], ["1", "0"])
        order = RecordingOrder()
        empty = BaselineRunner(config(), batch_order=order).run([])
        self.assertEqual(order.calls, [])
        self.assertEqual(empty.summary["batch_order"], "fifo")

    def test_invalid_permutations_fail_before_any_route_call(self):
        class InvalidOrder:
            def __init__(self, answer):
                self.answer = answer

            def order_batch(self, requests, endpoints, now_ms):
                return self.answer

        class NoRoute:
            def select(self, request, endpoints, now_ms):
                raise AssertionError("Routing must not start for an invalid batch order")

        answers = [["0"], ["0", "0"], ["0", "foreign"], ["0", "1", "extra"],
                   [0, 1], [["0"], ["1"]], None, "01", {"0", "1"}]
        for answer in answers:
            with self.subTest(answer=answer), self.assertRaises((ValueError, TypeError)):
                BaselineRunner(config(), batch_order=InvalidOrder(answer), strategy=NoRoute()).run([req(0), req(1)])

    def test_order_inputs_are_readonly_and_original_membership_is_retained(self):
        case = self

        class ReadonlyOrder(ReverseOrder):
            def order_batch(self, requests, endpoints, now_ms):
                case.assertIsInstance(requests, tuple)
                case.assertIsInstance(endpoints, tuple)
                with case.assertRaises(FrozenInstanceError):
                    requests[0].input_tokens = 0
                with case.assertRaises(FrozenInstanceError):
                    endpoints[0].concurrency = 0
                return super().order_batch(requests, endpoints, now_ms)

        result = BaselineRunner(config(), batch_order=ReadonlyOrder()).run([req(0), req(1)])
        self.assertEqual(result.batches[0]["request_ids"], ["0", "1"])
        self.assertEqual(result.batches[0]["dispatch_order"], ["1", "0"])
        released = next(e for e in result.events if e["event"] == "batch_released")
        self.assertEqual(released["dispatch_order"], ["1", "0"])

    def test_order_preserves_light_completion_and_oversize_rejection(self):
        result = BaselineRunner(config(batch_size=2, batch_order="longest_first",
                                      endpoints=(endpoint(tpm=20),))).run([req(0), req(1, 1, 1), req(2, 21)])
        self.assertEqual(result.batches[0]["dispatch_order"], ["2", "0"])
        self.assertEqual(result.requests[2]["status"], "rejected")
        self.assertEqual(result.requests[2]["batch_position"], 0)
        self.assertEqual(result.requests[0]["status"], "completed")
        self.assertIsNone(result.requests[1]["batch_position"])
        self.assertEqual(result.requests[1]["latency_ms"], 0)

    def test_reordered_requests_still_obey_rolling_rpm_and_tpm(self):
        for limit in ({"rpm": 1}, {"tpm": 20}):
            with self.subTest(limit=limit):
                result = BaselineRunner(config(batch_size=2, batch_order="shortest_first",
                                               endpoints=(endpoint(**limit),))).run([req(0, 15), req(1, 10)])
                self.assertEqual(result.requests[1]["dispatch_at_ms"], 1)
                self.assertEqual(result.requests[0]["dispatch_at_ms"], 60001)
                self.assertEqual(result.endpoints[0]["concurrency"], 0)

    def test_loader_supports_builtin_class_factory_and_instance(self):
        for name in ("fifo", "shortest_first", "longest_first",
                     __name__ + ":ReverseOrder", __name__ + ":make_reverse", __name__ + ":reverse_instance"):
            with self.subTest(name=name):
                self.assertTrue(callable(load_batch_order(name).order_batch))
        for name in ("unknown", "missing_module:Order", __name__ + ":not_here",
                     "examples.min_tpm:MinTpmStrategy", __name__ + ":AsyncOrder"):
            with self.subTest(name=name), self.assertRaises((ValueError, TypeError)):
                load_batch_order(name)
        with self.assertRaises(TypeError):
            BaselineRunner(config(), batch_order=AsyncOrder())
        for value in (None, "", " ", 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config(batch_order=value)

    def test_execute_exports_order_and_supports_two_policy_instances(self):
        source = Path(__file__).resolve().parents[2] / "examples/length_requests.jsonl"
        with temporary_directory() as directory:
            settings = replace(load_config(), batch_size=2, batch_wait_ms=3)
            result, summary = execute(settings, source=source, source_format="lengths", output=directory,
                                      batch_order=ReverseOrder(), strategy=MinTpmStrategy())
            self.assertEqual(summary["completed_requests"], 9)
            self.assertEqual(summary["light_requests"], 5)
            self.assertEqual(summary["batch_triggers"], {"batch_size": 1, "timeout": 1, "end_of_input": 1})
            self.assertTrue(summary["batch_order_class"].endswith(".ReverseOrder"))
            self.assertEqual(summary["strategy_class"], "examples.min_tpm.MinTpmStrategy")
            batches = json.loads((directory / "batches.json").read_text(encoding="utf-8"))
            self.assertEqual(batches[0]["request_ids"], ["input_heavy_1", "output_heavy_2"])
            self.assertEqual(batches[0]["dispatch_order"], ["output_heavy_2", "input_heavy_1"])
            with (directory / "requests.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["batch_position"], "")
            self.assertEqual(float(rows[1]["batch_position"]), 1)
            self.assertIn("batch_order_class", json.loads((directory / "summary.json").read_text(encoding="utf-8")))

    def test_dashboard_rejects_custom_order_before_importing_or_starting(self):
        service = DashboardService()
        payload = load_config().to_dict() | {"batch_order": __name__ + ":ReverseOrder"}
        with self.assertRaises(ValueError):
            service.start({"config": payload, "limit": 2})
        self.assertEqual(service.snapshot()["status"], "idle")


if __name__ == "__main__":
    unittest.main()
