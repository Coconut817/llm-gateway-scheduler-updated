"""Behavioral checks for batching, deadlines, routing and capacity recovery."""
from contextlib import contextmanager
from pathlib import Path
import json
import shutil
import unittest
from uuid import uuid4

from ..baseline import BaselineConfig, BaselineRunner, EndpointConfig, WorkloadRequest, load_config
from ..baseline.routing import load_strategy
from ..baseline.run import execute
from ..baseline.source import read_prompt_requests
from ..common.paths import CACHE
from .test_stream import FakeTokenizer


@contextmanager
def temporary_directory():
    directory = (CACHE / ("baseline_test_" + uuid4().hex)).resolve()
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        if directory == CACHE.resolve() or not directory.is_relative_to(CACHE.resolve()):
            raise ValueError("Temporary directory escaped the test cache")
        shutil.rmtree(directory)


def endpoint(name="a", rpm=1000, tpm=1000000, concurrency=100, latency=1):
    return EndpointConfig(name, rpm, tpm, concurrency, latency, 1000000, 1000000)


def config(**overrides):
    defaults = dict(endpoints=(endpoint(),), input_threshold_tokens=10,
                    output_threshold_tokens=10, batch_size=2, batch_wait_ms=5)
    return BaselineConfig(**(defaults | overrides))


def req(index, input_tokens=10, output_tokens=1):
    return WorkloadRequest(str(index), input_tokens, output_tokens)


class BaselineTests(unittest.TestCase):
    def test_input_or_output_heavy_and_light_immediate(self):
        result = BaselineRunner(config(batch_size=1)).run([req(0, 1, 1), req(1, 10, 1), req(2, 1, 10), req(3, 10, 10)])
        self.assertEqual([r["heavy"] for r in result.requests], [False, True, True, True])
        self.assertEqual(result.summary["both_heavy_requests"], 1)
        light = result.requests[0]
        self.assertEqual(light["finished_at_ms"], light["arrival_at_ms"])
        self.assertIsNone(light["endpoint_id"])
        self.assertEqual(result.endpoints[0]["total_requests"], 3)

    def test_batch_size_releases_and_reranks_each_assignment(self):
        settings = config(endpoints=(endpoint("a", rpm=100), endpoint("b", rpm=200)), batch_size=4)
        result = BaselineRunner(settings).run([req(i) for i in range(4)])
        self.assertEqual(result.batches[0]["trigger"], "batch_size")
        self.assertEqual(result.batches[0]["released_at_ms"], 3)
        self.assertEqual([r["endpoint_id"] for r in result.requests], ["a", "b", "b", "a"])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [3]*4)

    def test_timeout_fires_between_arrivals(self):
        result = BaselineRunner(config(arrival_interval_ms=10, batch_size=100, batch_wait_ms=3)).run([req(0), req(1)])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [3, 13])
        self.assertEqual([b["trigger"] for b in result.batches], ["timeout", "timeout"])

    def test_light_arrivals_do_not_reset_oldest_deadline(self):
        requests = [req(0)] + [req(i, 1, 1) for i in range(1, 8)]
        result = BaselineRunner(config(batch_size=100, batch_wait_ms=5)).run(requests)
        self.assertEqual(result.requests[0]["dispatch_at_ms"], 5)
        self.assertEqual(result.batches[0]["trigger"], "timeout")

    def test_final_partial_batch_is_drained(self):
        result = BaselineRunner(config(batch_size=100, batch_wait_ms=100)).run([req(0), req(1)])
        self.assertEqual(result.batches[0]["trigger"], "end_of_input")
        self.assertEqual(result.batches[0]["released_at_ms"], 2)
        self.assertEqual(result.summary["completed_requests"], 2)
        self.assertEqual(result.endpoints[0]["concurrency"], 0)

    def test_concurrency_completion_unblocks_queue(self):
        result = BaselineRunner(config(batch_size=1, endpoints=(endpoint(concurrency=1, latency=9),))).run([req(0), req(1)])
        first, second = result.requests
        self.assertEqual(first["finished_at_ms"], 10)
        self.assertEqual(second["dispatch_at_ms"], 10)
        self.assertEqual(second["capacity_wait_ms"], 9)
        self.assertEqual(result.endpoints[0]["peak_concurrency"], 1)

    def test_rpm_recovers_at_exact_window_boundary(self):
        result = BaselineRunner(config(batch_size=1, endpoints=(endpoint(rpm=1),))).run([req(0), req(1), req(2)])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [0, 60000, 120000])
        self.assertEqual(result.endpoints[0]["total_requests"], 3)
        self.assertEqual(result.endpoints[0]["peak_requests_in_window"], 1)

    def test_tpm_reservation_and_window_recovery(self):
        result = BaselineRunner(config(batch_size=1, endpoints=(endpoint(tpm=15),))).run([req(0), req(1)])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [0, 60000])
        self.assertEqual(result.endpoints[0]["total_tokens"], 22)
        self.assertEqual(result.endpoints[0]["peak_tokens_in_window"], 11)

    def test_oversized_request_rejected_without_blocking_next(self):
        result = BaselineRunner(config(batch_size=1, endpoints=(endpoint(tpm=20),))).run([req(0, 21), req(1)])
        self.assertEqual(result.requests[0]["status"], "rejected")
        self.assertEqual(result.requests[1]["status"], "completed")
        self.assertEqual(result.summary["rejected_requests"], 1)

    def test_strategy_can_be_replaced(self):
        class LastEndpoint:
            def select(self, request, endpoints, now_ms):
                return endpoints[-1].endpoint_id
        settings = config(batch_size=1, endpoints=(endpoint("a"), endpoint("b")))
        result = BaselineRunner(settings, strategy=LastEndpoint()).run([req(0), req(1)])
        self.assertEqual([r["endpoint_id"] for r in result.requests], ["b", "b"])
        self.assertTrue(hasattr(load_strategy("workload_profiling.baseline.routing:MinRpmStrategy"), "select"))

    def test_invalid_strategy_selection_fails(self):
        class Invalid:
            def select(self, request, endpoints, now_ms):
                return "missing"
        with self.assertRaises(ValueError):
            BaselineRunner(config(batch_size=1), strategy=Invalid()).run([req(0)])

    def test_zero_wait_single_batch_and_empty_input(self):
        result = BaselineRunner(config(batch_wait_ms=0, batch_size=100)).run([req(0)])
        self.assertEqual(result.requests[0]["dispatch_at_ms"], 0)
        empty = BaselineRunner(config()).run([])
        self.assertEqual(empty.summary["total_requests"], 0)

    def test_duplicate_ids_and_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            BaselineRunner(config()).run([req(0), req(0)])
        for kwargs in ({"batch_size": 0}, {"arrival_interval_ms": 0}, {"batch_wait_ms": -1},
                       {"output_threshold_tokens": float("nan")}, {"endpoints": ()}, {"window_ms": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                config(**kwargs)
        self.assertEqual(load_config().batch_size, 16)

    def test_original_rows_not_history_expanded(self):
        row = {"prompt": {"messages": [{"role": "user", "content": "a"},
               {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]}, "response": "d"}
        with temporary_directory() as temporary:
            path = Path(temporary)/"source.jsonl"
            path.write_text(json.dumps(row)+"\n", encoding="utf-8")
            requests = list(read_prompt_requests(path, FakeTokenizer()))
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].output_tokens, 1)

    def test_cli_workflow_persists_complete_results(self):
        with temporary_directory() as temporary:
            directory = Path(temporary)
            path = directory/"source.jsonl"
            path.write_text('\n'.join(json.dumps({"input_tokens": n, "output_tokens": 1}) for n in [1, 10, 20])+"\n", encoding="utf-8")
            result, summary = execute(config(), source=path, source_format="lengths", output=directory/"results")
            self.assertEqual(summary["completed_requests"], 3)
            for name in ("requests.csv", "events.jsonl", "endpoints.json", "batches.json", "summary.json", "report.md", "config.json"):
                self.assertTrue((directory/"results"/name).exists())
            events = [json.loads(line) for line in (directory/"results/events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(e["event"] == "arrived" for e in events), 3)
            self.assertEqual(len(result.requests), 3)


if __name__ == "__main__":
    unittest.main()
