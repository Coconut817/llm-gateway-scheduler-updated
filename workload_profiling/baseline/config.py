"""Validated settings; all clock values use integer simulated milliseconds."""
from dataclasses import asdict, dataclass, field
import json
import math
from numbers import Real
from pathlib import Path

from ..common.paths import PACKAGE

CONFIG_PATH = PACKAGE / "config/baseline.json"


def positive_integer(value, name, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be {'nonnegative' if allow_zero else 'positive'} integer")


def finite_number(value, name, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}")


@dataclass(frozen=True)
class EndpointConfig:
    endpoint_id: str
    rpm_limit: int
    tpm_limit: int
    concurrency_limit: int
    base_latency_ms: int = 5
    input_tokens_per_ms: float = 2000
    output_tokens_per_ms: float = 20

    def __post_init__(self):
        if not isinstance(self.endpoint_id, str) or not self.endpoint_id.strip():
            raise ValueError("endpoint_id must be a nonempty string")
        for name in ("rpm_limit", "tpm_limit", "concurrency_limit"):
            positive_integer(getattr(self, name), name)
        positive_integer(self.base_latency_ms, "base_latency_ms", allow_zero=True)
        for name in ("input_tokens_per_ms", "output_tokens_per_ms"):
            finite_number(getattr(self, name), name)


@dataclass(frozen=True)
class BaselineConfig:
    arrival_interval_ms: int = 1
    batch_size: int = 16
    batch_wait_ms: int = 20
    input_threshold_tokens: float = 40342.5
    output_threshold_tokens: float = 578
    window_ms: int = 60000
    strategy: str = "min_rpm"
    endpoints: tuple[EndpointConfig, ...] = field(default_factory=tuple)

    def __post_init__(self):
        for name in ("arrival_interval_ms", "batch_size", "window_ms"):
            positive_integer(getattr(self, name), name)
        positive_integer(self.batch_wait_ms, "batch_wait_ms", allow_zero=True)
        if self.window_ms != 60000 or isinstance(self.window_ms, bool):
            raise ValueError("RPM/TPM use a fixed 60000 ms rolling window")
        for name in ("input_threshold_tokens", "output_threshold_tokens"):
            finite_number(getattr(self, name), name, allow_zero=True)
        if not isinstance(self.strategy, str) or not self.strategy:
            raise ValueError("strategy must be min_rpm or module:attribute")
        object.__setattr__(self, "endpoints", tuple(self.endpoints))
        if not self.endpoints or any(not isinstance(e, EndpointConfig) for e in self.endpoints):
            raise ValueError("At least one valid endpoint is required")
        if len({e.endpoint_id for e in self.endpoints}) != len(self.endpoints):
            raise ValueError("endpoint_id values must be unique")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Baseline config must be an object")
        settings = dict(data)
        settings["endpoints"] = tuple(EndpointConfig(**e) for e in settings.get("endpoints", []))
        return cls(**settings)


def load_config(path=CONFIG_PATH):
    return BaselineConfig.from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig")))
