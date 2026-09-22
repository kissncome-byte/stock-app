import os
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz
import requests
from fastapi import FastAPI, HTTPException, Query
from FinMind.data import DataLoader

TZ = pytz.timezone("Asia/Taipei")
FUGLE_TOKEN = os.getenv("FUGLE_TOKEN", "")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

app = FastAPI(title="Project Compass Market API", version="1.0.0")


def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ("", "-", "None", "nan", "NaN"):
            return default
        return float(str(x).replace(",", "").replace("%", "").strip())
    except Exception:
        return default


def finmind_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try:
            api.login_by_token(FINMIND_TOKEN)
        except Exception:
            pass
    return api


def history(stock_id: str, days: int = 240):
    try:
        raw = finmind_api().taiwan_stock_daily(
            stock_id=stock_id,
            start_date=(datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d"),
        )
        if raw is None or raw.empty:
            return pd.DataFrame()
        df = raw.copy().sort_values("date").drop_duplicates("date")
        df = df.rename(columns={"max": "high", "min": "low", "Trading_Volume": "volume"})
        for c in ("open", "high", "low", "close", "volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["close"])
    except Exception:
        return pd.DataFrame()


def live_quote(stock_id: str, market_type: str = "TSE"):
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    if FUGLE_TOKEN:
        try:
            r = s.get(
                f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{stock_id}",
                headers={"X-API-KEY": FUGLE_TOKEN}, timeout=4,
            )
            if r.ok:
                d = r.json().get("data", r.json())
                total = d.get("total", {}) or {}
                px = safe_float(d.get("closePrice")) or safe_float(d.get("referencePrice"))
                if px > 0:
                    return {
                        "stock_id": stock_id,
                        "price": px,
                        "open": safe_float(d.get("openPrice")) or px,
                        "high": safe_float(d.get("highPrice")) or px,
                        "low": safe_float(d.get("lowPrice")) or px,
                        "previous_close": safe_float(d.get("previousClose")) or safe_float(d.get("referencePrice")),
                        "volume_lots": safe_float(total.get("tradeVolume")),
                        "quote_time": total.get("time") or d.get("lastUpdated") or d.get("closeTime"),
                        "source": "Fugle",
                        "volume_valid": safe_float(total.get("tradeVolume")) > 0,
                    }
        except Exception:
            pass

    is_otc = any(x in str(market_type).upper() for x in ("OTC", "TWO", "櫃", "上櫃"))
    for prefix in (["otc", "tse"] if is_otc else ["tse", "otc"]):
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_id}.tw&json=1&delay=0&_={int(time.time()*1000)}"
            r = s.get(url, headers={"Referer": "https://mis.twse.com.tw/"}, timeout=4)
            p = r.json() if r.ok else {}
            if p.get("msgArray"):
                d = p["msgArray"][0]
                px = safe_float(d.get("z")) or safe_float(str(d.get("b", "")).split("_")[0]) or safe_float(d.get("o"))
                if px > 0:
                    vol = safe_float(d.get("v"))
                    return {
                        "stock_id": stock_id,
                        "price": px,
                        "open": safe_float(d.get("o")) or px,
                        "high": safe_float(d.get("h")) or px,
                        "low": safe_float(d.get("l")) or px,
                        "previous_close": safe_float(d.get("y")),
                        "volume_lots": vol,
                        "quote_time": f"{d.get('d', '')} {d.get('t', '')}".strip(),
                        "source": f"TWSE-{prefix.upper()}",
                        "volume_valid": vol > 0,
                    }
        except Exception:
            continue
    raise HTTPException(status_code=503, detail=f"No live quote available for {stock_id}")


def indicators(df: pd.DataFrame):
    if df.empty or len(df) < 20:
        return {}
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float) if "volume" in df.columns else pd.Series(index=df.index, dtype=float)

    out = {}
    for n in (5, 10, 20, 60):
        if len(close) >= n:
            out[f"ma{n}"] = round(float(close.rolling(n).mean().iloc[-1]), 4)
    for n in (5, 20, 60):
        if len(vol.dropna()) >= n:
            out[f"vol_ma{n}"] = round(float(vol.rolling(n).mean().iloc[-1] / 1000.0), 2)

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    out["rsi14"] = round(float(rsi.iloc[-1]), 2) if pd.notna(rsi.iloc[-1]) else None

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    out["macd_dif"] = round(float(dif.iloc[-1]), 4)
    out["macd_signal"] = round(float(dea.iloc[-1]), 4)
    out["macd_hist"] = round(float((dif - dea).iloc[-1]), 4)

    ll9 = low.rolling(9).min()
    hh9 = high.rolling(9).max()
    rsv = (close - ll9) / (hh9 - ll9).replace(0, np.nan) * 100
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()
    out["kd_k"] = round(float(k.iloc[-1]), 2) if pd.notna(k.iloc[-1]) else None
    out["kd_d"] = round(float(d.iloc[-1]), 2) if pd.notna(d.iloc[-1]) else None

    prev_close = close.shift(1)
    tr = pd.concat([(high-low).abs(), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    out["atr14"] = round(float(atr.iloc[-1]), 4) if pd.notna(atr.iloc[-1]) else None

    out["res20"] = round(float(high.tail(20).max()), 4)
    out["sup20"] = round(float(low.tail(20).min()), 4)
    if len(df) >= 60:
        out["res60"] = round(float(high.tail(60).max()), 4)
        out["sup60"] = round(float(low.tail(60).min()), 4)
    out["history_last_date"] = str(df.iloc[-1].get("date", ""))
    out["history_last_close"] = round(float(close.iloc[-1]), 4)
    return out


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(TZ).isoformat()}


@app.get("/quote/{stock_id}")
def quote(stock_id: str, market: str = Query("TSE")):
    q = live_quote(stock_id, market)
    df = history(stock_id)
    q["indicators"] = indicators(df)
    q["server_time"] = datetime.now(TZ).isoformat()
    return q


@app.get("/portfolio")
def portfolio(
    stocks: str = Query(..., description="Comma-separated stock ids"),
    otc: str = Query("", description="Comma-separated OTC stock ids"),
):
    ids = [x.strip() for x in stocks.split(",") if x.strip()]
    otc_ids = {x.strip() for x in otc.split(",") if x.strip()}
    if not ids or len(ids) > 30:
        raise HTTPException(status_code=400, detail="stocks must contain 1-30 ids")
    result = []
    for sid in ids:
        try:
            q = live_quote(sid, "OTC" if sid in otc_ids else "TSE")
            q["indicators"] = indicators(history(sid))
            result.append(q)
        except Exception as exc:
            result.append({"stock_id": sid, "error": str(exc)})
    return {"server_time": datetime.now(TZ).isoformat(), "count": len(result), "data": result}
