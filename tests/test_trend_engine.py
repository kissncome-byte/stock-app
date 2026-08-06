from datetime import date, timedelta
import pytest

from stockpilot.engines import TrendEngine, TrendEngineError
from stockpilot.models import DailyBar, TrendEngineInput, TrendState

def make_bars(closes: list[float]) -> tuple[DailyBar, ...]:
    start = date(2026, 1, 1)
    return tuple(
        DailyBar(
            trading_date=start + timedelta(days=i),
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=1000 + i,
        )
        for i, close in enumerate(closes)
    )

def test_identifies_persistent_strong_uptrend() -> None:
    closes = [100 + i * 1.2 for i in range(120)]
    result = TrendEngine().evaluate(TrendEngineInput(bars=make_bars(closes)))
    assert result.formal_state is TrendState.STRONG_UPTREND
    assert result.raw_state is TrendState.STRONG_UPTREND
    assert result.days_in_state >= 2
    assert len(result.evidence) == 4

def test_one_noisy_day_does_not_immediately_reverse_formal_state() -> None:
    closes = [100 + i * 1.0 for i in range(119)]
    closes.append(closes[-1] * 0.97)
    result = TrendEngine().evaluate(
        TrendEngineInput(bars=make_bars(closes), confirmation_days=2)
    )
    assert result.formal_state is not TrendState.DOWNTREND

def test_major_breakdown_can_immediately_move_to_downtrend() -> None:
    closes = [100 + i * 0.8 for i in range(100)]
    last = closes[-1]
    for _ in range(19):
        last *= 0.99
        closes.append(last)
    closes.append(last * 0.80)
    result = TrendEngine().evaluate(
        TrendEngineInput(bars=make_bars(closes), confirmation_days=2)
    )
    assert result.formal_state is TrendState.DOWNTREND

def test_requires_sufficient_completed_bars() -> None:
    with pytest.raises(TrendEngineError):
        TrendEngine().evaluate(
            TrendEngineInput(bars=make_bars([100 + i for i in range(40)]))
        )

def test_cost_is_not_part_of_trend_input() -> None:
    fields = TrendEngineInput.__dataclass_fields__
    assert "cost" not in fields
    assert "holding_cost" not in fields
