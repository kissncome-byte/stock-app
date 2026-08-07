from __future__ import annotations

from dataclasses import dataclass

from .market_bar import DailyBar


@dataclass(frozen=True, slots=True)
class VolumeEngineInput:
    bars: tuple[DailyBar, ...]
    lookback_days: int = 60
