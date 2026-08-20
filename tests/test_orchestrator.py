from datetime import date, datetime, timedelta, timezone

from stockpilot.models import (
    ChipEngineInput,
    DailyBar,
    InstitutionalFlow,
    ListingMarket,
    MarginRecord,
    MarketEngineInput,
    OrchestratorInput,
    PortfolioContext,
    PriceEngineInput,
    Strategy,
    TrendEngineInput,
    VolumeEngineInput,
)
from stockpilot.services import DecisionOrchestrator


def bars(count=120, start_price=100.0, step=0.8):
    start = date(2026, 1, 1)
    result = []
    for i in range(count):
        close = start_price + i * step
        result.append(
            DailyBar(
                trading_date=start + timedelta(days=i),
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=1000.0 + i,
            )
        )
    return tuple(result)


def chip_input_from_bars(stock_bars):
    ds = tuple(bar.trading_date for bar in stock_bars[-25:])
    flows = tuple(
        InstitutionalFlow(
            trading_date=d,
            foreign=100.0,
            trust=50.0,
            dealer=0.0,
        )
        for d in ds
    )
    margins = tuple(
        MarginRecord(
            trading_date=d,
            margin_balance=1000.0 - i * 5.0,
        )
        for i, d in enumerate(ds)
    )
    closes = tuple(bar.close for bar in stock_bars[-25:])
    return ChipEngineInput(
        institutional_flows=flows,
        margin_records=margins,
        closes=closes,
        dates=ds,
    )


def make_input(is_holding=False, cost=0.0):
    stock_bars = bars()
    market_bars = bars(start_price=20000.0, step=30.0)
    current = stock_bars[-1].close

    return OrchestratorInput(
        schema_version="4.0.0",
        symbol="3037",
        company_name="欣興",
        as_of=datetime(
            2026, 8, 7, 9, 0,
            tzinfo=timezone(timedelta(hours=8)),
        ),
        price_input=PriceEngineInput(
            current_price=current,
            atr=5.0,
            ma20=current - 5.0,
            ma60=current - 20.0,
            recent_resistance=current + 10.0,
            breakout_level=current + 8.0,
            recent_swing_low=current - 8.0,
            platform_floor=current - 25.0,
            original_target=current + 25.0,
            fair_value=current - 3.0,
            tick_size=0.5,
        ),
        trend_input=TrendEngineInput(bars=stock_bars),
        market_input=MarketEngineInput(
            listing_market=ListingMarket.LISTED,
            index_bars=market_bars,
            reference_index="TAIEX",
            expected_reference_index="TAIEX",
        ),
        chip_input=chip_input_from_bars(stock_bars),
        volume_input=VolumeEngineInput(bars=stock_bars[-60:]),
        portfolio=PortfolioContext(
            is_holding=is_holding,
            cost=cost,
            current_price=current,
        ),
        data_quality_score=100.0,
    )


def test_orchestrator_builds_single_snapshot():
    snapshot = DecisionOrchestrator().build_snapshot(
        make_input(is_holding=False, cost=0.0)
    )
    assert snapshot.symbol == "3037"
    assert snapshot.current_price is not None
    assert snapshot.metadata["market_reference_index"] == "TAIEX"
    assert snapshot.strategy in {Strategy.WAIT, Strategy.BUILD}


def test_holder_snapshot_uses_same_price_object_for_decision():
    snapshot = DecisionOrchestrator().build_snapshot(
        make_input(is_holding=True, cost=120.0)
    )
    assert snapshot.prices.moving_protection.value is not None
    assert snapshot.prices.structural_exit.value is not None
    assert snapshot.primary_trigger is not None


def test_snapshot_has_one_formal_trend_and_one_strategy():
    snapshot = DecisionOrchestrator().build_snapshot(
        make_input(is_holding=True, cost=80.0)
    )
    assert snapshot.trend_state.value in {
        "strong_uptrend",
        "uptrend_pullback",
        "range",
        "bear_rally",
        "downtrend",
    }
    assert snapshot.strategy.value in {
        "wait",
        "build",
        "hold",
        "hold_no_add",
        "reduce",
        "exit",
    }


def test_ui_facing_snapshot_contains_explainable_evidence():
    snapshot = DecisionOrchestrator().build_snapshot(
        make_input(is_holding=True, cost=80.0)
    )
    total = (
        len(snapshot.supporting_evidence)
        + len(snapshot.opposing_evidence)
        + len(snapshot.neutral_evidence)
    )
    assert total > 0


def test_cost_change_does_not_change_trend_state():
    a = DecisionOrchestrator().build_snapshot(
        make_input(is_holding=True, cost=80.0)
    )
    b = DecisionOrchestrator().build_snapshot(
        make_input(is_holding=True, cost=180.0)
    )
    assert a.trend_state is b.trend_state
