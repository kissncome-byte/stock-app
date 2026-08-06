from __future__ import annotations
from dataclasses import dataclass
from datetime import date

@dataclass(frozen=True, slots=True)
class DailyBar:
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
