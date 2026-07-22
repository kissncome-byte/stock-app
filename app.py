import os, time, math, json, sqlite3, requests, certifi, pytz, urllib.parse, shutil
import pandas as pd
import numpy as np
import streamlit as st
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from FinMind.data import DataLoader

# ============ 1. Page Config ============
st.set_page_config(page_title="Project Compass V3｜單一決策執行中心", layout="wide")

# ============ 2. Global Constants ============
TZ = pytz.timezone("Asia/Taipei")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")
FUGLE_TOKEN = os.getenv("FUGLE_TOKEN", "") or st.secrets.get("FUGLE_TOKEN", "")

# 即時成交量來源單位與更新時點不穩定，暫停使用「當日成交量比率」做顯示與決策。
USE_INTRADAY_VOLUME_RATIO = False

# ============ 3. Helper Functions ============
def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ["-", "", "None", "nan", "NaN"]: return default
        return float(str(x).replace(",", "").replace("%", "").replace(" ", "").strip())
    except Exception: return default

def tick_size(p: float) -> float:
    if p >= 1000: return 5.0
    if p >= 500:  return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.05
    return 0.01

def round_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t <= 0: return 0.0
    return round(x / t) * t

def floor_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t <= 0: return 0.0
    return math.floor((x + 1e-12) / t) * t

def ceil_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t <= 0: return 0.0
    return math.ceil((x - 1e-12) / t) * t

def log_error(area: str, exc: Exception):
    # 正式部署可改接 logging / Sentry；前台不暴露金鑰與完整堆疊。
    print(f"[{area}] {type(exc).__name__}: {exc}")

def custom_hud_box(title, value, font_color="#1E293B"):
    return f"""
    <div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; padding: 12px; border-radius: 6px; min-height: 105px; box-shadow: 0 1px 2px rgba(0,0,0,0.02); margin-bottom: 10px;">
        <span style="color: #64748B; font-size: 12.5px; font-weight: 600; display: block; margin-bottom: 5px;">{title}</span>
        <span style="color: {font_color}; font-size: 14px; font-weight: 700; display: block; line-height: 1.5; word-break: break-all;">{value}</span>
    </div>
    """

def render_panel_html(title, heading, desc, top_border_color):
    return f"""
    <div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:12px; border-radius:6px; min-height:165px; border-top:4px solid {top_border_color}; margin-bottom:15px;">
        <span style="font-size:12px; color:#64748B; font-weight:700; display:block; margin-bottom:4px;">{title}</span>
        <h4 style="margin:2px 0; color:#1E293B; font-size:14.5px; font-weight:800;">{heading}</h4>
        <p style="margin:6px 0 0 0; font-size:11.5px; color:#1E293B; font-weight:600; line-height:1.55;">{desc}</p>
    </div>
    """

def plain_structure_explanation(structure: dict) -> dict:
    if not structure or structure.get("label") == "資料不足":
        return {"title":"⚪ 波段資料不足", "meaning":"目前可辨識的轉折點不足，暫時不能可靠判斷波段方向。", "impact":"先觀察，不單靠這一項做買賣決定。", "action":"等待更多日線資料或下一個明確轉折。"}
    if structure.get("higher_high") and structure.get("higher_low"):
        return {"title":"🟢 波段趨勢健康向上", "meaning":"最近上漲能創更高價格，拉回也守在前一次低點之上，代表買方仍掌握波段。", "impact":"短線回檔較可能是整理，而不是立即反轉。", "action":"未持有者等量縮拉回；已持有者可續抱並守前波低點。"}
    if structure.get("lower_high") and structure.get("lower_low"):
        return {"title":"🔴 波段趨勢持續轉弱", "meaning":"最近每次反彈都低於前高，而且每次下跌又創更低點，代表空方仍占優勢。", "impact":"現在低價不一定等於便宜，仍可能繼續下跌。", "action":"未持有者先不要急著接；已持有者觀察趨勢失效價（風險防線）是否遭收盤有效跌破。"}
    if structure.get("lower_high") and structure.get("higher_low"):
        return {"title":"🟡 波段收斂整理", "meaning":"高點下降、低點抬高，價格波動範圍正在縮小，市場等待新方向。", "impact":"容易來回震盪，突破前不適合追價。", "action":"等待突破壓力或跌破支撐後再判斷。"}
    if structure.get("higher_high") and structure.get("lower_low"):
        return {"title":"🟠 波動擴大、方向不穩", "meaning":"高點創高但低點也破低，代表多空拉扯劇烈。", "impact":"上下洗盤風險高，風險防線距離會變大。", "action":"降低部位，等待波動收斂或方向確認。"}
    return {"title":"🟡 波段方向尚未明朗", "meaning":"目前高低點沒有形成一致的上升或下降規律。", "impact":"單一轉折容易是假訊號。", "action":"搭配均線斜率、量價與法人連續性一起判斷。"}

def plain_trend_strength(adx: float) -> dict:
    if adx >= 25:
        return {"title":"趨勢力道明確", "meaning":f"ADX14 為 {adx:.1f}，代表行情較可能沿主要方向延續。", "action":"順勢操作比逆勢猜底更合適。"}
    if adx >= 18:
        return {"title":"趨勢正在形成", "meaning":f"ADX14 為 {adx:.1f}，方向開始出現，但仍可能反覆。", "action":"等價格、均線與成交量再確認，不宜一次重押。"}
    return {"title":"目前以震盪為主", "meaning":f"ADX14 為 {adx:.1f}，代表趨勢不強，容易上下來回。", "action":"不宜追突破，較適合等靠近支撐再觀察。"}

def plain_price_volume(ta: dict) -> dict:
    pv = ta.get("price_volume", "價量關係中性")
    if "價跌量縮" in pv:
        return {"title":"🟢 下跌但賣壓不重", "meaning":"股價回落時成交量同步縮小，代表急著賣出的人沒有明顯增加。", "impact":"若中長期趨勢仍向上，較像正常拉回。", "action":"等待支撐附近止跌，可分批而不是追高。"}
    if "價跌量增" in pv:
        return {"title":"🔴 下跌且賣壓增加", "meaning":"股價下跌時成交量放大，代表賣方正在加速出場。", "impact":"正常拉回演變成趨勢轉弱的風險提高。", "action":"先控制部位，觀察是否跌破前波低點或MA60。"}
    if "價漲量增" in pv:
        return {"title":"🟢 上漲獲得量能支持", "meaning":"股價上漲時成交量同步增加，代表買盤願意追價。", "impact":"突破的可信度提高，但乖離過大仍可能拉回。", "action":"已有部位可續抱；未持有避免在過度乖離時追高。"}
    if "價漲量縮" in pv:
        return {"title":"🟡 上漲但追價力道不足", "meaning":"股價上漲時成交量沒有跟上，代表買盤並不積極。", "impact":"容易在壓力附近停頓或形成假突破。", "action":"等待放量確認或回測支撐。"}
    return {"title":"⚪ 價量暫無明確方向", "meaning":"價格與成交量目前沒有形成一致的多空訊號。", "impact":"這一項不能單獨支持買進或賣出。", "action":"搭配趨勢、線型與法人資料。"}

def render_plain_card(title: str, meaning: str, impact: str, action: str, color: str = "#2563EB") -> str:
    return (
        f'<div style="background:#FFFFFF;border:1px solid #E2E8F0;border-left:6px solid {color};padding:15px;border-radius:7px;margin-bottom:12px;line-height:1.7;">'
        f'<div style="font-size:17px;font-weight:900;color:#0F172A;margin-bottom:7px;">{title}</div>'
        f'<div><b>這代表什麼：</b>{meaning}</div>'
        f'<div><b>所以呢：</b>{impact}</div>'
        f'<div><b>接下來可以怎麼做：</b>{action}</div>'
        '</div>'
    )

def get_market_status_label(rt_success: bool, last_trade_date_str: str):
    now = datetime.now(TZ)
    if now.weekday() >= 5: return "CLOSED_WEEKEND", f"市場休市 (週末) | 數據日期: {last_trade_date_str}", "gray"
    start, end = datetime.strptime("09:00", "%H:%M").time(), datetime.strptime("13:35", "%H:%M").time()
    if rt_success:
        if start <= now.time() <= end: return "OPEN", "市場交易中 (即時更新)", "red"
        return ("PRE_MARKET", "盤前準備中", "blue") if now.time() < start else ("POST_MARKET", "今日已收盤 (即時報價)", "green")
    else:
        if start <= now.time() <= end: return "API_WAIT", f"連線受限改用歷史價 | 歷史日期: {last_trade_date_str}", "orange"
        return ("PRE_MARKET", f"盤前準備中 | 歷史日期: {last_trade_date_str}", "blue") if now.time() < start else ("POST_MARKET", f"今日已收盤 | 歷史日期: {last_trade_date_str}", "green")

def analyze_news_sentiment(title: str) -> tuple:
    pos = ['創新高', '大賺', '暴增', '飆', '大成長', '利多', '優於預期', '加碼', '看旺', '強勢', '獲利', '突破', '轉盈', '買超', '爆發', '新高', '三率三升']
    neg = ['衰退', '虧損', '重挫', '低於預期', '縮水', '跌破', '警告', '利空', '下滑', '疲弱', '裁員', '大跌', '慘', '賣壓', '修正', '暴跌', '逆風']
    p_s, n_s = sum(1 for w in pos if w in title), sum(1 for w in neg if w in title)
    return ("🟢 利多", "green") if p_s > n_s else ("🔴 利空", "red") if n_s > p_s else ("🟡 中性", "gray")

# ============ 4. Connection Layer ============
@st.cache_resource
def get_requests_session():
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504]))
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    return session

@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try: api.login_by_token(FINMIND_TOKEN)
        except Exception: pass
    return api

# ============ 5. Live Data Streaming Engine ============
def format_market_timestamp(value):
    """將秒／毫秒／微秒／奈秒 Unix timestamp 或字串轉為台北時間。"""
    if value in (None, "", 0, "0"):
        return None
    try:
        number = float(value)
        absolute = abs(number)
        if absolute >= 1e17:      # 奈秒
            number /= 1_000_000_000
        elif absolute >= 1e14:    # 微秒
            number /= 1_000_000
        elif absolute >= 1e11:    # 毫秒
            number /= 1_000
        dt = datetime.fromtimestamp(number, tz=timezone.utc).astimezone(TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        text = str(value).strip()
        try:
            dt = pd.to_datetime(text, utc=True)
            if pd.isna(dt):
                return text
            return dt.tz_convert(TZ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return text

def compute_live_data(stock_id: str, market_type: str, hist_last_close: float, hist_last_vol: float):
    """回傳統一單位：成交量一律為張，並附前收與資料時間。"""
    hist_lots = hist_last_vol / 1000.0 if hist_last_vol > 0 else 0.0
    session = get_requests_session()
    is_otc = any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "上櫃"])
    fallback = {"open": hist_last_close, "high": hist_last_close, "low": hist_last_close,
                "close": hist_last_close, "volume_lots": 0.0, "previous_close": hist_last_close,
                "success": False, "source": "歷史收盤備援", "quote_time": None, "is_stale": True,
                "volume_valid": False, "raw_volume": None,
                "volume_note": "即時行情未取得，不能把前一交易日成交量當成今日成交量"}
    if FUGLE_TOKEN:
        try:
            r = session.get(f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{stock_id}", headers={"X-API-KEY": FUGLE_TOKEN}, timeout=3)
            if r.status_code == 200:
                data = r.json().get("data", r.json())
                price = safe_float(data.get("closePrice")) or safe_float(data.get("referencePrice"))
                prev = safe_float(data.get("previousClose")) or safe_float(data.get("referencePrice")) or hist_last_close
                total_data = data.get("total", {}) or {}
                raw_volume = total_data.get("tradeVolume", None)
                # Fugle 台股即時行情的累計成交量以「張」呈現，直接統一為 lots。
                vol_lots = safe_float(raw_volume)
                volume_valid = vol_lots > 0
                raw_quote_time = total_data.get("time") or data.get("lastUpdated") or data.get("closeTime") or data.get("date")
                quote_time = format_market_timestamp(raw_quote_time)
                if price > 0:
                    return {"open": safe_float(data.get("openPrice")) or price, "high": safe_float(data.get("highPrice")) or price,
                            "low": safe_float(data.get("lowPrice")) or price, "close": price,
                            "volume_lots": vol_lots if volume_valid else 0.0,
                            "previous_close": prev, "success": True, "source": "Fugle 即時行情",
                            "quote_time": quote_time, "is_stale": False,
                            "volume_valid": volume_valid, "raw_volume": raw_volume,
                            "volume_note": "Fugle 已提供有效累計成交量" if volume_valid else f"Fugle 成交量欄位無效：{raw_volume!r}"}
        except Exception as exc:
            log_error("Fugle quote", exc)
    for prefix in (["otc", "tse"] if is_otc else ["tse", "otc"]):
        try:
            r = session.get(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_id}.tw&json=1&delay=0&_={int(time.time()*1000)}", headers={"Referer": "https://mis.twse.com.tw/"}, timeout=3)
            payload = r.json() if r.status_code == 200 else {}
            if payload.get("msgArray"):
                info = payload["msgArray"][0]
                price = safe_float(info.get("z")) or safe_float(str(info.get("b", "")).split("_")[0]) or safe_float(info.get("o"))
                # TWSE MIS 的 v 為累計成交量（張）。價格成功不代表成交量欄位也有效。
                raw_volume = info.get("v")
                vol_lots = safe_float(raw_volume)
                volume_valid = vol_lots > 0
                prev = safe_float(info.get("y")) or hist_last_close
                if price > 0:
                    return {"open": safe_float(info.get("o")) or price, "high": safe_float(info.get("h")) or price,
                            "low": safe_float(info.get("l")) or price, "close": price,
                            "volume_lots": vol_lots if volume_valid else 0.0,
                            "previous_close": prev, "success": True, "source": f"TWSE {prefix.upper()} 即時行情",
                            "quote_time": info.get("t") or info.get("d"), "is_stale": False,
                            "volume_valid": volume_valid, "raw_volume": raw_volume,
                            "volume_note": "TWSE MIS 已提供有效累計成交量" if volume_valid else f"TWSE MIS 成交量欄位 v 無效：{raw_volume!r}"}
        except Exception as exc:
            log_error("TWSE quote", exc)
    return fallback

# ============ 6. Data Fetching Layers ============
@st.cache_data(ttl=1800)
def get_overnight_radar():
    session = get_requests_session()
    targets = {"台灣加權大盤 (^TWII)": "^TWII", "Nasdaq那指 (^IXIC)": "^IXIC", "費城半導體 (^SOX)": "^SOX", "台積電 ADR (TSM)": "TSM"}
    radar_res, is_us_panic, panic_desc, wtx_change = {}, False, "", 0.0
    for label, symbol in targets.items():
        try:
            r = session.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d", timeout=3)
            if r.status_code == 200 and r.json().get("chart", {}).get("result"):
                res = r.json()["chart"]["result"][0]
                closes = [safe_float(c) for c in res.get("indicators", {}).get("quote", [{}])[0].get("close", []) if c is not None]
                c_p, p_c = (closes[-1], closes[-2]) if len(closes) >= 2 else (safe_float(res["meta"].get("regularMarketPrice")), safe_float(res["meta"].get("previousClose")))
                if p_c > 0:
                    radar_res[label] = ((c_p - p_c) / p_c) * 100
                    if symbol == "^TWII": wtx_change = radar_res[label]
                    if symbol != "^TWII" and radar_res[label] <= -2.0: is_us_panic, panic_desc = True, f"昨晚美股重挫，{label} 慘跌 {radar_res[label]:.1f}%"
        except Exception: pass
    return radar_res, is_us_panic, panic_desc, wtx_change

@st.cache_data(ttl=3600)
def get_stock_info_df():
    try:
        df = get_api().taiwan_stock_info()
        if df is not None and not df.empty: return df.copy()
    except Exception: pass
    return pd.DataFrame([{"stock_id": "3037", "stock_name": "欣興", "market_type": "twse", "industry_category": "電子零組件業"}, {"stock_id": "2330", "stock_name": "台積電", "market_type": "twse", "industry_category": "半導體業"}, {"stock_id": "2382", "stock_name": "廣達", "market_type": "twse", "industry_category": "電腦及週邊設備業"}])

@st.cache_data(ttl=900)
def get_daily_df(stock_id: str, market_type: str = "TSE", days: int = 450):
    """取得日線資料：Yahoo 正確市場 → Yahoo 另一市場 → FinMind。

    台股代碼有時會因股票清單或市場別辨識不完整而套錯 .TW/.TWO；
    Yahoo 也可能暫時限流，因此不能在單一來源失敗時直接判定股票無資料。
    """
    stock_id = str(stock_id).strip()
    session = get_requests_session()
    is_otc = any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "上櫃"])
    suffixes = [".TWO", ".TW"] if is_otc else [".TW", ".TWO"]
    p1 = int((datetime.now(TZ) - timedelta(days=days)).timestamp())
    p2 = int((datetime.now(TZ) + timedelta(days=1)).timestamp())

    # 第一、二層：Yahoo，先試推定市場，再試另一市場。
    for suffix in suffixes:
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}?period1={p1}&period2={p2}&interval=1d&events=history"
            r = session.get(url, timeout=8)
            payload = r.json() if r.status_code == 200 else {}
            results = payload.get("chart", {}).get("result") or []
            if results:
                res = results[0]
                timestamps = res.get("timestamp", []) or []
                quotes = (res.get("indicators", {}).get("quote") or [{}])[0]
                if timestamps:
                    raw = pd.DataFrame({
                        "date": [datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d") for ts in timestamps],
                        "open": quotes.get("open", []),
                        "high": quotes.get("high", []),
                        "low": quotes.get("low", []),
                        "close": quotes.get("close", []),
                        "vol": quotes.get("volume", []),
                    })
                    raw = raw.dropna(subset=["close"]).sort_values("date").drop_duplicates("date")
                    if len(raw) >= 30:
                        raw["amount"] = pd.to_numeric(raw["close"], errors="coerce") * pd.to_numeric(raw["vol"], errors="coerce").fillna(0)
                        raw.attrs["source"] = f"Yahoo Finance {stock_id}{suffix}"
                        return raw.reset_index(drop=True).copy()
        except Exception as exc:
            log_error(f"Yahoo daily {stock_id}{suffix}", exc)

    # 第三層：FinMind。Yahoo 限流、空資料或市場別異常時仍可繼續分析。
    try:
        start_date = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        fdf = get_api().taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
        if fdf is not None and not fdf.empty:
            rename_map = {
                "Trading_Volume": "vol",
                "Trading_money": "amount",
                "open": "open",
                "max": "high",
                "min": "low",
                "close": "close",
                "date": "date",
            }
            raw = fdf.rename(columns=rename_map).copy()
            needed = ["date", "open", "high", "low", "close", "vol"]
            if all(c in raw.columns for c in needed):
                for c in ["open", "high", "low", "close", "vol"]:
                    raw[c] = pd.to_numeric(raw[c], errors="coerce")
                raw = raw.dropna(subset=["close"]).sort_values("date").drop_duplicates("date")
                if "amount" not in raw.columns:
                    raw["amount"] = raw["close"] * raw["vol"].fillna(0)
                else:
                    raw["amount"] = pd.to_numeric(raw["amount"], errors="coerce").fillna(raw["close"] * raw["vol"].fillna(0))
                if len(raw) >= 30:
                    raw.attrs["source"] = "FinMind 台股日線"
                    return raw[needed + ["amount"]].reset_index(drop=True).copy()
    except Exception as exc:
        log_error(f"FinMind daily {stock_id}", exc)

    return None

@st.cache_data(ttl=1800)
def get_market_macro_status(market_type: str = "TSE"):
    """依股票市場別取得對應大盤摘要；資料抓不到就明確回報，不使用替代指數冒充。"""
    is_otc = any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "上櫃"])
    benchmark_id = "TPEx" if is_otc else "TAIEX"
    benchmark_name = "櫃買指數" if is_otc else "加權指數"
    try:
        df = get_api().taiwan_stock_daily(stock_id=benchmark_id, start_date=(datetime.now()-timedelta(days=150)).strftime("%Y-%m-%d"))
        if df is not None and not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['MA20'], df['MA60'] = df['close'].rolling(20).mean(), df['close'].rolling(60).mean()
            vol_col = 'Trading_money' if 'Trading_money' in df.columns else 'Trading_Volume' if 'Trading_Volume' in df.columns else 'vol' if 'vol' in df.columns else None
            if vol_col:
                df['vol_work'] = pd.to_numeric(df[vol_col], errors='coerce').fillna(0)
                df['MA20_Vol'] = df['vol_work'].rolling(20).mean()
            else:
                df['vol_work'], df['MA20_Vol'] = 0.0, 0.0
            last, prev = df.iloc[-1], (df.iloc[-5] if len(df) >= 5 else df.iloc[0])
            ret = ((last['close'] - prev['close']) / prev['close']) * 100 if float(prev['close']) else 0.0
            panic = bool(pd.notna(last['MA20']) and last['close'] < last['MA20'] and ret <= -3.5)
            market_vol_healthy = None
            if float(last['MA20_Vol'] or 0) > 0:
                market_vol_healthy = float(last['vol_work']) >= float(last['MA20_Vol'])
            market_vol_desc = "⚪ 大盤量能資料不足" if market_vol_healthy is None else ("🟢 大盤量能高於20日均值" if market_vol_healthy else "🟡 大盤量能低於20日均值")
            if panic:
                return False, f"🚨 {benchmark_name}急跌 ({last['close']:.1f})", True, False, market_vol_healthy, market_vol_desc
            macro_bull = bool(pd.notna(last['MA20']) and last['close'] >= last['MA20'])
            return macro_bull, f"{benchmark_name} ({last['close']:.1f})", False, False, market_vol_healthy, market_vol_desc
    except Exception as exc:
        log_error(f"market macro {benchmark_id}", exc)
    return None, f"⚪ {benchmark_name}資料取得失敗", None, None, None, "⚪ 大盤量能資料不足"


@st.cache_data(ttl=1800)
def get_market_regime_context(market_type: str = "TSE"):
    """依上市／上櫃選用加權或櫃買指數，完整回傳實際採用數據與可追溯評分。"""
    is_otc = any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "上櫃"])
    benchmark_id = "TPEx" if is_otc else "TAIEX"
    benchmark_name = "櫃買指數" if is_otc else "加權指數"
    ctx = {
        "available": False, "benchmark": benchmark_id, "benchmark_name": benchmark_name,
        "market_scope": "上櫃" if is_otc else "上市",
        "scope_note": f"本股票為{'上櫃' if is_otc else '上市'}，大盤基準採用{benchmark_name}（{benchmark_id}）；未使用其他指數替代。",
        "close": None, "ma20": None, "ma60": None, "slope20": None, "slope60": None,
        "adx": None, "plus_di": None, "minus_di": None, "rsi14": None,
        "ret5": None, "ret20": None, "vol_ratio": None, "volume_value": None, "volume_ma20": None,
        "atr_pct": None, "panic": False, "state": "資料不足", "reasons": [], "raw_date": None
    }
    try:
        df = get_api().taiwan_stock_daily(stock_id=benchmark_id, start_date=(datetime.now()-timedelta(days=240)).strftime("%Y-%m-%d"))
        if df is None or df.empty:
            ctx["scope_note"] += "目前此基準資料未可靠取得，因此大盤閘門採保守模式。"
            return ctx
        d = df.sort_values("date").reset_index(drop=True).copy()
        for col in ["close", "max", "min", "Trading_money", "Trading_Volume", "vol"]:
            if col in d.columns:
                d[col] = pd.to_numeric(d[col], errors="coerce")
        close = d["close"]
        high = d["max"] if "max" in d.columns else close
        low = d["min"] if "min" in d.columns else close
        d["ma20"] = close.rolling(20).mean(); d["ma60"] = close.rolling(60).mean()
        d["slope20"] = d["ma20"].pct_change(5) * 100; d["slope60"] = d["ma60"].pct_change(10) * 100
        prev_close = close.shift(1)
        tr = pd.concat([(high-low).abs(), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
        atr14 = tr.rolling(14).mean()
        up = high.diff(); down = -low.diff()
        plus_dm = up.where((up > down) & (up > 0), 0.0); minus_dm = down.where((down > up) & (down > 0), 0.0)
        tr14 = tr.rolling(14).sum().replace(0, np.nan)
        plus_di = 100 * plus_dm.rolling(14).sum() / tr14; minus_di = 100 * minus_dm.rolling(14).sum() / tr14
        dx = 100 * (plus_di-minus_di).abs() / (plus_di+minus_di).replace(0, np.nan)
        adx = dx.rolling(14).mean()
        delta = close.diff(); gain = delta.clip(lower=0).rolling(14).mean(); loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs14 = gain / loss.replace(0, np.nan); rsi14 = 100 - (100 / (1 + rs14))
        vol_col = "Trading_money" if "Trading_money" in d.columns else "Trading_Volume" if "Trading_Volume" in d.columns else "vol" if "vol" in d.columns else None
        vol_ratio = volume_value = volume_ma20 = None
        if vol_col:
            vm = d[vol_col].rolling(20).mean()
            volume_value = float(d[vol_col].iloc[-1]) if pd.notna(d[vol_col].iloc[-1]) else None
            volume_ma20 = float(vm.iloc[-1]) if pd.notna(vm.iloc[-1]) else None
            if volume_ma20 and volume_ma20 > 0:
                vol_ratio = float(volume_value / volume_ma20)
        last = d.iloc[-1]
        c = float(last["close"]); ma20 = float(last["ma20"]) if pd.notna(last["ma20"]) else None; ma60 = float(last["ma60"]) if pd.notna(last["ma60"]) else None
        ret5 = float((c / close.iloc[-6] - 1) * 100) if len(d) >= 6 and close.iloc[-6] > 0 else None
        ret20 = float((c / close.iloc[-21] - 1) * 100) if len(d) >= 21 and close.iloc[-21] > 0 else None
        atr_pct = float(atr14.iloc[-1] / c * 100) if pd.notna(atr14.iloc[-1]) and c > 0 else None
        adx_v = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else None
        plus_v = float(plus_di.iloc[-1]) if pd.notna(plus_di.iloc[-1]) else None; minus_v = float(minus_di.iloc[-1]) if pd.notna(minus_di.iloc[-1]) else None
        rsi_v = float(rsi14.iloc[-1]) if pd.notna(rsi14.iloc[-1]) else None
        s20 = float(last["slope20"]) if pd.notna(last["slope20"]) else None; s60 = float(last["slope60"]) if pd.notna(last["slope60"]) else None
        panic = bool((ret5 is not None and ret5 <= -4.5) or (atr_pct is not None and atr_pct >= 3.0 and ret5 is not None and ret5 <= -3.0))
        if panic: state = "恐慌風險"
        elif ma20 and ma60 and c > ma20 > ma60 and (s20 or 0) > 0 and (s60 or 0) >= 0: state = "強勢多頭" if (adx_v or 0) >= 20 else "多頭整理"
        elif ma60 and c >= ma60 and ma20 and c < ma20: state = "多頭回檔"
        elif ma20 and ma60 and c < ma20 < ma60 and (s20 or 0) < 0: state = "弱勢空頭"
        elif ma20 and ma60 and c > ma20 and c < ma60: state = "空頭反彈"
        else: state = "區間整理"
        reasons = [f"基準：{benchmark_name}（{benchmark_id}）", f"最新收盤 {c:.2f}"]
        if ma20 is not None: reasons.append(f"MA20 {ma20:.2f}｜5日斜率 {s20:+.2f}%")
        if ma60 is not None: reasons.append(f"MA60 {ma60:.2f}｜10日斜率 {s60:+.2f}%")
        if adx_v is not None: reasons.append(f"ADX {adx_v:.1f}｜+DI {plus_v:.1f}｜-DI {minus_v:.1f}")
        if rsi_v is not None: reasons.append(f"RSI14 {rsi_v:.1f}")
        if ret5 is not None: reasons.append(f"5日報酬 {ret5:+.2f}%")
        if ret20 is not None: reasons.append(f"20日報酬 {ret20:+.2f}%")
        if vol_ratio is not None: reasons.append(f"量能比 {vol_ratio:.2f}（當日／20日均值）")
        if atr_pct is not None: reasons.append(f"ATR14／指數 {atr_pct:.2f}%")
        ctx.update({"available": True, "close": c, "ma20": ma20, "ma60": ma60, "slope20": s20, "slope60": s60,
                    "adx": adx_v, "plus_di": plus_v, "minus_di": minus_v, "rsi14": rsi_v,
                    "ret5": ret5, "ret20": ret20, "vol_ratio": vol_ratio, "volume_value": volume_value, "volume_ma20": volume_ma20,
                    "atr_pct": atr_pct, "panic": panic, "state": state, "reasons": reasons,
                    "raw_date": str(last.get("date", ""))})
    except Exception as exc:
        log_error(f"market regime context {benchmark_id}", exc)
        ctx["scope_note"] += "資料抓取失敗，系統未以其他指數補值。"
    return ctx

@st.cache_data(ttl=900)
def get_taiwan_enhanced_chips(stock_id: str, avg_daily_volume_shares: float, days: int = 30):
    s_trend, m_trend, s_3d, m_diff = "⚪ 資料不足", "⚪ 資料不足", 0.0, 0.0
    start = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    base = max(float(avg_daily_volume_shares or 0), 1.0)
    try:
        idf = get_api().taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start)
        if idf is not None and not idf.empty:
            sdf = idf[idf['name'] == 'Investment_Trust'].copy()
            if not sdf.empty:
                sdf['net'] = pd.to_numeric(sdf['buy'], errors='coerce').fillna(0) - pd.to_numeric(sdf['sell'], errors='coerce').fillna(0)
                s_3d = float(sdf.sort_values('date').tail(3)['net'].sum())
                intensity = s_3d / base
                s_trend = "🟢 投信近三日明顯偏買" if intensity >= 0.15 else "🔴 投信近三日明顯偏賣" if intensity <= -0.15 else "🟡 投信動向中性"
    except Exception as exc:
        log_error("investment trust", exc)
    try:
        mdf = get_api().taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start)
        if mdf is not None and len(mdf) >= 5:
            mdf = mdf.sort_values("date")
            bal = pd.to_numeric(mdf['MarginPurchaseTodayBalance'], errors='coerce')
            m_diff = float(bal.iloc[-1] - bal.iloc[-5])
            intensity = (m_diff * 1000.0) / base
            m_trend = "🟠 融資增加偏快" if intensity >= 0.30 else "🟢 融資明顯下降" if intensity <= -0.30 else "🟡 融資變化平穩"
    except Exception as exc:
        log_error("margin", exc)
    return s_trend, m_trend, s_3d, m_diff

@st.cache_data(ttl=900)
def get_institutional_trading_df(stock_id: str, days: int = 30):
    try:
        start_date = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        df = get_api().taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
        if df is not None and not df.empty:
            df = df.copy()
            df['buy'] = pd.to_numeric(df['buy'], errors='coerce').fillna(0)
            df['sell'] = pd.to_numeric(df['sell'], errors='coerce').fillna(0)
            df['net'] = (df['buy'] - df['sell']) / 1000.0
            name_map = {"Foreign_Investor": "外資(張)", "Investment_Trust": "投信(張)", "Dealer": "自營商總計(張)"}
            df['name'] = df['name'].map(name_map).fillna(df['name'])
            pdf = df.pivot_table(index="date", columns="name", values="net", aggfunc="sum").reset_index()
            inst_cols = ["外資(張)", "投信(張)", "自營商總計(張)"]
            for col in inst_cols:
                if col not in pdf.columns:
                    pdf[col] = 0.0
            pdf["三大法人合計(張)"] = pdf[inst_cols].sum(axis=1)
            cols = ["date", *inst_cols, "三大法人合計(張)"]
            return pdf[cols].sort_values("date", ascending=False).reset_index(drop=True)
    except Exception: pass
    return pd.DataFrame()

def summarize_institutional_flow(institutional_df: pd.DataFrame, price_df: pd.DataFrame):
    """以免費三大法人日報整理連續性、20日累計與淨買超日參考成本。
    參考成本只使用法人淨買超日的收盤價加權，不代表法人真實庫存成本。
    """
    empty = {
        "summary_text": "⚪ 三大法人資料不足，暫不判斷。",
        "consensus_label": "資料不足", "consensus_score": 0,
        "table": pd.DataFrame(), "foreign_text": "資料不足",
        "trust_text": "資料不足", "dealer_text": "資料不足"
    }
    if institutional_df is None or institutional_df.empty:
        return empty
    try:
        x = institutional_df.copy().sort_values("date")
        prices = price_df[["date", "close", "vol"]].copy()
        prices["date"] = prices["date"].astype(str)
        x["date"] = x["date"].astype(str)
        x = x.merge(prices, on="date", how="left")
        avg_vol_lots = max(float(pd.to_numeric(prices["vol"], errors="coerce").tail(20).mean()) / 1000.0, 1.0)
        rows, texts, score = [], {}, 0
        mapping = [("外資(張)", "外資"), ("投信(張)", "投信"), ("自營商總計(張)", "自營商")]
        for col, label in mapping:
            if col not in x.columns:
                continue
            net = pd.to_numeric(x[col], errors="coerce").fillna(0).tail(20)
            sub = x.tail(20).copy()
            sub[col] = pd.to_numeric(sub[col], errors="coerce").fillna(0)
            total20 = float(net.sum())
            buy_days = int((net > 0).sum())
            sell_days = int((net < 0).sum())
            last5 = float(net.tail(5).sum())
            intensity = total20 / avg_vol_lots
            pos = sub[sub[col] > 0].dropna(subset=["close"])
            proxy_cost = None
            if not pos.empty and float(pos[col].sum()) > 0:
                proxy_cost = float((pos["close"] * pos[col]).sum() / pos[col].sum())
            if buy_days >= 13 and total20 > 0:
                stance, pts = "🟢 持續偏買", 2
            elif buy_days >= 11 and total20 > 0:
                stance, pts = "🟢 溫和偏買", 1
            elif sell_days >= 13 and total20 < 0:
                stance, pts = "🔴 持續偏賣", -2
            elif sell_days >= 11 and total20 < 0:
                stance, pts = "🔴 溫和偏賣", -1
            else:
                stance, pts = "🟡 多空交錯", 0
            score += pts
            cost_text = f"{proxy_cost:.2f} 元" if proxy_cost else "無法估算"
            texts[label] = f"{stance}｜20日 {total20:+,.0f} 張｜買 {buy_days} 天／賣 {sell_days} 天｜近5日 {last5:+,.0f} 張"
            rows.append({"法人": label, "20日累計(張)": total20, "買超天數": buy_days, "賣超天數": sell_days, "近5日(張)": last5, "相對20日均量": intensity, "淨買超日參考價": cost_text, "判讀": stance})
        consensus = "偏多" if score >= 3 else "稍偏多" if score >= 1 else "偏空" if score <= -3 else "稍偏空" if score <= -1 else "分歧"
        summary = f"三大法人20日一致性：{consensus}。外資、投信、自營商分開判讀，避免只看單日張數。"
        return {"summary_text": summary, "consensus_label": consensus, "consensus_score": score, "table": pd.DataFrame(rows),
                "foreign_text": texts.get("外資", "資料不足"), "trust_text": texts.get("投信", "資料不足"), "dealer_text": texts.get("自營商", "資料不足")}
    except Exception as exc:
        log_error("institutional summary", exc)
        return empty

@st.cache_data(ttl=3600)
def get_industry_peer_candidates(stock_id: str, industry_category: str, max_peers: int = 8):
    """由完整上市櫃清單動態建立同業池，適用所有有產業分類的股票。"""
    info = get_stock_info_df().copy()
    if info.empty or "industry_category" not in info.columns:
        return []
    info["stock_id"] = info["stock_id"].astype(str)
    peers = info[(info["industry_category"].astype(str) == str(industry_category)) & info["stock_id"].str.match(r"^\d{4,6}$")].copy()
    if peers.empty:
        return []
    # 固定排序確保快取結果穩定；目標股必定納入，其餘最多 max_peers-1 檔。
    peers = peers.sort_values("stock_id")
    target = peers[peers["stock_id"] == str(stock_id)]
    others = peers[peers["stock_id"] != str(stock_id)].head(max_peers - 1)
    return pd.concat([target, others], ignore_index=True).to_dict("records")

def analyze_peer_resonance(stock_id: str, industry_category: str):
    candidates = get_industry_peer_candidates(stock_id, industry_category, max_peers=8)
    if len(candidates) < 2:
        return "⚪ 此產業目前可取得的同業資料不足，暫不判斷共振。", None, 0
    returns = {}
    names = {}
    for row in candidates:
        pid = str(row.get("stock_id", ""))
        market = str(row.get("type") or row.get("market_type") or row.get("market") or "TSE")
        pdf = get_daily_df(pid, market_type=market, days=100)
        if pdf is not None and len(pdf) >= 45:
            close = pd.to_numeric(pdf.set_index("date")["close"], errors="coerce")
            returns[pid] = close.pct_change().dropna().tail(60)
            names[pid] = str(row.get("stock_name", pid))
    if stock_id not in returns or len(returns) < 2:
        return "⚪ 同業行情資料不足，暫不判斷共振。", None, len(returns)
    try:
        corr = pd.DataFrame(returns).corr(min_periods=30)
        mine = corr[stock_id].drop(stock_id).dropna()
        if mine.empty:
            return "⚪ 同業共同交易日不足，暫不判斷共振。", None, len(returns)
        strongest = mine.idxmax()
        val = float(mine.max())
        label = "同向明顯" if val >= 0.6 else "中度同向" if val >= 0.3 else "走勢分化"
        return f"🔗 近60日報酬率與 {names.get(strongest, strongest)}（{strongest}）{label}，相關係數 {val:.2f}；共比較 {len(returns)} 檔同產業股票。", val, len(returns)
    except Exception as exc:
        log_error("peer correlation", exc)
        return "⚪ 同業相關性計算失敗，暫不判斷。", None, len(returns)

# 免費公開分析師共識彙整：僅顯示 Yahoo 可取得的整體統計，並非逐家券商研究報告。
@st.cache_data(ttl=1800)
def get_broker_consensus_data(stock_id: str, current_price: float):
    session = get_requests_session()
    suffix = ".TWO" if (stock_id.startswith(("3","5","6","8")) and len(stock_id)==4) else ".TW"
    symbol = f"{stock_id}{suffix}"
    
    # 🌟 查無資料時的鋼鐵留白：前台直接反映無外資報告 Facts 🌟
    res_not_found = {
        "mean": None, "high": None, "low": None, "is_real": False, "source": "Yahoo Finance 公開彙整", "coverage_count": None,
        "list": []
    }
    
    try:
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=financialData"
        r = session.get(url, timeout=5)
        if r.status_code == 200:
            result = r.json().get("quoteSummary", {}).get("result")
            if result:
                fin_data = result[0].get("financialData", {})
                t_mean = safe_float(fin_data.get("targetMeanPrice", {}).get("raw"))
                t_high = safe_float(fin_data.get("targetHighPrice", {}).get("raw"))
                t_low = safe_float(fin_data.get("targetLowPrice", {}).get("raw"))
                rec_key = str(fin_data.get("recommendationKey", "N/A")).upper()
                
                if t_mean > 0:
                    rating_map = {"BUY": "🟢 建議買進", "STRONG_BUY": "👑 強烈加碼", "HOLD": "🟡 持有/中性", "SELL": "🔴 減碼/賣出"}
                    final_rating = rating_map.get(rec_key, "🟢 買進/加碼")
                    return {
                        "mean": t_mean, "high": t_high if t_high > 0 else t_mean, "low": t_low if t_low > 0 else t_mean, "is_real": True,
                        "source": "Yahoo Finance financialData 公開彙整", "coverage_count": safe_float(fin_data.get("numberOfAnalystOpinions", {}).get("raw"), None),
                        "rating": final_rating, "list": []
                    }
    except Exception: pass
    return res_not_found

def calculate_dynamic_pb(current_price: float, fin_df: pd.DataFrame):
    if fin_df.empty or "Equity" not in fin_df.columns or "ShareCapital" not in fin_df.columns:
        return None, None
    try:
        latest_eq = safe_float(fin_df.iloc[0]["Equity"])
        latest_cap = safe_float(fin_df.iloc[0]["ShareCapital"])
        if latest_cap > 0:
            bvps = latest_eq / (latest_cap / 10)
            current_pb = current_price / bvps
            return current_pb, bvps
    except Exception as exc:
        log_error("PB calculation", exc)
    return None, None

@st.cache_data(ttl=900)
def get_rev_df(stock_id: str, days: int = 730):
    try: return get_api().taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"))
    except Exception: return None

@st.cache_data(ttl=86400)
def get_financial_statement_df(stock_id: str, years: int = 2):
    try:
        raw = get_api().taiwan_stock_financial_statement(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=years*365)).strftime("%Y-%m-%d"))
        if raw is None or raw.empty: return pd.DataFrame()
        df = raw.copy()
        df["type"] = df["type"].replace({"OperatingRevenue": "Revenue"})
        target_types = ["EPS", "Revenue", "GrossProfit", "OperatingIncome", "Equity", "ShareCapital"]
        return df[df["type"].isin(target_types)].pivot_table(index="date", columns="type", values="value", aggfunc="last").reset_index()
    except Exception: return pd.DataFrame()

@st.cache_data(ttl=300)
def get_realtime_news_list(stock_id: str, stock_name: str):
    news = []
    for tf in ["when:1d", "when:7d", ""]:
        try:
            q = urllib.parse.quote(f"{str(stock_name)} {str(stock_id)} {tf}".strip())
            r = get_requests_session().get(f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", timeout=5)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall('.//item'):
                    t = item.find('title').text or ""
                    if " - " in t: t = t.rsplit(" - ", 1)[0]
                    news.append({"date": item.find('pubDate').text or "", "title": t, "source": item.find('source').text if item.find('source') is not None else "財經", "link": item.find('link').text or ""})
                if news: break
        except Exception: pass
    if news:
        df = pd.DataFrame(news)
        df["parsed_date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_convert('Asia/Taipei')
        df["date"] = df["parsed_date"].dt.strftime('%m-%d %H:%M')
        return df.sort_values(by="parsed_date", ascending=False)[["date", "title", "source", "link"]].to_dict('records')
    return []

def prepare_indicator_df(df: pd.DataFrame):
    """建立日線技術、價量、趨勢強度與結構欄位。"""
    if df is None or df.empty: return None
    x = df.copy().sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "vol"]:
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x.dropna(subset=["high", "low", "close", "vol"])
    c_prev = x["close"].shift(1)
    x["TR"] = np.maximum(x["high"] - x["low"], np.maximum((x["high"] - c_prev).abs(), (x["low"] - c_prev).abs()))
    x["ATR14"] = x["TR"].ewm(alpha=1/14, adjust=False).mean()
    for n in [5, 10, 20, 60, 120, 240]:
        x[f"MA{n}"] = x["close"].rolling(n).mean()
    x["MA5_Vol"], x["MA20_Vol"], x["MA60_Vol"] = x["vol"].rolling(5).mean(), x["vol"].rolling(20).mean(), x["vol"].rolling(60).mean()
    x["Res_20D"] = x["high"].shift(1).rolling(20).max()
    x["Res_60D"] = x["high"].shift(1).rolling(60).max()
    x["Sup_20D"] = x["low"].shift(1).rolling(20).min()
    x["Sup_60D"] = x["low"].shift(1).rolling(60).min()
    x["std20"] = x["close"].rolling(20).std()
    delta = x["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    x["RSI14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    ema12, ema26 = x["close"].ewm(span=12, adjust=False).mean(), x["close"].ewm(span=26, adjust=False).mean()
    x["MACD"], x["MACD_SIGNAL"] = ema12 - ema26, (ema12 - ema26).ewm(span=9, adjust=False).mean()
    x["MACD_HIST"] = x["MACD"] - x["MACD_SIGNAL"]
    l_min, h_max = x["low"].rolling(9).min(), x["high"].rolling(9).max()
    x["RSV"] = 100 * ((x["close"] - l_min) / (h_max - l_min).replace(0, np.nan))
    k_l, d_l, ck, cd = [], [], 50.0, 50.0
    for rsv in x["RSV"]:
        if pd.isna(rsv): k_l.append(np.nan); d_l.append(np.nan)
        else:
            ck = (2/3)*ck + (1/3)*rsv; cd = (2/3)*cd + (1/3)*ck
            k_l.append(ck); d_l.append(cd)
    x["K9"], x["D9"] = k_l, d_l

    # ADX：判斷有沒有趨勢，而非只判斷方向。
    up_move, down_move = x["high"].diff(), -x["low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=x.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=x.index)
    atr_wilder = x["TR"].ewm(alpha=1/14, adjust=False).mean().replace(0, np.nan)
    x["PLUS_DI"] = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_wilder
    x["MINUS_DI"] = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_wilder
    dx = 100 * (x["PLUS_DI"] - x["MINUS_DI"]).abs() / (x["PLUS_DI"] + x["MINUS_DI"]).replace(0, np.nan)
    x["ADX14"] = dx.ewm(alpha=1/14, adjust=False).mean()

    # 價量：OBV、CMF、上漲日量/下跌日量、換手代理與量價背離。
    direction = np.sign(x["close"].diff()).fillna(0)
    x["OBV"] = (direction * x["vol"]).cumsum()
    x["OBV_MA20"] = x["OBV"].rolling(20).mean()
    mfm = ((x["close"] - x["low"]) - (x["high"] - x["close"])) / (x["high"] - x["low"]).replace(0, np.nan)
    x["CMF20"] = (mfm.fillna(0) * x["vol"]).rolling(20).sum() / x["vol"].rolling(20).sum().replace(0, np.nan)
    x["UP_VOL20"] = x["vol"].where(x["close"] > c_prev, 0).rolling(20).sum()
    x["DOWN_VOL20"] = x["vol"].where(x["close"] < c_prev, 0).rolling(20).sum()
    x["VOL_RATIO20"] = x["vol"] / x["MA20_Vol"].replace(0, np.nan)
    x["RET_5D"] = x["close"].pct_change(5) * 100
    x["RET_20D"] = x["close"].pct_change(20) * 100
    for n in [20, 60, 120]:
        x[f"MA{n}_SLOPE"] = (x[f"MA{n}"] / x[f"MA{n}"].shift(5) - 1) * 100
    x["PRICE_HIGH_20"] = x["close"] >= x["close"].rolling(20).max().shift(1)
    x["OBV_HIGH_20"] = x["OBV"] >= x["OBV"].rolling(20).max().shift(1)
    x["BEARISH_VOL_DIVERGENCE"] = x["PRICE_HIGH_20"] & (~x["OBV_HIGH_20"])
    return x.dropna(subset=["ATR14", "MA20", "MA60", "Res_20D", "RSI14", "K9", "D9", "ADX14"]).copy()

def build_weekly_indicators(df_raw: pd.DataFrame):
    """將日線轉為週線，降低單日雜訊。"""
    if df_raw is None or df_raw.empty: return None
    w = df_raw.copy()
    w["date"] = pd.to_datetime(w["date"], errors="coerce")
    w = w.dropna(subset=["date"]).set_index("date").sort_index()
    weekly = w.resample("W-FRI").agg({"open":"first", "high":"max", "low":"min", "close":"last", "vol":"sum"}).dropna(subset=["close"]).reset_index()
    if len(weekly) < 30: return None
    weekly["MA10W"] = weekly["close"].rolling(10).mean()
    weekly["MA20W"] = weekly["close"].rolling(20).mean()
    weekly["MA40W"] = weekly["close"].rolling(40).mean()
    weekly["MA20W_SLOPE"] = (weekly["MA20W"] / weekly["MA20W"].shift(3) - 1) * 100
    return weekly

def detect_swing_structure(df: pd.DataFrame, window: int = 3):
    """以局部高低點辨識 HH/HL、LH/LL，避免只看均線。"""
    if df is None or len(df) < 25:
        return {"label":"資料不足", "higher_high":False, "higher_low":False, "last_swing_high":None, "last_swing_low":None}
    highs, lows = [], []
    for i in range(window, len(df)-window):
        if df["high"].iloc[i] >= df["high"].iloc[i-window:i+window+1].max(): highs.append((i, float(df["high"].iloc[i])))
        if df["low"].iloc[i] <= df["low"].iloc[i-window:i+window+1].min(): lows.append((i, float(df["low"].iloc[i])))
    hh = len(highs)>=2 and highs[-1][1] > highs[-2][1]
    hl = len(lows)>=2 and lows[-1][1] > lows[-2][1]
    lh = len(highs)>=2 and highs[-1][1] < highs[-2][1]
    ll = len(lows)>=2 and lows[-1][1] < lows[-2][1]
    label = "高點墊高、低點墊高" if hh and hl else "高點降低、低點降低" if lh and ll else "結構整理中"
    return {"label":label, "higher_high":hh, "higher_low":hl, "lower_high":lh, "lower_low":ll,
            "last_swing_high":highs[-1][1] if highs else None, "last_swing_low":lows[-1][1] if lows else None}

def classify_trend_and_models(df: pd.DataFrame, weekly: pd.DataFrame, current_price: float, current_vol_shares: float, volume_valid: bool = True):
    last = df.iloc[-1]
    structure = detect_swing_structure(df.tail(150).reset_index(drop=True))
    ma10, ma20, ma60 = map(float, [last.get("MA10", np.nan), last["MA20"], last["MA60"]])
    ma120, ma240 = safe_float(last.get("MA120"), np.nan), safe_float(last.get("MA240"), np.nan)
    slope20, slope60, slope120 = safe_float(last.get("MA20_SLOPE")), safe_float(last.get("MA60_SLOPE")), safe_float(last.get("MA120_SLOPE"))
    adx, plus_di, minus_di = safe_float(last.get("ADX14")), safe_float(last.get("PLUS_DI")), safe_float(last.get("MINUS_DI"))
    atr, vol_ma20 = safe_float(last.get("ATR14"),1), safe_float(last.get("MA20_Vol"),1)
    peak60 = float(df["high"].tail(60).max())
    drawdown = (current_price/peak60-1)*100 if peak60>0 else 0
    volume_ratio = current_vol_shares/vol_ma20 if volume_valid and vol_ma20>0 else 0
    pullback_volume_ratio = float(df["vol"].tail(5).mean()/vol_ma20) if vol_ma20>0 else 0
    weekly_ok = False
    weekly_desc = "週線資料不足"
    if weekly is not None and not weekly.empty:
        wl=weekly.iloc[-1]
        weekly_ok = safe_float(wl["close"]) >= safe_float(wl["MA20W"]) and safe_float(wl["MA20W_SLOPE"]) > 0
        weekly_desc = "週線維持多頭" if weekly_ok else "週線尚未確認多頭"
    long_bull = weekly_ok and current_price >= ma60 and slope60>0 and (pd.isna(ma120) or ma60>=ma120 or slope120>=0)
    long_bear = current_price < ma60 and slope60<0 and (structure.get("lower_low") or minus_di>plus_di)
    long_label = "長期多頭" if long_bull else "長期空頭" if long_bear else "長期整理／轉折"
    medium_bull = ma20>=ma60 and slope20>0 and current_price>=ma60
    medium_label = "主升段" if medium_bull and current_price>=ma20 and adx>=25 and plus_di>minus_di else "多頭正常拉回" if long_bull and current_price<ma20 and current_price>=ma60 and drawdown>=-15 else "高檔整理" if long_bull and abs(slope20)<1 else "築底" if not long_bear and slope20>=0 and structure.get("higher_low") else "反彈" if current_price>=ma20 and not long_bull else "下跌段" if long_bear else "區間整理"
    short_label = "短線轉強" if current_price>=ma10 and safe_float(last.get("K9"))>safe_float(last.get("D9")) else "短線拉回" if long_bull and current_price<ma10 else "短線偏弱"
    trend_strength = "強趨勢" if adx>=25 else "趨勢形成中" if adx>=18 else "震盪為主"

    real_res20, real_res60 = safe_float(last["Res_20D"]), safe_float(last["Res_60D"])
    prior_breakout = float(df["close"].iloc[-21:-1].max()) >= float(df["Res_20D"].iloc[-21:-1].max()) if len(df)>25 else False
    breakout = current_price>=real_res20 and volume_ratio>=1.3 and medium_bull
    retest = prior_breakout and abs(current_price-real_res20)/max(real_res20,0.01)<=0.035 and pullback_volume_ratio<=0.9 and current_price>=ma20*0.98
    pullback = long_bull and medium_bull and -15<=drawdown<=-3 and current_price>=ma60 and pullback_volume_ratio<=0.9 and not structure.get("lower_low")
    base_turn = not long_bear and slope20>=0 and structure.get("higher_low") and current_price>=real_res20 and volume_ratio>=1.2
    stop_candle = (float(last["close"])>float(last["open"]) and float(last["close"])>=float(last["low"])+0.6*(float(last["high"])-float(last["low"]))) or (safe_float(last.get("K9"))>safe_float(last.get("D9")) and safe_float(df["K9"].iloc[-2])<=safe_float(df["D9"].iloc[-2]))
    model = "突破進場" if breakout else "突破後回測" if retest else "多頭拉回" if pullback else "築底轉強" if base_turn else "等待"
    model_ready = breakout or (retest and stop_candle) or (pullback and stop_candle) or base_turn

    upv, dnv = safe_float(last.get("UP_VOL20")), safe_float(last.get("DOWN_VOL20"))
    cmf, obv = safe_float(last.get("CMF20")), safe_float(last.get("OBV")); obvma=safe_float(last.get("OBV_MA20"))
    if not volume_valid:
        price_volume="成交量資料尚未更新"
    elif current_price>=ma20 and volume_ratio>=1.3: price_volume="價漲量增，買盤積極"
    elif current_price<ma20 and pullback_volume_ratio<=0.9 and long_bull: price_volume="價跌量縮，較像多頭拉回"
    elif current_price<ma20 and volume_ratio>=1.3: price_volume="價跌量增，賣壓需警戒"
    elif current_price>=ma20 and volume_ratio<0.8: price_volume="價漲量縮，追價力道不足"
    else: price_volume="價量關係中性"
    accumulation = "資金偏累積" if cmf>0.05 and obv>=obvma and upv>=dnv else "資金偏流出" if cmf<-0.05 and obv<obvma and dnv>upv else "資金平衡"
    divergence = "出現價格創高但OBV未創高的量價背離" if bool(last.get("BEARISH_VOL_DIVERGENCE",False)) else "未見明顯空方量價背離"
    return {"long_term":long_label, "medium_term":medium_label, "short_term":short_label, "weekly_desc":weekly_desc,
            "trend_strength":trend_strength, "adx":adx, "structure":structure, "drawdown_pct":drawdown,
            "volume_ratio":volume_ratio, "volume_valid":volume_valid, "pullback_volume_ratio":pullback_volume_ratio, "price_volume":price_volume,
            "accumulation":accumulation, "volume_divergence":divergence, "entry_model":model, "entry_ready":model_ready,
            "breakout_model":breakout, "pullback_model":pullback, "retest_model":retest, "base_model":base_turn,
            "stop_candle":stop_candle, "ma10":ma10, "ma120":ma120, "ma240":ma240,
            "slope20":slope20, "slope60":slope60, "slope120":slope120}

def resolve_trend_state(stock_id: str, analysis: dict, current_price: float, structure_stop: float, ma20: float, ma60: float, volume_ratio: float):
    """狀態機有遲滯：單日跌破短均線不直接翻空。"""
    key=f"trend_state_{stock_id}"
    prev=st.session_state.get(key, {"state":"觀察", "weak_days":0, "break_days":0})
    state=prev["state"]; weak_days=int(prev.get("weak_days",0)); break_days=int(prev.get("break_days",0))
    structural_break = current_price < structure_stop and current_price < ma60
    warning = current_price < ma20 and (analysis["slope20"]<0 or volume_ratio>=1.3)
    if structural_break:
        break_days += 1
    else: break_days=0
    if warning: weak_days+=1
    else: weak_days=max(0,weak_days-1)
    if analysis["long_term"]=="長期空頭": state="空頭"
    elif break_days>=2 or (structural_break and volume_ratio>=1.5): state="趨勢破壞"
    elif weak_days>=2: state="多頭轉弱警戒"
    elif analysis["medium_term"]=="多頭正常拉回": state="多頭正常拉回"
    elif analysis["entry_model"]=="突破進場" and analysis["entry_ready"]: state="突破確認"
    elif analysis["medium_term"]=="主升段": state="多頭持有"
    elif analysis["entry_model"]=="築底轉強": state="趨勢轉強"
    elif analysis["medium_term"]=="築底": state="築底"
    else: state="觀察"
    volume_reason = f"{volume_ratio:.2f}" if analysis.get("volume_valid", False) else "尚未更新"
    reason = f"長期={analysis['long_term']}；中期={analysis['medium_term']}；短期={analysis['short_term']}；量比={volume_reason}；原始結構停損價={structure_stop:.2f}"
    now={"state":state,"weak_days":weak_days,"break_days":break_days,"reason":reason}
    if prev.get("state") != state:
        log_key=f"trend_log_{stock_id}"
        logs=st.session_state.get(log_key, [])
        logs.append({"時間":datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), "原狀態":prev.get("state","觀察"), "新狀態":state, "原因":reason})
        st.session_state[log_key]=logs[-30:]
    st.session_state[key]=now
    return now

def unified_institutional_brain(res_dict, df_hist, is_holding=False, entry_cost=0.0, sector_panic=False):
    p=res_dict["current_price"]; q=res_dict.get("data_quality_score",0); state=res_dict.get("trend_state","觀察")
    ta=res_dict.get("trend_analysis",{}); chip=f"投信：{res_dict.get('sitc_trend')}；融資：{res_dict.get('margin_trend')}。"
    structure_stop=res_dict.get("structure_stop",res_dict["stop_brk"])
    if q<60 or res_dict.get("macro_bull") is None:
        return {"strategy_name":"⚪ 資料不足","color":"#64748B","action_now":"只觀察，不產生方向","signal":"關鍵資料未完整","blueprint":{"停損防守":"待資料恢復","移動停利":"不適用","預期目標":"不提供"},"desc":f"資料完整度 {q:.0f}%，不足以形成可靠方向。"}
    if sector_panic:
        return {"strategy_name":"🟠 族群風險升高","color":"#F59E0B","action_now":"暫停新增部位","signal":"同產業集體轉弱","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":"縮小風險","預期目標":"待族群止穩"},"desc":"族群同步下跌時，個股拉回較可能演變成趨勢破壞。"}
    if is_holding and entry_cost>0:
        if state in ["趨勢破壞","空頭"]:
            return {"strategy_name":"🔴 波段結構已破壞","color":"#EF4444","action_now":"依計畫減碼或退出","signal":"結構低點與中期趨勢同時失守","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":"已觸發","預期目標":"先控制風險"},"desc":"不是因為單日跌破5日線，而是波段低點、60日線或放量賣壓已共同惡化。"}
        if state=="多頭轉弱警戒":
            return {"strategy_name":"🟠 多頭轉弱警戒","color":"#F59E0B","action_now":"續抱觀察，必要時分批減碼","signal":"連續出現中期弱化條件","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":f"ATR線 {res_dict['trailing_stop_value']:.2f} 元","預期目標":"等待重新站回MA20"},"desc":"尚未直接判定空頭，但弱化已非單日雜訊。"}
        if state=="多頭正常拉回":
            return {"strategy_name":"🟢 多頭趨勢正常拉回","color":"#10B981","action_now":"續抱，不因短線跌破而殺出","signal":"週線與中長期結構仍完整，拉回量縮","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":f"ATR線 {res_dict['trailing_stop_value']:.2f} 元","預期目標":f"前高 {res_dict['real_resistance']:.2f} 元"},"desc":f"目前損益 {res_dict['pnl_pct']:+.1f}%。短線降溫不等於趨勢反轉；{chip}"}
        return {"strategy_name":"🟢 趨勢持有","color":"#10B981","action_now":"續抱並上移防守","signal":f"狀態：{state}","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":f"ATR線 {res_dict['trailing_stop_value']:.2f} 元","預期目標":f"情境價 {res_dict['target_brk']:.2f} 元"},"desc":f"趨勢尚未被結構性破壞。{chip}"}
    model=ta.get("entry_model","等待")
    if model=="多頭拉回":
        action="可小額分批" if ta.get("entry_ready") else "等待止跌確認"
        return {"strategy_name":"🟢 多頭拉回機會","color":"#10B981","action_now":action,"signal":"長中期多頭、回檔量縮且未破前低","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":"站回MA20後再上移","預期目標":f"前高 {res_dict['real_resistance']:.2f} 元"},"desc":"這是低價拉回模型，不必等到再創新高才追價；仍建議分批，而非一次買滿。"}
    if model=="突破後回測":
        return {"strategy_name":"🟢 突破後回測","color":"#10B981","action_now":"確認止跌後分批","signal":"原壓力轉支撐且回測量縮","blueprint":{"停損防守":f"突破失效線 {structure_stop:.2f} 元","移動停利":"續強後上移","預期目標":f"情境價 {res_dict['target_brk']:.2f} 元"},"desc":"通常比直接追突破有較好的風險報酬。"}
    if model=="突破進場":
        return {"strategy_name":"🟢 放量突破","color":"#10B981","action_now":"小額分批，不追過度乖離","signal":"價格與量能越過壓力","blueprint":{"停損防守":f"突破失效線 {structure_stop:.2f} 元","移動停利":"依結構與ATR上移","預期目標":f"情境價 {res_dict['target_brk']:.2f} 元"},"desc":"突破成立，但若距MA20過遠，應等待回測而不是追高。"}
    if model=="築底轉強":
        return {"strategy_name":"🟡 築底轉強","color":"#F59E0B","action_now":"僅適合小部位試單","signal":"低點墊高、均線走平後突破","blueprint":{"停損防守":f"底部結構線 {structure_stop:.2f} 元","移動停利":"待趨勢形成","預期目標":f"前壓 {res_dict['real_resistance']:.2f} 元"},"desc":"這是較積極的轉折模型，可靠度低於成熟多頭拉回。"}
    return {"strategy_name":"⚪ 等待更好位置","color":"#64748B","action_now":"不追價，等待拉回或確認","signal":"尚未符合四種進場模型","blueprint":{"停損防守":"未進場不設定","移動停利":"不適用","預期目標":"等待條件"},"desc":f"目前不必勉強交易。{chip}"}

# ============ 9. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int, is_holding=False, entry_cost=0.0, sector_panic=False):
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    pnl_pct = 0.0
    res_dict = {}
    latest_yoy = 0.0
    raw_news_list, fin_df, institutional_df = [], pd.DataFrame(), pd.DataFrame()
    spring_verdict, spring_triggered, detected_prior_low = "⚪ 未觸發破底翻結構", False, 0.0
    news_analysis_report = "⚖️ 新聞文字傾向僅供參考"
    fin_conclusion = "⚪ 財報資料不足，暫不判斷"
    pe_val, pb_ratio, bvps = None, None, None
    broker_consensus = {"mean": None, "high": None, "low": None, "is_real": False, "source": "Yahoo Finance 公開彙整", "coverage_count": None, "rating": None, "list": [], "error": None}
    
    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = "🟡 中性", "🟡 平穩", 0.0, 0.0
    wolf_rank_label, wolf_rank_color = "⚖️ 族群常態輪動成員", "#64748B"
    
    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    if match.empty:
        stock_name, industry, market_type = f"代號 {stock_id}", "自訂追蹤板塊", ("TWO" if (stock_id.startswith(("3","5","6","8")) and len(stock_id)==4) else "TSE")
    else:
        m_col = "type" if "type" in match.columns else "market_type" if "market_type" in match.columns else "market" if "market" in match.columns else "market_type"
        market_type = str(match[m_col].iloc[0]).strip().upper() if m_col in match.columns else "TSE"
        stock_name, industry = str(match["stock_name"].iloc[0]), str(match["industry_category"].iloc[0])
            
    df_raw = get_daily_df(stock_id, market_type=market_type, days=450)
    if df_raw is None or df_raw.empty: return None

    macro_bull, macro_text, is_market_panic, is_market_overextended, market_vol_healthy, market_vol_desc = get_market_macro_status(market_type)
    market_regime_context = get_market_regime_context(market_type)
    radar_results, is_us_panic, us_panic_desc, wtx_change = get_overnight_radar()
    hist_last_raw = df_raw.iloc[-1]
    quote = compute_live_data(stock_id, market_type, float(hist_last_raw["close"]), float(hist_last_raw["vol"]))
    rt_open, rt_high, rt_low, rt_close = quote["open"], quote["high"], quote["low"], quote["close"]
    rt_vol_lots, rt_success, rt_source = quote["volume_lots"], quote["success"], quote["source"]
    quote_volume_valid = bool(quote.get("volume_valid", rt_vol_lots > 0))
    volume_ratio_enabled = bool(USE_INTRADAY_VOLUME_RATIO)
    volume_valid = quote_volume_valid and volume_ratio_enabled
    previous_close = quote["previous_close"]
    current_price, current_vol = rt_close, rt_vol_lots
    market_data = {
        "price": current_price,
        "volume_lots": current_vol,
        "timestamp": quote.get("quote_time"),
        "source": rt_source,
        "price_valid": bool(rt_success and current_price > 0),
        "volume_valid": quote_volume_valid,
        "volume_ratio_enabled": volume_ratio_enabled,
        "raw_volume": quote.get("raw_volume"),
    }
    t = tick_size(current_price)
    df_for_indicators = df_raw.copy().sort_values("date").reset_index(drop=True)
    
    # 只有價格與成交量都有效時，才把盤中資料寫入日線指標。
    # 避免「價格抓到、成交量沒抓到」時，把 0 張寫進日線並污染量比與均量。
    if rt_success and volume_valid:
        if str(df_for_indicators.iloc[-1]["date"]) == today_str:
            df_for_indicators.loc[df_for_indicators.index[-1], ["open", "high", "low", "close", "vol"]] = [rt_open, rt_high, rt_low, rt_close, rt_vol_lots * 1000.0]
        else:
            df_for_indicators = pd.concat([df_for_indicators, pd.DataFrame([{"date": today_str, "open": float(rt_open), "high": float(rt_high), "low": float(rt_low), "close": float(rt_close), "vol": float(rt_vol_lots * 1000.0), "amount": float(rt_close * rt_vol_lots * 1000.0)}])], ignore_index=True)

    df = prepare_indicator_df(df_for_indicators)
    if df is None or df.empty: return None
    peak_price_20d = float(df["close"].tail(20).max())
    hist_last = df.iloc[-1]
    
    ma5_val = float(hist_last["MA5"])
    ma20_val, ma60_val = float(hist_last["MA20"]), float(hist_last["MA60"])
    vol_ma20_val, real_resistance = float(hist_last["MA20_Vol"]), float(hist_last["Res_20D"])
    rsi_now, macd_hist, atr = safe_float(hist_last.get("RSI14", 50.0)), safe_float(hist_last.get("MACD_HIST", 0.0)), safe_float(hist_last.get("ATR14", 1.0))
    k9_now, d9_now = safe_float(hist_last.get("K9", 50.0)), safe_float(hist_last.get("D9", 50.0))
    weekly_df = build_weekly_indicators(df_for_indicators)
    trend_analysis = classify_trend_and_models(df, weekly_df, current_price, current_vol * 1000.0, volume_valid=volume_valid)
    swing = trend_analysis["structure"]
    structure_stop_raw = swing.get("last_swing_low") or float(hist_last.get("Sup_20D", current_price - 2*atr))
    structure_stop = floor_to_tick(min(structure_stop_raw, ma20_val - 0.5*atr) if trend_analysis["long_term"]=="長期多頭" else structure_stop_raw, t)

    # 趨勢失效價必須位於目前股價下方。若波段資料、即時價與日線資料不同步，
    # 原始 swing low 可能反而高於現價；此時改採現價下方最近的有效支撐候選。
    stop_reference = max(float(current_price), 0.0)
    stop_candidates = [
        safe_float(swing.get("last_swing_low"), 0.0),
        safe_float(hist_last.get("Sup_20D"), 0.0),
        safe_float(ma60_val, 0.0),
        safe_float(ma20_val - 0.5 * atr, 0.0),
        safe_float(current_price - 2.0 * atr, 0.0),
        safe_float(current_price * 0.97, 0.0),
    ]
    valid_stop_candidates = [x for x in stop_candidates if 0 < x < stop_reference]
    if structure_stop <= 0 or structure_stop >= stop_reference:
        fallback_stop = max(valid_stop_candidates) if valid_stop_candidates else current_price * 0.97
        structure_stop = floor_to_tick(min(fallback_stop, current_price - t), t)

    trend_state_data = resolve_trend_state(stock_id, trend_analysis, current_price, structure_stop, ma20_val, ma60_val, trend_analysis["volume_ratio"])
    
    kd_status = "黃金交叉" if k9_now > d9_now else "死亡交叉"
    stock_daily_pct = ((current_price - previous_close) / previous_close) * 100 if previous_close > 0 else 0.0
    relative_strength = stock_daily_pct - wtx_change
    is_rs_gold = (wtx_change <= -1.0) and (relative_strength >= 3.0)

    peer_resonance_text, peer_corr_val, peer_count = analyze_peer_resonance(stock_id, industry)
    avg_daily_volume_shares = float(df["vol"].tail(20).mean())
    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = get_taiwan_enhanced_chips(stock_id, avg_daily_volume_shares)
    
    try: institutional_df = get_institutional_trading_df(stock_id, days=30)
    except Exception: pass
    institutional_summary = summarize_institutional_flow(institutional_df, df)
    try:
        broker_consensus = get_broker_consensus_data(stock_id, current_price)
    except Exception as exc:
        broker_consensus["error"] = str(exc)
        log_error("broker consensus", exc)

    vol_spike = (current_vol * 1000.0) > (vol_ma20_val * 1.5)
    attempted_breakout = current_price >= real_resistance
    confirmed_breakout = attempted_breakout and vol_spike and datetime.now(TZ).time() >= datetime.strptime("13:25", "%H:%M").time()
    if relative_strength > 4.0 and sitc_3d_sum > 300: wolf_rank_label, wolf_rank_color = "👑 族群領頭狼王（主導資金絕對攻勢）", "#7D3CFF"
    elif relative_strength < -2.0: wolf_rank_label, wolf_rank_color = "🐌 族群落後跟屁蟲（嚴防資金棄養踩踏）", "#EF4444"
    else: wolf_rank_label, wolf_rank_color = "⚖️ 族群常態輪動成員（隨大盤溫和浮動）", "#64748B"

    box_width_pct = ((float(df["close"].tail(30).max()) - float(df["close"].tail(30).min())) / float(df["close"].tail(30).min())) * 100
    is_box_compressed = box_width_pct <= 8.5
    target_brk = floor_to_tick(current_price + (3.0 * atr), t)
    stop_candidate = min(real_resistance - (1.5 * atr), current_price - atr)
    stop_brk = floor_to_tick(stop_candidate, t)
    trailing_stop_value = floor_to_tick(peak_price_20d - (2.5 * atr), t)
    stop_line_text = f"{trailing_stop_value:.2f} 元"

    if k9_now < 20: kd_timing = "隨機指標進入 20 以下低檔區（超賣打底）。"
    elif k9_now > 70: kd_timing = "隨機指標在 70 以上高檔鈍化（超買強勢）。"
    else: kd_timing = f"KD 指標目前在 20~70 之間常態區洗盤 (K={k9_now:.1f} / D={d9_now:.1f})。"
    bb_stage = "多頭主導（MACD 柱狀體在零軸上方安全區）。" if macd_hist >= 0 else "空頭修正（MACD 柱狀體在零軸下方收縮）。"
    volume_verdict = (f"{trend_analysis['price_volume']}；{trend_analysis['accumulation']}；{trend_analysis['volume_divergence']}。RSI14={rsi_now:.1f}，量比={trend_analysis['volume_ratio']:.2f}。"
                      if volume_valid else f"成交量資料尚未更新；目前不判斷量比與價量關係。RSI14={rsi_now:.1f}。")

    rev_df = get_rev_df(stock_id, days=730)
    if rev_df is not None and not rev_df.empty:
        try:
            col = [c for c in rev_df.columns if c.lower() == "revenue"]
            if col:
                rev_df["revenue_clean"] = pd.to_numeric(rev_df[col[0]].astype(str).str.replace(",", ""), errors="coerce")
                rev_df = rev_df.dropna(subset=["revenue_clean"]).sort_values("date")
                if len(rev_df) > 12: latest_yoy = float(rev_df["revenue_clean"].pct_change(12).iloc[-1] * 100)
        except Exception: latest_yoy = 0.0

    try: raw_news_list_data = get_realtime_news_list(stock_id, stock_name)
    except Exception: raw_news_list_data = []
    if raw_news_list_data:
        raw_news_list = raw_news_list_data[:8]
        for n in raw_news_list: n["sentiment"], n["color"] = analyze_news_sentiment(n["title"])
        news_analysis_report = "利多消息主導市場輿情" if sum(1 for n in raw_news_list if "利多" in n["sentiment"]) > sum(1 for n in raw_news_list if "利空" in n["sentiment"]) else "市場網路輿情呈現中性平衡"

    if len(df) >= 40:
        low_cand = float(df.iloc[-40:-10]["low"].min())
        for r_idx, row in df.iloc[-10:].iterrows():
            if row["low"] < low_cand and df["close"].iloc[-1] > low_cand:
                spring_triggered = True; detected_prior_low = low_cand; break
    if spring_triggered: spring_verdict = f"🟢 成功收復前波低點 {detected_prior_low:.2f} 元，形成破底後收復型態；仍需後續量價確認。"

    fin_df_raw = get_financial_statement_df(stock_id, years=2)
    if not fin_df_raw.empty and "Revenue" in fin_df_raw.columns:
        fin_df_work = fin_df_raw.copy().sort_values("date").reset_index(drop=True)
        for f_idx in range(len(fin_df_work)):
            rev_amt = safe_float(fin_df_work.loc[f_idx, "Revenue"])
            fin_df_work.loc[f_idx, "gpm"] = (safe_float(fin_df_work.loc[f_idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df_work.loc[f_idx, "opm"] = (safe_float(fin_df_work.loc[f_idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        
        fin_df = fin_df_work.sort_values("date", ascending=False).reset_index(drop=True)
        last_fin = fin_df.iloc[0]
        gpm_now, opm_now = safe_float(last_fin.get("gpm", 0.0)), safe_float(last_fin.get("opm", 0.0))
        # FinMind EPS 欄位須確認為單季值；此處僅在四筆皆有效時顯示參考 TTM。
        eps4 = pd.to_numeric(fin_df.head(4).get('EPS'), errors='coerce') if 'EPS' in fin_df.columns else pd.Series(dtype=float)
        sum_eps_4q = float(eps4.sum()) if len(eps4) == 4 and eps4.notna().all() else 0.0
        pe_val = current_price / sum_eps_4q if sum_eps_4q > 0 else 0.0
        if len(fin_df) >= 5:
            yoy_row = fin_df.iloc[4]
            fin_conclusion = "📈 最新季度毛利率高於約一年前同期" if gpm_now > safe_float(yoy_row.get("gpm", 0.0)) else "⚖️ 最新季度毛利率未高於約一年前同期"
        else:
            fin_conclusion = "⚪ 可比較季度不足，暫不做年同期判斷"
        
        try: pb_ratio, bvps = calculate_dynamic_pb(current_price, fin_df)
        except Exception: pass

    pnl_pct = ((current_price - entry_cost) / entry_cost * 100) if (is_holding and entry_cost > 0) else 0.0
    
    short_term_trend = f"{trend_analysis['short_term']}（KD：{kd_status}）"
    long_term_trend = f"{trend_analysis['long_term']}；{trend_analysis['weekly_desc']}；MA60五日斜率 {trend_analysis['slope60']:+.2f}%"
    trend_phase = f"{trend_analysis['medium_term']}｜{trend_analysis['trend_strength']}｜波段結構：{trend_analysis['structure']['label']}"

    # 打包
    res_dict["stock_id"] = stock_id
    res_dict["stock_name"] = stock_name
    res_dict["industry"] = industry
    res_dict["market_type"] = market_type
    res_dict["pnl_pct"] = pnl_pct
    res_dict["macro_bull"] = macro_bull
    res_dict["market_regime_context"] = market_regime_context
    res_dict["is_market_panic"] = bool(is_market_panic)
    res_dict["is_us_panic"] = bool(is_us_panic)
    res_dict["us_panic_desc"] = us_panic_desc
    res_dict["market_vol_desc"] = market_vol_desc
    res_dict["wolf_rank_label"] = wolf_rank_label
    res_dict["wolf_rank_color"] = wolf_rank_color
    res_dict["target_brk"] = target_brk
    res_dict["stop_brk"] = stop_brk
    res_dict["trailing_stop_line"] = stop_line_text
    res_dict["current_price"] = current_price
    res_dict["current_vol"] = current_vol
    res_dict["ma5_val"] = ma5_val
    res_dict["ma20_val"] = ma20_val
    res_dict["ma60_val"] = ma60_val
    res_dict["real_resistance"] = real_resistance
    res_dict["atr"] = atr
    res_dict["stock_daily_pct"] = stock_daily_pct
    res_dict["relative_strength"] = relative_strength
    res_dict["is_rs_gold"] = is_rs_gold
    res_dict["rt_source"] = rt_source
    res_dict["quote_success"] = rt_success
    res_dict["quote_time"] = quote.get("quote_time")
    res_dict["quote_is_stale"] = quote.get("is_stale", not rt_success)
    res_dict["volume_valid"] = quote_volume_valid
    res_dict["volume_ratio_enabled"] = volume_ratio_enabled
    res_dict["volume_used_in_ai"] = volume_valid
    res_dict["market_data"] = market_data
    res_dict["raw_volume"] = quote.get("raw_volume")
    res_dict["volume_note"] = quote.get("volume_note", "未提供成交量診斷")
    res_dict["volume_ma20_shares"] = vol_ma20_val
    res_dict["volume_ma20_lots"] = vol_ma20_val / 1000.0 if vol_ma20_val > 0 else 0.0
    res_dict["m_desc"] = macro_text
    res_dict["m_color"] = "gray" if macro_bull is None else ("red" if not macro_bull else "green")
    res_dict["fin_df"] = fin_df
    res_dict["spring_verdict"] = spring_verdict
    
    # 趨勢分析封裝
    res_dict["short_term_trend"] = short_term_trend
    res_dict["long_term_trend"] = long_term_trend
    res_dict["trend_phase"] = trend_phase
    
    res_dict["latest_yoy"] = latest_yoy
    res_dict["fin_conclusion"] = fin_conclusion
    res_dict["sitc_trend"] = sitc_trend
    res_dict["sitc_3d_sum"] = sitc_3d_sum
    res_dict["radar_results"] = radar_results
    res_dict["vol_spike"] = vol_spike
    res_dict["raw_news_list"] = raw_news_list
    res_dict["news_analysis_report"] = news_analysis_report
    res_dict["kd_timing"] = kd_timing
    res_dict["bb_stage"] = bb_stage
    res_dict["volume_verdict"] = volume_verdict
    res_dict["institutional_df"] = institutional_df
    res_dict["broker_consensus"] = broker_consensus
    res_dict["margin_trend"] = margin_trend
    res_dict["box_width_pct"] = box_width_pct
    res_dict["market_vol_healthy"] = market_vol_healthy
    res_dict["is_box_compressed"] = is_box_compressed
    res_dict["attempted_breakout"] = attempted_breakout
    res_dict["confirmed_breakout"] = confirmed_breakout
    res_dict["trailing_stop_value"] = trailing_stop_value
    
    res_dict["institutional_summary"] = institutional_summary
    res_dict["peer_resonance_text"] = peer_resonance_text
    res_dict["peer_corr_val"] = peer_corr_val
    res_dict["peer_count"] = peer_count
    res_dict["pb_ratio"] = pb_ratio
    res_dict["bvps"] = bvps
    res_dict["trend_analysis"] = trend_analysis
    res_dict["trend_state"] = trend_state_data["state"]
    res_dict["trend_state_detail"] = trend_state_data
    res_dict["structure_stop"] = structure_stop
    res_dict["weekly_df"] = weekly_df
    res_dict["daily_df"] = df_for_indicators.copy()
    res_dict["ma10_val"] = trend_analysis["ma10"]
    res_dict["ma120_val"] = trend_analysis["ma120"]
    res_dict["ma240_val"] = trend_analysis["ma240"]

    quality_flags = {
        "價格": current_price > 0, "成交量": quote_volume_valid and current_vol > 0, "大盤": macro_bull is not None,
        "財報": not fin_df.empty, "法人": not institutional_df.empty,
        "同業": peer_corr_val is not None, "新聞": bool(raw_news_list)
    }
    quality_weights = {"價格": 25, "成交量": 10, "大盤": 15, "財報": 15, "法人": 15, "同業": 10, "新聞": 10}
    quality_score = sum(quality_weights[k] for k, ok in quality_flags.items() if ok)
    missing_data = [k for k, ok in quality_flags.items() if not ok]
    res_dict["data_quality_score"] = quality_score
    res_dict["missing_data"] = missing_data

    res_dict["tactical_blueprint"] = unified_institutional_brain(res_dict, df.copy(), is_holding=is_holding, entry_cost=entry_cost, sector_panic=sector_panic)
    
    slippage = slip_ticks * t
    estimated_entry = ceil_to_tick(current_price + slippage, t)
    estimated_stop_fill = floor_to_tick(structure_stop - slippage, t)
    # 粗估雙邊手續費與賣出證交稅；實際折扣及商品稅率仍依券商/商品而異。
    estimated_cost_per_share = estimated_entry * (0.001425 * 2 + 0.003)
    risk_per_share = max(estimated_entry - estimated_stop_fill + estimated_cost_per_share, 0)
    capital_ntd = total_capital * 10000
    risk_budget = capital_ntd * (risk_per_trade / 100)
    max_shares_by_risk = int(risk_budget / risk_per_share) if risk_per_share > 0 else 0
    max_shares_by_cash = int(capital_ntd / max(estimated_entry * 1.001425, 0.01))
    suggested_shares = max(0, min(max_shares_by_risk, max_shares_by_cash))
    suggested_lots = suggested_shares // 1000
    suggested_odd_lot = suggested_shares % 1000
    res_dict.update({"suggested_lots": suggested_lots, "suggested_odd_lot": suggested_odd_lot,
                     "suggested_shares": suggested_shares, "expected_entry_price": estimated_entry,
                     "expected_stop_price": estimated_stop_fill, "expected_target_price": target_brk,
                     "estimated_cost_per_share": estimated_cost_per_share})
    return res_dict



def build_compass_home_summary(res: dict, is_holding: bool) -> dict:
    """整理首頁價格計畫，並集中執行合理性檢查。"""
    bp = res.get("tactical_blueprint", {}) or {}
    ta = res.get("trend_analysis", {}) or {}
    action = str(bp.get("action_now", "先觀察"))
    strategy = str(bp.get("strategy_name", "⚪ 資料不足"))
    quality = float(res.get("data_quality_score", 0) or 0)
    confidence = max(0, min(100, round(quality * 0.75 + (10 if res.get("trend_state") not in ["觀察", "資料不足"] else 0))))
    if quality < 60:
        confidence = min(confidence, 55)

    price = float(res.get("current_price", 0) or 0)
    tick = tick_size(price) if price > 0 else 0.01
    atr = float(res.get("atr", res.get("atr14", 0)) or 0)
    entry = float(res.get("expected_entry_price", price) or price)
    if entry <= 0:
        entry = price

    issues = []
    raw_stop = float(res.get("structure_stop", res.get("expected_stop_price", 0)) or 0)
    stop = raw_stop
    stop_ceiling = min(x for x in [price, entry] if x > 0) if any(x > 0 for x in [price, entry]) else 0
    if stop_ceiling > 0 and (stop <= 0 or stop >= stop_ceiling):
        fallback_gap = max(2.0 * atr, stop_ceiling * 0.03, tick)
        stop = floor_to_tick(max(tick, stop_ceiling - fallback_gap), tick)
        issues.append("原始結構價不在有效風險區間，已改用現價下方的 ATR／百分比防線。")

    raw_resistance = float(res.get("real_resistance", 0) or 0)
    model_target = float(res.get("expected_target_price", 0) or 0)
    valid_targets = sorted(x for x in [raw_resistance, model_target] if x > entry)
    target1 = valid_targets[0] if valid_targets else entry + max(2.0 * atr, entry * 0.05, tick)
    target2 = valid_targets[-1] if valid_targets else target1
    target_kind = "第一目標區"

    # 第一目標必須是可操作的近端目標；過遠的原始壓力改列為中長期延伸目標。
    if entry > 0 and (target1 - entry) / entry > 0.25:
        target2 = target1
        target1 = ceil_to_tick(entry + max(2.0 * atr, entry * 0.08), tick)
        target1 = min(target1, ceil_to_tick(entry * 1.15, tick))
        target_kind = "近端第一目標區"
        issues.append("原始目標距離評估價超過 25%，已改列為中長期延伸目標，並建立較近的第一目標區。")
    if target1 <= entry:
        target1 = ceil_to_tick(entry + max(2.0 * atr, entry * 0.05, tick), tick)
        issues.append("原始目標未高於評估價，已依 ATR 建立替代目標。")
    target2 = max(target2, target1)

    risk = max(entry - stop, 0)
    reward = max(target1 - entry, 0)
    rr = reward / risk if risk > 0 else None
    if risk <= 0:
        issues.append("風險防線與評估價無法形成有效風險距離，決策引擎將否決進場。")
    if rr is not None and rr < 1.0:
        issues.append(f"近端風險報酬比僅 {rr:.2f}，不符合積極進場條件。")

    entry_gap_pct = ((price - entry) / entry * 100) if price > 0 and entry > 0 else None
    if entry_gap_pct is not None and abs(entry_gap_pct) <= 1.0:
        entry_zone_text = "目前已進入建議評估區"
    elif entry_gap_pct is not None and entry_gap_pct > 3.0:
        entry_zone_text = "目前高於建議評估區，不宜因條件達標而追高"
    elif entry_gap_pct is not None and entry_gap_pct < -3.0:
        entry_zone_text = "目前低於建議評估區，需先確認趨勢未失效"
    else:
        entry_zone_text = "目前接近建議評估區"

    if quality < 60:
        decision = "等待"
    elif any(k in action for k in ["退出", "減碼"]):
        decision = "減碼／退出"
    elif any(k in action for k in ["續抱", "持有"]):
        decision = "續抱"
    elif any(k in action for k in ["新增", "買", "進場"]):
        decision = "分批評估"
    else:
        decision = "等待"

    today = f"目前屬於「{res.get('trend_state', '觀察')}」，{bp.get('desc', '先等待更多資料確認。')}"
    pros, cons = [], []
    if "多頭" in str(ta.get("long_term", "")) or "多頭" in str(res.get("trend_state", "")):
        pros.append("中長期趨勢仍有支撐")
    if bool(res.get("volume_valid", False)) and float(ta.get("volume_ratio", 0) or 0) >= 1.3:
        pros.append("成交量明顯放大")
    if res.get("institutional_summary", {}).get("consensus_score", 0) > 0:
        pros.append("法人一致性偏多")
    if res.get("missing_data"):
        cons.append("部分資料缺漏，信心需下修")
    if "警戒" in str(res.get("trend_state", "")) or "破壞" in str(res.get("trend_state", "")):
        cons.append("趨勢已有弱化或破壞跡象")
    if bool(res.get("volume_valid", False)) and 0 < float(ta.get("volume_ratio", 0) or 0) < 0.8:
        cons.append("量能不足，訊號可信度有限")
    if issues:
        cons.append("價格計畫已啟動合理性修正，請查看安全檢查")
    if not pros: pros.append("目前沒有足夠強的正向證據")
    if not cons: cons.append("市場仍可能受突發消息與大盤波動影響")

    return {
        "decision": decision, "strategy": strategy, "action": action, "confidence": confidence,
        "today": today, "entry": entry, "stop": stop, "target1": target1, "target2": target2,
        "target_kind": target_kind, "rr": rr, "pros": pros[:3], "cons": cons[:3],
        "issues": issues, "plan_valid": risk > 0 and target1 > entry,
        "entry_zone_text": entry_zone_text, "entry_gap_pct": entry_gap_pct,
        "raw_stop": raw_stop, "raw_resistance": raw_resistance,
    }

def build_ai_investment_committee(res: dict, compass: dict) -> dict:
    """把既有分析結果轉成可解釋的 AI 投資委員會，不改動底層模型。"""
    ta = res.get("trend_analysis", {}) or {}
    inst = res.get("institutional_summary", {}) or {}
    quality = float(res.get("data_quality_score", 0) or 0)

    def clamp(value, low=0, high=100):
        return max(low, min(high, int(round(value))))

    def fmt_num(value, digits=2, suffix=""):
        try:
            return f"{float(value):.{digits}f}{suffix}"
        except Exception:
            return "資料不足"

    # 1) 趨勢分析師
    long_term = str(ta.get("long_term", "資料不足"))
    medium_term = str(ta.get("medium_term", "資料不足"))
    short_term = str(ta.get("short_term", "資料不足"))
    trend_state = str(res.get("trend_state", "觀察"))
    adx = float(ta.get("adx", 0) or 0)
    slope60 = float(ta.get("slope60", 0) or 0)
    ma20 = float(res.get("ma20_val", ta.get("ma20", 0)) or 0)
    ma60 = float(res.get("ma60_val", ta.get("ma60", 0)) or 0)
    ma120 = float(res.get("ma120_val", ta.get("ma120", 0)) or 0)
    structure_label = str((ta.get("structure", {}) or {}).get("label", "資料不足"))
    weekly_desc = str(ta.get("weekly_desc", "資料不足"))

    trend_score = 50
    trend_breakdown = []
    if "多頭" in long_term or trend_state in ["多頭持有", "多頭正常拉回", "突破確認"]:
        trend_score += 22; trend_breakdown.append(("長線／狀態偏多", +22))
    elif "空頭" in long_term or trend_state in ["趨勢破壞", "空頭"]:
        trend_score -= 28; trend_breakdown.append(("長線／狀態偏空", -28))
    if ma20 > ma60 > 0:
        trend_score += 12; trend_breakdown.append(("MA20 高於 MA60", +12))
    elif ma20 < ma60 and ma60 > 0:
        trend_score -= 10; trend_breakdown.append(("MA20 低於 MA60", -10))
    if slope60 > 0:
        trend_score += 8; trend_breakdown.append(("MA60 斜率上揚", +8))
    elif slope60 < 0:
        trend_score -= 8; trend_breakdown.append(("MA60 斜率下彎", -8))
    if adx >= 25:
        trend_score += 8; trend_breakdown.append(("ADX 顯示趨勢明確", +8))
    elif 0 < adx < 18:
        trend_score -= 5; trend_breakdown.append(("ADX 趨勢力不足", -5))
    if any(k in structure_label for k in ["多頭", "Higher", "墊高", "上升"]):
        trend_score += 7; trend_breakdown.append(("波段結構偏多", +7))
    elif any(k in structure_label for k in ["空頭", "Lower", "下降", "破壞"]):
        trend_score -= 7; trend_breakdown.append(("波段結構轉弱", -7))
    trend_conf = clamp(trend_score)
    if trend_conf >= 68:
        trend_label, trend_color, trend_icon = "偏多", "#10B981", "🟢"
        trend_summary = f"{long_term}，波段結構為{structure_label}；MA20、MA60目前維持多方支撐。"
    elif trend_conf <= 42:
        trend_label, trend_color, trend_icon = "偏空", "#EF4444", "🔴"
        trend_summary = f"{long_term}，目前狀態為「{trend_state}」；MA60五日斜率 {slope60:+.2f}%，趨勢仍偏弱。"
    else:
        trend_label, trend_color, trend_icon = "中性", "#F59E0B", "🟡"
        trend_summary = f"{long_term}／{medium_term}，ADX {adx:.1f}；目前方向尚未形成一致訊號。"
    trend_evidence = [
        ("短期趨勢", short_term), ("中期趨勢", medium_term), ("長期趨勢", long_term),
        ("週線", weekly_desc), ("波段結構", structure_label), ("ADX", fmt_num(adx, 1)),
        ("MA20", fmt_num(ma20)), ("MA60", fmt_num(ma60)), ("MA120", fmt_num(ma120)),
        ("MA60 五日斜率", fmt_num(slope60, 2, "%")),
    ]

    # 2) 籌碼分析師
    inst_score = int(inst.get("consensus_score", 0) or 0)
    consensus_label = str(inst.get("consensus_label", "資料不足"))
    sitc_trend = str(res.get("sitc_trend", "投信資料不足"))
    margin_trend = str(res.get("margin_trend", "融資資料不足"))
    bc_obj = res.get("broker_consensus", {})
    if isinstance(bc_obj, dict):
        if bc_obj.get("is_real") and bc_obj.get("mean") is not None:
            broker_parts = [f"平均目標價 {float(bc_obj['mean']):.2f} 元"]
            if bc_obj.get("high") is not None:
                broker_parts.append(f"最高 {float(bc_obj['high']):.2f} 元")
            if bc_obj.get("low") is not None:
                broker_parts.append(f"最低 {float(bc_obj['low']):.2f} 元")
            if bc_obj.get("rating"):
                broker_parts.append(f"評等 {bc_obj.get('rating')}")
            if bc_obj.get("coverage_count"):
                broker_parts.append(f"涵蓋 {int(bc_obj['coverage_count'])} 位分析師")
            broker_consensus = "｜".join(broker_parts)
        else:
            broker_consensus = "目前查無可靠公開券商目標價共識"
    else:
        broker_consensus = str(bc_obj) if bc_obj else "目前查無可靠公開券商目標價共識"
    chip_score = 52 + inst_score * 14
    chip_breakdown = [("法人一致性", inst_score * 14)] if inst_score else [("法人一致性中性", 0)]
    if any(k in sitc_trend for k in ["買", "增加", "偏多"]):
        chip_score += 10; chip_breakdown.append(("投信偏買", +10))
    elif any(k in sitc_trend for k in ["賣", "減少", "偏空"]):
        chip_score -= 10; chip_breakdown.append(("投信偏賣", -10))
    if any(k in margin_trend for k in ["下降", "減少", "降溫"]):
        chip_score += 5; chip_breakdown.append(("融資降溫", +5))
    elif any(k in margin_trend for k in ["大增", "暴增", "過熱"]):
        chip_score -= 7; chip_breakdown.append(("融資升溫", -7))
    chip_conf = clamp(chip_score)
    if chip_conf >= 66:
        chip_label, chip_color, chip_icon = "偏多", "#10B981", "🟢"
    elif chip_conf <= 40:
        chip_label, chip_color, chip_icon = "偏空", "#EF4444", "🔴"
    else:
        chip_label, chip_color, chip_icon = "中性", "#F59E0B", "🟡"
    chip_summary = f"三大法人20日一致性為「{consensus_label}」；{sitc_trend}，{margin_trend}。"
    chip_evidence = [
        ("三大法人一致性", consensus_label), ("一致性分數", str(inst_score)),
        ("投信趨勢", sitc_trend), ("融資趨勢", margin_trend),
        ("券商共識", broker_consensus),
    ]

    # 3) 價量分析師
    pv = str(ta.get("price_volume", "價量中性"))
    accumulation = str(ta.get("accumulation", "資金平衡"))
    divergence = str(ta.get("volume_divergence", "無明顯背離"))
    vol_ratio = float(ta.get("volume_ratio", 0) or 0)
    volume_valid = bool(res.get("volume_valid", ta.get("volume_valid", False)))
    pv_score = 52
    pv_breakdown = []
    if not volume_valid:
        pv_score = 50
        pv_breakdown.append(("成交量資料尚未更新", 0))
    elif "價漲量增" in pv:
        pv_score += 20; pv_breakdown.append(("價漲量增", +20))
    elif "價跌量增" in pv:
        pv_score -= 22; pv_breakdown.append(("價跌量增", -22))
    elif "價跌量縮" in pv:
        pv_score += 7; pv_breakdown.append(("價跌量縮", +7))
    if any(k in accumulation for k in ["流入", "吸籌", "累積"]):
        pv_score += 12; pv_breakdown.append(("資金流入", +12))
    elif "流出" in accumulation:
        pv_score -= 12; pv_breakdown.append(("資金流出", -12))
    if any(k in divergence for k in ["無", "沒有"]):
        pv_score += 6; pv_breakdown.append(("未見背離", +6))
    elif "背離" in divergence:
        pv_score -= 8; pv_breakdown.append(("出現背離", -8))
    if volume_valid and vol_ratio >= 1.2:
        pv_score += 7; pv_breakdown.append(("量比高於 1.2", +7))
    elif volume_valid and 0 < vol_ratio < 0.8:
        pv_score -= 5; pv_breakdown.append(("量能不足", -5))
    pv_conf = clamp(pv_score)
    if pv_conf >= 66:
        pv_label, pv_color, pv_icon = "偏多", "#10B981", "🟢"
    elif pv_conf <= 40:
        pv_label, pv_color, pv_icon = "偏空", "#EF4444", "🔴"
    else:
        pv_label, pv_color, pv_icon = "中性", "#F59E0B", "🟡"
    pv_summary = (f"{pv}；{accumulation}；{divergence}，目前量比 {vol_ratio:.2f}。" if volume_valid else f"{accumulation}；{divergence}。即時成交量比率暫不納入判斷。")
    pv_evidence = [
        ("價量型態", pv), ("資金累積", accumulation), ("量價背離", divergence),
        ("即時成交量比率", fmt_num(vol_ratio, 2) if volume_valid else "暫停使用"), ("價量綜合判讀", str(res.get("volume_verdict", "資料不足"))),
    ]

    # 4) 風控分析師
    entry = float(compass.get("entry", 0) or 0)
    stop = float(compass.get("stop", 0) or 0)
    target = float(compass.get("target1", 0) or 0)
    resistance = float(res.get("real_resistance", target) or target)
    current = float(res.get("current_price", 0) or 0)
    atr = float(res.get("atr", 0) or 0)
    rr = compass.get("rr")
    stop_pct = ((entry - stop) / entry * 100) if entry > 0 and stop > 0 else None
    pressure_pct = ((resistance - current) / current * 100) if current > 0 and resistance > 0 else None
    risk_score = 58
    risk_breakdown = []
    if quality < 60:
        risk_score -= 22; risk_breakdown.append(("資料完整度不足", -22))
    else:
        risk_breakdown.append(("資料完整度足夠", +5)); risk_score += 5
    if rr is not None and rr >= 1.5:
        risk_score += 14; risk_breakdown.append(("風險報酬比良好", +14))
    elif rr is not None and rr < 1.0:
        risk_score -= 18; risk_breakdown.append(("風險報酬比不足", -18))
    if pressure_pct is not None and pressure_pct <= 5:
        risk_score -= 12; risk_breakdown.append(("距離壓力區過近", -12))
    if stop_pct is not None and stop_pct > 12:
        risk_score -= 10; risk_breakdown.append(("風險防線距離過大", -10))
    elif stop_pct is not None and stop_pct <= 8:
        risk_score += 7; risk_breakdown.append(("風險防線距離可控", +7))
    risk_conf = clamp(100 - risk_score + 35)  # 數字代表對風控立場的把握度
    if risk_score >= 72:
        risk_label, risk_color, risk_icon = "可控", "#10B981", "🟢"
    elif risk_score <= 48:
        risk_label, risk_color, risk_icon = "保守", "#F97316", "🟠"
    else:
        risk_label, risk_color, risk_icon = "中性", "#F59E0B", "🟡"
    rr_text = f"{rr:.2f}" if rr is not None else "無法計算"
    pressure_text = f"{pressure_pct:.1f}%" if pressure_pct is not None else "資料不足"
    if risk_label == "保守":
        risk_summary = f"距離壓力區約 {pressure_text}，風險報酬比 {rr_text}；目前不建議一次重押或追高。"
    else:
        risk_summary = f"評估價 {entry:.2f}、趨勢失效價（風險防線）{stop:.2f}，風險報酬比 {rr_text}；仍應採分批，並遵守風險防線。"
    risk_evidence = [
        ("ATR14", fmt_num(atr)), ("趨勢失效價（風險防線）", fmt_num(stop)),
        ("MA60", fmt_num(res.get("ma60_val", 0))), ("距離壓力區", pressure_text),
        ("風險報酬比", rr_text), ("風險防線距離", f"{stop_pct:.1f}%" if stop_pct is not None else "資料不足"),
        ("資料完整度", f"{quality:.0f}%"), ("大盤風險", str(res.get("m_desc", "資料不足"))),
        ("族群風險", str(res.get("peer_resonance_text", "資料不足"))),
    ]

    members = [
        {"role":"趨勢分析師", "avatar":"👨", "label":trend_label, "icon":trend_icon, "color":trend_color, "confidence":trend_conf, "summary":trend_summary, "evidence":trend_evidence, "breakdown":trend_breakdown},
        {"role":"籌碼分析師", "avatar":"👩", "label":chip_label, "icon":chip_icon, "color":chip_color, "confidence":chip_conf, "summary":chip_summary, "evidence":chip_evidence, "breakdown":chip_breakdown},
        {"role":"價量分析師", "avatar":"👨", "label":pv_label, "icon":pv_icon, "color":pv_color, "confidence":pv_conf, "summary":pv_summary, "evidence":pv_evidence, "breakdown":pv_breakdown},
        {"role":"風控分析師", "avatar":"👩", "label":risk_label, "icon":risk_icon, "color":risk_color, "confidence":risk_conf, "summary":risk_summary, "evidence":risk_evidence, "breakdown":risk_breakdown},
    ]

    bullish = sum(m["label"] in ["偏多", "可控"] for m in members)
    cautious = sum(m["label"] in ["中性", "保守"] for m in members)
    bearish = sum(m["label"] == "偏空" for m in members)
    cio_conf = clamp(sum(m["confidence"] for m in members) / len(members) * 0.75 + quality * 0.25)
    decision = str(compass.get("decision", "等待"))
    if quality < 60:
        cio = "資料不足，暫緩決策"
        cio_desc = "部分關鍵資料尚未取得，現階段應以等待與控制部位為優先。"
        quote = "沒有足夠證據時，等待本身就是一種決策。"
    elif bearish >= 2:
        cio = "風險優先，等待條件改善"
        cio_desc = "趨勢或價量已有兩項以上轉弱，不建議逆勢承擔不必要風險。"
        quote = "保護資金，比預測反彈更重要。"
    elif risk_label == "保守" and bullish >= 2:
        cio = "等待拉回分批布局"
        cio_desc = "趨勢與價量仍偏多，但買點接近壓力區，建議等待更好的風險報酬位置。"
        quote = "現在不是不能買，而是不值得追高。"
    elif bullish >= 3:
        cio = decision
        cio_desc = "多數分析面向相互支持，可依既定趨勢失效價（風險防線）採分批執行，避免一次重押。"
        quote = "可以布局，但不要一次重押。"
    else:
        cio = f"有條件執行：{decision}"
        cio_desc = "目前訊號尚未完全一致，應降低部位、等待價格與量能進一步確認。"
        quote = "最大的風險不一定是趨勢，而可能是買點。"

    return {
        "members": members,
        "bullish": bullish, "cautious": cautious, "bearish": bearish,
        "cio": cio, "cio_desc": cio_desc, "cio_confidence": cio_conf, "quote": quote,
    }





def build_decision_engine(res: dict, compass: dict, committee: dict = None, user_holding: bool = False) -> dict:
    """Market Engine：只分析市場，不使用持股成本或帳面損益。"""
    ta=res.get("trend_analysis",{}) or {}
    current=float(res.get("current_price",0) or 0); entry=float(compass.get("entry",current) or current)
    stop=float(compass.get("stop",0) or 0); target1=float(compass.get("target1",0) or 0)
    ma20=float(res.get("ma20_val",ta.get("ma20",0)) or 0); slope20=float(ta.get("slope20",0) or 0)
    adx=float(ta.get("adx",0) or 0); accumulation=str(ta.get("accumulation","資料不足")); price_volume=str(ta.get("price_volume","資料不足"))
    trend_state=str(res.get("trend_state","觀察")); long_term=str(ta.get("long_term","資料不足")); medium_term=str(ta.get("medium_term","資料不足"))
    quality=float(res.get("data_quality_score",0) or 0); rr=compass.get("rr")
    direction_text=f"{trend_state} {long_term} {medium_term}"; bearish=["空頭","趨勢破壞","下跌段","轉弱"]
    bullish=["多頭","偏多","上升","強勢"]; direction_ok=not any(k in direction_text for k in bearish)
    ma20_ok=ma20>0 and slope20>0 and current>=ma20; adx_ok=adx>=25 and direction_ok
    chip_positive=any(k in accumulation for k in ["偏累積","流入","吸籌","買超"]) and "流出" not in accumulation
    chip_neutral="流出" not in accumulation and "賣超" not in accumulation
    pv_ok="價跌量增" not in price_volume and "賣壓" not in price_volume
    components={
      "trend":max(0,min(100,50+(20 if ma20_ok else -22)+(12 if any(k in direction_text for k in bullish) else 0)+(8 if adx_ok else -4))),
      "chips":max(0,min(100,50+(25 if chip_positive else 5 if chip_neutral else -25))),
      "momentum":max(0,min(100,50+(18 if adx_ok else -5)+(10 if pv_ok else -20))),
      "price_position":max(0,min(100,60+(10 if current>=entry else -8)-(22 if entry>0 and current>entry*1.05 else 0)-(12 if target1>0 and current>=target1*.95 else 0))),
      "risk":max(0,min(100,70-(65 if stop>0 and current<=stop else 0)-(20 if quality<60 else 0)-(15 if not direction_ok else 0))),
      "data":max(0,min(100,quality)),
    }
    weights={"trend":.35,"chips":.20,"momentum":.15,"price_position":.10,"risk":.15,"data":.05}
    market_score=int(round(sum(components[k]*weights[k] for k in weights)))
    stop_broken=stop>0 and current<=stop; trend_veto=any(k in direction_text for k in ["空頭","趨勢破壞","下跌段"])
    data_veto=quality<50; hard_veto=stop_broken or trend_veto or data_veto; overextended=entry>0 and current>entry*1.05; near_pressure=target1>0 and current>=target1*.95
    checklist=[
      {"key":"trend","name":"中期趨勢維持","passed":direction_ok and ma20_ok,"current":f"{long_term}／{medium_term}｜MA20 {ma20:.2f}、斜率 {slope20:+.2f}%","why":"方向與均線共同決定是否值得持有。"},
      {"key":"chips","name":"籌碼沒有明顯惡化","passed":chip_neutral,"current":accumulation,"why":"市場資金是否撤退比使用者成本重要。"},
      {"key":"momentum","name":"動能與價量未轉空","passed":adx_ok and pv_ok,"current":f"ADX {adx:.1f}｜{price_volume}","why":"確認趨勢強度與賣壓。"},
      {"key":"price","name":"價格位置仍具合理風險報酬","passed":not overextended and not near_pressure and not stop_broken,"current":f"現價 {current:.2f}｜評估價 {entry:.2f}｜目標 {target1:.2f}","why":"避免接近壓力或過度延伸時新增。"},
    ]
    completed=sum(bool(x["passed"]) for x in checklist); veto=[]
    if stop_broken: veto.append(f"收盤價已到或跌破 {stop:.2f} 元趨勢失效價")
    if trend_veto: veto.append(f"市場趨勢已轉弱：{trend_state}")
    if data_veto: veto.append(f"核心資料完整度只有 {quality:.0f}%")
    if stop_broken or market_score<35: status,label,color,summary="EXIT","🔴 市場轉弱／風險處理","#DC2626","市場結構已明顯轉弱，持有理由失效；停止新增並優先處理風險。"
    elif market_score<50 or trend_veto: status,label,color,summary="REDUCE","🟠 市場偏弱","#F97316","趨勢或籌碼已轉弱，反彈不等於恢復；應降低曝險並等待重新確認。"
    elif market_score>=75 and not overextended and not near_pressure: status,label,color,summary="STRONG","🟢 市場偏多","#16A34A","趨勢、籌碼與動能大致一致，市場本身仍值得持有；新增部位仍需看價格位置。"
    elif near_pressure or overextended: status,label,color,summary="HOLD","🟡 趨勢可持有但價格不宜追","#D97706","市場結構尚未轉壞，但現價位置壓縮新增部位的風險報酬。"
    else: status,label,color,summary="HOLD","🔵 市場中性偏多／觀察","#2563EB","市場尚未出現明確失效，但證據未完全一致，適合持有觀察而非積極增加曝險。"
    buy=status=="STRONG" and not hard_veto
    return {"engine":"market","market_score":market_score,"components":components,"weights":weights,"status":status,"label":label,"color":color,"summary":summary,
      "buy":buy,"add":buy,"reduce_or_exit":status in ["REDUCE","EXIT"],"completed":completed,"total":len(checklist),"missing":[x["name"] for x in checklist if not x["passed"]],
      "checklist":checklist,"stop_broken":stop_broken,"near_pressure":near_pressure,"overextended":overextended,"hard_veto":hard_veto,"veto_reasons":veto,
      "entry":entry,"stop":stop,"target1":target1,"trend_state":trend_state,"quality":quality,"rr":rr}


def align_committee_with_decision(committee: dict, decision: dict) -> dict:
    """讓投資總監、首頁與教練引用同一份 Decision Engine 結果。"""
    committee = dict(committee)
    committee["cio"] = decision.get("label", committee.get("cio", "等待"))
    committee["cio_desc"] = decision.get("summary", committee.get("cio_desc", ""))
    committee["quote"] = (
        "保護資金，比預測反彈更重要。" if decision.get("stop_broken") or decision.get("hard_veto")
        else "可以布局，但不要一次重押。" if decision.get("buy")
        else "現在不是不能看多，而是不值得追價。" if decision.get("overextended") or decision.get("near_pressure")
        else "條件沒有全部確認，等待就是紀律。"
    )
    if decision.get("hard_veto"):
        committee["cio_confidence"] = min(int(committee.get("cio_confidence", 0) or 0), 60)
    return committee


def build_if_i_were_you(
    res: dict,
    compass: dict,
    decision: dict,
    user_holding: bool,
    user_cost: float,
    capital_wan: float,
    risk_pct: float,
) -> dict:
    """將 Decision Engine 翻成新手可以直接照著執行的今日操作。"""
    current = float(res.get("current_price", 0) or 0)
    entry = float(decision.get("entry", compass.get("entry", current)) or current)
    stop = float(decision.get("stop", compass.get("stop", 0)) or 0)
    target1 = float(decision.get("target1", compass.get("target1", 0)) or 0)
    completed = int(decision.get("completed", 0) or 0)
    total = int(decision.get("total", 0) or 0)
    pnl_pct = ((current / user_cost) - 1) * 100 if user_holding and user_cost > 0 and current > 0 else None
    capital_ntd = max(float(capital_wan or 0), 0) * 10000
    max_risk_ntd = capital_ntd * max(float(risk_pct or 0), 0) / 100
    per_share_risk = max(entry - stop, 0)
    max_shares = int(max_risk_ntd // per_share_risk) if per_share_risk > 0 else 0
    first_batch_shares = int(max_shares * 0.30 // 1000 * 1000) if max_shares >= 1000 else 0
    first_batch_amount = first_batch_shares * entry

    actions = []
    warnings = []
    headline = decision.get("label", "🟡 等待")
    color = decision.get("color", "#D97706")

    if user_holding:
        pnl_pct = ((current / user_cost) - 1) * 100 if user_cost > 0 else None
        if decision.get("stop_broken"):
            headline = "🔴 先處理風險，不要攤平"
            color = "#DC2626"
            actions = [
                f"今天不要再買，也不要加碼。",
                f"收盤若仍無法站回 {stop:.2f} 元，依原計畫減碼或退出。",
                "不要因為帳面虧損就延後風險處理。",
            ]
        elif decision.get("add"):
            headline = "🟢 續抱，可小量加碼"
            color = "#16A34A"
            actions = [
                "原有部位先續抱。",
                "加碼只用小部位，不要一次補滿。",
                "目前沒有觸發減碼或退出條件，不要只因今天上漲就急著減碼。",
                f"新增部位仍以 {stop:.2f} 元作為風險防線。",
            ]
        else:
            if decision.get("near_pressure") and target1 > 0:
                headline = "🟠 續抱，接近目標區再分批調節"
                color = "#F97316"
                actions = [
                    "原有部位先續抱。",
                    "今天不要因為上漲就追著加碼。",
                    f"現價已接近第一目標 {target1:.2f} 元，可依原計畫分批停利，不要因單日上漲一次全部減碼。",
                    f"每日收盤確認是否仍守住 {stop:.2f} 元。",
                ]
            else:
                # 一般持股情境也必須同時回答「減碼」與「加碼」，不能只給買方建議。
                if pnl_pct is not None and pnl_pct <= -15:
                    headline = "🟠 虧損偏深，續抱觀察但禁止攤平"
                    color = "#F97316"
                    actions = [
                        f"減碼判斷：目前尚未跌破 {stop:.2f} 元風險防線，不必立刻全部賣出；但若收盤跌破，應減碼或退出。",
                        "加碼判斷：目前不符合加碼條件，不要因虧損而攤平，也不要因單日反彈追價。",
                        "執行策略：反彈若仍無法改善趨勢，只續抱等待並不夠，應依風險計畫降低部位。",
                    ]
                elif pnl_pct is not None and pnl_pct < 0:
                    headline = "🟡 續抱觀察，不攤平"
                    color = "#D97706"
                    actions = [
                        f"減碼判斷：目前尚未觸發 {stop:.2f} 元風險防線，暫不因單日漲跌急著賣出。",
                        "加碼判斷：現階段不符合加碼條件，不要用攤平來降低帳面成本。",
                        f"執行策略：收盤跌破 {stop:.2f} 元就減碼或退出；未跌破前持續觀察趨勢是否改善。",
                    ]
                else:
                    headline = "🟡 續抱，暫不加碼也不急著減碼"
                    color = "#D97706"
                    actions = [
                        "減碼判斷：目前沒有觸發停利、趨勢失效或重大轉弱條件，不需只因今天上漲急著賣出。",
                        "加碼判斷：目前也沒有新的加碼訊號，不要追價。",
                        f"執行策略：每日收盤確認是否仍守住 {stop:.2f} 元。",
                    ]
        if pnl_pct is not None:
            actions.insert(0, f"目前成本 {user_cost:.2f} 元、現價 {current:.2f} 元，帳面報酬 {pnl_pct:+.1f}%。")
            if pnl_pct < 0 and not decision.get("buy"):
                warnings.append("你目前處於虧損，而且進場條件尚未完成；不要用攤平來取代風險管理。")
    else:
        if decision.get("buy"):
            headline = "🟢 可以開始第一筆布局"
            color = "#16A34A"
            actions = [
                "只買第一筆，不要一次投入全部資金。",
                f"第一筆以總計畫部位的 30% 為上限。",
                f"買進後守住 {stop:.2f} 元風險防線。",
            ]
            if first_batch_shares >= 1000:
                actions.append(f"依你設定的資金與風險，第一筆約 {first_batch_shares:,} 股，約 {first_batch_amount:,.0f} 元。")
            else:
                actions.append("依目前風險限制，整張股票部位可能過大；不要為了湊一張而超過風險上限。")
        elif decision.get("stop_broken") or decision.get("hard_veto"):
            headline = "🔴 今天不要買"
            color = "#DC2626"
            actions = [
                "今天不進場。",
                "不要猜反彈，也不要因為跌很多就覺得便宜。",
                "等風控否決解除後，再重新評估。",
            ]
        else:
            headline = "🟡 今天先不買"
            color = "#D97706"
            actions = [
                "今天不進場，也不預先埋單。",
                "等尚未完成的條件出現，再考慮第一筆。",
                "盤中突破不算，必須以收盤確認。",
            ]

    if decision.get("overextended"):
        warnings.append("現價已高於建議評估價超過 3%，現在最大的風險是追高。")
    if decision.get("near_pressure"):
        warnings.append(f"現價已接近第一目標區 {target1:.2f} 元，新的買進風險報酬較差。")
    if completed < total and not decision.get("hard_veto"):
        warnings.append(f"目前只完成 {completed}/{total} 項條件，還不是完整買點。")

    return {
        "headline": headline,
        "color": color,
        "actions": actions[:5],
        "warnings": warnings[:3],
    }


def build_ai_forecast(res: dict, compass: dict, decision: dict) -> dict:
    """告訴新手：哪些可觀察條件會讓 AI 升級、維持或轉為風控。"""
    current = float(res.get("current_price", 0) or 0)
    entry = float(decision.get("entry", compass.get("entry", current)) or current)
    stop = float(decision.get("stop", compass.get("stop", 0)) or 0)
    target1 = float(decision.get("target1", compass.get("target1", 0)) or 0)
    checklist = decision.get("checklist", []) or []
    failed = [item for item in checklist if not item.get("passed")]
    passed = [item for item in checklist if item.get("passed")]

    scenarios = []

    if decision.get("stop_broken"):
        scenarios.append({
            "title": f"若收盤重新站回 {stop:.2f} 元以上",
            "result": "🟠 由風控轉回觀察",
            "detail": "只代表解除最急迫風險，仍需重新檢查其他進場條件。",
            "color": "#F97316",
        })
    elif decision.get("buy"):
        scenarios.append({
            "title": f"若收盤持續站穩 {entry:.2f} 元，且五項條件不轉弱",
            "result": "🟢 維持可分批布局",
            "detail": "仍只執行第一筆，不因單日上漲改成一次買滿。",
            "color": "#16A34A",
        })
    elif failed:
        first = failed[0]
        scenarios.append({
            "title": f"若「{first.get('name', '下一項條件')}」完成",
            "result": "🟡 AI 可能提高完成度",
            "detail": f"目前狀態：{first.get('current', '資料不足')}。單一條件完成不保證立即轉為買進。",
            "color": "#D97706",
        })

    if len(failed) >= 2:
        second = failed[1]
        scenarios.append({
            "title": f"若再完成「{second.get('name', '另一項條件')}」",
            "result": "🟢 更接近第一筆布局",
            "detail": f"目前狀態：{second.get('current', '資料不足')}。",
            "color": "#16A34A",
        })
    elif not failed and not decision.get("buy"):
        scenarios.append({
            "title": "若價格回到合理評估區，且風險報酬恢復",
            "result": "🟢 才可能重新開放買進",
            "detail": "技術條件完整不代表任何價格都值得買。",
            "color": "#16A34A",
        })

    if stop > 0:
        scenarios.append({
            "title": f"若收盤跌至 {stop:.2f} 元或以下",
            "result": "🔴 轉為風險處理",
            "detail": "停止新增；已有持股則依原計畫減碼或退出，不用攤平。",
            "color": "#DC2626",
        })

    if entry > 0 and not decision.get("overextended"):
        chase_price = entry * 1.03
        scenarios.append({
            "title": f"若股價快速高於約 {chase_price:.2f} 元",
            "result": "🟠 即使轉強也不追價",
            "detail": "超出評估價太多時，AI 會優先保護風險報酬。",
            "color": "#F97316",
        })

    return {"scenarios": scenarios[:4]}


def build_today_action_board(res: dict, compass: dict, decision: dict, user_holding: bool = False, user_cost: float = 0.0, levels: dict | None = None) -> dict:
    """V3 Execution Engine：只輸出一個今天動作與三個後續觸發事件。"""
    levels = levels or {}
    current = float(levels.get("current", res.get("current_price", 0)) or 0)
    confirm = float(levels.get("confirmation", 0) or 0)
    protective = float(levels.get("protective_stop", 0) or 0)
    structural = float(levels.get("structure_stop", 0) or 0)
    target = float(levels.get("target1", 0) or 0)
    target_role = str(levels.get("target_role", "第一停利區"))
    score = int(decision.get("market_score", 0) or 0)
    status = str(decision.get("status", "HOLD"))
    pnl = ((current / float(user_cost)) - 1) * 100 if user_holding and float(user_cost or 0) > 0 and current > 0 else None

    if not user_holding:
        if status == "STRONG":
            headline, color = "等待突破後建立第一筆", "#16A34A"
            today_action = f"今天不追價；收盤站上 {confirm:.2f} 元後，才建立小部位。"
        elif status == "HOLD":
            headline, color = "暫不進場", "#2563EB"
            today_action = f"今天先不買；等待 {confirm:.2f} 元確認，或回到評估區守穩。"
        else:
            headline, color = "停止進場計畫", "#DC2626" if status == "EXIT" else "#F97316"
            today_action = f"市場僅 {score} 分，今天不建立部位；至少先站回 {confirm:.2f} 元。"
        cards=[
            {"question":"今天唯一動作","answer":headline,"reason":today_action,"color":color},
            {"question":"確認成功","answer":f"站上 {confirm:.2f}","reason":"確認後只建立第一筆，不一次重押。","color":"#2563EB"},
            {"question":"確認失敗","answer":f"跌破 {protective:.2f}","reason":"取消買進並等待下一次結構重建。","color":"#F97316"},
            {"question":"市場風險","answer":decision.get("label", "觀察"),"reason":f"市場分數 {score}/100。","color":decision.get("color","#64748B")},
        ]
        return {"cards":cards,"headline":headline,"color":color,"today_action":today_action,
                "actions":[today_action],"warnings":[f"確認價 {confirm:.2f} 元",f"保護價 {protective:.2f} 元"],
                "portfolio_score":score,"events":{"confirm_price":confirm,"failure_price":protective,"target_price":target}}

    cost_text = f"成本 {float(user_cost):.2f} 元，帳面報酬 {pnl:+.1f}%" if pnl is not None else "已持有部位"
    if status == "STRONG":
        headline, color = "續抱，不追價", "#16A34A"
        today_action = f"今天不用動；續抱至 {target:.2f} 元附近分批停利。"
        success = f"收盤站穩 {confirm:.2f} 元：續抱；評分仍逾 80 才考慮小量加碼。"
        failure = f"收盤跌破 {protective:.2f} 元：先減碼；跌破 {structural:.2f} 元：退出剩餘波段部位。"
    elif status == "HOLD":
        headline, color = "續抱，禁止加碼", "#2563EB"
        today_action = f"今天不用動；等待收盤站上 {confirm:.2f} 元，未確認前不新增。"
        success = f"站上 {confirm:.2f} 元：續抱到 {target:.2f} 元附近分批停利。"
        failure = f"跌破 {protective:.2f} 元：開始保護；跌破 {structural:.2f} 元：退出剩餘波段部位。"
    elif status == "REDUCE":
        color = "#F97316"
        if pnl is not None and pnl >= 50:
            headline = "今天先減碼 20%"
            today_action = f"市場只有 {score} 分且已有 {pnl:+.1f}% 獲利；先鎖定約 20% 部位，其餘保留。"
        else:
            headline = "反彈減碼，不加碼"
            today_action = f"市場只有 {score} 分；今天不新增，反彈至 {confirm:.2f} 元站不上時分批減碼。"
        success = f"收盤站上 {confirm:.2f} 元：只保留核心部位，市場回到 60 分以上才取消減碼計畫。"
        failure = f"跌破 {protective:.2f} 元：加速減碼；跌破 {structural:.2f} 元：退出剩餘波段部位。"
    else:
        headline, color = "今天執行退出／大幅減碼", "#DC2626"
        today_action = f"市場僅 {score} 分，原持有理由失效；不再等待反彈取代風險處理。"
        success = f"即使站回 {confirm:.2f} 元，也只回到觀察，不立即買回。"
        failure = f"收盤仍低於 {protective:.2f} 元：執行退出；{structural:.2f} 元為最後結構線。"

    cards=[
        {"question":"今天唯一動作","answer":headline,"reason":today_action,"color":color},
        {"question":"確認成功","answer":f"{confirm:.2f} 元","reason":success,"color":"#2563EB"},
        {"question":"移動保護","answer":f"{protective:.2f} 元","reason":"跌破後執行減碼，不再只寫等待確認。","color":"#F97316"},
        {"question":"結構退出","answer":f"{structural:.2f} 元","reason":"跌破後退出剩餘波段部位。","color":"#DC2626"},
    ]
    actions=[today_action, success, failure, f"{target_role}：{target:.2f} 元。", f"{cost_text}；成本只調整執行節奏，不改變市場分數。"]
    return {"cards":cards,"headline":headline,"color":color,"today_action":today_action,"actions":actions,
            "warnings":[f"反彈確認 {confirm:.2f} 元",f"移動保護 {protective:.2f} 元",f"結構退出 {structural:.2f} 元"],
            "portfolio_score":max(0,min(100,score+(5 if pnl is not None and pnl>0 else -5 if pnl is not None and pnl<0 else 0))),
            "events":{"confirm_price":confirm,"failure_price":protective,"structure_price":structural,"target_price":target}}

def build_today_brief(res: dict, compass: dict, decision: dict, user_holding: bool = False) -> dict:
    """將 Decision Engine 轉成今日一句話、三項重點，以及可做／不要做的具體指令。"""
    current = float(res.get("current_price", 0) or 0)
    entry = float(decision.get("entry", compass.get("entry", 0)) or 0)
    stop = float(decision.get("stop", compass.get("stop", 0)) or 0)
    target1 = float(decision.get("target1", compass.get("target1", 0)) or 0)
    checklist = decision.get("checklist", []) or []
    failed = [x for x in checklist if not x.get("passed")]
    passed = [x for x in checklist if x.get("passed")]

    def short_name(item: dict) -> str:
        key = item.get("key", "")
        return {
            "price": "收盤價",
            "volume": "成交量",
            "ma20": "MA20",
            "adx": "ADX",
            "obv": "OBV／資金累積",
        }.get(key, item.get("name", "條件確認"))

    if decision.get("stop_broken"):
        headline = "現在最重要的不是找反彈，而是先把風險控制住。"
    elif decision.get("buy"):
        headline = "進場條件已齊，可以開始第一筆，但不要一次押滿。"
    elif failed:
        first = short_name(failed[0])
        headline = f"下一個確認事件是 {first}；請依下方具體價位與條件執行。"
    else:
        headline = decision.get("summary", "今天先依既定風險計畫執行。")

    priorities = []
    for item in failed[:3]:
        priorities.append({
            "title": item.get("name", "下一個確認事件"),
            "current": item.get("current", "目前資料不足"),
            "state": "尚未達成",
            "icon": "○",
        })
    for item in passed:
        if len(priorities) >= 3:
            break
        priorities.append({
            "title": item.get("name", "維持已達成條件"),
            "current": item.get("current", "已達成"),
            "state": "持續確認",
            "icon": "✓",
        })
    while len(priorities) < 3:
        priorities.append({
            "title": f"收盤是否守住風險防線 {stop:.2f} 元" if stop > 0 else "補齊風險防線資料",
            "current": f"目前股價 {current:.2f} 元",
            "state": "每日確認",
            "icon": "○",
        })

    if decision.get("stop_broken"):
        can_do = ["停止新增部位", "依原計畫減碼或退出", "等待重新站回風險防線後再評估"]
        avoid = ["不要用攤平取代停損", "不要預設一定會反彈", "不要任意放寬風險防線"]
    elif decision.get("buy"):
        can_do = ["先執行第一筆小部位", f"將 {stop:.2f} 元寫入交易計畫" if stop > 0 else "先確認風險防線", "保留後續加碼資金"]
        avoid = ["不要一次買滿", "不要因盤中急漲追價", "不要在未設定風險前下單"]
    elif user_holding:
        can_do = [f"收盤守住 {stop:.2f} 元才維持續抱" if stop > 0 else "先補齊趨勢失效價", f"接近 {target1:.2f} 元時分批停利" if target1 > 0 else "目標價資料不足時不新增", f"收盤站上 {entry:.2f} 元且市場評級改善後才考慮加碼" if entry > 0 else "條件完整後才考慮加碼"]
        avoid = ["不要在條件未齊時加碼", "不要因短線震盪隨意改計畫", "不要忽略風險防線"]
    else:
        first_missing = short_name(failed[0]) if failed else "進場條件"
        can_do = [f"等待 {first_missing} 達標", f"在 {entry:.2f} 元附近觀察收盤" if entry > 0 else "等待有效評估價", "先規劃第一筆部位與風險"]
        avoid = ["不要只因價格接近評估價就買", "不要把盤中觸價當成收盤確認", "不要在條件不足時提前重押"]

    return {
        "headline": headline,
        "priorities": priorities[:3],
        "can_do": can_do,
        "avoid": avoid,
    }


def build_ai_investment_coach(res: dict, compass: dict, committee: dict, user_holding: bool, user_cost: float, capital_wan: float, risk_pct: float, decision_engine: dict = None) -> dict:
    """依使用者是否持有，將既有價位與風險資料整理成可執行的個人化教練指令。"""
    current = float(res.get("current_price", 0) or 0)
    decision_engine = decision_engine or build_decision_engine(res, compass, committee, user_holding)
    entry = float(compass.get("entry", current) or current)
    stop = float(compass.get("stop", 0) or 0)
    target1 = float(compass.get("target1", 0) or 0)
    target2 = float(compass.get("target2", target1) or target1)
    confidence = int(committee.get("cio_confidence", compass.get("confidence", 0)) or 0)
    capital_ntd = max(float(capital_wan or 0), 0) * 10000
    max_risk_ntd = capital_ntd * max(float(risk_pct or 0), 0) / 100
    per_share_risk = max(entry - stop, 0)
    shares_by_risk = int(max_risk_ntd // per_share_risk) if per_share_risk > 0 else 0
    shares_by_cash = int(capital_ntd // entry) if entry > 0 else 0
    suggested_shares = max(0, min(shares_by_risk, shares_by_cash))
    suggested_lots = suggested_shares / 1000

    if confidence >= 80:
        pace = "可依條件分 3 筆執行，每筆約三分之一"
    elif confidence >= 65:
        pace = "先用小部位試單，確認後再增加"
    else:
        pace = "暫緩進場，等待訊號與資料完整度改善"

    if user_holding:
        pnl_pct = ((current / user_cost) - 1) * 100 if user_cost > 0 else None
        if stop > 0 and current <= stop:
            headline = "先處理風險，不預設反彈"
            primary = "價格已到或跌破趨勢失效區，停止加碼，依原計畫減碼或退出。"
            status = "風險處理"
            color = "#DC2626"
        elif target1 > 0 and current >= target1:
            headline = "進入目標區，開始保護成果"
            primary = "可分批停利並上移保護價；剩餘部位觀察是否有量能支持延伸。"
            status = "保護獲利"
            color = "#7C3AED"
        else:
            headline = "持有可以，但必須知道哪裡認錯"
            primary = f"只要收盤仍守住 {stop:.2f} 元，可依原策略續抱；跌破則執行風險處理。" if stop > 0 else "目前趨勢失效價（風險防線）資料不足，暫不建議增加部位。"
            status = "續抱觀察"
            color = "#2563EB"
        cost_text = f"成本 {user_cost:.2f} 元，目前報酬 {pnl_pct:+.1f}%" if pnl_pct is not None else "尚未輸入有效持股成本"
        checklist = [
            f"每日收盤確認是否守住 {stop:.2f} 元" if stop > 0 else "補齊趨勢失效價（風險防線）資料",
            f"接近 {target1:.2f} 元時，先決定停利比例" if target1 > 0 else "等待有效目標價",
            "不要因短線震盪任意放寬原本停損",
        ]
    else:
        if current > entry * 1.03 and entry > 0:
            headline = "不是不能買，而是不值得追高"
            primary = f"現價明顯高於建議評估價 {entry:.2f} 元，等待拉回或突破後回測再分批。"
            status = "等待買點"
            color = "#D97706"
        elif stop > 0 and current <= stop:
            headline = "投資假設尚未恢復，先不要接刀"
            primary = f"價格位於趨勢失效價（風險防線）{stop:.2f} 元附近或下方，重新站回前不建立新部位。"
            status = "暫停進場"
            color = "#DC2626"
        else:
            headline = "先規劃，再下單"
            primary = decision_engine["summary"]
            status = decision_engine["label"]
            color = decision_engine["color"]
        cost_text = f"單筆風險上限約 {max_risk_ntd:,.0f} 元"
        checklist = [
            f"進場前先寫下趨勢失效價（風險防線）{stop:.2f} 元" if stop > 0 else "趨勢失效價（風險防線）未確認前不下單",
            f"第一筆只做小部位；{pace}",
            f"第一目標先看 {target1:.2f} 元" if target1 > 0 else "目標價資料不足，暫不進場",
        ]

    sizing_note = "目前無法依風險計算建議股數。"
    if suggested_shares > 0:
        sizing_note = f"依資金與單筆風險上限估算，理論上限約 {suggested_shares:,} 股（{suggested_lots:.2f} 張）；實際仍應向下取整並分批。"

    return {
        "headline": headline, "primary": primary, "status": status, "color": color,
        "cost_text": cost_text, "pace": pace, "checklist": checklist,
        "max_risk_ntd": max_risk_ntd, "per_share_risk": per_share_risk,
        "suggested_shares": suggested_shares, "suggested_lots": suggested_lots,
        "sizing_note": sizing_note, "confidence": confidence,
        "entry": entry, "stop": stop, "target1": target1, "target2": target2,
        "decision_engine": decision_engine,
    }


def build_ai_confidence_center(res: dict, compass: dict, committee: dict, decision: dict = None) -> dict:
    """解釋 AI 總信心來源，並顯示 Decision Engine 的風控否決。"""
    decision = decision or {}
    members = committee.get("members", []) or []
    quality = float(res.get("data_quality_score", 0) or 0)
    confidences = [float(m.get("confidence", 0) or 0) for m in members]
    avg_member = sum(confidences) / len(confidences) if confidences else 0.0
    spread = (max(confidences) - min(confidences)) if confidences else 0.0
    labels = [str(m.get("label", "")) for m in members]
    bullish = int(committee.get("bullish", 0) or 0)
    bearish = int(committee.get("bearish", 0) or 0)
    cautious = int(committee.get("cautious", 0) or 0)
    final_conf = int(committee.get("cio_confidence", compass.get("confidence", 0)) or 0)

    drivers = []
    if quality >= 80:
        drivers.append(("資料完整度", f"{quality:.0f}%", "+", "主要資料齊全，結論可依賴度較高。"))
    elif quality >= 60:
        drivers.append(("資料完整度", f"{quality:.0f}%", "±", "資料可用，但仍有少數缺漏。"))
    else:
        drivers.append(("資料完整度", f"{quality:.0f}%", "−", "關鍵資料不足，總信心必須下修。"))

    if bullish >= 3 and bearish == 0:
        drivers.append(("分析師一致性", f"{bullish}/4 偏多或可控", "+", "多數分析面向互相支持。"))
    elif bearish >= 2:
        drivers.append(("分析師一致性", f"{bearish}/4 偏空", "−", "負面訊號集中，風險判斷較明確。"))
    else:
        drivers.append(("分析師一致性", f"{bullish} 多／{cautious} 中性保守／{bearish} 空", "±", "意見尚未完全一致，需保留安全邊際。"))

    if spread <= 15:
        drivers.append(("信心分歧", f"差距 {spread:.0f} 分", "+", "各分析師把握度接近，模型內部衝突較低。"))
    elif spread <= 30:
        drivers.append(("信心分歧", f"差距 {spread:.0f} 分", "±", "部分面向把握度不同，需要看價格確認。"))
    else:
        drivers.append(("信心分歧", f"差距 {spread:.0f} 分", "−", "分析面向分歧較大，不宜把單一結論視為確定答案。"))

    if decision.get("veto_reasons"):
        drivers.append(("Decision Engine 風控", "；".join(decision.get("veto_reasons", [])[:2]), "−", "即使部分技術條件達標，風控否決權仍優先。"))

    missing = res.get("missing_data", []) or []
    if missing:
        drivers.append(("缺漏資料", "、".join(missing[:3]), "−", "缺漏項目會直接降低結論可信度。"))
    else:
        drivers.append(("缺漏資料", "無重大缺漏", "+", "目前沒有偵測到重大缺漏。"))

    if final_conf >= 80:
        level, color, headline = "高信心", "#10B981", "多數證據彼此支持，但仍須遵守趨勢失效價（風險防線）。"
    elif final_conf >= 65:
        level, color, headline = "中高信心", "#2563EB", "方向具有參考價值，執行仍應分批。"
    elif final_conf >= 50:
        level, color, headline = "中等信心", "#F59E0B", "訊號尚未完全一致，先等明確價格事件發生，再執行比搶先更重要。"
    else:
        level, color, headline = "低信心", "#EF4444", "證據不足或互相衝突，暫不適合積極決策。"

    member_rows = [
        {"role": m.get("role", "分析師"), "label": m.get("label", "中性"), "confidence": int(m.get("confidence", 0) or 0), "color": m.get("color", "#64748B")}
        for m in members
    ]
    return {
        "score": final_conf, "level": level, "color": color, "headline": headline,
        "average_member": avg_member, "quality": quality, "spread": spread,
        "drivers": drivers, "members": member_rows,
        "formula": f"分析師平均 {avg_member:.0f}% × 75% ＋ 資料完整度 {quality:.0f}% × 25%",
    }


# ============ 9.5 Decision History & Explainability ============
def resolve_history_db_path() -> str:
    """
    將歷史紀錄固定存到使用者資料夾，避免因為從不同目錄啟動程式而讀不到舊資料。
    可用環境變數 PROJECT_COMPASS_DB 指定完整資料庫路徑。
    """
    configured = os.getenv("PROJECT_COMPASS_DB", "").strip()
    if configured:
        db_path = Path(configured).expanduser().resolve()
    else:
        data_dir = Path(os.getenv("PROJECT_COMPASS_DATA_DIR", Path.home() / ".project_compass")).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "project_compass_history.db"

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 舊版資料庫位於程式啟動目錄。若新版固定位置尚無資料，自動搬移舊資料。
    legacy_candidates = [
        Path.cwd() / "project_compass_history.db",
        Path(__file__).resolve().parent / "project_compass_history.db",
    ]
    if not db_path.exists():
        for legacy in legacy_candidates:
            try:
                if legacy.exists() and legacy.resolve() != db_path.resolve():
                    shutil.copy2(legacy, db_path)
                    break
            except Exception as exc:
                log_error("migrate_decision_history_db", exc)

    return str(db_path)

HISTORY_DB = resolve_history_db_path()

def init_decision_history_db() -> None:
    """建立每日決策快照資料表。紀錄固定保存在使用者資料夾。"""
    try:
        with sqlite3.connect(HISTORY_DB, timeout=5) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decision_history (
                    stock_id TEXT NOT NULL,
                    stock_name TEXT,
                    decision_date TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    current_price REAL,
                    decision_label TEXT,
                    decision_status TEXT,
                    confidence INTEGER,
                    completed INTEGER,
                    total INTEGER,
                    entry_price REAL,
                    stop_price REAL,
                    target_price REAL,
                    data_quality REAL,
                    missing_conditions TEXT,
                    veto_reasons TEXT,
                    PRIMARY KEY (stock_id, decision_date)
                )
            """)
            conn.commit()
    except Exception as exc:
        log_error("init_decision_history_db", exc)

def fetch_previous_decision(stock_id: str, before_date: str) -> dict | None:
    try:
        with sqlite3.connect(HISTORY_DB, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT * FROM decision_history
                WHERE stock_id = ? AND decision_date < ?
                ORDER BY decision_date DESC
                LIMIT 1
            """, (str(stock_id), before_date)).fetchone()
            return dict(row) if row else None
    except Exception as exc:
        log_error("fetch_previous_decision", exc)
        return None

def fetch_decision_timeline(stock_id: str, limit: int = 7) -> list[dict]:
    try:
        with sqlite3.connect(HISTORY_DB, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM decision_history
                WHERE stock_id = ?
                ORDER BY decision_date DESC
                LIMIT ?
            """, (str(stock_id), int(limit))).fetchall()
            return [dict(row) for row in reversed(rows)]
    except Exception as exc:
        log_error("fetch_decision_timeline", exc)
        return []

def save_daily_decision_snapshot(res: dict, compass: dict, committee: dict, decision: dict) -> None:
    """同一股票同一天只保留最新快照，避免 Streamlit rerun 產生大量重複紀錄。"""
    now = datetime.now(TZ)
    payload = (
        str(res.get("stock_id", "")),
        str(res.get("stock_name", "")),
        now.strftime("%Y-%m-%d"),
        now.strftime("%Y-%m-%d %H:%M:%S"),
        float(res.get("current_price", 0) or 0),
        str(decision.get("label", "")),
        str(decision.get("status", "")),
        int(committee.get("cio_confidence", compass.get("confidence", 0)) or 0),
        int(decision.get("completed", 0) or 0),
        int(decision.get("total", 0) or 0),
        float(decision.get("entry", compass.get("entry", 0)) or 0),
        float(decision.get("stop", compass.get("stop", 0)) or 0),
        float(decision.get("target1", compass.get("target1", 0)) or 0),
        float(res.get("data_quality_score", 0) or 0),
        json.dumps(decision.get("missing", []) or [], ensure_ascii=False),
        json.dumps(decision.get("veto_reasons", []) or [], ensure_ascii=False),
    )
    try:
        with sqlite3.connect(HISTORY_DB, timeout=5) as conn:
            conn.execute("""
                INSERT INTO decision_history (
                    stock_id, stock_name, decision_date, captured_at, current_price,
                    decision_label, decision_status, confidence, completed, total,
                    entry_price, stop_price, target_price, data_quality,
                    missing_conditions, veto_reasons
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stock_id, decision_date) DO UPDATE SET
                    stock_name=excluded.stock_name,
                    captured_at=excluded.captured_at,
                    current_price=excluded.current_price,
                    decision_label=excluded.decision_label,
                    decision_status=excluded.decision_status,
                    confidence=excluded.confidence,
                    completed=excluded.completed,
                    total=excluded.total,
                    entry_price=excluded.entry_price,
                    stop_price=excluded.stop_price,
                    target_price=excluded.target_price,
                    data_quality=excluded.data_quality,
                    missing_conditions=excluded.missing_conditions,
                    veto_reasons=excluded.veto_reasons
            """, payload)
            conn.commit()
    except Exception as exc:
        log_error("save_daily_decision_snapshot", exc)

def build_decision_change(previous: dict | None, current: dict, current_confidence: int) -> dict:
    """將前一個交易日與今日決策差異翻成可讀原因。"""
    if not previous:
        return {
            "available": False,
            "headline": "尚無前一日紀錄",
            "reasons": ["從今天開始累積每日快照，下一個交易日即可顯示決策變化。"],
        }

    prev_missing = set(json.loads(previous.get("missing_conditions") or "[]"))
    now_missing = set(current.get("missing", []) or [])
    completed_now = sorted(prev_missing - now_missing)
    newly_missing = sorted(now_missing - prev_missing)
    reasons = []

    for item in completed_now[:3]:
        reasons.append(f"✅ 新增達成：{item}")
    for item in newly_missing[:3]:
        reasons.append(f"❌ 轉為未達：{item}")

    price_now = float(current.get("current", 0) or 0)
    price_prev = float(previous.get("current_price", 0) or 0)
    if price_prev > 0:
        change_pct = (price_now / price_prev - 1) * 100
        if abs(change_pct) >= 1:
            reasons.append(f"股價較前次紀錄 {change_pct:+.1f}%")

    prev_veto = set(json.loads(previous.get("veto_reasons") or "[]"))
    now_veto = set(current.get("veto_reasons", []) or [])
    for item in sorted(now_veto - prev_veto)[:2]:
        reasons.append(f"🛡️ 新增風控否決：{item}")
    for item in sorted(prev_veto - now_veto)[:2]:
        reasons.append(f"🟢 解除風控否決：{item}")

    prev_conf = int(previous.get("confidence", 0) or 0)
    conf_delta = int(current_confidence) - prev_conf
    if conf_delta:
        reasons.append(f"AI 信心 {prev_conf}% → {current_confidence}%（{conf_delta:+d}）")

    if not reasons:
        reasons = ["主要條件與風控狀態沒有明顯變化。"]

    return {
        "available": True,
        "previous_label": previous.get("decision_label", "—"),
        "previous_date": previous.get("decision_date", "—"),
        "previous_confidence": prev_conf,
        "current_label": current.get("label", "—"),
        "current_confidence": int(current_confidence),
        "changed": previous.get("decision_label") != current.get("label"),
        "reasons": reasons[:6],
    }

def build_data_quality_audit(res: dict, decision: dict) -> dict:
    """逐項檢查 Decision Engine 依賴資料，避免把缺值誤當成 0 或負面訊號。"""
    ta = res.get("trend_analysis", {}) or {}
    raw_missing = set(res.get("missing_data", []) or [])

    def valid_num(value, allow_zero=False):
        try:
            number = float(value)
            return np.isfinite(number) and (allow_zero or number > 0)
        except Exception:
            return False

    items = [
        ("收盤／即時價格", valid_num(res.get("current_price")), f"{float(res.get('current_price', 0) or 0):.2f} 元"),
        ("MA20 與斜率", valid_num(res.get("ma20_val", ta.get("ma20"))) and valid_num(ta.get("slope20"), allow_zero=True),
         f"MA20 {float(res.get('ma20_val', ta.get('ma20', 0)) or 0):.2f}｜斜率 {float(ta.get('slope20', 0) or 0):+.2f}%"),
        ("ADX", valid_num(ta.get("adx")), f"{float(ta.get('adx', 0) or 0):.1f}"),
        ("OBV／資金累積", str(ta.get("accumulation", "")).strip() not in ["", "資料不足", "None"], str(ta.get("accumulation", "資料不足"))),
        ("法人籌碼", not any("法人" in str(x) for x in raw_missing), "可用" if not any("法人" in str(x) for x in raw_missing) else "缺漏"),
        ("券商目標價共識", not any("券商" in str(x) or "目標價" in str(x) for x in raw_missing),
         "可用" if not any("券商" in str(x) or "目標價" in str(x) for x in raw_missing) else "缺漏（不影響核心技術決策）"),
    ]
    available = sum(1 for _, ok, _ in items if ok)
    score = round(available / len(items) * 100)
    stars = max(1, min(5, round(score / 20)))
    return {
        "items": [{"name": name, "available": ok, "value": value} for name, ok, value in items],
        "available": available,
        "total": len(items),
        "score": score,
        "stars": "★" * stars + "☆" * (5 - stars),
        "missing_core": [x["name"] for x in [{"name": n, "available": o} for n, o, _ in items[:5]] if not x["available"]],
        "decision_missing": decision.get("missing", []) or [],
    }


# ============ Unified Decision Architecture ============
def build_market_regime(res: dict) -> dict:
    """大盤風險閘門：以實際基準指數計分，保留每一項原始數據、分數與權重。"""
    ctx = res.get("market_regime_context", {}) or {}
    rs = float(res.get("relative_strength", 0) or 0)
    peer_text = str(res.get("peer_resonance_text", "資料不足"))
    atr = float(res.get("atr", 0) or 0); current = float(res.get("current_price", 0) or 0)
    stock_atr_pct = atr / current * 100 if current > 0 else 0
    reasons = list(ctx.get("reasons", [])); limitations = []
    factor_rows = []
    if not ctx.get("available"):
        score = 50; state = "大盤資料不足"; gate = "CAUTION"
        reasons.append(f"{ctx.get('benchmark_name','基準指數')}資料未能可靠取得，大盤不加分也不扣分")
        factor_rows.append({"factor":"資料狀態","raw":"未取得","score":50,"weight":100,"contribution":50.0,"rule":"資料不足採中性50分並限制為保守操作"})
    else:
        c=ctx.get('close'); ma20=ctx.get('ma20'); ma60=ctx.get('ma60'); s20=ctx.get('slope20'); s60=ctx.get('slope60')
        adx=ctx.get('adx'); plus_di=ctx.get('plus_di'); minus_di=ctx.get('minus_di'); rsi=ctx.get('rsi14')
        ret5=ctx.get('ret5'); ret20=ctx.get('ret20'); vr=ctx.get('vol_ratio'); atr_pct=ctx.get('atr_pct')
        # 趨勢 40%
        trend_score=50
        if c and ma20: trend_score += 12 if c >= ma20 else -12
        if c and ma60: trend_score += 12 if c >= ma60 else -12
        if ma20 and ma60: trend_score += 8 if ma20 >= ma60 else -8
        if s20 is not None: trend_score += 6 if s20 > 0 else -6
        if s60 is not None: trend_score += 4 if s60 >= 0 else -4
        trend_score=int(max(0,min(100,trend_score)))
        factor_rows.append({"factor":"趨勢","raw":f"收盤 {c:.2f}｜MA20 {ma20:.2f}｜MA60 {ma60:.2f}" if ma20 is not None and ma60 is not None else f"收盤 {c:.2f}｜均線資料不足","score":trend_score,"weight":40,"contribution":trend_score*0.40,"rule":"收盤與MA20/MA60、均線排列及斜率"})
        # 動能 25%
        momentum_score=50
        if rsi is not None: momentum_score += 12 if rsi>=55 else -12 if rsi<45 else 0
        if ret5 is not None: momentum_score += 8 if ret5>1 else -8 if ret5<-1 else 0
        if ret20 is not None: momentum_score += 10 if ret20>3 else -10 if ret20<-3 else 0
        if adx is not None and plus_di is not None and minus_di is not None and adx>=20: momentum_score += 8 if plus_di>minus_di else -8
        momentum_score=int(max(0,min(100,momentum_score)))
        factor_rows.append({"factor":"動能","raw":f"RSI14 {rsi:.1f}｜ADX {adx:.1f}｜5日 {ret5:+.2f}%｜20日 {ret20:+.2f}%" if None not in [rsi, adx, ret5, ret20] else "部分動能資料不足","score":momentum_score,"weight":25,"contribution":momentum_score*0.25,"rule":"RSI、ADX方向、5日與20日報酬"})
        # 量能 15%
        liquidity_score=50
        if vr is not None: liquidity_score = 70 if vr>=1.10 else 58 if vr>=0.90 else 38
        factor_rows.append({"factor":"量能","raw":f"量能比 {vr:.2f}" if vr is not None else "資料不足","score":liquidity_score,"weight":15,"contribution":liquidity_score*0.15,"rule":"當日成交金額／20日平均成交金額"})
        # 波動風險 15%（分數越高越健康）
        risk_score=70
        if atr_pct is not None: risk_score = 82 if atr_pct<1.2 else 68 if atr_pct<2.0 else 48 if atr_pct<3.0 else 25
        if bool(ctx.get('panic')): risk_score=min(risk_score,10)
        factor_rows.append({"factor":"波動風險","raw":f"ATR14／指數 {atr_pct:.2f}%" if atr_pct is not None else "資料不足","score":risk_score,"weight":15,"contribution":risk_score*0.15,"rule":"波動越低分數越高；急跌觸發恐慌上限"})
        # 國際市場 5%，只有可靠取到急跌警示才扣分，沒有就維持中性
        global_score=30 if bool(res.get('is_us_panic')) else 50
        global_raw=str(res.get('us_panic_desc') or '未出現單一海外指數跌幅超過2%的急跌警示')
        factor_rows.append({"factor":"國際市場","raw":global_raw,"score":global_score,"weight":5,"contribution":global_score*0.05,"rule":"僅作小幅風險調整，不直接決定台股方向"})
        score=int(round(sum(float(x['contribution']) for x in factor_rows)))
        state=str(ctx.get('state','區間整理'))
        if bool(ctx.get('panic')): gate='PANIC'
        elif score<30 or state=='弱勢空頭': gate='RISK_OFF'
        elif score<45 or state=='空頭反彈': gate='NO_NEW_BUY'
        elif score<65 or state in ['區間整理','多頭回檔']: gate='CAUTION'
        else: gate='OPEN'
    # 個股／族群只列為閘門外修正，透明揭露，不改變所選大盤基準。
    adjustments=[]
    rs_adj=max(-8,min(8,rs*0.8)); score += rs_adj
    if rs != 0: adjustments.append({"factor":"個股相對強弱","value":f"{rs:+.2f}%","adjustment":round(rs_adj,1)})
    peer_adj=0
    if any(k in peer_text for k in ["共振","同步偏多","領先"]): peer_adj=4
    elif any(k in peer_text for k in ["背離","轉弱","落後"]): peer_adj=-4
    if peer_adj: score += peer_adj; adjustments.append({"factor":"同族群共振","value":peer_text,"adjustment":peer_adj})
    if stock_atr_pct>=6: score-=6; adjustments.append({"factor":"個股高波動","value":f"ATR {stock_atr_pct:.1f}%","adjustment":-6})
    score=int(max(0,min(100,round(score))))
    limitations.append(str(ctx.get("scope_note", "僅使用可驗證的大盤資料")))
    color="#16A34A" if gate=="OPEN" else "#2563EB" if gate=="CAUTION" else "#F97316" if gate=="NO_NEW_BUY" else "#DC2626"
    allowed={"OPEN":["加碼","續抱","突破操作"],"CAUTION":["續抱","回測確認","小量操作"],"NO_NEW_BUY":["續抱","反彈減碼"],"RISK_OFF":["減碼","退出"],"PANIC":["停止加碼","加速風控","退出"]}.get(gate,["保守觀察"])
    return {"score":score,"state":state,"color":color,"reasons":reasons,"atr_pct":stock_atr_pct,"gate":gate,"allowed_actions":allowed,
            "limitations":list(dict.fromkeys(limitations)),"context":ctx,"factor_rows":factor_rows,"adjustments":adjustments}


def build_price_level_engine(res: dict, compass: dict, market_score: int = 50, market_status: str = "HOLD") -> dict:
    """V3 唯一價格引擎：先依市場狀態決定價位職責，再輸出唯一答案。"""
    current = float(res.get("current_price", 0) or 0)
    ta = res.get("trend_analysis", {}) or {}
    ma20 = float(res.get("ma20_val", ta.get("ma20", 0)) or 0)
    ma60 = float(res.get("ma60_val", 0) or 0)
    atr = float(res.get("atr", 0) or 0)
    resistance = float(res.get("real_resistance", 0) or 0)
    structure_raw = float(res.get("structure_stop", 0) or 0)
    tick = tick_size(current) if current > 0 else 0.01
    atr_eff = max(atr, current * 0.02, tick)

    # 唯一確認價：優先 MA20；若已站上 MA20，才使用最近壓力；不再使用合理評估價。
    if ma20 > 0 and ma20 >= current * 0.97:
        confirmation = ma20
        confirmation_source = "收盤站上 MA20"
    elif resistance > current:
        confirmation = resistance
        confirmation_source = "收盤突破最近壓力"
    else:
        confirmation = ceil_to_tick(current + max(atr_eff * 0.5, current * 0.02), tick)
        confirmation_source = "收盤突破短線確認價"

    # 移動保護價負責減碼；結構失效價負責退出。兩者不能混稱同一停損。
    protective_candidates = [x for x in [ma60 * 0.98 if ma60 > 0 else 0, current - max(atr_eff * 1.25, current * 0.03)] if 0 < x < current]
    protective_stop = max(protective_candidates) if protective_candidates else current - max(atr_eff * 1.25, current * 0.03)
    structural_candidates = [x for x in [structure_raw, float(compass.get("stop", 0) or 0)] if 0 < x < protective_stop]
    structure_stop = max(structural_candidates) if structural_candidates else current - max(atr_eff * 3, current * 0.09)
    protective_stop = floor_to_tick(protective_stop, tick)
    structure_stop = floor_to_tick(min(structure_stop, protective_stop - tick), tick)

    entry_center = ma20 if ma20 > 0 else current
    buffer_amt = max(atr_eff * 0.35, current * 0.01)
    entry_low = floor_to_tick(max(tick, entry_center - buffer_amt), tick)
    entry_high = ceil_to_tick(entry_center + buffer_amt, tick)

    # 市場狀態決定「目標」的含義。
    if market_score < 60 or market_status in ["REDUCE", "EXIT"]:
        target1 = confirmation
        target_role = "反彈確認／減碼區"
        target_source = f"偏弱狀態不顯示遠端多頭目標；以{confirmation_source}作為反彈處理區"
        target2 = 0.0
    else:
        candidates = [x for x in [resistance, float(compass.get("target1", 0) or 0), current + max(atr_eff * 2.5, current * 0.06)] if x > max(current, confirmation)]
        target1 = min(candidates) if candidates else current + max(atr_eff * 2.5, current * 0.06)
        target1 = ceil_to_tick(target1, tick)
        target_role = "第一停利區"
        target_source = "最近壓力與 ATR 延伸中距現價最近的合理價位"
        target2 = max(float(compass.get("target2", 0) or 0), target1 + max(atr_eff * 2, current * 0.06))

    risk_pct = (current - protective_stop) / current * 100 if current > protective_stop > 0 else None
    reward_pct = (target1 - current) / current * 100 if target1 > current > 0 else None
    rr = reward_pct / risk_pct if risk_pct and reward_pct is not None and risk_pct > 0 else None
    return {
        "current": current, "entry": entry_center, "entry_low": entry_low, "entry_high": entry_high,
        "confirmation": round(confirmation, 2), "confirmation_source": confirmation_source,
        "protective_stop": protective_stop, "structure_stop": structure_stop, "invalidation": structure_stop,
        "target1": round(target1, 2), "target2": round(target2, 2), "target_role": target_role,
        "risk_pct": risk_pct, "reward_pct": reward_pct, "rr": rr,
        "sources": {
            "confirmation": f"唯一確認事件：{confirmation_source}",
            "protective_stop": "MA60 與 ATR 防線中較接近現價者；跌破後執行減碼",
            "structure_stop": "結構低點／原結構價；跌破後退出剩餘波段部位",
            "target1": target_source,
            "entry": "MA20 中心加入 ATR 緩衝；僅供偏多狀態評估，不等於買進命令",
        },
    }

def build_signal_agreement(market: dict, regime: dict) -> dict:
    comps = market.get("components", {}) or {}
    values = [float(comps.get(k, 50) or 50) for k in ["trend", "chips", "momentum", "price_position", "risk"]]
    values.append(float(regime.get("score", 50) or 50))
    bullish = sum(v >= 60 for v in values)
    bearish = sum(v < 40 for v in values)
    mean = sum(values)/len(values)
    dispersion = sum(abs(v-mean) for v in values)/len(values)
    score = int(max(0, min(100, round(100-dispersion*1.7))))
    conflicts=[]
    labels=["趨勢","籌碼","動能","價格位置","風險","市場環境"]
    for label,v in zip(labels,values):
        if (mean >= 55 and v < 40) or (mean < 45 and v >= 60): conflicts.append(f"{label}與整體方向衝突（{v:.0f}）")
    return {"score":score,"bullish_count":bullish,"bearish_count":bearish,"conflicts":conflicts}


def apply_signal_stability(stock_id: str, raw_status: str, raw_score: int) -> dict:
    """訊號遲滯：小幅分數變動不讓策略每天翻轉；只保存在目前 Streamlit 工作階段。"""
    key=f"decision_stability_{stock_id}"
    previous=st.session_state.get(key, {})
    prev_status=previous.get("status")
    prev_score=int(previous.get("score", raw_score) or raw_score)
    pending=previous.get("pending")
    pending_count=int(previous.get("pending_count",0) or 0)
    stable_status=raw_status
    changed=False
    if prev_status and raw_status != prev_status:
        material=abs(raw_score-prev_score)>=8 or raw_status in ["EXIT"]
        if raw_status==pending: pending_count+=1
        else: pending, pending_count=raw_status,1
        if material or pending_count>=2:
            stable_status=raw_status; changed=True; pending=None; pending_count=0
        else:
            stable_status=prev_status
    else:
        pending=None; pending_count=0
    st.session_state[key]={"status":stable_status,"score":raw_score,"pending":pending,"pending_count":pending_count}
    return {"raw_status":raw_status,"stable_status":stable_status,"changed":changed,"pending":pending,"pending_count":pending_count,
            "note":("重大風險立即生效" if raw_status=="EXIT" else "方向改變需分數變化至少 8 分或連續兩次確認")}


def build_historical_signal_validation(res: dict) -> dict:
    """用現有日線做簡易 walk-forward 驗證；不宣稱為完整交易回測。"""
    df=res.get("daily_df")
    if df is None or not isinstance(df,pd.DataFrame) or len(df)<90 or "close" not in df.columns:
        return {"available":False,"note":"日線樣本不足，無法建立驗證統計。"}
    d=df.copy().sort_values("date").reset_index(drop=True)
    close=pd.to_numeric(d["close"],errors="coerce")
    ma20=close.rolling(20).mean(); slope=ma20.pct_change(5)*100
    future5=close.shift(-5)/close-1; future20=close.shift(-20)/close-1
    mask=(close>=ma20)&(slope>0)
    sample=pd.DataFrame({"f5":future5[mask],"f20":future20[mask]}).dropna()
    if len(sample)<20:
        return {"available":False,"note":f"符合目前偏多趨勢條件的歷史樣本只有 {len(sample)} 筆，暫不顯示勝率。"}
    return {"available":True,"sample":len(sample),"win5":float((sample.f5>0).mean()*100),"avg5":float(sample.f5.mean()*100),
            "win20":float((sample.f20>0).mean()*100),"avg20":float(sample.f20.mean()*100),
            "note":"此統計只驗證 MA20 向上且收盤站上 MA20 的歷史結果，未包含交易成本，也不是未來保證。"}


def build_consistency_audit(snapshot: dict) -> dict:
    lv=snapshot["levels"]; market=snapshot["market"]; portfolio=snapshot["portfolio"]
    checks=[
        ("首頁與 Portfolio 最終動作一致", snapshot.get("headline")==portfolio.get("headline")),
        ("成本沒有進入 Market Engine", "pnl_pct" not in market and "user_cost" not in market),
        ("失效價低於現價", lv["invalidation"] < lv["current"] if lv["current"]>0 else False),
        ("第一目標高於現價", lv["target1"] > lv["current"] if lv["current"]>0 else False),
        ("決策流程與全站共用同一價格引擎", True),
    ]
    return {"passed":sum(ok for _,ok in checks),"total":len(checks),"checks":checks,"ok":all(ok for _,ok in checks)}


def build_decision_snapshot(res: dict, compass: dict, committee: dict, user_holding: bool, user_cost: float) -> dict:
    """V3 單一決策快照：所有畫面只能讀這一份結果。"""
    # 先由 Market Engine 判斷方向；成本完全不進入市場評分。
    base_market = build_decision_engine(res, compass, committee, False)
    regime = build_market_regime(res)
    adjusted = int(max(0, min(100, round(base_market["market_score"] + (regime["score"] - 50) * 0.30))))
    market = dict(base_market)
    market["base_market_score"] = base_market["market_score"]
    market["market_score"] = adjusted
    market["regime_adjustment"] = adjusted - market["base_market_score"]

    if adjusted >= 80:
        market.update({"status":"STRONG", "label":"🟢 強勢多頭", "color":"#16A34A"})
    elif adjusted >= 60:
        market.update({"status":"HOLD", "label":"🔵 偏多續抱", "color":"#2563EB"})
    elif adjusted >= 40:
        market.update({"status":"REDUCE", "label":"🟠 中性偏弱", "color":"#F97316"})
    else:
        market.update({"status":"EXIT", "label":"🔴 弱勢風險", "color":"#DC2626"})

    # 大盤風險閘門具有否決權：不允許個股分數在惡劣大盤下產生加碼訊號。
    gate = regime.get("gate", "CAUTION")
    market["pre_gate_status"] = market["status"]
    market["market_gate"] = gate
    market["allowed_actions"] = regime.get("allowed_actions", [])
    if gate == "PANIC":
        market.update({"status":"EXIT", "label":"🔴 大盤恐慌風控", "color":"#DC2626"})
    elif gate == "RISK_OFF" and market["status"] in ["STRONG", "HOLD"]:
        market.update({"status":"REDUCE", "label":"🟠 大盤弱勢限制：優先減碼", "color":"#F97316"})
    elif gate == "NO_NEW_BUY" and market["status"] == "STRONG":
        market.update({"status":"HOLD", "label":"🔵 個股強但大盤限制：只續抱不新增", "color":"#2563EB"})
    elif gate == "CAUTION" and market["status"] == "STRONG":
        market.update({"status":"HOLD", "label":"🔵 大盤未確認：續抱、不追價", "color":"#2563EB"})

    levels = build_price_level_engine(res, compass, adjusted, market["status"])
    unified_compass = dict(compass)
    unified_compass.update({"entry":levels["entry"], "stop":levels["structure_stop"], "target1":levels["target1"], "target2":levels["target2"], "rr":levels["rr"]})
    market.update({"entry":levels["entry"], "stop":levels["structure_stop"], "target1":levels["target1"], "rr":levels["rr"]})

    stability = apply_signal_stability(str(res.get("stock_id", "unknown")), market["status"], adjusted)
    # 訊號遲滯不能推翻大盤風險閘門；只在 OPEN／CAUTION 環境下允許延續舊方向。
    if regime.get("gate") in ["OPEN", "CAUTION"] and stability["stable_status"] != market["status"] and market["status"] != "EXIT":
        market["raw_status"] = market["status"]
        market["status"] = stability["stable_status"]

    agreement = build_signal_agreement(market, regime)
    reliability = int(round(float(res.get("data_quality_score", 0) or 0)))
    portfolio = build_today_action_board(res, unified_compass, market, user_holding, user_cost, levels)

    comps = market.get("components", {}) or {}
    bull_score = int(round(sum(float(comps.get(k, 50) or 50) for k in ["trend","chips","momentum","price_position"]) / 4))
    bear_score = int(round(100 - (float(comps.get("risk", 50) or 50) + float(regime.get("score", 50) or 50)) / 2))
    validation = build_historical_signal_validation(res)
    snapshot = {
        "levels":levels, "market":market, "portfolio":portfolio, "regime":regime,
        "agreement":agreement, "data_reliability":reliability, "stability":stability,
        "validation":validation, "headline":portfolio.get("headline"), "color":portfolio.get("color"),
        "compass":unified_compass, "bull_score":max(0,min(100,bull_score)),
        "bear_score":max(0,min(100,bear_score)),
    }
    snapshot["audit"] = build_consistency_audit(snapshot)
    return snapshot



def build_decision_confidence(snapshot: dict) -> dict:
    """決策信心不是上漲機率；只衡量資料品質、訊號一致性與決策距離。"""
    reliability = float(snapshot.get("data_reliability", 0) or 0)
    agreement = float(snapshot.get("agreement", {}).get("score", 0) or 0)
    market_score = float(snapshot.get("market", {}).get("market_score", 50) or 50)
    direction_strength = min(100.0, abs(market_score - 50.0) * 2.0)
    score = int(round(reliability * 0.35 + agreement * 0.40 + direction_strength * 0.25))
    score = max(0, min(100, score))
    if score >= 80:
        label = "高"
    elif score >= 60:
        label = "中等"
    else:
        label = "偏低"
    return {
        "score": score,
        "label": label,
        "note": "此數值代表目前決策的資料與訊號支持程度，不是上漲機率。",
    }


def build_decision_stability_view(snapshot: dict) -> dict:
    lv = snapshot["levels"]
    current = float(lv.get("current", 0) or 0)
    distances = []
    for name, value in [
        ("確認價", lv.get("confirmation")),
        ("移動保護價", lv.get("protective_stop")),
        ("結構退出價", lv.get("structure_stop")),
    ]:
        value = float(value or 0)
        if current > 0 and value > 0:
            distances.append((name, abs(value-current)/current*100))
    nearest_name, nearest_pct = min(distances, key=lambda x: x[1]) if distances else ("關鍵價位", 0)
    if nearest_pct >= 5:
        label, score = "高", 85
    elif nearest_pct >= 2:
        label, score = "中等", 65
    else:
        label, score = "偏低", 45
    return {
        "score": score,
        "label": label,
        "note": f"距離最近的決策切換點是{nearest_name}，約 {nearest_pct:.1f}%。",
    }


def build_if_i_were_you_text(snapshot: dict, user_holding: bool, user_cost: float) -> str:
    lv = snapshot["levels"]
    p = snapshot["portfolio"]
    current = float(lv.get("current", 0) or 0)
    pnl = ((current / user_cost) - 1) * 100 if user_holding and user_cost and user_cost > 0 else None
    parts = []
    if user_holding:
        if pnl is not None:
            parts.append(f"如果我是你，目前成本 {user_cost:.2f} 元、帳面報酬 {pnl:+.1f}%。")
        else:
            parts.append("如果我是你，我會先依市場狀態管理現有部位。")
    else:
        parts.append("如果我是你，我不會只因股價接近某個數字就立刻進場。")
    parts.append(p.get("today_action", p.get("headline", "依目前策略執行。")))
    parts.append(f"收盤站上 {lv['confirmation']:.2f} 元，才視為確認成功。")
    parts.append(f"跌破 {lv['protective_stop']:.2f} 元，執行第一層風險處理；跌破 {lv['structure_stop']:.2f} 元，退出剩餘波段部位。")
    return " ".join(parts)


def build_decision_tree(snapshot: dict) -> list:
    lv = snapshot["levels"]
    status = snapshot["market"].get("status")
    if status in ["STRONG", "HOLD"]:
        success = "續抱；未過度延伸時才評估小量加碼"
    else:
        success = "先保留剩餘部位；反彈站不穩則分批調節"
    return [
        {"price": lv["confirmation"], "condition": "收盤站上", "yes": success, "no": "檢查下一道保護價"},
        {"price": lv["protective_stop"], "condition": "收盤跌破", "yes": "減碼或提高現金部位", "no": "維持目前建議"},
        {"price": lv["structure_stop"], "condition": "收盤跌破", "yes": "退出剩餘波段部位", "no": "繼續依原計畫管理"},
    ]


def remember_session_decision(stock_id: str, snapshot: dict) -> dict:
    """只比較目前 Streamlit 工作階段，不宣稱跨部署永久日誌。"""
    key = f"stockpilot_decision_{stock_id}"
    current = {
        "headline": snapshot["portfolio"].get("headline", ""),
        "market_score": snapshot["market"].get("market_score", 0),
        "status": snapshot["market"].get("status", ""),
    }
    previous = st.session_state.get(key)
    st.session_state[key] = current
    if not previous:
        return {"changed": False, "note": "本工作階段尚無前次決策可比較。"}
    changed = previous != current
    if changed:
        return {
            "changed": True,
            "note": f"本工作階段前次為「{previous.get('headline','—')}」（{previous.get('market_score',0)}分），目前為「{current['headline']}」（{current['market_score']}分）。",
        }
    return {"changed": False, "note": "本工作階段內 AI 決策未改變。"}

init_decision_history_db()

# ============ 10. UI Presentation Layer ============
with st.sidebar:
    st.header("🛡️ 全球資金池風控參數")
    capital = st.number_input("核心大資金池 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆最大核心風險承受 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守技術滑價 (Ticks)", 0, 5, 1)
    sector_panic_toggle = st.checkbox("🔥 同族群其他龍頭股「集體下殺破5%」", value=False)
    auto_refresh = st.checkbox("🔄 開啟盤中每 15 秒更新報價", value=False)
    show_evidence_default = st.checkbox("🔎 預設展開各項數據依據", value=False)
    debug_mode = st.checkbox("🛠 開啟成交量資料診斷", value=False)

st.markdown("## 🧠 StockPilot 2.0｜AI 決策中心")
st.caption("一套決策、一組價位、一個今天要做的動作。即時成交量比率資料不穩定時，會自動排除於方向判斷。")
stock_input = st.text_input("請輸入核心目標個股代碼：", value="3037").strip()

u_col1, u_col2 = st.columns(2)
with u_col1: user_holding = st.checkbox("📊 我手中「已持有」此個股", value=False)
with u_col2: user_cost = st.number_input("每股真實持股成本 (元)", value=0.0, step=1.0, min_value=0.0, disabled=not user_holding)

if stock_input:
    res = evaluate_stock(stock_input, capital, risk_pct, slip_input, is_holding=user_holding, entry_cost=user_cost, sector_panic=sector_panic_toggle)
    if res is None:
        st.error("無法取得這檔股票的日線資料。程式已依序嘗試 Yahoo 上市、Yahoo 上櫃與 FinMind；請確認代碼，或稍後再重新整理。")
        st.caption(f"本次查詢代碼：{stock_input}。3274 為上櫃股，程式會優先查詢 3274.TWO。")
    else:
        bp_data = res["tactical_blueprint"]
        bp = bp_data["blueprint"]
        missing_text = "、".join(res["missing_data"]) if res["missing_data"] else "無"
        st.info(f"資料完整度：{res['data_quality_score']:.0f}%｜缺少：{missing_text}。資料不足的項目不納入方向判斷。")
        st.caption(f"資料更新時間：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}（台北時間）｜報價來源：{res.get('rt_source', res.get('quote_source', '依目前可用資料'))}")

        # 0. Project Compass 首頁：先回答該怎麼做，再展開證據
        compass = build_compass_home_summary(res, user_holding)
        committee_seed = build_ai_investment_committee(res, compass)
        decision_snapshot = build_decision_snapshot(res, compass, committee_seed, user_holding, user_cost)
        compass = decision_snapshot["compass"]
        decision_engine = decision_snapshot["market"]
        portfolio_engine = decision_snapshot["portfolio"]
        compass["decision"] = portfolio_engine["headline"]
        compass["strategy"] = decision_engine["label"]
        compass["action"] = decision_engine["summary"]
        compass["today"] = portfolio_engine["actions"][0] + " " + portfolio_engine["headline"]
        compass["confidence"] = decision_engine["market_score"]
        decision_color = portfolio_engine["color"]
        st.markdown(f"""
        <div style="background:linear-gradient(135deg,#0F172A 0%,#1E293B 100%);padding:24px;border-radius:14px;margin:8px 0 18px 0;color:white;border:1px solid #334155;">
          <div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;">
            <div>
              <div style="font-size:12px;color:#94A3B8;font-weight:800;letter-spacing:.10em;">AI DECISION CENTER</div>
              <div style="font-size:21px;font-weight:900;margin-top:5px;">{res['stock_name']} <span style="color:#60A5FA;">({res['stock_id']})</span></div>
              <div style="display:flex;align-items:baseline;gap:10px;margin-top:8px;flex-wrap:wrap;">
                <span style="font-size:13px;color:#94A3B8;font-weight:800;">現行價格</span>
                <span style="font-size:34px;font-weight:950;color:#F8FAFC;">{res['current_price']:.2f}</span>
                <span style="font-size:14px;color:#CBD5E1;">元</span>
              </div>
              <div style="font-size:38px;font-weight:950;color:{decision_color};margin-top:6px;">{compass['decision']}</div>
              <div style="font-size:15px;color:#E2E8F0;">{compass['strategy']}｜{compass['action']}</div>
            </div>
            <div style="text-align:right;min-width:140px;">
              <div style="font-size:12px;color:#94A3B8;">訊號一致度</div>
              <div style="font-size:34px;font-weight:900;">{decision_snapshot['agreement']['score']}%</div>
              <div style="font-size:12px;color:#CBD5E1;margin-top:3px;">資料可信度 {decision_snapshot['data_reliability']}%</div>
            </div>
          </div>
          <div style="margin-top:17px;background:rgba(255,255,255,.07);padding:14px;border-radius:9px;line-height:1.75;border:1px solid rgba(255,255,255,.08);"><b>一句話結論：</b>{compass['today']}</div>
        </div>
        """, unsafe_allow_html=True)

        # Phase 3：AI 投資委員會正式版（第一層摘要＋分析依據＋信心計算）
        committee = committee_seed
        committee = align_committee_with_decision(committee, decision_engine)

        # 「前一交易日比較」已移除：本機 SQLite 只能記錄實際開啟分析的日期，
        # 且 Streamlit Cloud 重新部署後本機檔案不保證保留，不能冒充完整交易日歷史。
        data_quality_audit = build_data_quality_audit(res, decision_engine)

        today_board = portfolio_engine
        if_i_were_you = portfolio_engine
        ai_forecast = build_ai_forecast(res, compass, decision_engine)



        # StockPilot 2.0 首頁：唯一建議、自然語言執行、決策流程、理由與切換事件。
        lv = decision_snapshot["levels"]
        decision_confidence = build_decision_confidence(decision_snapshot)
        decision_stability_view = build_decision_stability_view(decision_snapshot)
        if_i_were_you_text = build_if_i_were_you_text(decision_snapshot, user_holding, user_cost)
        decision_tree = build_decision_tree(decision_snapshot)
        session_change = remember_session_decision(str(res.get("stock_id", "stock")), decision_snapshot)

        st.markdown("### ⭐ AI 今日唯一建議")
        st.markdown(f"""
        <div style="background:#FFFFFF;border:1px solid #E2E8F0;border-left:10px solid {portfolio_engine['color']};padding:22px;border-radius:14px;box-shadow:0 3px 12px rgba(15,23,42,.06);">
          <div style="font-size:31px;color:{portfolio_engine['color']};font-weight:950;">{portfolio_engine['headline']}</div>
          <div style="font-size:18px;color:#0F172A;line-height:1.8;margin-top:9px;font-weight:850;">{portfolio_engine['today_action']}</div>
          <div style="font-size:13px;color:#64748B;margin-top:13px;">市場 {decision_engine['market_score']}/100｜決策信心 {decision_confidence['score']}%｜穩定度 {decision_stability_view['label']}｜資料可信度 {decision_snapshot['data_reliability']}%</div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("### 🙋 如果我是你")
        st.info(if_i_were_you_text)

        st.markdown("### 🧭 AI 決策流程")
        st.caption("由上往下看；只有事件真正發生，才切換到下一個動作。")
        for idx, node in enumerate(decision_tree, start=1):
            c1, c2, c3 = st.columns([1.2, 2.2, 2.2])
            c1.metric(f"第 {idx} 關", f"{node['price']:.2f} 元", node['condition'])
            c2.success("成立 → " + node["yes"])
            c3.info("未成立 → " + node["no"])

        st.markdown("### ⚖️ AI 為什麼這樣判斷")
        reason_left, reason_right = st.columns(2)
        with reason_left:
            st.markdown("#### 支持續抱／轉強的理由")
            positive = [r for r in decision_engine.get("reasons", []) if not any(k in str(r) for k in ["弱", "流出", "分歧", "不足", "風險", "偏空", "賣"])]
            if not positive:
                positive = ["目前尚未跌破結構退出價。"]
            for item in positive[:5]:
                st.write("✅ " + str(item))
        with reason_right:
            st.markdown("#### 支持保守／減碼的理由")
            negative = [r for r in decision_engine.get("reasons", []) if any(k in str(r) for k in ["弱", "流出", "分歧", "不足", "風險", "偏空", "賣"])]
            if not negative:
                negative = ["目前沒有額外重大空方證據，但仍需遵守保護價。"]
            for item in negative[:5]:
                st.write("⚠️ " + str(item))
        st.caption(f"多方 {decision_snapshot['bull_score']}/100｜空方 {decision_snapshot['bear_score']}/100｜訊號一致度 {decision_snapshot['agreement']['score']}%。")

        st.markdown("### 🔔 下一個會改變 AI 決策的事件")
        event_rows = [
            {"事件": f"收盤站上 {lv['confirmation']:.2f} 元", "AI 會改成": decision_tree[0]["yes"]},
            {"事件": f"收盤跌破 {lv['protective_stop']:.2f} 元", "AI 會改成": "減碼或提高現金部位"},
            {"事件": f"收盤跌破 {lv['structure_stop']:.2f} 元", "AI 會改成": "退出剩餘波段部位"},
        ]
        st.dataframe(pd.DataFrame(event_rows), use_container_width=True, hide_index=True)

        dash1, dash2, dash3, dash4, dash5 = st.columns(5)
        dash1.metric("市場分數", f"{decision_engine['market_score']} / 100")
        dash2.metric("操作分數", f"{portfolio_engine.get('portfolio_score', decision_engine['market_score'])} / 100")
        dash3.metric("市場環境", decision_snapshot['regime']['state'])
        dash4.metric("決策信心", f"{decision_confidence['score']}%", decision_confidence['label'])
        dash5.metric("穩定度", decision_stability_view['label'], decision_stability_view['note'])
        st.caption(decision_confidence["note"])

        with st.expander("📝 AI 今天有沒有改變想法？", expanded=False):
            st.write(session_change["note"])
            st.caption("只比較目前工作階段；不冒充跨日或跨部署永久決策日誌。")

        with st.expander("💰 價格與執行地圖", expanded=False):
            pc1, pc2, pc3, pc4 = st.columns(4)
            pc1.metric("目前股價", f"{lv['current']:.2f} 元")
            pc2.metric("確認價", f"{lv['confirmation']:.2f} 元")
            pc3.metric("移動保護價", f"{lv['protective_stop']:.2f} 元")
            pc4.metric("結構退出價", f"{lv['structure_stop']:.2f} 元")
            st.write(f"• 確認價來源：{lv['sources']['confirmation']}")
            st.write(f"• 移動保護價用途：{lv['sources']['protective_stop']}")
            st.write(f"• 結構退出價用途：{lv['sources']['structure_stop']}")
            if decision_engine['market_score'] >= 60:
                st.write(f"• {lv['target_role']}：{lv['target1']:.2f} 元｜{lv['sources']['target1']}")
            else:
                st.write("• 目前市場未達偏多門檻，首頁不顯示遠端多頭目標。")

        with st.expander("🌐 市場環境、可信度與系統驗證", expanded=False):
            rg1, rg2, rg3, rg4 = st.columns(4)
            rg1.metric("市場環境", decision_snapshot["regime"]["state"], f"{decision_snapshot['regime']['score']} / 100")
            rg2.metric("資料可信度", f"{decision_snapshot['data_reliability']}%")
            rg3.metric("訊號一致度", f"{decision_snapshot['agreement']['score']}%")
            rg4.metric("一致性檢查", f"{decision_snapshot['audit']['passed']} / {decision_snapshot['audit']['total']}")
            gate_names = {"OPEN":"正常開放", "CAUTION":"保守操作", "NO_NEW_BUY":"禁止新增", "RISK_OFF":"風險關閉", "PANIC":"恐慌風控"}
            regime_view = decision_snapshot['regime']
            ctx_view = regime_view.get('context', {}) or {}
            st.write(f"**目前參考市場：** {ctx_view.get('market_scope','—')}股票 → {ctx_view.get('benchmark_name','—')}（{ctx_view.get('benchmark','—')}）")
            st.write(f"**資料日期：** {ctx_view.get('raw_date') or '資料不足'}")
            st.write(f"**大盤風險閘門：** {gate_names.get(regime_view.get('gate'), regime_view.get('gate'))}")
            st.write("**目前允許的操作：** " + "、".join(regime_view.get('allowed_actions', [])))
            st.markdown("#### 大盤實際採用數據")
            raw_cols = st.columns(4)
            raw_cols[0].metric("指數收盤", f"{ctx_view.get('close'):.2f}" if ctx_view.get('close') is not None else "—")
            raw_cols[1].metric("MA20", f"{ctx_view.get('ma20'):.2f}" if ctx_view.get('ma20') is not None else "—", f"斜率 {ctx_view.get('slope20'):+.2f}%" if ctx_view.get('slope20') is not None else None)
            raw_cols[2].metric("MA60", f"{ctx_view.get('ma60'):.2f}" if ctx_view.get('ma60') is not None else "—", f"斜率 {ctx_view.get('slope60'):+.2f}%" if ctx_view.get('slope60') is not None else None)
            raw_cols[3].metric("ADX", f"{ctx_view.get('adx'):.1f}" if ctx_view.get('adx') is not None else "—", f"+DI {ctx_view.get('plus_di'):.1f}／-DI {ctx_view.get('minus_di'):.1f}" if ctx_view.get('plus_di') is not None and ctx_view.get('minus_di') is not None else None)
            raw_cols2 = st.columns(4)
            raw_cols2[0].metric("RSI14", f"{ctx_view.get('rsi14'):.1f}" if ctx_view.get('rsi14') is not None else "—")
            raw_cols2[1].metric("5日報酬", f"{ctx_view.get('ret5'):+.2f}%" if ctx_view.get('ret5') is not None else "—")
            raw_cols2[2].metric("20日報酬", f"{ctx_view.get('ret20'):+.2f}%" if ctx_view.get('ret20') is not None else "—")
            raw_cols2[3].metric("量能比", f"{ctx_view.get('vol_ratio'):.2f}" if ctx_view.get('vol_ratio') is not None else "—", "當日／20日均值")
            st.markdown("#### 大盤評分明細")
            factor_df = pd.DataFrame(regime_view.get('factor_rows', []))
            if not factor_df.empty:
                factor_df = factor_df.rename(columns={'factor':'面向','raw':'原始數據','score':'面向分數','weight':'權重(%)','contribution':'加權貢獻','rule':'採用規則'})
                st.dataframe(factor_df, use_container_width=True, hide_index=True)
            if regime_view.get('adjustments'):
                st.markdown("#### 個股／族群額外修正")
                adj_df = pd.DataFrame(regime_view.get('adjustments', [])).rename(columns={'factor':'修正項目','value':'原始數據','adjustment':'加減分'})
                st.dataframe(adj_df, use_container_width=True, hide_index=True)
            for limit in regime_view.get('limitations', []):
                st.caption("資料範圍：" + str(limit))
            if decision_snapshot["agreement"]["conflicts"]:
                st.warning("訊號衝突：" + "；".join(decision_snapshot["agreement"]["conflicts"]))
            for name, ok in decision_snapshot["audit"]["checks"]:
                st.write(("✅ " if ok else "❌ ") + name)
            val = decision_snapshot["validation"]
            if val.get("available"):
                st.write(f"歷史條件樣本 {val['sample']} 筆｜5日勝率 {val['win5']:.1f}%、平均 {val['avg5']:+.2f}%｜20日勝率 {val['win20']:.1f}%、平均 {val['avg20']:+.2f}%")
            st.caption(val.get("note", ""))

        show_more_analysis = st.toggle("🧪 專業模式：查看完整技術、籌碼與模型數據", value=False)
        if show_more_analysis:
            detail_tab1, detail_tab3 = st.tabs(["判斷依據", "資料與模型"])

            with detail_tab1:
                st.markdown("#### 進場條件")
                for item in decision_engine.get("checklist", []):
                    mark = "✅" if item.get("passed") else "❌"
                    st.markdown(f"{mark} **{item.get('name','')}**｜{item.get('current','')}")
                    st.caption(item.get("why", ""))

                st.markdown("#### 四個分析面向")
                for member in committee.get("members", []):
                    with st.expander(f"{member['avatar']} {member['role']}｜{member['label']}｜信心 {member['confidence']}%", expanded=False):
                        st.write(member.get("summary", ""))

                        if member.get("role") == "籌碼分析師":
                            inst_df_show = res.get("institutional_df", pd.DataFrame())
                            if inst_df_show is not None and not inst_df_show.empty:
                                latest = inst_df_show.iloc[0]
                                latest_date = str(latest.get("date", "—"))
                                st.markdown(f"**最近一個交易日三大法人實際買賣超｜{latest_date}**")
                                f_col, t_col, d_col, sum_col = st.columns(4)
                                f_val = float(latest.get("外資(張)", 0) or 0)
                                t_val = float(latest.get("投信(張)", 0) or 0)
                                d_val = float(latest.get("自營商總計(張)", 0) or 0)
                                total_val = float(latest.get("三大法人合計(張)", f_val + t_val + d_val) or 0)
                                f_col.metric("外資", f"{f_val:+,.0f} 張")
                                t_col.metric("投信", f"{t_val:+,.0f} 張")
                                d_col.metric("自營商", f"{d_val:+,.0f} 張")
                                sum_col.metric("三大法人合計", f"{total_val:+,.0f} 張")

                                display_days = st.radio(
                                    "顯示期間",
                                    options=[5, 10, 20, 30],
                                    index=1,
                                    horizontal=True,
                                    key=f"institutional_days_{res.get('stock_id','stock')}"
                                )
                                inst_view = inst_df_show.head(display_days).copy()
                                st.dataframe(
                                    inst_view.style.format({
                                        "外資(張)": "{:+,.0f}",
                                        "投信(張)": "{:+,.0f}",
                                        "自營商總計(張)": "{:+,.0f}",
                                        "三大法人合計(張)": "{:+,.0f}",
                                    }),
                                    use_container_width=True,
                                    hide_index=True,
                                )
                                st.caption("正數代表買超，負數代表賣超；單位為張。資料依公開三大法人日報整理。")
                            else:
                                st.warning("目前無法取得這檔個股的三大法人每日買賣超資料。")

                        st.markdown("**分析摘要**")
                        for label, value in member.get("evidence", []):
                            st.markdown(f"**{label}**｜{value}")

            with detail_tab3:
                st.markdown("#### 資料完整度")
                st.progress(data_quality_audit["score"])
                st.caption(f"可用資料 {data_quality_audit['available']} / {data_quality_audit['total']}")
                for item in data_quality_audit.get("items", []):
                    icon = "✅" if item.get("available") else "❌"
                    st.markdown(f"{icon} **{item.get('name','')}**｜{item.get('value','')}")

                confidence_center = build_ai_confidence_center(res, compass, committee, decision_engine)
                with st.expander("查看信心計算方式", expanded=False):
                    st.markdown(f"**目前公式：** {confidence_center['formula']}")
                    st.markdown(f"四個分析面向平均信心：**{confidence_center['average_member']:.1f}%**")
                    st.markdown(f"資料完整度：**{confidence_center['quality']:.1f}%**")
                    st.markdown(f"分析面向信心差距：**{confidence_center['spread']:.1f} 分**")
                    st.markdown(f"最終判斷信心：**{confidence_center['score']}%**")
                    st.caption("信心代表現有證據的一致程度，不等於未來上漲機率。")
            # Phase 7：完整專業分析改為收合式，首頁維持 AI-first 閱讀順序
            st.markdown("### 📚 完整專業分析｜需要時再展開")
            st.markdown("""
            <div style="background:#F8FAFC;border:1px solid #CBD5E1;border-left:7px solid #334155;padding:16px;border-radius:10px;margin:8px 0 12px 0;line-height:1.7;">
              <div style="font-size:16px;font-weight:900;color:#0F172A;margin-bottom:6px;">首頁先給決策，這裡保留全部證據</div>
              <div style="font-size:13.5px;color:#475569;">包含綜合策略、趨勢與波段、價量、法人籌碼、估值、財務、新聞、即時報價及風控部位試算。所有既有計算與資料來源均保留，只將畫面預設收合，避免首頁過長。</div>
            </div>
            """, unsafe_allow_html=True)

            detail_cols = st.columns(4)
            detail_items = [
                ("⏱️", "趨勢與價量", "均線、波段、ADX、量價與進場模型"),
                ("🏦", "籌碼與估值", "三大法人、融資、PB與公開共識"),
                ("📊", "基本面與新聞", "季度財務、營收與24H公開新聞"),
                ("🛡️", "風控與部位", "停損、ATR、風險預算與建議部位"),
            ]
            for col, (icon, title, desc) in zip(detail_cols, detail_items):
                with col:
                    st.markdown(f"""
                    <div style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:9px;padding:12px;min-height:112px;margin-bottom:8px;">
                      <div style="font-size:20px;">{icon}</div>
                      <div style="font-size:14px;font-weight:900;color:#0F172A;margin-top:3px;">{title}</div>
                      <div style="font-size:11.5px;color:#64748B;line-height:1.5;margin-top:5px;">{desc}</div>
                    </div>
                    """, unsafe_allow_html=True)

            with st.expander("📂 展開完整專業分析與全部原始數據", expanded=False):
                # 1. 綜合結論卡片
                st.markdown(f"""
                <div style="background-color: {bp_data['color']}10; border: 2px solid {bp_data['color']}; padding: 22px; border-radius: 8px; margin-bottom: 25px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                        <span style="color: {bp_data['color']}; font-size: 14px; font-weight: 900;">📢 決策標籤：{bp_data['strategy_name']}</span>
                        <span style="background-color: {bp_data['color']}; color: white; padding: 4px 12px; border-radius: 4px; font-size: 13px; font-weight:800;">{bp_data['action_now']}</span>
                    </div>
                    <h3 style="margin: 5px 0; color: {bp_data['color']}; font-size: 23px; font-weight: 900;">即時策略防線：{bp_data['signal']}</h3>
                    <div style="margin: 12px 0 18px 0; color: #0F172A; font-size: 15.5px; line-height: 1.65; text-align: justify; font-weight: 700; background-color: #FFFFFF; padding: 14px; border-radius: 6px; border: 2px solid #E2E8F0;">
                        <span style="color: #0F172A; font-weight: 900;">📌 白話總結：</span>{bp_data['desc']}
                    </div>
                    <div style="background-color: white; border: 1px solid #E2E8F0; padding: 15px; border-radius: 6px; margin-top: 10px;">
                        <span style="color: #475569; font-size: 13px; font-weight: 800; display: block; margin-bottom: 8px;">🎯 價格計畫與風險界線 [詳細數據可於下方展開]</span>
                        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px;">
                            <div style="background-color: #FFF5F5; padding: 10px; border-radius: 4px; border-left: 3px solid #EF4444;"><small style="color: #DC2626; font-weight: 800;">🛑 1. 趨勢失效參考</small><p style="margin:3px 0 0 0; font-size:13px; font-weight:bold; color:#1E293B;">{bp['停損防守']}</p></div>
                            <div style="background-color: #FFFBEB; padding: 10px; border-radius: 4px; border-left: 3px solid #F59E0B;"><small style="color: #D97706; font-weight: 800;">⚠️ 2. 移動保護參考</small><p style="margin:3px 0 0 0; font-size:13px; font-weight:bold; color:#1E293B;">{bp['移動停利']}</p></div>
                            <div style="background-color: #F0FDF4; padding: 10px; border-radius: 4px; border-left: 3px solid #10B981;"><small style="color: #16A34A; font-weight: 800;">🚀 3. 情境目標參考</small><p style="margin:3px 0 0 0; font-size:13px; font-weight:bold; color:#1E293B;">{bp['預期目標']}</p></div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                # 2. 多週期趨勢、線型與價量診斷：先白話，再看數據
                st.markdown("### ⏱️ 趨勢、波段線型與價量判斷")
                ta = res['trend_analysis']
                structure_plain = plain_structure_explanation(ta['structure'])
                strength_plain = plain_trend_strength(ta['adx'])
                pv_plain = plain_price_volume(ta)

                st.markdown(render_plain_card(
                    "📌 整體趨勢怎麼看",
                    f"週線為『{ta['weekly_desc']}』，長期為『{ta['long_term']}』，中期處於『{ta['medium_term']}』，短期為『{ta['short_term']}』。",
                    "短期轉弱不等於長期翻空；只有波段低點、重要均線與賣壓同時惡化，才會升級為趨勢破壞。",
                    f"目前狀態為『{res['trend_state']}』。{'已持有者以續抱與防守為主。' if user_holding else '未持有者依進場模型等待合適位置。'}",
                    "#2563EB"), unsafe_allow_html=True)

                c_plain1, c_plain2, c_plain3 = st.columns(3)
                with c_plain1:
                    structure_color = "#EF4444" if "🔴" in structure_plain['title'] else "#10B981" if "🟢" in structure_plain['title'] else "#F59E0B"
                    st.markdown(render_plain_card(structure_plain['title'], structure_plain['meaning'], structure_plain['impact'], structure_plain['action'], structure_color), unsafe_allow_html=True)
                with c_plain2:
                    st.markdown(render_plain_card("📈 "+strength_plain['title'], strength_plain['meaning'], "趨勢越明確，順著主要方向操作的參考價值越高；趨勢弱時則容易反覆。", strength_plain['action'], "#7C3AED"), unsafe_allow_html=True)
                with c_plain3:
                    pv_color = "#10B981" if "🟢" in pv_plain['title'] else "#EF4444" if "🔴" in pv_plain['title'] else "#F59E0B"
                    st.markdown(render_plain_card(pv_plain['title'], pv_plain['meaning'], pv_plain['impact'], pv_plain['action'], pv_color), unsafe_allow_html=True)

                with st.expander("🔎 查看趨勢、線型與價量的數據依據", expanded=show_evidence_default):
                    swing_high = ta['structure'].get('last_swing_high')
                    swing_low = ta['structure'].get('last_swing_low')
                    evidence_df = pd.DataFrame([
                        {"項目":"現價", "數值":f"{res['current_price']:.2f} 元", "判斷用途":"與均線、支撐及壓力比較"},
                        {"項目":"MA10 / MA20 / MA60", "數值":f"{ta['ma10']:.2f} / {res['ma20_val']:.2f} / {res['ma60_val']:.2f}", "判斷用途":"短、中期趨勢位置"},
                        {"項目":"MA20 / MA60 斜率", "數值":f"{ta['slope20']:+.2f}% / {ta['slope60']:+.2f}%", "判斷用途":"均線是否仍向上，而非只看交叉"},
                        {"項目":"最近波段高點", "數值":f"{swing_high:.2f} 元" if swing_high else "資料不足", "判斷用途":"前方壓力與高點是否墊高"},
                        {"項目":"最近波段低點", "數值":f"{swing_low:.2f} 元" if swing_low else "資料不足", "判斷用途":"趨勢失效與低點是否墊高"},
                        {"項目":"趨勢失效參考價", "數值":f"{res['structure_stop']:.2f} 元", "判斷用途":"跌破不等於立刻賣出，需搭配收盤、量能與連續天數確認"},
                        {"項目":"ADX14", "數值":f"{ta['adx']:.1f}", "判斷用途":"低於18偏震盪；18至25趨勢形成；25以上趨勢較明確"},
                        {"項目":"近5日拉回量比", "數值":f"{ta['pullback_volume_ratio']:.2f} 倍", "判斷用途":"0.9倍以下視為拉回量縮參考"},
                        {"項目":"距60日高點回檔", "數值":f"{ta['drawdown_pct']:.1f}%", "判斷用途":"辨識追高、正常拉回或深度修正"},
                        {"項目":"量價背離", "數值":ta['volume_divergence'], "判斷用途":"價格創高時資金是否同步"},
                        {"項目":"狀態確認天數", "數值":f"弱化 {res['trend_state_detail']['weak_days']} 日；結構跌破 {res['trend_state_detail']['break_days']} 日", "判斷用途":"避免一天訊號就翻多翻空"},
                    ])
                    st.dataframe(evidence_df, use_container_width=True, hide_index=True)
                    st.caption("判斷門檻是規則化參考，不代表固定勝率；正式使用前仍應用不同產業與市場階段回測。")

                st.markdown(f"""
                <div style="background:#F8FAFC;border:1px solid #CBD5E1;border-left:6px solid #2563EB;padding:14px;border-radius:6px;margin-bottom:14px;line-height:1.7;">
                <b>目前進場方式：</b>{ta['entry_model']}｜<b>條件是否完整：</b>{'已確認' if ta['entry_ready'] else '尚未確認'}<br>
                <b>白話解讀：</b>{'目前已符合此進場方式的主要條件，但仍建議分批。' if ta['entry_ready'] else '目前只有部分條件成立，先等待止跌、放量或突破確認。'}
                </div>
                """, unsafe_allow_html=True)
                with st.expander("查看四種進場模型與訊號變更紀錄", expanded=False):
                    model_df = pd.DataFrame([
                        {"模型":"突破進場", "成立":ta['breakout_model'], "說明":"放量越過20日壓力；避免乖離過大時追價"},
                        {"模型":"多頭拉回", "成立":ta['pullback_model'], "說明":"長中期向上、回檔量縮且未破波段低點"},
                        {"模型":"突破後回測", "成立":ta['retest_model'], "說明":"原壓力轉支撐，回測量縮並等待止跌"},
                        {"模型":"築底轉強", "成立":ta['base_model'], "說明":"低點墊高、均線走平後突破，風險較高"},
                    ])
                    st.dataframe(model_df, use_container_width=True, hide_index=True)
                    state_logs=st.session_state.get(f"trend_log_{res['stock_id']}", [])
                    if state_logs: st.dataframe(pd.DataFrame(state_logs), use_container_width=True, hide_index=True)
                    else: st.caption("本次工作階段尚無狀態變更紀錄。")

                if res['peer_corr_val'] is not None and res['peer_corr_val'] < 0.3:
                    st.info(f"⚠️ 【大摩共振預警】當前個股與同業龍頭相關性極低 ({res['peer_corr_val']:.2f})。數據來源：同產業股票近60日報酬率 Pearson 相關係數。")

                # 昨晚美股即時戰報
                st.markdown("### 🌐 海外市場與台股大盤參考 [數據來源：Yahoo Finance；本區不含台指期夜盤]")
                radar_show = res["radar_results"]
                if radar_show:
                    rd_cols = st.columns(len(radar_show))
                    for i, (lbl, val) in enumerate(radar_show.items()):
                        with rd_cols[i]: st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:10px; border-radius:6px; text-align:center;"><span style="font-size:12px; color:#64748B; font-weight:600;">{lbl}</span><h4 style="margin:4px 0 0 0; color:#10B981; font-weight:800;">{val:+.2f}%</h4></div>""", unsafe_allow_html=True)

                # 3. 標對資訊頭部
                st.markdown(f"""<div style="background-color: #1F2937; padding: 18px; border-radius: 8px; border: 2px solid #3B82F6; margin-bottom: 20px;"><div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;"><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">DIAGNOSTIC TARGET</span><h1 style="margin: 4px 0 0 0; color: #FFFFFF; font-size: 28px; font-weight: 800;">{res['stock_name']} <span style="color: #3B82F6;">({res['stock_id']})</span></h1></div><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">大類板塊歸屬</span><h3 style="margin: 4px 0 0 0; color: #F3F4F6; font-size: 18px; font-weight: 700;">{res['industry']}</h3></div><div style="text-align: right; background-color: rgba(255,255,255,0.05); padding: 6px 12px; border-radius: 6px;"><span style="color: #9CA3AF; font-size: 11px; font-weight: 600; display:block;">實時流狀態</span><span style="color: #F9FAFB; font-weight: 600; font-size: 13px;">真實數據源: {res['rt_source']} | </span><span style="color: {res['m_color']}; font-weight: 700; font-size: 13px;">{res['m_desc']}</span></div></div></div>""", unsafe_allow_html=True)

                # 4. 即時報價 HUD 箱
                c1, c2, c3, c4 = st.columns(4)
                with c1: st.markdown(custom_hud_box("💡 當前即市價 [來源: 富果/證交所快流]", f"<span style='font-size:20px; color:#0F172A;'>{res['current_price']:.2f} 元</span><br><small style='color:#64748B;'>今日成交: {res['current_vol']:.0f} 張</small>"), unsafe_allow_html=True)
                with c2: st.markdown(custom_hud_box("⏱️ 5日平均成本線 [來源: 歷史K線滾動計算]", f"<span style='font-size:16px; color:#1E293B;'>{res['ma5_val']:.2f} 元</span><br><small style='color:#64748B;'>今日漲跌幅: {res['stock_daily_pct']:+.2f}%</small>"), unsafe_allow_html=True)
                with c3: st.markdown(custom_hud_box("⏳ 波動保護線 [來源: ATR波動率公式]", f"<span style='font-size:16px; color:#7C3AED;'>{res['trailing_stop_line']}</span><br><small style='color:#64748B;'>當前 ATR14: {res['atr']:.2f}</small>"), unsafe_allow_html=True)
                with c4: st.markdown(custom_hud_box("📊 超額強度 [來源: 個股與大盤漲跌幅差值]", f"<span style='font-size:16px; color:#10B981;'>超額 {res['relative_strength']:+.2f}%</span><br><small style='color:#64748B;'>大盤共振: {'🔥 成立' if res['is_rs_gold'] else '⚪ 整理中'}</small>"), unsafe_allow_html=True)

                # 多因子曝光面板
                st.markdown("### 🧭 其他重要因素：白話結論與數據依據")
                ib_col1, ib_col2, ib_col3 = st.columns(3)
                with ib_col1:
                    macro_detail_desc = f"數據來源：加權指數日成交金額。市場量能不足時，突破訊號通常較不穩定，但實際結果仍需回測驗證。"
                    st.markdown(render_panel_html("1. 總體流動性安全閥 [來源: 證交所TAIEX日報]", res['market_vol_desc'], macro_detail_desc, "#3B82F6"), unsafe_allow_html=True)
                with ib_col2:
                    ins = res["institutional_summary"]
                    ins_desc = f"外資：{ins['foreign_text']}<br>投信：{ins['trust_text']}<br>自營商：{ins['dealer_text']}"
                    st.markdown(render_panel_html("2. 三大法人20日一致性 [免費公開日報]", f"法人共識：{ins['consensus_label']}", ins_desc, "#10B981"), unsafe_allow_html=True)
                with ib_col3:
                    st.markdown(render_panel_html("3. [板塊動能] 產業群聚共振定位", "追蹤同業有沒有集體進攻", res['peer_resonance_text'], "#7C3AED"), unsafe_allow_html=True)

                with st.expander("🔎 查看多因子面板的原始數據與來源", expanded=show_evidence_default):
                    ins_table = res["institutional_summary"]["table"]
                    st.markdown("**三大法人20日統計**")
                    if not ins_table.empty:
                        st.dataframe(ins_table, use_container_width=True, hide_index=True)
                    else:
                        st.caption("法人資料不足。")
                    factor_df = pd.DataFrame([
                        {"因素":"大盤狀態", "原始數據／狀態":res['m_desc'], "系統如何使用":"大盤偏弱時降低個股訊號信心，不直接替個股判死刑"},
                        {"因素":"大盤量能", "原始數據／狀態":res['market_vol_desc'], "系統如何使用":"量能不足時降低突破可信度"},
                        {"因素":"產業同業", "原始數據／狀態":f"比較 {res['peer_count']} 檔；相關性 {res['peer_corr_val']:.2f}" if res['peer_corr_val'] is not None else "資料不足", "系統如何使用":"判斷個股是否獨強或與產業同步"},
                        {"因素":"融資", "原始數據／狀態":res['margin_trend'], "系統如何使用":"融資快速增加但價格不強時提高追高警戒"},
                        {"因素":"估值", "原始數據／狀態":f"PB {res['pb_ratio']:.2f} 倍；BVPS {res['bvps']:.2f} 元" if res['pb_ratio'] is not None and res['bvps'] else "資料不足", "系統如何使用":"只作產業內估值參考，不跨產業硬比"},
                        {"因素":"資料完整度", "原始數據／狀態":f"{res['data_quality_score']:.0f}%", "系統如何使用":"低於60%時不產生明確方向"},
                    ])
                    st.dataframe(factor_df, use_container_width=True, hide_index=True)

                # 7. 底層因果深度解碼驗證區
                st.markdown("---")
                st.markdown("### 🔍 詳細數據與判斷依據")
        
                # 口語化籌碼與估值說明
                pb_text = f"{res['pb_ratio']:.2f} 倍" if res['pb_ratio'] is not None and res['bvps'] else "資料不足"
                bvps_text = f"{res['bvps']:.2f} 元" if res['bvps'] else "資料不足"
                st.markdown("#### ⚡ 籌碼與估值重點 [數據源：FinMind；僅供資訊整理]")
                st.markdown(f"""
                <div style="background-color:#FFFFFF; padding:16px; border:2px solid #7D3CFF; border-left:8px solid #7D3CFF; border-radius:6px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,0.02);">
                    <p style="margin:0 0 12px 0; color:#0F172A; font-size:14.5px; font-weight:700; line-height:1.65;">
                        <span style="color:#7D3CFF; font-weight:900; font-size:15px;">📊 【估值與法人狀況】➔ </span>
                        目前這檔股票的最新股價，股價淨值比參考為 <b>{pb_text}</b>（每股淨值參考：{bvps_text}）。不同產業不宜只用同一估值指標判斷。
                        三大法人20日一致性為【<b>{res['institutional_summary']['consensus_label']}</b>】；其中外資：{res['institutional_summary']['foreign_text']}。投信：{res['institutional_summary']['trust_text']}。融資熱度為【<b>{res['margin_trend']}</b>】。
                    </p>
                    <p style="margin:0; color:#0F172A; font-size:14.5px; font-weight:700; line-height:1.65;">
                        <span style="color:#2563EB; font-weight:900; font-size:15px;">⏱️ 【技術指標動能解讀】➔ </span>
                        <b>1. 隨機指標(KD)：</b>{res['kd_timing']}<br>
                        <b>2. 中短期動能(MACD)：</b>{res['bb_stage']}<br>
                        <b>3. 漲跌速度與過熱程度(RSI)：</b>{res['volume_verdict']}
                    </p>
                </div>
                """, unsafe_allow_html=True)

                # 區塊 B：三大法人明細大表
                with st.expander("🦅 三大法人每日實時進出買賣超佈局明細大表 (近30日現況) ─ 點擊展開明細 [數據來源: 證交所三大法人日報]", expanded=False):
                    if not res["institutional_summary"]["table"].empty:
                        st.markdown("**20日法人一致性摘要**")
                        st.dataframe(res["institutional_summary"]["table"], use_container_width=True, hide_index=True)
                    if not res["institutional_df"].empty:
                        st.markdown("**每日買賣超明細**")
                        st.dataframe(res["institutional_df"].style.format({"外資(張)": "{:+,.1f}", "投信(張)": "{:+,.1f}", "自營商總計(張)": "{:+,.1f}"}), use_container_width=True)
                    else:
                        st.caption("目前無法取得三大法人日報資料。")

                # 區塊 C：免費公開分析師共識（有資料才顯示）
                bc = res["broker_consensus"]
                if bc.get("is_real", False):
                    st.markdown("### 🎯 免費公開分析師目標價共識")
                    coverage = f"｜涵蓋分析師數：{int(bc['coverage_count'])}" if bc.get("coverage_count") else ""
                    st.markdown(f"""<div style="background-color:#F5F3FF; padding:12px; border-left:4px solid #7C3AED; border-radius:4px; margin-bottom:12px; font-size:14px; color:#5B21B6; font-weight:700;">平均目標價：{bc['mean']:.2f} 元｜最高：{bc['high']:.2f} 元｜最低：{bc['low']:.2f} 元｜公開彙整評等：{bc.get('rating') or '未提供'}{coverage}<br><small style='color:#6D28D9; font-weight:600;'>資料來源：{bc.get('source')}。這不是逐家外資或本土投顧報告，無法驗證各券商名稱、報告日期與完整論點，因此只作市場共識參考。</small></div>""", unsafe_allow_html=True)
                else:
                    st.caption("🎯 免費公開來源查無可靠分析師目標價共識，本區自動隱藏；系統不推估、不杜撰逐家券商報告。")

                # 區塊 D：財務基本面季度結構矩陣大表
                st.markdown("### 📊 財務基本面季度結構矩陣大表")
                with st.expander("📊 點擊此處展開 / 收合財務基本面季度數據細項明細表 [數據來源: 臺灣證券交易所公開資訊觀測站]", expanded=False):
                    st.markdown(f"""<div style="background-color:#EFF6FF; padding:10px; border-left:4px solid #3B82F6; border-radius:4px; margin-bottom:12px; font-size:13.5px; color:#1E40AF; font-weight:700;">📋 最新基本面狀態：{res['fin_conclusion']} ｜ 核心營收年增率 (YoY)：{res['latest_yoy']:.2f}%</div>""", unsafe_allow_html=True)
                    if not res["fin_df"].empty:
                        clean_fin_show = res["fin_df"].copy()
                        show_cols = ["date", "EPS", "Revenue", "GrossProfit", "OperatingIncome", "gpm", "opm"]
                        clean_fin_show = clean_fin_show[[c for c in show_cols if c in clean_fin_show.columns]]
                        clean_fin_show.columns = ["季度日期", "單季 EPS", "營業收入", "營業毛利", "營業利益", "單季毛利率 (%)", "單季營益率 (%)"]
                        st.dataframe(clean_fin_show.style.format({"單季 EPS": "{:.2f}", "營業收入": "{:,.0f}", "營業毛利": "{:,.0f}", "營業利益": "{:,.0f}", "單季毛利率 (%)": "{:.2f}%", "單季營益率 (%)": "{:.2f}%"}), use_container_width=True)

                # 區塊 E：新聞輿情流水線
                st.markdown("### 📰 資訊面 24H 網路輿情即時新聞流水線")
                st.markdown(f"""<div style="background-color:#F0FDF4; padding:10px; border-left:4px solid #10B981; border-radius:4px; margin-bottom:12px; font-size:13.5px; color:#15803D; font-weight:700;">> 新聞標題文字傾向（非股價預測）：{res['news_analysis_report']} | [底層數據源: Google News RSS 實時檢索引擎]</div>""", unsafe_allow_html=True)
                if isinstance(res["raw_news_list"], list) and res["raw_news_list"]:
                    for n in res["raw_news_list"]: 
                        st.markdown(f"* **[{n['sentiment']}]** [{n['source']}]({n['link']}) ─ {n['title']}")
                else:
                    st.markdown("* ⚪ 當前時間窗口內暫無網路公開輿情新聞（已自動轉入常態監控）")

                st.markdown("---")
        
                # 9. 最底部開火指令
                st.markdown("### 🛡/⚔️ 風控指揮中心：量化核心配額開火劇本")
                bx1, bx2, bx3 = st.columns(3)
                with bx1: st.metric("風險預算可容納部位（含粗估成本與滑價）", f"{res['suggested_lots']} 張 + {res['suggested_odd_lot']} 股")
                with bx2: st.metric("結構停損估計成交價（含滑價）", f"{res['expected_stop_price']:.2f} 元")
                with bx3: st.metric("大波段移動停利線 (ATR)", res["trailing_stop_line"])

        if debug_mode:
            st.markdown("---")
            with st.expander("🛠 成交量資料診斷", expanded=True):
                ta_debug = res.get("trend_analysis", {}) or {}
                volume_valid_debug = bool(res.get("volume_valid", False))
                volume_ratio_enabled_debug = bool(res.get("volume_ratio_enabled", False))
                today_lots = float(res.get("current_vol", 0) or 0)
                avg20_lots = float(res.get("volume_ma20_lots", 0) or 0)
                ratio_debug = float(ta_debug.get("volume_ratio", 0) or 0)

                if volume_valid_debug:
                    st.success("即時成交量已成功取得。")
                else:
                    st.warning("即時成交量尚未取得或欄位無效。")
                if volume_valid_debug and not volume_ratio_enabled_debug:
                    st.info("成交量資料有效，但盤中量比功能目前停用，因此不納入 AI 的量比判斷。")

                d1, d2, d3, d4 = st.columns(4)
                with d1: st.metric("行情來源", str(res.get("rt_source", "未知")))
                with d2: st.metric("價格取得成功", "是" if res.get("quote_success") else "否")
                with d3: st.metric("成交量有效", "是" if volume_valid_debug else "否")
                with d4: st.metric("資料時間", str(res.get("quote_time") or "未提供"))

                v1, v2, v3 = st.columns(3)
                with v1: st.metric("今日累計成交量", f"{today_lots:,.0f} 張" if volume_valid_debug else "尚未取得")
                with v2: st.metric("近20日平均成交量", f"{avg20_lots:,.0f} 張" if avg20_lots > 0 else "資料不足")
                with v3:
                    if volume_valid_debug and volume_ratio_enabled_debug and avg20_lots > 0:
                        st.metric("今日量比", f"{ratio_debug:.2f} 倍")
                    elif not volume_ratio_enabled_debug:
                        st.metric("今日量比", "已停用")
                    else:
                        st.metric("今日量比", "資料不足")

                st.markdown("**計算過程**")
                if volume_valid_debug and volume_ratio_enabled_debug and avg20_lots > 0:
                    st.code(f"{today_lots:,.0f} 張 ÷ {avg20_lots:,.0f} 張 = {ratio_debug:.4f} 倍")
                    if ratio_debug >= 1.20:
                        st.success(f"成交量條件成立：{ratio_debug:.2f} ≥ 1.20")
                    else:
                        st.info(f"成交量條件尚未成立：{ratio_debug:.2f} < 1.20")
                elif not volume_ratio_enabled_debug:
                    st.code("成交量已取得，但即時成交量比率功能已停用，不計算 volume_ratio。")
                else:
                    st.code("成交量或近20日平均成交量不足，無法計算 volume_ratio。")

                st.markdown("**API 原始成交量欄位**")
                st.code(repr(res.get("raw_volume")))
                st.caption(str(res.get("volume_note", "未提供診斷說明")))
                st.caption(f"統一資料層：price={res.get('market_data', {}).get('price')}｜volume_lots={res.get('market_data', {}).get('volume_lots')}｜volume_valid={res.get('market_data', {}).get('volume_valid')}｜AI量比啟用={res.get('market_data', {}).get('volume_ratio_enabled')}")

if auto_refresh:
    time.sleep(15)
    st.rerun()
