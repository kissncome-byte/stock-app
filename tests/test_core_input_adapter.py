from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from stockpilot.adapters import CoreInputAdapter
from stockpilot.models import ListingMarket, RawDailyBar, RawInstitutionalRecord, RawMarginRecord, RawMarketBundle, RawQuote

def bars(count, start=100.0, step=1.0):
    d0=date(2026,1,1)
    return tuple(RawDailyBar(d0+timedelta(days=i), start+i*step,
        (start+i*step)*1.01,(start+i*step)*0.99,start+i*step,1000+i)
        for i in range(count))

def inst(count=25):
    d0=date(2026,1,1)
    return tuple(RawInstitutionalRecord(d0+timedelta(days=i),100,50,0) for i in range(count))

def margin(count=25):
    d0=date(2026,1,1)
    return tuple(RawMarginRecord(d0+timedelta(days=i),1000-i*5) for i in range(count))

def make_bundle(market="上市", index="TAIEX"):
    sb=bars(120)
    ib=bars(120,20000,20)
    return RawMarketBundle(
        symbol="3037", company_name="欣興", listing_market=market,
        quote=RawQuote("3037",sb[-1].close,
            datetime(2026,8,17,10,0,tzinfo=timezone(timedelta(hours=8))),
            "Fugle",True),
        stock_bars=sb,index_symbol=index,index_bars=ib,
        institutional=inst(),margin=margin(),atr=5,ma20=sb[-1].close-5,
        ma60=sb[-1].close-20,recent_resistance=sb[-1].close+10,
        breakout_level=sb[-1].close+8,recent_swing_low=sb[-1].close-8,
        platform_floor=sb[-1].close-25,original_target=sb[-1].close+25,
        fair_value=sb[-1].close-3,tick_size=0.5)

def test_listed_to_taiex():
    r=CoreInputAdapter().build(make_bundle(),is_holding=False,cost=0)
    assert r.market_input.listing_market is ListingMarket.LISTED
    assert r.market_input.expected_reference_index=="TAIEX"

def test_otc_to_tpex():
    r=CoreInputAdapter().build(make_bundle("上櫃","TPEx"),is_holding=False,cost=0)
    assert r.market_input.listing_market is ListingMarket.OTC
    assert r.market_input.expected_reference_index=="TPEx"

def test_invalid_quote_falls_back_with_warning():
    b=make_bundle()
    b=replace(b,quote=RawQuote("3037",None,b.quote.timestamp,"Fugle",False))
    r=CoreInputAdapter().build(b,is_holding=False,cost=0)
    assert r.price_input.current_price==b.stock_bars[-1].close
    assert any("即時報價無效" in w for w in r.data_warnings)

def test_missing_volume_stays_none():
    b=make_bundle()
    ls=list(b.stock_bars)
    last=ls[-1]
    ls[-1]=RawDailyBar(last.trading_date,last.open,last.high,last.low,last.close,None)
    b=replace(b,stock_bars=tuple(ls))
    r=CoreInputAdapter().build(b,is_holding=False,cost=0)
    assert r.volume_input.bars[-1].volume is None

def test_market_mismatch_not_silently_corrected():
    r=CoreInputAdapter().build(make_bundle("上櫃","TAIEX"),is_holding=False,cost=0)
    assert r.market_input.reference_index=="TAIEX"
    assert r.market_input.expected_reference_index=="TPEx"
    assert any("市場指數錯配" in w for w in r.data_warnings)
