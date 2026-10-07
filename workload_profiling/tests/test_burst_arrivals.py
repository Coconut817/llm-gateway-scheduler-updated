from dataclasses import replace
import unittest

from ..baseline.arrivals import burst_arrival_times
from ..baseline import BaselineRunner, load_config
from .test_baseline import config, endpoint, req


class BurstArrivalTests(unittest.TestCase):
    def test_count_order_span_and_repeatability(self):
        settings = replace(load_config(), arrival_mode="burst")
        for count in (0, 1, 2, 511, 512, 513, 3168):
            times = burst_arrival_times(count, settings)
            self.assertEqual(len(times), count)
            self.assertEqual(times, tuple(sorted(times)))
            self.assertEqual(times, burst_arrival_times(count, settings))
            if times:
                self.assertEqual(times[0], 0)
                self.assertEqual(times[-1], (count - 1) * settings.arrival_interval_ms)
        times = burst_arrival_times(3168, settings)
        self.assertGreater(max(b - a for a, b in zip(times, times[1:])), 100)
        self.assertLess(times[511] - times[0], 30)

    def test_same_time_arrivals_batch_and_capacity(self):
        settings = config(arrival_mode="burst", burst_size=4, burst_span_ms=0,
                          batch_size=2, endpoints=(endpoint(concurrency=1, latency=9),))
        result = BaselineRunner(settings).run([req(i) for i in range(8)])
        self.assertEqual([r["arrival_at_ms"] for r in result.requests], [0]*4 + [7]*4)
        self.assertEqual(result.summary["completed_requests"], 8)
        self.assertGreater(result.requests[-1]["capacity_wait_ms"], 0)
        self.assertEqual(result.endpoints[0]["peak_concurrency"], 1)
        self.assertTrue(all(r["queue_wait_ms"] == r["batch_wait_ms"] + r["capacity_wait_ms"]
                            for r in result.requests))

    def test_timeout_during_quiet_gap_and_eof(self):
        settings = config(arrival_mode="burst", burst_size=2, burst_span_ms=0,
                          arrival_interval_ms=10, batch_size=100, batch_wait_ms=3)
        result = BaselineRunner(settings).run([req(i) for i in range(4)])
        self.assertEqual([r["arrival_at_ms"] for r in result.requests], [0, 0, 30, 30])
        self.assertEqual([b["released_at_ms"] for b in result.batches], [3, 33])
        self.assertEqual(BaselineRunner(settings).run([]).summary["total_requests"], 0)
        eof = BaselineRunner(replace(settings, batch_wait_ms=100)).run([req(0)])
        self.assertEqual(eof.batches[0]["trigger"], "end_of_input")
        self.assertEqual(eof.batches[0]["released_at_ms"], 10)

    def test_invalid_settings(self):
        for changes in ({"arrival_mode": "unknown"}, {"burst_size": 1},
                        {"burst_span_ms": -1}, {"burst_span_ms": True},
                        {"arrival_mode": "burst", "burst_size": 4, "burst_span_ms": 3}):
            with self.assertRaises(ValueError):
                replace(load_config(), **changes)
