"""拥塞状态提供接口。当前不估计 RPM/TPM/Concurrency，也不处理 hysteresis。"""
from abc import ABC, abstractmethod
from enum import Enum


class CongestionState(Enum):
    IDLE = "idle"
    NORMAL = "normal"
    BUSY = "busy"
    CRITICAL = "critical"


def validate_congestion_state(state):
    if not isinstance(state, CongestionState):
        raise ValueError("congestion_state must be a CongestionState enum value")
    return state


class CongestionProvider(ABC):
    # 未来真实 controller 只需实现此方法；状态进入/退出规则由它负责。
    @abstractmethod
    def get_state(self) -> CongestionState:
        raise NotImplementedError


class StaticCongestionProvider(CongestionProvider):
    """人工指定状态的测试工具，不是 congestion estimation 算法。"""
    def __init__(self, state=CongestionState.NORMAL):
        self.set_state(state)

    def set_state(self, state: CongestionState):
        self._state = validate_congestion_state(state)

    def get_state(self) -> CongestionState:
        return self._state
