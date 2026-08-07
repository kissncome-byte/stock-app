from datetime import date, timedelta

from stockpilot.engines import VolumeEngine
from stockpilot.models import DailyBar, VolumeEngineInput, VolumeState


def make_bars(last_close, last_volume):
    start = date(2026, 1, 1)
    bars = []
    for i in range(20):
        close = 100.0 + i * 0.1
        bars.append(DailyBar(
            trading_date=start + timedelta(days=i),
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=1000.0,
        ))
    bars.append(DailyBar(
        trading_date=start + timedelta(days=20),
        open=100.0,
        high=max(last_close, 100.0) * 1.01,
        low=min(last_close, 100.0) * 0.99,
        close=last_close,
        volume=last_volume,
    ))
    return tuple(bars)


def test_volume_confirms_high_volume_rise():
    result = VolumeEngine().evaluate(
        VolumeEngineInput(make_bars(105.0, 2000.0))
    )
    assert result.state is VolumeState.CONFIRM
    assert result.pattern == "放量上漲"


def test_volume_warns_high_volume_drop():
    result = VolumeEngine().evaluate(
        VolumeEngineInput(make_bars(95.0, 2000.0))
    )
    assert result.state is VolumeState.WARNING
    assert result.pattern == "放量下跌"


def test_volume_missing_is_unavailable_not_zero():
    bars = list(make_bars(100.0, 1000.0))
    bars[-1] = DailyBar(
        trading_date=bars[-1].trading_date,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=None,
    )
    result = VolumeEngine().evaluate(
        VolumeEngineInput(tuple(bars))
    )
    assert result.state is VolumeState.UNAVAILABLE
    assert result.volume_ratio_20d is None
