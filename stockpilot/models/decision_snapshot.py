"""The single source of truth consumed by the UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .action import Action
from .evidence import Evidence
from .market_state import MarketState
from .price_levels import PriceLevels
from .strategy import Strategy
from .trend_state import TrendState


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    schema_version: str
    symbol: str
    company_name: str | None
    as_of: datetime
    current_price: float | None
    market_state: MarketState
    trend_state: TrendState
    strategy: Strategy
    action: Action
    prices: PriceLevels
    supporting_evidence: tuple[Evidence, ...] = ()
    opposing_evidence: tuple[Evidence, ...] = ()
    neutral_evidence: tuple[Evidence, ...] = ()
    primary_trigger: str | None = None
    secondary_trigger: str | None = None
    invalidation: str | None = None
    data_quality_score: float | None = None
    data_warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
