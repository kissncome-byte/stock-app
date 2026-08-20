from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .chip_input import ChipEngineInput
from .decision_input import PortfolioContext
from .market_input import MarketEngineInput
from .price_input import PriceEngineInput
from .trend_input import TrendEngineInput
from .volume_input import VolumeEngineInput


@dataclass(frozen=True, slots=True)
class OrchestratorInput:
    schema_version: str
    symbol: str
    company_name: str | None
    as_of: datetime
    price_input: PriceEngineInput
    trend_input: TrendEngineInput
    market_input: MarketEngineInput
    chip_input: ChipEngineInput
    volume_input: VolumeEngineInput
    portfolio: PortfolioContext
    data_quality_score: float | None = None
    data_warnings: tuple[str, ...] = ()
