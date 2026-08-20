from datetime import date, datetime, timedelta, timezone
from stockpilot.services.shadow_integration import ShadowIntegration

def bars(count=120, start=100.0, step=1.0):
    d0 = date(2026, 1, 1)
    return [
        {
            "date": (d0 + timedelta(days=i)).isoformat(),
            "open": start + i*step,
            "high": (start + i*step)*1.01,
            "low": (start + i*step)*0.99,
            "close": start + i*step,
            "volume": 1000+i,
        }
        for i in range(count)
    ]

def payload():
    sb = bars()
    mb = bars(start=20000.0, step=20.0)
    current = sb[-1]["close"]
    d0 = date(2026, 4, 1)
    return {
        "symbol":"3037",
        "company_name":"欣興",
        "listing_market":"上市",
        "current_price":current,
        "quote_timestamp":datetime(2026,8,20,10,0,tzinfo=timezone(timedelta(hours=8))).isoformat(),
        "quote_source":"Fugle",
        "quote_valid":True,
        "stock_bars":sb,
        "index_symbol":"TAIEX",
        "index_bars":mb,
        "institutional":[
            {"date":(d0+timedelta(days=i)).isoformat(),"foreign":100,"trust":50,"dealer":0}
            for i in range(25)
        ],
        "margin":[
            {"date":(d0+timedelta(days=i)).isoformat(),"margin_balance":1000-i*5}
            for i in range(25)
        ],
        "atr":5.0,
        "ma20":current-5,
        "ma60":current-20,
        "recent_resistance":current+10,
        "breakout_level":current+8,
        "recent_swing_low":current-8,
        "platform_floor":current-25,
        "original_target":current+25,
        "fair_value":current-3,
        "tick_size":0.5,
    }

def test_shadow_runs_without_replacing_legacy():
    result = ShadowIntegration().run(
        payload(),
        is_holding=False,
        cost=0,
        legacy_action="等待",
    )
    assert result.symbol == "3037"
    assert result.legacy_action == "等待"
    assert result.core_strategy.value in {
        "wait","build","hold","hold_no_add","reduce","exit"
    }

def test_unknown_legacy_action_gives_no_fake_match():
    result = ShadowIntegration().run(
        payload(),
        is_holding=False,
        cost=0,
        legacy_action="特殊文字",
    )
    assert result.same_direction is None
