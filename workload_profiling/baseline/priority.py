"""Reproducible experimental business priorities, independent of token lengths."""
from dataclasses import replace
import hashlib
import json


def assign_priority(request, config):
    # Honor explicitly supplied business priorities, including Python injection.
    if (config.priority_assignment != "synthetic" or request.priority_source != "default"
            or request.base_priority != 1 or request.priority_class != "normal"):
        return request
    key = json.dumps([config.priority_seed, request.request_id], separators=(",", ":")).encode("utf-8")
    bucket = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 100
    label = "high" if bucket < 20 else ("normal" if bucket < 80 else "low")
    value = getattr(config, f"priority_{label}_weight")
    return replace(request, base_priority=value, priority_class=label, priority_source="synthetic")


def priority_fields(row):
    """Optional explicit source labels; never infer business priority from length."""
    if "base_priority" not in row:
        if "priority_class" in row:
            raise ValueError("priority_class requires base_priority")
        return {}
    value = row["base_priority"]
    label = row.get("priority_class", {10: "high", 3: "normal", 1: "low"}.get(value, "custom"))
    return {"base_priority": value, "priority_class": label, "priority_source": "recorded"}


def heavy_for(request, config):
    return (request.input_tokens >= config.input_threshold_tokens
            or request.output_tokens >= config.output_threshold_tokens)


def effective_priority(request, config):
    return request.base_priority * (config.heavy_priority_discount if heavy_for(request, config) else 1.0)
