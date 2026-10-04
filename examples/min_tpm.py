"""Example replacement policy: choose minimum TPM utilization among candidates."""
from workload_profiling.baseline import EndpointView, WorkloadRequest


class MinTpmStrategy:
    def select(self, request: WorkloadRequest, endpoints: tuple[EndpointView, ...], now_ms: int) -> str:
        return min(endpoints, key=lambda endpoint: endpoint.tpm_utilization).endpoint_id
