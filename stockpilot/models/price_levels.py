"""Single-source price level models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PriceLevel:
    value: float | None
    source: str
    valid: bool
    formula: str | None = None
    confidence: str | None = None


@dataclass(frozen=True, slots=True)
class PriceLevels:
    entry_zone_low: PriceLevel
    entry_zone_high: PriceLevel
    confirmation: PriceLevel
    moving_protection: PriceLevel
    structural_exit: PriceLevel
    first_target: PriceLevel
    extended_target: PriceLevel
    reward_pct: float | None = None
    risk_pct: float | None = None
    reward_risk_ratio: float | None = None
