from __future__ import annotations
from dataclasses import dataclass
from .evidence import Evidence
from .trend_state import TrendState

@dataclass(frozen=True, slots=True)
class TrendResult:
    formal_state: TrendState
    raw_state: TrendState
    days_in_state: int
    evidence: tuple[Evidence, ...]
    state_history: tuple[TrendState, ...]
