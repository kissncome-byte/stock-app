from __future__ import annotations
from dataclasses import dataclass
from .evidence import Evidence
from .market_state import MarketState

@dataclass(frozen=True, slots=True)
class MarketResult:
    state: MarketState
    reference_index: str
    evidence: tuple[Evidence, ...]
    data_valid: bool
    warning: str | None = None
