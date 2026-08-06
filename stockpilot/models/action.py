"""Execution action attached to a strategy decision."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Action:
    title: str
    detail: str
    priority: int = 0
