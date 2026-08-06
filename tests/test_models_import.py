"""Minimal Sprint 1 import and construction tests."""

from datetime import datetime

from stockpilot.models import (
    Action,
    DecisionSnapshot,
    Evidence,
    EvidenceDirection,
    EvidenceEngine,
    EvidenceSeverity,
    MarketState,
    PriceLevel,
    PriceLevels,
    Strategy,
    TrendState,
)


def test_models_can_be_constructed() -> None:
    unavailable = PriceLevel(
        value=None,
        source="not_calculated",
        valid=False,
    )
    prices = PriceLevels(
        entry_zone_low=unavailable,
        entry_zone_high=unavailable,
        confirmation=unavailable,
        moving_protection=unavailable,
        structural_exit=unavailable,
        first_target=unavailable,
        extended_target=unavailable,
    )
    evidence = Evidence(
        code="TREND_DATA_PENDING",
        engine=EvidenceEngine.TREND,
        title="趨勢資料尚未建立",
        direction=EvidenceDirection.UNAVAILABLE,
        severity=EvidenceSeverity.LOW,
        explanation="Sprint 1 只建立資料模型，不執行趨勢計算。",
        data_valid=False,
    )
    snapshot = DecisionSnapshot(
        schema_version="4.0.0",
        symbol="3037",
        company_name="欣興",
        as_of=datetime(2026, 8, 6, 9, 0, 0),
        current_price=None,
        market_state=MarketState.UNAVAILABLE,
        trend_state=TrendState.UNAVAILABLE,
        strategy=Strategy.WAIT,
        action=Action(title="等待", detail="核心引擎尚未接入。"),
        prices=prices,
        neutral_evidence=(evidence,),
    )
    assert snapshot.symbol == "3037"
    assert snapshot.strategy is Strategy.WAIT
    assert snapshot.neutral_evidence[0].data_valid is False
