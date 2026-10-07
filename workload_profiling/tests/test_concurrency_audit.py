import unittest

from examples.concurrency_audit import audit_concurrency_waits
from ..baseline import BaselineRunner
from .test_baseline import config, endpoint, req


class ConcurrencyAuditTests(unittest.TestCase):
    def test_reconstruction_proves_concurrency_pressure(self):
        settings = config(batch_scope="all", arrival_mode="burst", burst_size=4, burst_span_ms=0,
                          batch_size=2, endpoints=(endpoint(concurrency=1, latency=9),))
        result = BaselineRunner(settings).run([req(i, 1, 1) for i in range(8)])
        audit, snapshots = audit_concurrency_waits(result, settings)
        self.assertTrue(audit["all_wait_events_due_to_full_concurrency"])
        self.assertGreater(audit["saturated_with_ready_queue_ms"], 0)
        self.assertTrue(all(s["total_concurrency"] == 1 for s in snapshots))
        self.assertEqual(audit["quota_blocked_wait_events"], 0)

    def test_quota_wait_is_not_misreported_as_concurrency(self):
        settings = config(batch_scope="all", batch_size=1, endpoints=(endpoint(rpm=1),))
        result = BaselineRunner(settings).run([req(0, 1, 1), req(1, 1, 1)])
        with self.assertRaisesRegex(AssertionError, "solely"):
            audit_concurrency_waits(result, settings)

    def test_no_wait_and_empty_workload(self):
        settings = config(batch_scope="all", batch_size=1)
        for requests in ([req(0, 1, 1)], []):
            audit, snapshots = audit_concurrency_waits(BaselineRunner(settings).run(requests), settings)
            self.assertFalse(audit["all_wait_events_due_to_full_concurrency"])
            self.assertEqual(snapshots, [])
