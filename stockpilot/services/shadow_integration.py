from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from stockpilot.adapters import CoreInputAdapter, LegacyDictAdapter
from stockpilot.models import DecisionSnapshot, Strategy
from stockpilot.services.decision_orchestrator import DecisionOrchestrator


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    symbol: str
    legacy_action: str | None
    core_strategy: Strategy
    same_direction: bool | None
    differences: tuple[str, ...]
    snapshot: DecisionSnapshot


class ShadowIntegration:
    def __init__(self) -> None:
        self.legacy_adapter = LegacyDictAdapter()
        self.core_adapter = CoreInputAdapter()
        self.orchestrator = DecisionOrchestrator()

    def run(
        self,
        legacy_payload: dict[str, Any],
        *,
        is_holding: bool,
        cost: float | None,
        legacy_action: str | None = None,
    ) -> ShadowComparison:
        bundle = self.legacy_adapter.from_dict(legacy_payload)
        core_input = self.core_adapter.build(
            bundle,
            is_holding=is_holding,
            cost=cost,
        )
        snapshot = self.orchestrator.build_snapshot(core_input)

        legacy_norm = self._normalize_legacy_action(legacy_action)
        core_norm = snapshot.strategy.value
        same = None if legacy_norm is None else legacy_norm == core_norm

        diffs = []
        if legacy_norm is not None and legacy_norm != core_norm:
            diffs.append(f"legacy={legacy_norm}, core={core_norm}")
        diffs.extend(f"data_warning: {w}" for w in snapshot.data_warnings)

        return ShadowComparison(
            symbol=snapshot.symbol,
            legacy_action=legacy_action,
            core_strategy=snapshot.strategy,
            same_direction=same,
            differences=tuple(diffs),
            snapshot=snapshot,
        )

    def _normalize_legacy_action(self, action: str | None) -> str | None:
        if action is None:
            return None
        text = action.strip().lower()

        mapping = (
            ("exit", ("退出", "停損", "清倉", "exit")),
            ("reduce", ("減碼", "降低曝險", "reduce")),
            ("hold_no_add", ("續抱不加碼", "不加碼", "hold_no_add")),
            ("hold", ("續抱", "持有", "hold")),
            ("build", ("建立", "買進", "進場", "build")),
            ("wait", ("等待", "觀察", "wait")),
        )
        for normalized, tokens in mapping:
            if any(token in text for token in tokens):
                return normalized
        return None
