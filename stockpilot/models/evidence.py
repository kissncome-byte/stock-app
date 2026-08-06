"""Evidence domain model.

This module contains data definitions only.
It must not calculate indicators or make investment decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EvidenceDirection(str, Enum):
    SUPPORT = "support"
    OPPOSE = "oppose"
    NEUTRAL = "neutral"
    UNAVAILABLE = "unavailable"


class EvidenceSeverity(int, Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class EvidenceEngine(str, Enum):
    RISK = "risk"
    MARKET = "market"
    TREND = "trend"
    CHIP = "chip"
    VOLUME = "volume"
    PRICE = "price"


@dataclass(frozen=True, slots=True)
class Evidence:
    code: str
    engine: EvidenceEngine
    title: str
    direction: EvidenceDirection
    severity: EvidenceSeverity
    explanation: str
    value: Any = None
    threshold: Any = None
    source: str | None = None
    as_of: datetime | None = None
    data_valid: bool = True
    backtestable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
