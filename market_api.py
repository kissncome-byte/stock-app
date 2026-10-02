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


def intraday_candles(stock_id: str, market: str = "TSE", interval: str = "5",
                     limit: int = 60, resistance: float | None = None) -> dict:
    """Return actual exchange-session bars, without inferring a pattern from a quote snapshot."""
    sid = str(stock_id).strip()
    if not sid.isdigit() or len(sid) > 10:
        raise ValueError("Invalid stock id")
    if interval not in ("1", "5"):
        raise ValueError("interval must be 1 or 5 minutes")
    if not 1 <= limit <= 390:
        raise ValueError("limit must be between 1 and 390")
    if resistance is not None and (not np.isfinite(resistance) or resistance <= 0):
        raise ValueError("resistance must be a positive finite price")

    suffixes = (".TWO", ".TW") if str(market).upper() in ("OTC", "TWO") else (".TW", ".TWO")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    payload, source = None, None
    if FUGLE_TOKEN:
        try:
            response = session.get(
                f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/candles/{sid}",
                headers={"X-API-KEY": FUGLE_TOKEN}, params={"timeframe": interval}, timeout=6,
            )
            if response.ok:
                candidate = response.json()
                if candidate.get("data"):
                    payload, source = candidate, "Fugle"
        except (requests.RequestException, ValueError, TypeError):
            pass

    # The existing app already uses Yahoo chart data as a fallback. Only accept
    # a symbol that exactly matches the requested market suffix and has bars.
    if payload is None:
        for suffix in suffixes:
            try:
                response = session.get(
                    f"https://query2.finance.yahoo.com/v8/finance/chart/{sid}{suffix}",
                    params={"interval": f"{interval}m", "range": "1d"}, timeout=6,
                )
                results = (response.json().get("chart", {}).get("result") or []) if response.ok else []
                if not results:
                    continue
                result = results[0]
                if result.get("meta", {}).get("symbol", "").upper() != f"{sid}{suffix}":
                    continue
                quote = (result.get("indicators", {}).get("quote") or [{}])[0]
                timestamps = result.get("timestamp") or []
                data = [
                    {"date": datetime.fromtimestamp(ts, TZ).isoformat(),
                     "open": quote["open"][i], "high": quote["high"][i],
                     "low": quote["low"][i], "close": quote["close"][i],
                     "volume": quote["volume"][i] / 1000 if quote["volume"][i] is not None else None}
                    for i, ts in enumerate(timestamps)
                ]
                if data:
                    payload, source = {"data": data}, f"Yahoo Finance {sid}{suffix}"
                    break
            except (requests.RequestException, ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
                continue
    if payload is None:
        raise HTTPException(status_code=503, detail=f"No intraday candles available for {sid}")

    bars = []
    for raw in payload["data"]:
        try:
            bar_time = datetime.fromisoformat(raw["date"].replace("Z", "+00:00")).astimezone(TZ)
            if source.startswith("Yahoo") and (bar_time.minute % int(interval) or bar_time.second or bar_time.microsecond):
                continue  # Yahoo can append a non-aligned, zero-volume quote as a partial bar.
            prices = [float(raw[k]) for k in ("open", "high", "low", "close")]
            volume = float(raw["volume"])
            if not all(np.isfinite(x) and x > 0 for x in prices) or not np.isfinite(volume) or volume < 0:
                continue
            bars.append({"time": bar_time.isoformat(), "open": prices[0], "high": prices[1],
                         "low": prices[2], "close": prices[3], "volume_lots": volume})
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if not bars:
        raise HTTPException(status_code=503, detail=f"No valid intraday candles available for {sid}")
    bars.sort(key=lambda bar: bar["time"])
    session_date = bars[-1]["time"][:10]
    bars = [bar for bar in bars if bar["time"].startswith(session_date)]
    # A bar beginning within the current interval may still change. Never
    # count it as a confirmed breakout or failure.
    now = datetime.now(TZ)
    selected = bars[-limit:]
    confirmed = [bar for bar in selected if datetime.fromisoformat(bar["time"]) + timedelta(minutes=int(interval)) <= now]
    last_bar_age_minutes = round((now - datetime.fromisoformat(selected[-1]["time"])).total_seconds() / 60, 1)
    delay_minutes = round(max(0.0, last_bar_age_minutes - int(interval)), 1)
    is_current_session = session_date == now.date().isoformat()
    output = {"stock_id": sid, "market_requested": market, "interval_minutes": int(interval),
              "source": source, "session_date": session_date, "is_current_session": is_current_session,
              "server_time": now.isoformat(), "volume_unit": "lots", "bars": selected,
              "bar_count": len(selected), "confirmed_bar_count": len(confirmed),
              "last_bar_age_minutes": last_bar_age_minutes, "estimated_data_delay_minutes": delay_minutes,
              "is_recent_for_intraday_decisions": is_current_session and delay_minutes <= 10,
              "last_bar_complete": selected[-1] in confirmed}
    if resistance is not None:
        events = []
        for bar in confirmed:
            if bar["high"] >= resistance:
                events.append({"time": bar["time"], "high": bar["high"], "close": bar["close"],
                               "volume_lots": bar["volume_lots"],
                               "closed_above": bar["close"] >= resistance})
        output["resistance_review"] = {
            "level": resistance, "touch_bars": events, "touch_bar_count": len(events),
            "closes_above_count": sum(event["closed_above"] for event in events),
            "closed_back_below_after_breakout": any(
                confirmed[i]["close"] < resistance and
                any(previous["close"] >= resistance for previous in confirmed[:i])
                for i in range(len(confirmed))
            ),
            "latest_confirmed_close_above": confirmed[-1]["close"] >= resistance if confirmed else None,
            "note": "觸及根數不等於獨立挑戰次數；請依 K 線間隔、收盤與成交量判讀。",
        }
    return output


mcp = MCPServer(
    "Taiwan Stock Live Market",
    instructions="Read-only Taiwan stock market data for portfolio analysis. Use get_stock_quote for one security, get_portfolio_quotes for multiple securities, and get_intraday_candles to inspect actual 1/5-minute OHLCV and resistance tests. Always inspect quote_time, source, is_current_session, is_recent_for_intraday_decisions, estimated_data_delay_minutes, and volume validity before describing data as live. Never give current intraday signals from delayed bars. The latest intraday bar may be incomplete; resistance_review counts touching bars, not independent attempts. Technical indicators are based on completed daily history. Do not execute trades.",
)


@mcp.tool()
def get_stock_quote(stock_id: str, market: str = "TSE") -> dict:
    """Get a read-only Taiwan stock snapshot with intraday price/OHLC/volume and completed-daily technical indicators. market may be TSE or OTC."""
    return stock_snapshot(stock_id, market)


@mcp.tool()
def get_portfolio_quotes(stock_ids: list[str], otc_ids: list[str] | None = None) -> dict:
    """Get read-only live snapshots and technical indicators for 1-30 Taiwan stock IDs in one call. Put OTC stock IDs in otc_ids."""
    return portfolio_snapshot(stock_ids, otc_ids)


@mcp.tool()
def get_intraday_candles(stock_id: str, market: str = "TSE", interval: str = "5",
                         limit: int = 60, resistance: float | None = None) -> dict:
    """Get today's 1/5-minute OHLCV (lots) and optional resistance touches and reclaim checks. Check session date, delay, recency, and last_bar_complete."""
    return intraday_candles(stock_id, market, interval, limit, resistance)


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request):
    return JSONResponse({"ok": True, "service": "Project Compass Market API", "version": "1.2.1", "mcp": "/mcp"})


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
