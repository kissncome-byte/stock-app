from datetime import date, timedelta
from stockpilot.engines import MarketEngine
from stockpilot.models import DailyBar, ListingMarket, MarketEngineInput, MarketState

def make_bars(closes):
    start = date(2026, 1, 1)
    return tuple(
        DailyBar(
            trading_date=start + timedelta(days=i),
            open=c, high=c*1.01, low=c*0.99, close=c, volume=1000+i
        )
        for i, c in enumerate(closes)
    )

def test_listed_uses_taiex():
    result = MarketEngine().evaluate(MarketEngineInput(
        listing_market=ListingMarket.LISTED,
        index_bars=make_bars([100+i for i in range(120)]),
        reference_index="TAIEX",
        expected_reference_index="TAIEX",
    ))
    assert result.reference_index == "TAIEX"
    assert result.data_valid is True

def test_otc_uses_tpex():
    result = MarketEngine().evaluate(MarketEngineInput(
        listing_market=ListingMarket.OTC,
        index_bars=make_bars([100+i*0.5 for i in range(120)]),
        reference_index="TPEx",
        expected_reference_index="TPEx",
    ))
    assert result.reference_index == "TPEx"
    assert result.data_valid is True

def test_otc_does_not_fallback_to_taiex():
    result = MarketEngine().evaluate(MarketEngineInput(
        listing_market=ListingMarket.OTC,
        index_bars=make_bars([100+i for i in range(120)]),
        reference_index="TAIEX",
        expected_reference_index="TPEx",
    ))
    assert result.state is MarketState.UNAVAILABLE
    assert result.data_valid is False

def test_insufficient_market_data_is_unavailable():
    result = MarketEngine().evaluate(MarketEngineInput(
        listing_market=ListingMarket.LISTED,
        index_bars=make_bars([100+i for i in range(20)]),
        reference_index="TAIEX",
        expected_reference_index="TAIEX",
    ))
    assert result.state is MarketState.UNAVAILABLE
    assert result.data_valid is False

def test_market_result_exposes_used_data():
    result = MarketEngine().evaluate(MarketEngineInput(
        listing_market=ListingMarket.LISTED,
        index_bars=make_bars([100+i for i in range(120)]),
        reference_index="TAIEX",
        expected_reference_index="TAIEX",
    ))
    codes = {e.code for e in result.evidence}
    assert "MARKET_TREND_STRUCTURE" in codes
    assert "MARKET_MOMENTUM" in codes
    assert "MARKET_VOLUME_RATIO" in codes
    assert "MARKET_RISK" in codes
