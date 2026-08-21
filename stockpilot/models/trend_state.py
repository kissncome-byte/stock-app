"""Formal stock trend states."""

from enum import Enum


class TrendState(str, Enum):
    STRONG_UPTREND = "strong_uptrend"
    UPTREND_PULLBACK = "uptrend_pullback"
    RANGE = "range"
    BEAR_RALLY = "bear_rally"
    DOWNTREND = "downtrend"
    UNAVAILABLE = "unavailable"
