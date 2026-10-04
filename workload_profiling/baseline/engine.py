"""Virtual-clock baseline: arrivals, batch deadlines, dispatches and completions."""
from collections import Counter, deque
from dataclasses import asdict, dataclass
import heapq
import math

from .models import EndpointState, WorkloadRequest
from .routing import load_strategy


@dataclass
class RunResult:
    requests: list[dict]
    events: list[dict]
    batches: list[dict]
    endpoints: list[dict]
    summary: dict


class BaselineRunner:
    def __init__(self, config, *, strategy=None):
        self.config = config
        self.strategy = strategy if strategy is not None else load_strategy(config.strategy)
        if not callable(getattr(self.strategy, "select", None)):
            raise TypeError("strategy must implement select")
        self._used = False

    def run(self, requests):
        if self._used:
            raise RuntimeError("Create a new BaselineRunner for each replay")
        self._used = True
        config = self.config
        endpoints = [EndpointState(e) for e in config.endpoints]
        by_id = {e.config.endpoint_id: e for e in endpoints}
        source = iter(requests)
        rows, events, batches = [], [], []
        pending, ready, running = deque(), deque(), []
        seen = set()
        next_arrival, now, sequence = 0, 0, 0

        def event(kind, **fields):
            events.append({"time_ms": now, "event": kind, **fields})

        def release(reason):
            if not pending:
                return
            batch_id = len(batches)
            members = list(pending)
            pending.clear()
            batch = {"batch_id": batch_id, "trigger": reason, "released_at_ms": now,
                     "size": len(members), "request_ids": [r[0].request_id for r in members]}
            batches.append(batch)
            event("batch_released", **batch)
            for request, row in members:
                row.update(batch_id=batch_id, batch_trigger=reason, batch_released_at_ms=now,
                           batch_wait_ms=now - row["arrival_at_ms"])
                ready.append((request, row))

        while next_arrival is not None or pending or ready or running:
            times = []
            if next_arrival is not None:
                times.append(next_arrival)
            if pending:
                times.append(pending[0][1]["arrival_at_ms"] + config.batch_wait_ms)
            if running:
                times.append(running[0][0])
            if ready:
                for endpoint in endpoints:
                    expiry = endpoint.next_expiry(config.window_ms)
                    if expiry is not None:
                        times.append(expiry)
            if not times:
                raise RuntimeError("Queued requests have no future capacity event")
            now = min(times)
            for endpoint in endpoints:
                endpoint.expire(now, config.window_ms)
            # Deterministic same-time order: completion, arrival, timeout, dispatch.
            while running and running[0][0] <= now:
                _, _, endpoint_id, row = heapq.heappop(running)
                by_id[endpoint_id].complete()
                row["status"] = "completed"
                event("completed", request_id=row["request_id"], endpoint_id=endpoint_id,
                      concurrency_after=by_id[endpoint_id].concurrency)
            if next_arrival == now:
                try:
                    request = next(source)
                except StopIteration:
                    next_arrival = None
                    release("end_of_input")
                else:
                    if not isinstance(request, WorkloadRequest):
                        raise TypeError("Replay expects WorkloadRequest objects")
                    if request.request_id in seen:
                        raise ValueError(f"Duplicate request_id: {request.request_id}")
                    seen.add(request.request_id)
                    input_heavy = request.input_tokens >= config.input_threshold_tokens
                    output_heavy = request.output_tokens >= config.output_threshold_tokens
                    heavy = input_heavy or output_heavy
                    row = {**asdict(request), "total_tokens": request.total_tokens,
                           "arrival_at_ms": now, "input_heavy": input_heavy,
                           "output_heavy": output_heavy, "heavy": heavy,
                           "batch_id": None, "batch_trigger": None, "batch_released_at_ms": None,
                           "batch_wait_ms": 0, "capacity_wait_ms": 0, "queue_wait_ms": 0,
                           "endpoint_id": None, "dispatch_at_ms": None, "finished_at_ms": None,
                           "service_ms": 0, "latency_ms": 0, "status": "queued", "rejection_reason": None,
                           "endpoint_rpm_before": None, "endpoint_tpm_before": None,
                           "endpoint_concurrency_before": None, "rpm_utilization_before": None,
                           "tpm_utilization_before": None, "concurrency_utilization_before": None}
                    rows.append(row)
                    event("arrived", request_id=request.request_id, heavy=heavy)
                    if heavy:
                        pending.append((request, row))
                        if len(pending) >= config.batch_size:
                            release("batch_size")
                    else:
                        row.update(status="completed", finished_at_ms=now)
                        event("light_completed", request_id=request.request_id)
                    next_arrival += config.arrival_interval_ms
            if pending and pending[0][1]["arrival_at_ms"] + config.batch_wait_ms <= now:
                release("timeout")
            while ready:
                request, row = ready[0]
                # FIFO capacity waiting; permanently oversized requests cannot deadlock.
                if all(request.total_tokens > e.config.tpm_limit for e in endpoints):
                    ready.popleft()
                    row.update(status="rejected", rejection_reason="tokens_exceed_every_endpoint_tpm_limit",
                               finished_at_ms=now, capacity_wait_ms=now-row["batch_released_at_ms"],
                               queue_wait_ms=now-row["arrival_at_ms"], latency_ms=now-row["arrival_at_ms"])
                    event("rejected", request_id=request.request_id, reason=row["rejection_reason"])
                    continue
                views = tuple(e.view(now, config.window_ms) for e in endpoints)
                candidates = tuple(v for v in views if v.can_accept(request))
                if not candidates:
                    event("capacity_wait", request_id=request.request_id)
                    break
                selected = self.strategy.select(request, candidates, now)
                if not isinstance(selected, str) or selected not in {v.endpoint_id for v in candidates}:
                    raise ValueError("Routing strategy selected an unavailable or unknown endpoint")
                view = next(v for v in candidates if v.endpoint_id == selected)
                endpoint = by_id[selected]
                finished = endpoint.dispatch(request, now, config.window_ms)
                ready.popleft()
                row.update(endpoint_id=selected, dispatch_at_ms=now, finished_at_ms=finished,
                           service_ms=finished-now, queue_wait_ms=now-row["arrival_at_ms"],
                           capacity_wait_ms=now-row["batch_released_at_ms"], latency_ms=finished-row["arrival_at_ms"],
                           status="running", endpoint_rpm_before=view.requests_in_window,
                           endpoint_tpm_before=view.tokens_in_window, endpoint_concurrency_before=view.concurrency,
                           rpm_utilization_before=view.rpm_utilization, tpm_utilization_before=view.tpm_utilization,
                           concurrency_utilization_before=view.concurrency_utilization)
                sequence += 1
                heapq.heappush(running, (finished, sequence, selected, row))
                event("dispatched", request_id=request.request_id, endpoint_id=selected,
                      batch_id=row["batch_id"], finished_at_ms=finished,
                      endpoint_state_after=asdict(endpoint.view(now, config.window_ms)))

        states = []
        for endpoint in endpoints:
            view = endpoint.view(now, config.window_ms)
            states.append({**asdict(view), "rpm_utilization": view.rpm_utilization,
                           "tpm_utilization": view.tpm_utilization,
                           "concurrency_utilization": view.concurrency_utilization,
                           "total_requests": endpoint.total_requests, "total_tokens": endpoint.total_tokens,
                           "peak_concurrency": endpoint.peak_concurrency,
                           "peak_requests_in_window": endpoint.peak_requests_in_window,
                           "peak_tokens_in_window": endpoint.peak_tokens_in_window})
        heavy_rows = [r for r in rows if r["heavy"]]
        completed_heavy = [r for r in heavy_rows if r["status"] == "completed"]

        def statistics(values):
            values = sorted(values)
            if not values:
                return {"mean": 0, "p95": 0, "max": 0}
            return {"mean": sum(values)/len(values), "p95": values[max(0, math.ceil(.95*len(values))-1)], "max": values[-1]}

        summary = {"clock": "virtual_ms", "output_length_mode": "oracle_recorded_response",
                   "strategy": config.strategy, "strategy_class": type(self.strategy).__module__ + "." + type(self.strategy).__qualname__,
                   "total_requests": len(rows), "light_requests": len(rows)-len(heavy_rows),
                   "heavy_requests": len(heavy_rows), "input_heavy_requests": sum(r["input_heavy"] for r in rows),
                   "output_heavy_requests": sum(r["output_heavy"] for r in rows),
                   "both_heavy_requests": sum(r["input_heavy"] and r["output_heavy"] for r in rows),
                   "completed_requests": sum(r["status"] == "completed" for r in rows),
                   "rejected_requests": sum(r["status"] == "rejected" for r in rows),
                   "batch_count": len(batches), "batch_triggers": dict(Counter(b["trigger"] for b in batches)),
                   "last_arrival_ms": rows[-1]["arrival_at_ms"] if rows else None,
                   "simulation_end_ms": now, "heavy_queue_wait_ms": statistics([r["queue_wait_ms"] for r in completed_heavy]),
                   "heavy_latency_ms": statistics([r["latency_ms"] for r in completed_heavy]),
                   "endpoint_dispatch_counts": {e["endpoint_id"]: e["total_requests"] for e in states}}
        return RunResult(rows, events, batches, states, summary)
