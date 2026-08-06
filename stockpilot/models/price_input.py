"""Validated inputs consumed by the Price Engine.

This module contains data definitions only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PriceEngineInput:
    current_price: float
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
