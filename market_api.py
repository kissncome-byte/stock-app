import os
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz
import requests
from fastapi import HTTPException
from FinMind.data import DataLoader
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

TZ = pytz.timezone("Asia/Taipei")
FUGLE_TOKEN = os.getenv("FUGLE_TOKEN", "")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")


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
        raw = finmind_api().taiwan_stock_daily(stock_id=stock_id, start_date=(datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d"))
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
            r = s.get(f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{stock_id}", headers={"X-API-KEY": FUGLE_TOKEN}, timeout=4)
            if r.ok:
                d = r.json().get("data", r.json())
                total = d.get("total", {}) or {}
                px = safe_float(d.get("closePrice")) or safe_float(d.get("referencePrice"))
                if px > 0:
                    return {"stock_id": stock_id, "price": px, "open": safe_float(d.get("openPrice")) or px, "high": safe_float(d.get("highPrice")) or px, "low": safe_float(d.get("lowPrice")) or px, "previous_close": safe_float(d.get("previousClose")) or safe_float(d.get("referencePrice")), "volume_lots": safe_float(total.get("tradeVolume")), "quote_time": total.get("time") or d.get("lastUpdated") or d.get("closeTime"), "source": "Fugle", "volume_valid": safe_float(total.get("tradeVolume")) > 0}
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
                    return {"stock_id": stock_id, "price": px, "open": safe_float(d.get("o")) or px, "high": safe_float(d.get("h")) or px, "low": safe_float(d.get("l")) or px, "previous_close": safe_float(d.get("y")), "volume_lots": vol, "quote_time": f"{d.get('d', '')} {d.get('t', '')}".strip(), "source": f"TWSE-{prefix.upper()}", "volume_valid": vol > 0}
        except Exception:
            continue
    raise HTTPException(status_code=503, detail=f"No live quote available for {stock_id}")


def indicators(df: pd.DataFrame):
    if df.empty or len(df) < 20:
        return {}
    close, high, low = df["close"].astype(float), df["high"].astype(float), df["low"].astype(float)
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
    ema12, ema26 = close.ewm(span=12, adjust=False).mean(), close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    out.update({"macd_dif": round(float(dif.iloc[-1]), 4), "macd_signal": round(float(dea.iloc[-1]), 4), "macd_hist": round(float((dif-dea).iloc[-1]), 4)})
    ll9, hh9 = low.rolling(9).min(), high.rolling(9).max()
    rsv = (close-ll9)/(hh9-ll9).replace(0,np.nan)*100
    k = rsv.ewm(alpha=1/3,adjust=False).mean()
    d = k.ewm(alpha=1/3,adjust=False).mean()
    out["kd_k"] = round(float(k.iloc[-1]),2) if pd.notna(k.iloc[-1]) else None
    out["kd_d"] = round(float(d.iloc[-1]),2) if pd.notna(d.iloc[-1]) else None
    prev_close = close.shift(1)
    tr = pd.concat([(high-low).abs(),(high-prev_close).abs(),(low-prev_close).abs()],axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    out["atr14"] = round(float(atr.iloc[-1]),4) if pd.notna(atr.iloc[-1]) else None
    out.update({"res20":round(float(high.tail(20).max()),4),"sup20":round(float(low.tail(20).min()),4),"history_last_date":str(df.iloc[-1].get("date","")),"history_last_close":round(float(close.iloc[-1]),4)})
    if len(df) >= 60:
        out.update({"res60":round(float(high.tail(60).max()),4),"sup60":round(float(low.tail(60).min()),4)})
    return out


def stock_snapshot(stock_id: str, market: str = "TSE") -> dict:
    sid = str(stock_id).strip()
    if not sid or len(sid) > 10:
        raise ValueError("Invalid stock id")
    q = live_quote(sid, market)
    q["indicators"] = indicators(history(sid))
    q["server_time"] = datetime.now(TZ).isoformat()
    return q


def portfolio_snapshot(stock_ids: list[str], otc_ids: list[str] | None = None) -> dict:
    ids = [str(x).strip() for x in stock_ids if str(x).strip()]
    if not ids or len(ids) > 30:
        raise ValueError("stock_ids must contain 1-30 ids")
    otc_set = {str(x).strip() for x in (otc_ids or []) if str(x).strip()}
    result = []
    for sid in ids:
        try:
            result.append(stock_snapshot(sid, "OTC" if sid in otc_set else "TSE"))
        except Exception as exc:
            result.append({"stock_id": sid, "error": str(exc)})
    return {"server_time": datetime.now(TZ).isoformat(), "count": len(result), "data": result}


mcp = MCPServer(
    "Taiwan Stock Live Market",
    instructions="Read-only Taiwan stock market data for portfolio analysis. Use get_stock_quote for one security and get_portfolio_quotes for multiple securities. Always inspect quote_time, source, and volume_valid before describing data as live. Technical indicators are based on completed daily history; intraday OHLC and volume come from the live quote source. Do not execute trades.",
)


@mcp.tool()
def get_stock_quote(stock_id: str, market: str = "TSE") -> dict:
    """Get a read-only Taiwan stock snapshot with intraday price/OHLC/volume and completed-daily technical indicators. market may be TSE or OTC."""
    return stock_snapshot(stock_id, market)


@mcp.tool()
def get_portfolio_quotes(stock_ids: list[str], otc_ids: list[str] | None = None) -> dict:
    """Get read-only live snapshots and technical indicators for 1-30 Taiwan stock IDs in one call. Put OTC stock IDs in otc_ids."""
    return portfolio_snapshot(stock_ids, otc_ids)


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request):
    return JSONResponse({"ok": True, "service": "Project Compass Market API", "version": "1.1.5", "mcp": "/mcp"})


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"ok": True, "time": datetime.now(TZ).isoformat()})


@mcp.custom_route("/quote/{stock_id}", methods=["GET"])
async def quote(request: Request):
    try:
        return JSONResponse(stock_snapshot(request.path_params["stock_id"], request.query_params.get("market", "TSE")))
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)


@mcp.custom_route("/portfolio", methods=["GET"])
async def portfolio(request: Request):
    try:
        stocks = [x.strip() for x in request.query_params.get("stocks", "").split(",") if x.strip()]
        otc = [x.strip() for x in request.query_params.get("otc", "").split(",") if x.strip()]
        return JSONResponse(portfolio_snapshot(stocks, otc))
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["stock-app-k17f.onrender.com", "stock-app-k17f.onrender.com:*"],
    allowed_origins=["https://chatgpt.com", "https://chat.openai.com"],
)

# Run the MCP Starlette app as the top-level ASGI application. Its own
# lifespan starts/stops the Streamable HTTP session manager correctly.
app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=security,
)
