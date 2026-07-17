from __future__ import annotations
import math
import numpy as np
import pandas as pd


def safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def tick_size(price: float) -> float:
    if price >= 1000: return 5.0
    if price >= 500: return 1.0
    if price >= 100: return 0.5
    if price >= 50: return 0.1
    if price >= 10: return 0.05
    return 0.01


def floor_tick(value: float, tick: float) -> float:
    return math.floor((value + 1e-12) / tick) * tick


def ceil_tick(value: float, tick: float) -> float:
    return math.ceil((value - 1e-12) / tick) * tick


def prepare_indicators(raw: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(raw.columns)
    if missing:
        raise ValueError(f"缺少欄位：{', '.join(sorted(missing))}")
    df = raw.copy().sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close", "high", "low", "volume"])
    for n in [5, 10, 20, 60, 120, 240]:
        df[f"MA{n}"] = df["close"].rolling(n, min_periods=max(3, min(n, 10))).mean()
    df["VOL_MA20"] = df["volume"].rolling(20, min_periods=5).mean()
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR14"] = tr.rolling(14, min_periods=5).mean()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=5).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=5).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI14"] = (100 - (100 / (1 + rs))).fillna(50)
    df["RES20"] = df["high"].rolling(20, min_periods=5).max().shift(1)
    df["SUP20"] = df["low"].rolling(20, min_periods=5).min().shift(1)
    df["RET5"] = df["close"].pct_change(5) * 100
    return df


def slope_pct(series: pd.Series, lookback: int = 5) -> float:
    if len(series.dropna()) < lookback + 1:
        return 0.0
    now = safe_float(series.iloc[-1])
    old = safe_float(series.iloc[-1 - lookback])
    return ((now / old) - 1) * 100 if old else 0.0


def detect_swings(df: pd.DataFrame, window: int = 3) -> dict:
    highs, lows = [], []
    for i in range(window, len(df) - window):
        segment = df.iloc[i-window:i+window+1]
        if df.iloc[i]["high"] >= segment["high"].max():
            highs.append(float(df.iloc[i]["high"]))
        if df.iloc[i]["low"] <= segment["low"].min():
            lows.append(float(df.iloc[i]["low"]))
    result = {"last_high": highs[-1] if highs else None, "prev_high": highs[-2] if len(highs) > 1 else None,
              "last_low": lows[-1] if lows else None, "prev_low": lows[-2] if len(lows) > 1 else None}
    result["higher_high"] = bool(result["last_high"] and result["prev_high"] and result["last_high"] > result["prev_high"])
    result["lower_high"] = bool(result["last_high"] and result["prev_high"] and result["last_high"] < result["prev_high"])
    result["higher_low"] = bool(result["last_low"] and result["prev_low"] and result["last_low"] > result["prev_low"])
    result["lower_low"] = bool(result["last_low"] and result["prev_low"] and result["last_low"] < result["prev_low"])
    return result
