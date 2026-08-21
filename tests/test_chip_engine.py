from datetime import date, timedelta

from stockpilot.engines import ChipEngine
from stockpilot.models import (
    ChipEngineInput,
    ChipState,
    InstitutionalFlow,
    MarginRecord,
)


def dates(count):
    start = date(2026, 1, 1)
    return tuple(start + timedelta(days=i) for i in range(count))


def test_chip_support_when_institutions_buy_and_margin_falls():
    ds = dates(25)
    flows = tuple(
        InstitutionalFlow(d, 100.0, 50.0, 0.0)
        for d in ds
    )
    margins = tuple(
        MarginRecord(d, 1000.0 - i * 5)
        for i, d in enumerate(ds)
    )
    closes = tuple(100 + i for i in range(25))

    result = ChipEngine().evaluate(
        ChipEngineInput(flows, margins, closes, ds)
    )
    assert result.state is ChipState.SUPPORT
    assert result.foreign_20d == 2000.0
    assert result.margin_20d_change_pct < 0


def test_chip_opposes_when_institutions_sell_and_margin_rises():
    ds = dates(25)
    flows = tuple(
        InstitutionalFlow(d, -100.0, -50.0, 0.0)
        for d in ds
    )
    margins = tuple(
        MarginRecord(d, 1000.0 + i * 10)
        for i, d in enumerate(ds)
    )
    closes = tuple(125 - i for i in range(25))

    result = ChipEngine().evaluate(
        ChipEngineInput(flows, margins, closes, ds)
    )
    assert result.state is ChipState.OPPOSE


def test_missing_institutional_data_is_not_zero():
    ds = dates(25)
    flows = tuple(
        InstitutionalFlow(d, None, None, None)
        for d in ds
    )
    margins = tuple(
        MarginRecord(d, 1000.0)
        for d in ds
    )
    closes = tuple(100 + i * 0.1 for i in range(25))

    result = ChipEngine().evaluate(
        ChipEngineInput(flows, margins, closes, ds)
    )
    assert result.foreign_20d is None
    assert any(
        evidence.direction.value == "unavailable"
        for evidence in result.evidence
    )
