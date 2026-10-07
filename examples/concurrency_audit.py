"""Reconstruct endpoint/ready states from events to identify actual wait causes."""
from collections import deque

from workload_profiling.baseline.models import EndpointState, WorkloadRequest


def audit_concurrency_waits(result, config):
    endpoints = {e.endpoint_id: EndpointState(e) for e in config.endpoints}
    rows = {r["request_id"]: r for r in result.requests}
    ready = deque()
    waiting_snapshots = []
    last_time, saturated_wait_ms, peak_ready = 0, 0, 0
    slots = sum(e.concurrency_limit for e in config.endpoints)
    for event in result.events:
        now = event["time_ms"]
        if now < last_time:
            raise AssertionError("Events are not chronological")
        if ready and all(e.concurrency == e.config.concurrency_limit for e in endpoints.values()):
            saturated_wait_ms += now - last_time
        last_time = now
        for endpoint in endpoints.values():
            endpoint.expire(now, config.window_ms)
        kind = event["event"]
        if kind == "batch_released":
            ready.extend(event["dispatch_order"])
            peak_ready = max(peak_ready, len(ready))
        elif kind == "dispatched":
            if not ready or ready.popleft() != event["request_id"]:
                raise AssertionError("Dispatch violates ready queue")
            row = rows[event["request_id"]]
            request = WorkloadRequest(row["request_id"], row["input_tokens"], row["output_tokens"])
            finished = endpoints[event["endpoint_id"]].dispatch(request, now, config.window_ms)
            if finished != event["finished_at_ms"]:
                raise AssertionError("Service completion mismatch")
        elif kind == "completed":
            endpoints[event["endpoint_id"]].complete()
        elif kind == "rejected":
            raise AssertionError("Concurrency experiment unexpectedly rejected a request")
        elif kind == "capacity_wait":
            if not ready or ready[0] != event["request_id"]:
                raise AssertionError("Wait event does not refer to queue head")
            row = rows[event["request_id"]]
            request = WorkloadRequest(row["request_id"], row["input_tokens"], row["output_tokens"])
            views = [e.view(now, config.window_ms) for e in endpoints.values()]
            quota_ok = all(v.requests_in_window < v.rpm_limit and
                           v.tokens_in_window + request.total_tokens <= v.tpm_limit for v in views)
            all_full = all(v.concurrency == v.concurrency_limit for v in views)
            if not quota_ok or not all_full:
                raise AssertionError("Waiting is not caused solely by full concurrency")
            waiting_snapshots.append({"time_ms": now, "head_request_id": request.request_id,
                                      "ready_queue_length": len(ready), "total_concurrency": sum(v.concurrency for v in views),
                                      "total_concurrency_limit": slots,
                                      "all_rpm_tpm_sufficient_for_head": quota_ok,
                                      **{f"{v.endpoint_id}_concurrency": v.concurrency for v in views}})
    if ready or any(e.concurrency for e in endpoints.values()):
        raise AssertionError("Replay did not drain")
    return {"all_wait_events_due_to_full_concurrency": bool(waiting_snapshots),
            "quota_blocked_wait_events": 0, "concurrency_wait_events": len(waiting_snapshots),
            "saturated_with_ready_queue_ms": saturated_wait_ms, "ready_queue_peak": peak_ready,
            "total_slots": slots}, waiting_snapshots
