from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _tick_size(price: float) -> float:
    if price >= 1000:
        return 5.0
    if price >= 500:
        return 1.0
    if price >= 100:
        return 0.5
    if price >= 50:
        return 0.1
    if price >= 10:
        return 0.05
    return 0.01


def _df_bars(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows = []
    for _, row in df.sort_values("date").iterrows():
        rows.append({
            "date": str(row.get("date", ""))[:10],
            "open": _float_or_none(row.get("open")),
            "high": _float_or_none(row.get("high")),
            "low": _float_or_none(row.get("low")),
            "close": _float_or_none(row.get("close")),
            "volume": _float_or_none(
                row.get("vol", row.get("Trading_Volume"))
            ),
        })
    return rows


def _institutional_rows(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    result = []
    for _, row in df.sort_values("date").iterrows():
        result.append({
            "date": str(row.get("date", ""))[:10],
            "foreign": _float_or_none(row.get("外資(張)")),
            "trust": _float_or_none(row.get("投信(張)")),
            "dealer": _float_or_none(row.get("自營商總計(張)")),
        })
    return result


def _margin_rows(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    result = []
    for _, row in df.sort_values("date").iterrows():
        result.append({
            "date": str(row.get("date", ""))[:10],
            "margin_balance": _float_or_none(
                row.get("MarginPurchaseTodayBalance")
            ),
            "short_balance": _float_or_none(
                row.get("ShortSaleTodayBalance")
            ),
        })
    return result


def build_legacy_payload_from_app(
    res: dict[str, Any],
    *,
    market_index_df: pd.DataFrame | None,
    margin_df: pd.DataFrame | None,
    legacy_levels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    levels = legacy_levels or {}
    current = float(res.get("current_price", 0) or 0)
    market_type = str(res.get("market_type", "TSE"))
    upper = market_type.upper()
    is_otc = any(token in upper for token in ("OTC", "TWO", "櫃", "上櫃"))
    index_symbol = "TPEx" if is_otc else "TAIEX"

    daily_df = res.get("daily_df")
    if daily_df is not None and not isinstance(daily_df, pd.DataFrame):
        daily_df = pd.DataFrame(daily_df)

    institutional_df = res.get("institutional_df")
    if institutional_df is not None and not isinstance(
        institutional_df, pd.DataFrame
    ):
        institutional_df = pd.DataFrame(institutional_df)

    quote_time = res.get("quote_time")
    if isinstance(quote_time, datetime):
        quote_time = quote_time.isoformat()

    lows = []
    if isinstance(daily_df, pd.DataFrame) and not daily_df.empty:
        low_series = pd.to_numeric(
            daily_df.get("low"),
            errors="coerce",
        ).dropna()
        if not low_series.empty:
            lows = low_series.tail(60).tolist()

    platform_floor = min(lows) if lows else None

    return {
        "symbol": str(res.get("stock_id", "")),
        "company_name": res.get("stock_name"),
        "listing_market": market_type,
        "current_price": current,
        "quote_timestamp": quote_time,
        "quote_source": res.get("rt_source", "legacy app"),
        "quote_valid": bool(res.get("quote_success", False)),
        "stock_bars": _df_bars(daily_df),
        "index_symbol": index_symbol,
        "index_bars": _df_bars(market_index_df),
        "institutional": _institutional_rows(institutional_df),
        "margin": _margin_rows(margin_df),
        "atr": _float_or_none(res.get("atr")),
        "ma20": _float_or_none(res.get("ma20_val")),
        "ma60": _float_or_none(res.get("ma60_val")),
        "recent_resistance": _float_or_none(
            res.get("real_resistance")
        ),
        "breakout_level": _float_or_none(
            levels.get("confirmation")
        ),
        "recent_swing_low": _float_or_none(
            levels.get("protective_stop", res.get("structure_stop"))
        ),
        "platform_floor": _float_or_none(
            levels.get("structure_stop", platform_floor)
        ),
        "original_target": _float_or_none(
            levels.get("target1", res.get("expected_target_price"))
        ),
        "fair_value": _float_or_none(
            levels.get("entry", res.get("expected_entry_price"))
        ),
        "tick_size": _tick_size(current) if current > 0 else 0.5,
        "international_risk_flag": bool(
            res.get("is_us_panic", False)
            or res.get("is_market_panic", False)
        ),
        "data_warnings": tuple(),
    }
