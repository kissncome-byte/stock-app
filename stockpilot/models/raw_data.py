from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime

@dataclass(frozen=True, slots=True)
class RawQuote:
    symbol: str
    current_price: float | None
    timestamp: datetime | None
    source: str
    valid: bool

@dataclass(frozen=True, slots=True)
class RawInstitutionalRecord:
    trading_date: date
    foreign: float | None
    trust: float | None
    dealer: float | None

@dataclass(frozen=True, slots=True)
class RawMarginRecord:
    trading_date: date
    margin_balance: float | None
    short_balance: float | None = None

@dataclass(frozen=True, slots=True)
class RawDailyBar:
    trading_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None

@dataclass(frozen=True, slots=True)
class RawMarketBundle:
    symbol: str
    company_name: str | None
    listing_market: str
    quote: RawQuote
    stock_bars: tuple[RawDailyBar, ...]
    index_symbol: str | None
    index_bars: tuple[RawDailyBar, ...]
    institutional: tuple[RawInstitutionalRecord, ...]
    margin: tuple[RawMarginRecord, ...]
    atr: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    recent_resistance: float | None = None
    breakout_level: float | None = None
    recent_swing_low: float | None = None
    platform_floor: float | None = None
    original_target: float | None = None
    fair_value: float | None = None
    tick_size: float = 0.5
    international_risk_flag: bool = False
    data_warnings: tuple[str, ...] = ()
