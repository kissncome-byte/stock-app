from __future__ import annotations
from dataclasses import dataclass
from .action import Action
from .evidence import Evidence
from .strategy import Strategy

@dataclass(frozen=True, slots=True)
class DecisionResult:
    strategy: Strategy
    action: Action
    supporting_evidence: tuple[Evidence, ...]
    opposing_evidence: tuple[Evidence, ...]
    neutral_evidence: tuple[Evidence, ...]
    primary_trigger: str | None
    secondary_trigger: str | None
    invalidation: str | None
    rationale: str
    agreement_ratio: float
