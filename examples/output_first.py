"""Example batch order: descending recorded output length, with stable ties."""
from workload_profiling.baseline import EndpointView, WorkloadRequest


class OutputLongestFirstOrder:
    def order_batch(self, requests: tuple[WorkloadRequest, ...],
                    endpoints: tuple[EndpointView, ...], now_ms: int) -> list[str]:
        return [request.request_id for request in sorted(requests, key=lambda r: r.output_tokens, reverse=True)]
