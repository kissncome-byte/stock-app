"""Market regime states."""

from enum import Enum


class MarketState(str, Enum):
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"
    RISK_OFF_HARD = "risk_off_hard"
    UNAVAILABLE = "unavailable"
