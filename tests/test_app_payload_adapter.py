from datetime import date
import pandas as pd

from stockpilot.adapters import build_legacy_payload_from_app


def test_app_payload_preserves_market_and_missing_volume():
    daily = pd.DataFrame([
        {"date": "2026-08-18", "open": 100, "high": 102, "low": 99, "close": 101, "vol": 1000},
        {"date": "2026-08-19", "open": 101, "high": 103, "low": 100, "close": 102, "vol": None},
    ])
    market = pd.DataFrame([
        {"date": "2026-08-18", "open": 20000, "high": 20100, "low": 19900, "close": 20050, "vol": 100000},
    ])
    institutional = pd.DataFrame([
        {"date": "2026-08-19", "外資(張)": 100, "投信(張)": 50, "自營商總計(張)": 0},
    ])
    margin = pd.DataFrame([
        {"date": "2026-08-19", "MarginPurchaseTodayBalance": 5000},
    ])
    res = {
        "stock_id": "3274",
        "stock_name": "測試",
        "market_type": "TWO",
        "current_price": 102.0,
        "quote_time": "2026-08-20 10:00:00",
        "rt_source": "Fugle 即時行情",
        "quote_success": True,
        "daily_df": daily,
        "institutional_df": institutional,
        "atr": 3.0,
        "ma20_val": 100.0,
        "ma60_val": 95.0,
        "real_resistance": 105.0,
        "structure_stop": 92.0,
        "expected_target_price": 112.0,
        "expected_entry_price": 101.0,
    }

    payload = build_legacy_payload_from_app(
        res,
        market_index_df=market,
        margin_df=margin,
        legacy_levels={
            "confirmation": 105.0,
            "protective_stop": 96.0,
            "structure_stop": 92.0,
            "target1": 112.0,
            "entry": 101.0,
        },
    )

    assert payload["listing_market"] == "TWO"
    assert payload["index_symbol"] == "TPEx"
    assert payload["stock_bars"][-1]["volume"] is None
    assert payload["margin"][0]["margin_balance"] == 5000.0
