"""Handoff baseline: ascending known output tokens, preserving arrival ties."""


class OutputShortestFirstOrder:
    def order_batch(self, requests, endpoints, now_ms):
        return [request.request_id for request in sorted(requests, key=lambda r: r.output_tokens)]
