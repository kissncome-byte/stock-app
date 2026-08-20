from __future__ import annotations

from stockpilot.engines import (
    ChipEngine,
    DecisionEngine,
    MarketEngine,
    PriceEngine,
    TrendEngine,
    VolumeEngine,
)
from stockpilot.models import (
    DecisionEngineInput,
    DecisionSnapshot,
    OrchestratorInput,
    PortfolioContext,
)


class OrchestratorError(ValueError):
    pass


class DecisionOrchestrator:
    def __init__(
        self,
        *,
        price_engine: PriceEngine | None = None,
        trend_engine: TrendEngine | None = None,
        market_engine: MarketEngine | None = None,
        chip_engine: ChipEngine | None = None,
        volume_engine: VolumeEngine | None = None,
        decision_engine: DecisionEngine | None = None,
    ) -> None:
        self.price_engine = price_engine or PriceEngine()
        self.trend_engine = trend_engine or TrendEngine()
        self.market_engine = market_engine or MarketEngine()
        self.chip_engine = chip_engine or ChipEngine()
        self.volume_engine = volume_engine or VolumeEngine()
        self.decision_engine = decision_engine or DecisionEngine()

    def build_snapshot(self, data: OrchestratorInput) -> DecisionSnapshot:
        self._validate_input(data)

        prices = self.price_engine.build(data.price_input)
        trend = self.trend_engine.evaluate(data.trend_input)
        market = self.market_engine.evaluate(data.market_input)
        chip = self.chip_engine.evaluate(data.chip_input)
        volume = self.volume_engine.evaluate(data.volume_input)

        portfolio = data.portfolio
        if portfolio.current_price is None:
            portfolio = PortfolioContext(
                is_holding=portfolio.is_holding,
                cost=portfolio.cost,
                current_price=data.price_input.current_price,
            )

        decision = self.decision_engine.evaluate(
            DecisionEngineInput(
                market=market,
                trend=trend,
                chip=chip,
                volume=volume,
                prices=prices,
                portfolio=portfolio,
            )
        )

        metadata = {
            "market_reference_index": market.reference_index,
            "market_data_valid": market.data_valid,
            "chip_state": chip.state.value,
            "chip_data_valid": chip.data_valid,
            "volume_state": volume.state.value,
            "volume_pattern": volume.pattern,
            "volume_data_valid": volume.data_valid,
            "trend_raw_state": trend.raw_state.value,
            "trend_days_in_state": trend.days_in_state,
            "decision_agreement_ratio": decision.agreement_ratio,
        }

        snapshot = DecisionSnapshot(
            schema_version=data.schema_version,
            symbol=data.symbol,
            company_name=data.company_name,
            as_of=data.as_of,
            current_price=data.price_input.current_price,
            market_state=market.state,
            trend_state=trend.formal_state,
            strategy=decision.strategy,
            action=decision.action,
            prices=prices,
            supporting_evidence=decision.supporting_evidence,
            opposing_evidence=decision.opposing_evidence,
            neutral_evidence=decision.neutral_evidence,
            primary_trigger=decision.primary_trigger,
            secondary_trigger=decision.secondary_trigger,
            invalidation=decision.invalidation,
            data_quality_score=data.data_quality_score,
            data_warnings=data.data_warnings,
            metadata=metadata,
        )
        self._audit_snapshot(snapshot)
        return snapshot

    def _validate_input(self, data: OrchestratorInput) -> None:
        if not data.schema_version:
            raise OrchestratorError("schema_version is required")
        if not data.symbol.strip():
            raise OrchestratorError("symbol is required")
        if data.as_of.tzinfo is None:
            raise OrchestratorError("as_of must be timezone-aware")
        if (
            data.portfolio.current_price is not None
            and abs(data.portfolio.current_price - data.price_input.current_price) > 1e-9
        ):
            raise OrchestratorError(
                "portfolio current_price must match PriceEngine current_price"
            )

    def _audit_snapshot(self, snapshot: DecisionSnapshot) -> None:
        prices = snapshot.prices
        structural = prices.structural_exit.value
        moving = prices.moving_protection.value
        target = prices.first_target.value
        current = snapshot.current_price

        if structural is not None and moving is not None and not structural < moving:
            raise OrchestratorError(
                "snapshot price order invalid: structural >= moving"
            )
        if moving is not None and current is not None and not moving < current:
            raise OrchestratorError(
                "snapshot price order invalid: moving >= current"
            )
        if target is not None and current is not None and not target > current:
            raise OrchestratorError(
                "snapshot price order invalid: target <= current"
            )
        if snapshot.strategy.value == "build" and snapshot.market_state.value in {
            "risk_off",
            "risk_off_hard",
            "unavailable",
        }:
            raise OrchestratorError(
                "BUILD cannot coexist with blocked market state"
            )
