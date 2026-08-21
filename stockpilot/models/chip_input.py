from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class InstitutionalFlow:
    trading_date: date
    foreign: float | None
    trust: float | None
    dealer: float | None


@dataclass(frozen=True, slots=True)
class MarginRecord:
    trading_date: date
    margin_balance: float | None
    short_balance: float | None = None


@dataclass(frozen=True, slots=True)
class ChipEngineInput:
    institutional_flows: tuple[InstitutionalFlow, ...]
    margin_records: tuple[MarginRecord, ...]
    closes: tuple[float, ...]
    dates: tuple[date, ...]
