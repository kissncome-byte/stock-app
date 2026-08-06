from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from .market_bar import DailyBar

class ListingMarket(str, Enum):
    LISTED = "listed"
    OTC = "otc"

@dataclass(frozen=True, slots=True)
class MarketEngineInput:
    listing_market: ListingMarket
    index_bars: tuple[DailyBar, ...]
    reference_index: str
    expected_reference_index: str
    international_risk_flag: bool = False
    lookback_days: int = 120
