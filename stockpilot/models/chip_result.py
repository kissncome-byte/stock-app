from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .evidence import Evidence


class ChipState(str, Enum):
    SUPPORT = "support"
    NEUTRAL = "neutral"
    OPPOSE = "oppose"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ChipResult:
    state: ChipState
    evidence: tuple[Evidence, ...]
    data_valid: bool
    foreign_5d: float | None = None
    foreign_10d: float | None = None
    foreign_20d: float | None = None
    trust_5d: float | None = None
    trust_10d: float | None = None
    trust_20d: float | None = None
    margin_5d_change_pct: float | None = None
    margin_10d_change_pct: float | None = None
    margin_20d_change_pct: float | None = None
