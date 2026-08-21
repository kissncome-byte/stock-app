from __future__ import annotations
from dataclasses import dataclass
from .market_bar import DailyBar

@dataclass(frozen=True, slots=True)
class TrendEngineInput:
    bars: tuple[DailyBar, ...]
    confirmation_days: int = 2
    lookback_days: int = 120
