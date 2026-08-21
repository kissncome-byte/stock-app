from __future__ import annotations
from dataclasses import dataclass
from .chip_result import ChipResult
from .market_result import MarketResult
from .price_levels import PriceLevels
from .trend_result import TrendResult
from .volume_result import VolumeResult

@dataclass(frozen=True, slots=True)
class PortfolioContext:
    is_holding: bool
    cost: float | None = None
    current_price: float | None = None

@dataclass(frozen=True, slots=True)
class DecisionEngineInput:
    market: MarketResult
    trend: TrendResult
    chip: ChipResult
    volume: VolumeResult
    prices: PriceLevels
    portfolio: PortfolioContext
