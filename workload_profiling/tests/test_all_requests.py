from dataclasses import replace
import unittest

from ..baseline import BaselineConfig, BaselineRunner
from .test_baseline import config, endpoint, req


class AllRequestTests(unittest.TestCase):
    def test_old_config_defaults_to_legacy(self):
        payload = config().to_dict()
        payload.pop("batch_scope")
        settings = BaselineConfig.from_dict(payload)
        result = BaselineRunner(settings).run([req(0, 1, 1)])
        self.assertEqual(settings.batch_scope, "heavy_only")
        self.assertEqual(result.summary["endpoint_executed_requests"], 0)
        self.assertIsNone(result.requests[0]["endpoint_id"])

    def test_mixed_batch_includes_light_and_charges_tokens(self):
        settings = config(batch_scope="all", batch_size=2)
        result = BaselineRunner(settings).run([req(0, 1, 1), req(1, 10, 1)])
        self.assertEqual(result.batches[0]["request_ids"], ["0", "1"])
        self.assertEqual(result.batches[0]["trigger"], "batch_size")
        self.assertEqual(result.summary["endpoint_executed_requests"], 2)
        self.assertEqual(result.summary["endpoint_executed_light_requests"], 1)
        self.assertEqual(result.endpoints[0]["total_tokens"], 13)
        self.assertTrue(all(r["service_ms"] >= 1 and r["endpoint_id"] is not None for r in result.requests))
        self.assertFalse(any(e["event"] == "light_completed" for e in result.events))
        self.assertFalse(result.requests[0]["heavy"])

    def test_light_requests_wait_for_concurrency_and_rpm(self):
        for limits, expected_dispatch in (({"concurrency": 1, "latency": 9}, 10), ({"rpm": 1}, 60000)):
            with self.subTest(limits=limits):
                result = BaselineRunner(config(batch_scope="all", batch_size=1,
                                               endpoints=(endpoint(**limits),))).run([req(0, 1, 1), req(1, 1, 1)])
                self.assertEqual(result.requests[1]["dispatch_at_ms"], expected_dispatch)
                self.assertEqual(result.summary["capacity_wait_requests"], 1)
                self.assertEqual(result.summary["completed_requests"], 2)
                for row in result.requests:
                    self.assertEqual(row["queue_wait_ms"], row["batch_wait_ms"] + row["capacity_wait_ms"])
                    self.assertEqual(row["latency_ms"], row["queue_wait_ms"] + row["service_ms"])

    def test_light_requests_trigger_timeout_and_eof(self):
        settings = config(batch_scope="all", batch_size=100, arrival_interval_ms=10, batch_wait_ms=3)
        result = BaselineRunner(settings).run([req(0, 1, 1), req(1, 1, 1)])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [3, 13])
        partial = BaselineRunner(replace(settings, batch_wait_ms=100)).run([req(0, 1, 1)])
        self.assertEqual(partial.batches[0]["trigger"], "end_of_input")
        self.assertEqual(partial.requests[0]["dispatch_at_ms"], 10)
        self.assertEqual(BaselineRunner(settings).run([]).summary["all_latency_ms"]["mean"], 0)

    def test_all_mode_combines_with_burst_jitter_and_order(self):
        from examples.output_shortest_first import OutputShortestFirstOrder
        settings = config(batch_scope="all", arrival_mode="burst", burst_size=4, burst_span_ms=0,
                          batch_size=4, endpoints=(replace(endpoint(concurrency=1, latency=9), service_jitter_fraction=.2),))
        result = BaselineRunner(settings, batch_order=OutputShortestFirstOrder()).run(
            [req(0, 1, 4), req(1, 1, 1), req(2, 1, 3), req(3, 1, 2), req(4, 1, 1)])
        self.assertEqual(result.batches[0]["dispatch_order"], ["1", "3", "2", "0"])
        self.assertEqual(result.summary["endpoint_executed_light_requests"], 5)
        self.assertEqual(result.endpoints[0]["peak_concurrency"], 1)

    def test_invalid_scope(self):
        with self.assertRaises(ValueError):
            config(batch_scope="invalid")
