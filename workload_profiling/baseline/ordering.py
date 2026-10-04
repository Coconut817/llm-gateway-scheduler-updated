"""Replaceable ordering of a released batch, independent of endpoint routing."""
from collections.abc import Sequence
import importlib
import inspect
from typing import Protocol

from .models import EndpointView, WorkloadRequest


class BatchOrderStrategy(Protocol):
    def order_batch(self, requests: tuple[WorkloadRequest, ...],
                    endpoints: tuple[EndpointView, ...], now_ms: int) -> Sequence[str]:
        """Return every batch request_id exactly once, in the desired order.

        Requests follow arrival order. Endpoints include ALL endpoint snapshots,
        including busy endpoints. Both tuples and their members are immutable.
        """
        ...


class FifoOrderStrategy:
    def order_batch(self, requests, endpoints, now_ms):
        return [request.request_id for request in requests]


class ShortestFirstOrderStrategy:
    def order_batch(self, requests, endpoints, now_ms):
        # Stable ties retain arrival order. Length is an offline work proxy.
        return [request.request_id for request in sorted(requests, key=lambda r: r.total_tokens)]


class LongestFirstOrderStrategy:
    def order_batch(self, requests, endpoints, now_ms):
        return [request.request_id for request in sorted(requests, key=lambda r: r.total_tokens, reverse=True)]


BUILTIN_BATCH_ORDERS = {
    "fifo": FifoOrderStrategy,
    "shortest_first": ShortestFirstOrderStrategy,
    "longest_first": LongestFirstOrderStrategy,
}


def load_batch_order(spec):
    if spec in BUILTIN_BATCH_ORDERS:
        return BUILTIN_BATCH_ORDERS[spec]()
    module, separator, attribute = spec.partition(":")
    if not separator or not module or not attribute:
        raise ValueError("batch_order must be fifo, shortest_first, longest_first or module:attribute")
    try:
        order = getattr(importlib.import_module(module), attribute)
    except (ImportError, AttributeError) as error:
        raise ValueError(f"Cannot load batch order {spec}: {type(error).__name__}") from None
    if inspect.isclass(order) or not hasattr(order, "order_batch"):
        order = order()
    if not callable(getattr(order, "order_batch", None)):
        raise TypeError("Batch order must implement order_batch(requests, endpoints, now_ms)")
    if inspect.iscoroutinefunction(order.order_batch):
        raise TypeError("Batch order must be synchronous")
    return order


def validate_batch_order(requests, ordered_ids):
    """Reject omissions, duplicates and foreign IDs before enqueuing the batch."""
    if not isinstance(ordered_ids, Sequence) or isinstance(ordered_ids, (str, bytes)):
        raise TypeError("order_batch must return a sequence of request_id strings (for example list or tuple)")
    ordered_ids = tuple(ordered_ids)
    if (len(ordered_ids) != len(requests)
            or any(not isinstance(request_id, str) for request_id in ordered_ids)
            or len(set(ordered_ids)) != len(ordered_ids)
            or set(ordered_ids) != {request.request_id for request in requests}):
        raise ValueError("order_batch must return every batch request_id exactly once, with no foreign IDs")
    return ordered_ids
