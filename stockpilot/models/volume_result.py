from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .evidence import Evidence


class VolumeState(str, Enum):
    CONFIRM = "confirm"
    NEUTRAL = "neutral"
    WARNING = "warning"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class VolumeResult:
    state: VolumeState
    pattern: str
    evidence: tuple[Evidence, ...]
    data_valid: bool
    volume_ratio_20d: float | None = None
    up_day_volume_ratio: float | None = None
