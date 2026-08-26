import os, time, math, json, sqlite3, requests, certifi, pytz, urllib.parse, shutil
import pandas as pd
import numpy as np
import streamlit as st
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from FinMind.data import DataLoader
from stockpilot.adapters import build_legacy_payload_from_app
from stockpilot.services.shadow_integration import ShadowIntegration

# ============ 1. Page Config ============
st.set_page_config(page_title="Project Compass V3｜單一決策執行中心", layout="wide")

# ============ 2. Global Constants ============
TZ = pytz.timezone("Asia/Taipei")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")
FUGLE_TOKEN = os.getenv("FUGLE_TOKEN", "") or st.secrets.get("FUGLE_TOKEN", "")

# 即時成交量來源單位與更新時點不穩定，暫停使用「當日成交量比率」做顯示與決策。
USE_INTRADAY_VOLUME_RATIO = False

# StockPilot 4.0 Shadow Mode：舊版仍是正式輸出，4.0 僅背景比對。
ENABLE_STOCKPILOT4_SHADOW = True
SHOW_STOCKPILOT4_SHADOW_PANEL = True

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
    # 正式部署可改接 logging / S進場區；前台不暴露金鑰與完整堆疊。
    print(f"[{area}] {type(exc).__name__}: {exc}")


def normalize_shadow_price_order(payload):
    """
    v8.9d：在 Shadow Decision Engine 前統一校正價格層級。
    支援 dict / list / Pydantic model / dataclass / 一般 Python 物件。
    規則：structural_exit 必須嚴格低於 moving_protection。
    """
    fixes = []
    visited = set()

    def _num(v):
        try:
            if v is None:
                return None
            return float(v)
        except Exception:
            return None

    def _correct_pair(container, getter, setter, path):
        try:
            structural = _num(getter("structural_exit"))
            moving = _num(getter("moving_protection"))
        except Exception:
            return

        if (
            structural is None or moving is None
            or structural <= 0 or moving <= 0
            or structural < moving
        ):
            return

        tick = tick_size(moving)
        corrected = floor_to_tick(max(tick, moving - tick), tick)
        if corrected >= moving:
            corrected = max(tick, moving - tick)

        try:
            setter("structural_exit", corrected)
            fixes.append({
                "path": path,
                "old_structural_exit": structural,
                "moving_protection": moving,
                "new_structural_exit": corrected,
            })
        except Exception:
            pass

    def _walk(obj, path="root"):
        if obj is None:
            return

        oid = id(obj)
        if oid in visited:
            return
        visited.add(oid)

        # dict
        if isinstance(obj, dict):
            if "structural_exit" in obj and "moving_protection" in obj:
                _correct_pair(
                    obj,
                    lambda k: obj.get(k),
                    lambda k, v: obj.__setitem__(k, v),
                    path,
                )
            for k, v in list(obj.items()):
                _walk(v, f"{path}.{k}")
            return

        # list / tuple
        if isinstance(obj, (list, tuple)):
            for idx, v in enumerate(obj):
                _walk(v, f"{path}[{idx}]")
            return

        # Pydantic v2 model
        if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
            fields = getattr(obj, "model_fields", {}) or {}
            if "structural_exit" in fields and "moving_protection" in fields:
                _correct_pair(
                    obj,
                    lambda k: getattr(obj, k, None),
                    lambda k, v: object.__setattr__(obj, k, v),
                    path,
                )
            for k in fields:
                try:
                    _walk(getattr(obj, k), f"{path}.{k}")
                except Exception:
                    pass
            return

        # dataclass / 一般物件
        if hasattr(obj, "__dict__"):
            d = vars(obj)
            if "structural_exit" in d and "moving_protection" in d:
                _correct_pair(
                    obj,
                    lambda k: getattr(obj, k, None),
                    lambda k, v: setattr(obj, k, v),
                    path,
                )
            for k, v in list(d.items()):
                _walk(v, f"{path}.{k}")

    _walk(payload)
    return payload, fixes


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



# ============ StockPilot 4.0 Shadow Data Bridge ============
@st.cache_data(ttl=900)
def get_shadow_market_index_df(market_type: str = "TSE"):
    """僅供 4.0 Shadow 使用；上市使用 TAIEX，上櫃使用 TPEx，不互相替代。"""
    is_otc = any(
        x in str(market_type).upper()
        for x in ["OTC", "TWO", "櫃", "上櫃"]
    )
    benchmark_id = "TPEx" if is_otc else "TAIEX"

    try:
        raw = get_api().taiwan_stock_daily(
            stock_id=benchmark_id,
            start_date=(datetime.now(TZ) - timedelta(days=240)).strftime("%Y-%m-%d"),
        )
        if raw is None or raw.empty:
            return pd.DataFrame()

        df = raw.copy().sort_values("date").drop_duplicates("date")
        df = df.rename(
            columns={
                "max": "high",
                "min": "low",
                "Trading_Volume": "vol",
            }
        )

        required = ["date", "open", "high", "low", "close"]
        if not all(col in df.columns for col in required):
            return pd.DataFrame()

        if "vol" not in df.columns:
            df["vol"] = np.nan

        for col in ["open", "high", "low", "close", "vol"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return (
            df[["date", "open", "high", "low", "close", "vol"]]
            .dropna(subset=["close"])
            .reset_index(drop=True)
        )
    except Exception as exc:
        log_error(f"StockPilot 4.0 market bridge {benchmark_id}", exc)
        return pd.DataFrame()


@st.cache_data(ttl=900)
def get_shadow_margin_df(stock_id: str):
    """僅供 4.0 Shadow 使用；保留融資融券原始缺值，不以 0 補值。"""
    try:
        raw = get_api().taiwan_stock_margin_purchase_short_sale(
            stock_id=str(stock_id),
            start_date=(datetime.now(TZ) - timedelta(days=120)).strftime("%Y-%m-%d"),
        )
        if raw is None or raw.empty:
            return pd.DataFrame()

        cols = [
            col
            for col in [
                "date",
                "MarginPurchaseTodayBalance",
                "ShortSaleTodayBalance",
            ]
            if col in raw.columns
        ]
        if "date" not in cols or "MarginPurchaseTodayBalance" not in cols:
            return pd.DataFrame()

        return raw[cols].copy().sort_values("date").reset_index(drop=True)
    except Exception as exc:
        log_error(f"StockPilot 4.0 margin bridge {stock_id}", exc)
        return pd.DataFrame()


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
            "accumulation":accumulation, "volume_divergence":divergence, "進場區_model":model, "進場區_ready":model_ready,
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
    elif analysis["進場區_model"]=="突破進場" and analysis["進場區_ready"]: state="突破確認"
    elif analysis["medium_term"]=="主升段": state="多頭持有"
    elif analysis["進場區_model"]=="築底轉強": state="趨勢轉強"
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

def unified_institutional_brain(res_dict, df_hist, is_holding=False, 進場區_cost=0.0, sector_panic=False):
    p=res_dict["current_price"]; q=res_dict.get("data_quality_score",0); state=res_dict.get("trend_state","觀察")
    ta=res_dict.get("trend_analysis",{}); chip=f"投信：{res_dict.get('sitc_trend')}；融資：{res_dict.get('margin_trend')}。"
    structure_stop=res_dict.get("structure_stop",res_dict["stop_brk"])
    if q<60 or res_dict.get("macro_bull") is None:
        return {"strategy_name":"⚪ 資料不足","color":"#64748B","action_now":"只觀察，不產生方向","signal":"關鍵資料未完整","blueprint":{"停損防守":"待資料恢復","移動停利":"不適用","預期目標":"不提供"},"desc":f"資料完整度 {q:.0f}%，不足以形成可靠方向。"}
    if sector_panic:
        return {"strategy_name":"🟠 族群風險升高","color":"#F59E0B","action_now":"暫停新增部位","signal":"同產業集體轉弱","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":"縮小風險","預期目標":"待族群止穩"},"desc":"族群同步下跌時，個股拉回較可能演變成趨勢破壞。"}
    if is_holding and 進場區_cost>0:
        if state in ["趨勢破壞","空頭"]:
            return {"strategy_name":"🔴 波段結構已破壞","color":"#EF4444","action_now":"依計畫減碼或退出","signal":"結構低點與中期趨勢同時失守","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":"已觸發","預期目標":"先控制風險"},"desc":"不是因為單日跌破5日線，而是波段低點、60日線或放量賣壓已共同惡化。"}
        if state=="多頭轉弱警戒":
            return {"strategy_name":"🟠 多頭轉弱警戒","color":"#F59E0B","action_now":"續抱觀察，必要時分批減碼","signal":"連續出現中期弱化條件","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":f"ATR線 {res_dict['trailing_stop_value']:.2f} 元","預期目標":"等待重新站回MA20"},"desc":"尚未直接判定空頭，但弱化已非單日雜訊。"}
        if state=="多頭正常拉回":
            return {"strategy_name":"🟢 多頭趨勢正常拉回","color":"#10B981","action_now":"續抱，不因短線跌破而殺出","signal":"週線與中長期結構仍完整，拉回量縮","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":f"ATR線 {res_dict['trailing_stop_value']:.2f} 元","預期目標":f"前高 {res_dict['real_resistance']:.2f} 元"},"desc":f"目前損益 {res_dict['pnl_pct']:+.1f}%。短線降溫不等於趨勢反轉；{chip}"}
        return {"strategy_name":"🟢 趨勢持有","color":"#10B981","action_now":"續抱並上移防守","signal":f"狀態：{state}","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":f"ATR線 {res_dict['trailing_stop_value']:.2f} 元","預期目標":f"情境價 {res_dict['target_brk']:.2f} 元"},"desc":f"趨勢尚未被結構性破壞。{chip}"}
    model=ta.get("進場區_model","等待")
    if model=="多頭拉回":
        action="可小額分批" if ta.get("進場區_ready") else "等待止跌確認"
        return {"strategy_name":"🟢 多頭拉回機會","color":"#10B981","action_now":action,"signal":"長中期多頭、回檔量縮且未破前低","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":"站回MA20後再上移","預期目標":f"前高 {res_dict['real_resistance']:.2f} 元"},"desc":"這是低價拉回模型，不必等到再創新高才追價；仍建議分批，而非一次買滿。"}
    if model=="突破後回測":
        return {"strategy_name":"🟢 突破後回測","color":"#10B981","action_now":"確認止跌後分批","signal":"原壓力轉支撐且回測量縮","blueprint":{"停損防守":f"突破失效線 {structure_stop:.2f} 元","移動停利":"續強後上移","預期目標":f"情境價 {res_dict['target_brk']:.2f} 元"},"desc":"通常比直接追突破有較好的風險報酬。"}
    if model=="突破進場":
        return {"strategy_name":"🟢 放量突破","color":"#10B981","action_now":"小額分批，不追過度乖離","signal":"價格與量能越過壓力","blueprint":{"停損防守":f"突破失效線 {structure_stop:.2f} 元","移動停利":"依結構與ATR上移","預期目標":f"情境價 {res_dict['target_brk']:.2f} 元"},"desc":"突破成立，但若距MA20過遠，應等待回測而不是追高。"}
    if model=="築底轉強":
        return {"strategy_name":"🟡 築底轉強","color":"#F59E0B","action_now":"僅適合小部位試單","signal":"低點墊高、均線走平後突破","blueprint":{"停損防守":f"底部結構線 {structure_stop:.2f} 元","移動停利":"待趨勢形成","預期目標":f"前壓 {res_dict['real_resistance']:.2f} 元"},"desc":"這是較積極的轉折模型，可靠度低於成熟多頭拉回。"}
    return {"strategy_name":"⚪ 等待更好位置","color":"#64748B","action_now":"不追價，等待拉回或確認","signal":"尚未符合四種進場模型","blueprint":{"停損防守":"未進場不設定","移動停利":"不適用","預期目標":"等待條件"},"desc":f"目前不必勉強交易。{chip}"}

# ============ 9. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int, is_holding=False, 進場區_cost=0.0, sector_panic=False):
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
    
    try: institutional_df = get_institutional_trading_df(stock_id, days=120)
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

    pnl_pct = ((current_price - 進場區_cost) / 進場區_cost * 100) if (is_holding and 進場區_cost > 0) else 0.0
    
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

    res_dict["tactical_blueprint"] = unified_institutional_brain(res_dict, df.copy(), is_holding=is_holding, 進場區_cost=進場區_cost, sector_panic=sector_panic)
    
    slippage = slip_ticks * t
    estimated_進場區 = ceil_to_tick(current_price + slippage, t)
    estimated_stop_fill = floor_to_tick(structure_stop - slippage, t)
    # 粗估雙邊手續費與賣出證交稅；實際折扣及商品稅率仍依券商/商品而異。
    estimated_cost_per_share = estimated_進場區 * (0.001425 * 2 + 0.003)
    risk_per_share = max(estimated_進場區 - estimated_stop_fill + estimated_cost_per_share, 0)
    capital_ntd = total_capital * 10000
    risk_budget = capital_ntd * (risk_per_trade / 100)
    max_shares_by_risk = int(risk_budget / risk_per_share) if risk_per_share > 0 else 0
    max_shares_by_cash = int(capital_ntd / max(estimated_進場區 * 1.001425, 0.01))
    suggested_shares = max(0, min(max_shares_by_risk, max_shares_by_cash))
    suggested_lots = suggested_shares // 1000
    suggested_odd_lot = suggested_shares % 1000
    res_dict.update({"suggested_lots": suggested_lots, "suggested_odd_lot": suggested_odd_lot,
                     "suggested_shares": suggested_shares, "expected_進場區_price": estimated_進場區,
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
    進場區 = float(res.get("expected_進場區_price", price) or price)
    if 進場區 <= 0:
        進場區 = price

    issues = []
    raw_stop = float(res.get("structure_stop", res.get("expected_stop_price", 0)) or 0)
    stop = raw_stop
    stop_ceiling = min(x for x in [price, 進場區] if x > 0) if any(x > 0 for x in [price, 進場區]) else 0
    if stop_ceiling > 0 and (stop <= 0 or stop >= stop_ceiling):
        fallback_gap = max(2.0 * atr, stop_ceiling * 0.03, tick)
        stop = floor_to_tick(max(tick, stop_ceiling - fallback_gap), tick)
        issues.append("原始結構價不在有效風險區間，已改用現價下方的 ATR／百分比防線。")

    raw_resistance = float(res.get("real_resistance", 0) or 0)
    model_target = float(res.get("expected_target_price", 0) or 0)
    valid_targets = sorted(x for x in [raw_resistance, model_target] if x > 進場區)
    target1 = valid_targets[0] if valid_targets else 進場區 + max(2.0 * atr, 進場區 * 0.05, tick)
    target2 = valid_targets[-1] if valid_targets else target1
    target_kind = "第一目標區"

    # 第一目標必須是可操作的近端目標；過遠的原始壓力改列為中長期延伸目標。
    if 進場區 > 0 and (target1 - 進場區) / 進場區 > 0.25:
        target2 = target1
        target1 = ceil_to_tick(進場區 + max(2.0 * atr, 進場區 * 0.08), tick)
        target1 = min(target1, ceil_to_tick(進場區 * 1.15, tick))
        target_kind = "近端第一目標區"
        issues.append("原始目標距離評估價超過 25%，已改列為中長期延伸目標，並建立較近的第一目標區。")
    if target1 <= 進場區:
        target1 = ceil_to_tick(進場區 + max(2.0 * atr, 進場區 * 0.05, tick), tick)
        issues.append("原始目標未高於評估價，已依 ATR 建立替代目標。")
    target2 = max(target2, target1)

    risk = max(進場區 - stop, 0)
    reward = max(target1 - 進場區, 0)
    rr = reward / risk if risk > 0 else None
    if risk <= 0:
        issues.append("風險防線與評估價無法形成有效風險距離，決策引擎將否決進場。")
    if rr is not None and rr < 1.0:
        issues.append(f"近端風險報酬比僅 {rr:.2f}，不符合積極進場條件。")

    進場區_gap_pct = ((price - 進場區) / 進場區 * 100) if price > 0 and 進場區 > 0 else None
    if 進場區_gap_pct is not None and abs(進場區_gap_pct) <= 1.0:
        進場區_zone_text = "目前已進入建議評估區"
    elif 進場區_gap_pct is not None and 進場區_gap_pct > 3.0:
        進場區_zone_text = "目前高於建議評估區，不宜因條件達標而追高"
    elif 進場區_gap_pct is not None and 進場區_gap_pct < -3.0:
        進場區_zone_text = "目前低於建議評估區，需先確認趨勢未失效"
    else:
        進場區_zone_text = "目前接近建議評估區"

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
        "today": today, "進場區": 進場區, "stop": stop, "target1": target1, "target2": target2,
        "target_kind": target_kind, "rr": rr, "pros": pros[:3], "cons": cons[:3],
        "issues": issues, "plan_valid": risk > 0 and target1 > 進場區,
        "進場區_zone_text": 進場區_zone_text, "進場區_gap_pct": 進場區_gap_pct,
        "raw_stop": raw_stop, "raw_resistance": raw_resistance,
    }

def build_ai_investment_committee(res: dict, compass: dict) -> dict:
    """把既有分析結果轉成可解釋的 AI 投資委員會，不改動底層模型。"""
    ta = res.get("trend_analysis", {}) or {}
    inst = res.get("institutional_summary", {}) or {}
    # 投資委員會是 DecisionSnapshot 的上游輸入，不能反向讀取尚未建立的快照。
    # Chip／Volume／Edge Engine 會在 build_decision_snapshot() 內建立，並由後續策略狀態機使用。
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
    進場區 = float(compass.get("進場區", 0) or 0)
    stop = float(compass.get("stop", 0) or 0)
    target = float(compass.get("target1", 0) or 0)
    resistance = float(res.get("real_resistance", target) or target)
    current = float(res.get("current_price", 0) or 0)
    atr = float(res.get("atr", 0) or 0)
    rr = compass.get("rr")
    stop_pct = ((進場區 - stop) / 進場區 * 100) if 進場區 > 0 and stop > 0 else None
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
        risk_summary = f"評估價 {進場區:.2f}、趨勢失效價（風險防線）{stop:.2f}，風險報酬比 {rr_text}；仍應採分批，並遵守風險防線。"
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
    """大盤環境判斷：只分析市場，不使用持股成本或帳面損益。"""
    ta=res.get("trend_analysis",{}) or {}
    current=float(res.get("current_price",0) or 0); 進場區=float(compass.get("進場區",current) or current)
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
      "price_position":max(0,min(100,60+(10 if current>=進場區 else -8)-(22 if 進場區>0 and current>進場區*1.05 else 0)-(12 if target1>0 and current>=target1*.95 else 0))),
      "risk":max(0,min(100,70-(65 if stop>0 and current<=stop else 0)-(20 if quality<60 else 0)-(15 if not direction_ok else 0))),
      "data":max(0,min(100,quality)),
    }
    weights={"trend":.35,"chips":.20,"momentum":.15,"price_position":.10,"risk":.15,"data":.05}
    market_score=int(round(sum(components[k]*weights[k] for k in weights)))
    stop_broken=stop>0 and current<=stop; trend_veto=any(k in direction_text for k in ["空頭","趨勢破壞","下跌段"])
    data_veto=quality<50; hard_veto=stop_broken or trend_veto or data_veto; overextended=進場區>0 and current>進場區*1.05; near_pressure=target1>0 and current>=target1*.95
    checklist=[
      {"key":"trend","name":"中期趨勢維持","passed":direction_ok and ma20_ok,"current":f"{long_term}／{medium_term}｜MA20 {ma20:.2f}、斜率 {slope20:+.2f}%","why":"方向與均線共同決定是否值得持有。"},
      {"key":"chips","name":"籌碼沒有明顯惡化","passed":chip_neutral,"current":accumulation,"why":"市場資金是否撤退比使用者成本重要。"},
      {"key":"momentum","name":"動能與價量未轉空","passed":adx_ok and pv_ok,"current":f"ADX {adx:.1f}｜{price_volume}","why":"確認趨勢強度與賣壓。"},
      {"key":"price","name":"價格位置仍具合理風險報酬","passed":not overextended and not near_pressure and not stop_broken,"current":f"現價 {current:.2f}｜評估價 {進場區:.2f}｜目標 {target1:.2f}","why":"避免接近壓力或過度延伸時新增。"},
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
      "進場區":進場區,"stop":stop,"target1":target1,"trend_state":trend_state,"quality":quality,"rr":rr}


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
    進場區 = float(decision.get("進場區", compass.get("進場區", current)) or current)
    stop = float(decision.get("stop", compass.get("stop", 0)) or 0)
    target1 = float(decision.get("target1", compass.get("target1", 0)) or 0)
    completed = int(decision.get("completed", 0) or 0)
    total = int(decision.get("total", 0) or 0)
    pnl_pct = ((current / user_cost) - 1) * 100 if user_holding and user_cost > 0 and current > 0 else None
    capital_ntd = max(float(capital_wan or 0), 0) * 10000
    max_risk_ntd = capital_ntd * max(float(risk_pct or 0), 0) / 100
    per_share_risk = max(進場區 - stop, 0)
    max_shares = int(max_risk_ntd // per_share_risk) if per_share_risk > 0 else 0
    first_batch_shares = int(max_shares * 0.30 // 1000 * 1000) if max_shares >= 1000 else 0
    first_batch_amount = first_batch_shares * 進場區

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
    進場區 = float(decision.get("進場區", compass.get("進場區", current)) or current)
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
            "title": f"若收盤持續站穩 {進場區:.2f} 元，且五項條件不轉弱",
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

    if 進場區 > 0 and not decision.get("overextended"):
        chase_price = 進場區 * 1.03
        scenarios.append({
            "title": f"若股價快速高於約 {chase_price:.2f} 元",
            "result": "🟠 即使轉強也不追價",
            "detail": "超出評估價太多時，AI 會優先保護風險報酬。",
            "color": "#F97316",
        })

    return {"scenarios": scenarios[:4]}


def build_holding_value_analysis(res: dict, market: dict, regime: dict, levels: dict, user_holding: bool, user_cost: float) -> dict:
    """持股價值引擎：成本只描述損益，不決定方向；核心是剩餘報酬是否值得承擔下行風險。"""
    current = float(levels.get("current", res.get("current_price", 0)) or 0)
    突破確認價 = float(levels.get("突破確認價", 0) or 0)
    protective = float(levels.get("protective_stop", 0) or 0)
    structural = float(levels.get("structure_stop", 0) or 0)
    target = float(levels.get("target1", 0) or 0)
    market_score = int(market.get("market_score", 0) or 0)
    regime_score = int(regime.get("score", 50) or 50)
    status = str(market.get("status", "HOLD"))
    pnl_pct = ((current / user_cost) - 1) * 100 if user_holding and user_cost > 0 and current > 0 else None

    # 偏弱市場不把遠端多頭目標當成可實現報酬；只採用反彈確認價。
    if status in ["REDUCE", "EXIT"] or market_score < 55:
        reward_price = 突破確認價 if 突破確認價 > current else max(current, target)
        reward_role = "反彈確認價"
    else:
        reward_price = target if target > current else 突破確認價
        reward_role = str(levels.get("target_role", "第一目標"))

    risk_price = protective if 0 < protective < current else structural
    upside_pct = ((reward_price / current) - 1) * 100 if reward_price > current > 0 else 0.0
    downside_pct = ((current / risk_price) - 1) * 100 if 0 < risk_price < current else 0.0
    rr = upside_pct / downside_pct if downside_pct > 0 else None

    trend_score = float((market.get("components", {}) or {}).get("trend", 50) or 50)
    momentum_score = float((market.get("components", {}) or {}).get("momentum", 50) or 50)
    structure_broken = structural > 0 and current <= structural
    protective_broken = protective > 0 and current <= protective

    reasons = []
    if market_score >= 60: reasons.append(f"個股市場分數 {market_score}，仍有基本趨勢支持")
    else: reasons.append(f"個股市場分數只有 {market_score}，上漲證據不足")
    if regime_score >= 55: reasons.append(f"大盤環境 {regime_score} 分，未明顯拖累")
    else: reasons.append(f"大盤環境只有 {regime_score} 分，持股承受額外壓力")
    reasons.append(f"至{reward_role}約有 {upside_pct:.1f}% 空間")
    reasons.append(f"至第一層風險線約有 {downside_pct:.1f}% 下行風險")
    if rr is not None: reasons.append(f"剩餘風險報酬比約 {rr:.2f}")

    if structure_broken or status == "EXIT":
        grade, color, action = "不值得", "#DC2626", "退出／大幅減碼"
        conclusion = "趨勢或結構已失效，不應用等待反彈取代風險處理。"
    elif protective_broken:
        grade, color, action = "不值得", "#DC2626", "立即減碼"
        conclusion = "第一層風險線已跌破，先降低曝險，再等待重新確認。"
    elif status == "REDUCE":
        if rr is not None and rr < 0.8:
            grade, color, action = "不值得", "#DC2626", "今天先減碼"
            conclusion = "反彈空間小於下行風險，沒有必要只為等解套繼續承擔完整部位。"
        else:
            grade, color, action = "普通", "#F97316", "反彈減碼"
            conclusion = "趨勢偏弱；可保留部分部位等待反彈，但站不上確認價就應降低曝險。"
    elif status == "HOLD":
        if rr is not None and rr < 0.8:
            grade, color, action = "不值得", "#DC2626", "先減碼，不等反彈"
            conclusion = "雖未正式破線，但剩餘上漲空間不足以補償下跌風險。"
        elif rr is not None and rr < 1.5:
            grade, color, action = "普通", "#D97706", "續抱但降低部位"
            conclusion = "趨勢尚未失效，但風險報酬普通，不適合滿倉等待。"
        else:
            grade, color, action = "值得", "#2563EB", "續抱，不加碼"
            conclusion = "趨勢尚在且剩餘報酬高於風險，可續抱觀察確認事件。"
    else:
        if rr is not None and rr >= 1.5 and trend_score >= 60 and momentum_score >= 55:
            grade, color, action = "值得", "#16A34A", "續抱"
            conclusion = "趨勢、動能與風險報酬仍支持持有；加碼仍需另外確認價格位置。"
        else:
            grade, color, action = "普通", "#D97706", "續抱但不加碼"
            conclusion = "方向偏多，但剩餘報酬或動能尚未強到值得增加曝險。"

    score = 50
    score += max(-20, min(20, (market_score - 50) * 0.4))
    score += max(-15, min(15, (regime_score - 50) * 0.25))
    if rr is not None: score += max(-20, min(20, (rr - 1.0) * 12))
    if protective_broken: score -= 25
    if structure_broken: score -= 40
    score = int(max(0, min(100, round(score))))

    return {
        "available": bool(user_holding), "grade": grade, "color": color, "recommended_action": action,
        "conclusion": conclusion, "score": score, "pnl_pct": pnl_pct,
        "reward_price": reward_price, "reward_role": reward_role, "risk_price": risk_price,
        "upside_pct": upside_pct, "downside_pct": downside_pct, "rr": rr, "reasons": reasons,
    }


def build_today_action_board(res: dict, compass: dict, decision: dict, user_holding: bool = False, user_cost: float = 0.0, levels: dict | None = None, holding_value: dict | None = None) -> dict:
    """V3 Execution Engine：只輸出一個今天動作與三個後續觸發事件。"""
    levels = levels or {}
    holding_value = holding_value or {}
    current = float(levels.get("current", res.get("current_price", 0)) or 0)
    confirm = float(levels.get("突破確認價", 0) or 0)
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

    # 持股價值引擎可在尚未正式破線前，因風險報酬過差而提前要求減碼。
    hv_action = str(holding_value.get("recommended_action", ""))
    if hv_action in ["今天先減碼", "先減碼，不等反彈", "立即減碼", "退出／大幅減碼"]:
        headline = hv_action
        color = holding_value.get("color", "#DC2626")
        today_action = holding_value.get("conclusion", today_action)
    elif hv_action == "續抱但降低部位":
        headline = "續抱，但先降低部位"
        color = holding_value.get("color", "#D97706")
        today_action = holding_value.get("conclusion", today_action)

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
    進場區 = float(decision.get("進場區", compass.get("進場區", 0)) or 0)
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
        can_do = [f"收盤守住 {stop:.2f} 元才維持續抱" if stop > 0 else "先補齊趨勢失效價", f"接近 {target1:.2f} 元時分批停利" if target1 > 0 else "目標價資料不足時不新增", f"收盤站上 {進場區:.2f} 元且市場評級改善後才考慮加碼" if 進場區 > 0 else "條件完整後才考慮加碼"]
        avoid = ["不要在條件未齊時加碼", "不要因短線震盪隨意改計畫", "不要忽略風險防線"]
    else:
        first_missing = short_name(failed[0]) if failed else "進場條件"
        can_do = [f"等待 {first_missing} 達標", f"在 {進場區:.2f} 元附近觀察收盤" if 進場區 > 0 else "等待有效評估價", "先規劃第一筆部位與風險"]
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
    進場區 = float(compass.get("進場區", current) or current)
    stop = float(compass.get("stop", 0) or 0)
    target1 = float(compass.get("target1", 0) or 0)
    target2 = float(compass.get("target2", target1) or target1)
    confidence = int(committee.get("cio_confidence", compass.get("confidence", 0)) or 0)
    capital_ntd = max(float(capital_wan or 0), 0) * 10000
    max_risk_ntd = capital_ntd * max(float(risk_pct or 0), 0) / 100
    per_share_risk = max(進場區 - stop, 0)
    shares_by_risk = int(max_risk_ntd // per_share_risk) if per_share_risk > 0 else 0
    shares_by_cash = int(capital_ntd // 進場區) if 進場區 > 0 else 0
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
        if current > 進場區 * 1.03 and 進場區 > 0:
            headline = "不是不能買，而是不值得追高"
            primary = f"現價明顯高於建議評估價 {進場區:.2f} 元，等待拉回或突破後回測再分批。"
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
        "進場區": 進場區, "stop": stop, "target1": target1, "target2": target2,
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
                    進場區_price REAL,
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
        float(decision.get("進場區", compass.get("進場區", 0)) or 0),
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
                    進場區_price, stop_price, target_price, data_quality,
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
                    進場區_price=excluded.進場區_price,
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
        突破確認價 = ma20
        突破確認價_source = "收盤站上 MA20"
    elif resistance > current:
        突破確認價 = resistance
        突破確認價_source = "收盤突破最近壓力"
    else:
        突破確認價 = ceil_to_tick(current + max(atr_eff * 0.5, current * 0.02), tick)
        突破確認價_source = "收盤突破短線確認價"

    # 移動保護價負責減碼；結構失效價負責退出。兩者不能混稱同一停損。
    protective_candidates = [x for x in [ma60 * 0.98 if ma60 > 0 else 0, current - max(atr_eff * 1.25, current * 0.03)] if 0 < x < current]
    protective_stop = max(protective_candidates) if protective_candidates else current - max(atr_eff * 1.25, current * 0.03)
    structural_candidates = [x for x in [structure_raw, float(compass.get("stop", 0) or 0)] if 0 < x < protective_stop]
    structure_stop = max(structural_candidates) if structural_candidates else current - max(atr_eff * 3, current * 0.09)
    protective_stop = floor_to_tick(protective_stop, tick)
    structure_stop = floor_to_tick(min(structure_stop, protective_stop - tick), tick)

    進場區_center = ma20 if ma20 > 0 else current
    buffer_amt = max(atr_eff * 0.35, current * 0.01)
    進場區_low = floor_to_tick(max(tick, 進場區_center - buffer_amt), tick)
    進場區_high = ceil_to_tick(進場區_center + buffer_amt, tick)

    # 市場狀態決定「目標」的含義。
    if market_score < 60 or market_status in ["REDUCE", "EXIT"]:
        target1 = 突破確認價
        target_role = "反彈確認／減碼區"
        target_source = f"偏弱狀態不顯示遠端多頭目標；以{突破確認價_source}作為反彈處理區"
        target2 = 0.0
    else:
        candidates = [x for x in [resistance, float(compass.get("target1", 0) or 0), current + max(atr_eff * 2.5, current * 0.06)] if x > max(current, 突破確認價)]
        target1 = min(candidates) if candidates else current + max(atr_eff * 2.5, current * 0.06)
        target1 = ceil_to_tick(target1, tick)
        target_role = "第一停利區"
        target_source = "最近壓力與 ATR 延伸中距現價最近的合理價位"
        target2 = max(float(compass.get("target2", 0) or 0), target1 + max(atr_eff * 2, current * 0.06))

    risk_pct = (current - protective_stop) / current * 100 if current > protective_stop > 0 else None
    reward_pct = (target1 - current) / current * 100 if target1 > current > 0 else None
    rr = reward_pct / risk_pct if risk_pct and reward_pct is not None and risk_pct > 0 else None
    return {
        "current": current, "進場區": 進場區_center, "進場區_low": 進場區_low, "進場區_high": 進場區_high,
        "突破確認價": round(突破確認價, 2), "突破確認價_source": 突破確認價_source,
        "protective_stop": protective_stop, "structure_stop": structure_stop, "invalidation": structure_stop,
        "target1": round(target1, 2), "target2": round(target2, 2), "target_role": target_role,
        "risk_pct": risk_pct, "reward_pct": reward_pct, "rr": rr,
        "sources": {
            "突破確認價": f"唯一確認事件：{突破確認價_source}",
            "protective_stop": "MA60 與 ATR 防線中較接近現價者；跌破後執行減碼",
            "structure_stop": "結構低點／原結構價；跌破後退出剩餘波段部位",
            "target1": target_source,
            "進場區": "MA20 中心加入 ATR 緩衝；僅供偏多狀態評估，不等於買進命令",
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
        ("成本沒有進入 大盤環境判斷", "pnl_pct" not in market and "user_cost" not in market),
        ("失效價低於現價", lv["invalidation"] < lv["current"] if lv["current"]>0 else False),
        ("第一目標高於現價", lv["target1"] > lv["current"] if lv["current"]>0 else False),
        ("決策流程與全站共用同一價格引擎", True),
    ]
    return {"passed":sum(ok for _,ok in checks),"total":len(checks),"checks":checks,"ok":all(ok for _,ok in checks)}


def build_decision_snapshot(res: dict, compass: dict, committee: dict, user_holding: bool, user_cost: float) -> dict:
    """V3 單一決策快照：所有畫面只能讀這一份結果。"""
    # 先由 大盤環境判斷 判斷方向；成本完全不進入市場評分。
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
    unified_compass.update({"進場區":levels["進場區"], "stop":levels["structure_stop"], "target1":levels["target1"], "target2":levels["target2"], "rr":levels["rr"]})
    market.update({"進場區":levels["進場區"], "stop":levels["structure_stop"], "target1":levels["target1"], "rr":levels["rr"]})

    # 正式趨勢不再使用 Streamlit 工作階段記憶覆寫。
    # 跨日穩定性由最近 120 個交易日日線重新推演的 Strategy State Machine 負責，
    # 因此重新部署、換裝置或重新整理後仍可得到一致的正式趨勢。
    stability = {
        "stable_status": market["status"],
        "raw_status": market["status"],
        "note": "正式趨勢由歷史日線狀態機管理；工作階段記憶不參與方向判斷。",
    }

    agreement = build_signal_agreement(market, regime)
    reliability = int(round(float(res.get("data_quality_score", 0) or 0)))
    holding_value = build_holding_value_analysis(res, market, regime, levels, user_holding, user_cost)
    portfolio = build_today_action_board(res, unified_compass, market, user_holding, user_cost, levels, holding_value)

    comps = market.get("components", {}) or {}
    bull_score = int(round(sum(float(comps.get(k, 50) or 50) for k in ["trend","chips","momentum","price_position"]) / 4))
    bear_score = int(round(100 - (float(comps.get("risk", 50) or 50) + float(regime.get("score", 50) or 50)) / 2))
    validation = build_historical_signal_validation(res)
    chip_engine = build_chip_engine(res)
    volume_engine = build_volume_engine(res)
    snapshot = {
        "levels":levels, "market":market, "portfolio":portfolio, "regime":regime, "holding_value":holding_value,
        "agreement":agreement, "data_reliability":reliability, "stability":stability,
        "validation":validation, "headline":portfolio.get("headline"), "color":portfolio.get("color"),
        "compass":unified_compass, "bull_score":max(0,min(100,bull_score)),
        "bear_score":max(0,min(100,bear_score)), "chip_engine":chip_engine, "volume_engine":volume_engine,
    }
    snapshot["edge_engine"] = build_edge_engine(snapshot, user_holding)
    snapshot["audit"] = build_consistency_audit(snapshot)
    return snapshot




def _daily_trend_score(row: pd.Series) -> int:
    """只使用日線可重建資料計算原始趨勢分數；不使用持股成本或盤中雜訊。"""
    close = safe_float(row.get("close"), 0)
    ma20 = safe_float(row.get("MA20"), 0)
    ma60 = safe_float(row.get("MA60"), 0)
    slope20 = safe_float(row.get("MA20_SLOPE"), 0)
    slope60 = safe_float(row.get("MA60_SLOPE"), 0)
    macd_hist = safe_float(row.get("MACD_HIST"), 0)
    rsi = safe_float(row.get("RSI14"), 50)
    plus_di = safe_float(row.get("PLUS_DI"), 0)
    minus_di = safe_float(row.get("MINUS_DI"), 0)
    adx = safe_float(row.get("ADX14"), 0)
    score = 0
    score += 22 if ma20 > 0 and close >= ma20 else 0
    score += 18 if ma20 > 0 and ma60 > 0 and ma20 >= ma60 else 0
    score += 15 if slope20 > 0 else 0
    score += 15 if slope60 > 0 else 0
    score += 10 if macd_hist >= 0 else 0
    score += 8 if rsi >= 50 else 0
    score += 7 if plus_di >= minus_di else 0
    score += 5 if adx >= 18 else 0
    return int(max(0, min(100, score)))


def _score_bucket(score: int) -> str:
    if score >= 78: return "STRONG_BULL"
    if score >= 62: return "BULL_PULLBACK"
    if score >= 45: return "RANGE"
    if score >= 30: return "BEAR_RALLY"
    return "BEAR"


def _state_label(state: str) -> str:
    return {
        "STRONG_BULL":"強勢多頭",
        "BULL_PULLBACK":"多頭整理",
        "RANGE":"區間整理",
        "BEAR_RALLY":"空頭反彈",
        "BEAR":"弱勢空頭",
    }.get(state, "區間整理")



def _institution_window_stats(df: pd.DataFrame, col: str, window: int) -> dict:
    if df is None or df.empty or col not in df.columns:
        return {"sum":0.0,"buy_days":0,"sell_days":0,"days":0,"streak":0,"direction":"資料不足"}
    x=df.copy().sort_values("date")
    vals=pd.to_numeric(x[col], errors="coerce").fillna(0).tail(window)
    if vals.empty:
        return {"sum":0.0,"buy_days":0,"sell_days":0,"days":0,"streak":0,"direction":"資料不足"}
    streak=0
    last_sign=1 if vals.iloc[-1]>0 else -1 if vals.iloc[-1]<0 else 0
    if last_sign:
        for v in reversed(vals.tolist()):
            sign=1 if v>0 else -1 if v<0 else 0
            if sign==last_sign: streak+=1
            else: break
    total=float(vals.sum())
    buy_days=int((vals>0).sum()); sell_days=int((vals<0).sum())
    direction="偏買" if total>0 and buy_days>sell_days else "偏賣" if total<0 and sell_days>buy_days else "交錯"
    return {"sum":total,"buy_days":buy_days,"sell_days":sell_days,"days":len(vals),"streak":streak*last_sign,"direction":direction}


def build_chip_engine(res: dict) -> dict:
    """三大法人籌碼引擎。只使用實際買賣超資料，不讀持有成本。"""
    df=res.get("institutional_df", pd.DataFrame())
    daily=res.get("daily_df", pd.DataFrame())
    if df is None or df.empty:
        return {"score":50,"state":"資料不足","quality":"LOW","foreign":{},"trust":{},"dealer":{},
                "positive":[],"negative":["三大法人日資料不足，籌碼不納入方向加減分。"],
                "warning_points":0,"veto":None,"rows":[]}
    avg_lots=1.0
    if daily is not None and not daily.empty and "vol" in daily.columns:
        avg_lots=max(float(pd.to_numeric(daily["vol"],errors="coerce").tail(20).mean())/1000.0,1.0)
    mapping=[("外資(張)","外資",0.45),("投信(張)","投信",0.35),("自營商總計(張)","自營商",0.20)]
    score=50.0; positive=[]; negative=[]; rows=[]; warning_points=0
    details={}
    for col,label,weight in mapping:
        stats={w:_institution_window_stats(df,col,w) for w in (5,10,20)}
        details[label]=stats
        s5,s10,s20=stats[5],stats[10],stats[20]
        intensity=s20["sum"]/avg_lots
        component=50.0
        component += max(-24,min(24,intensity*18))
        component += 8 if s20["buy_days"]>=13 else -8 if s20["sell_days"]>=13 else 0
        component += 6 if s5["sum"]>0 and s10["sum"]>0 else -6 if s5["sum"]<0 and s10["sum"]<0 else 0
        component += 4 if s5["streak"]>=3 else -4 if s5["streak"]<=-3 else 0
        component=max(0,min(100,component))
        score += (component-50)*weight
        if component>=62:
            positive.append(f"{label}近20日 {s20['sum']:+,.0f} 張，近5／10日方向一致偏買。")
        elif component<=38:
            negative.append(f"{label}近20日 {s20['sum']:+,.0f} 張，近5／10日方向一致偏賣。")
            warning_points += 3 if label in ["外資","投信"] else 1
        elif component<48:
            negative.append(f"{label}籌碼略偏弱，近20日 {s20['sum']:+,.0f} 張。")
            warning_points += 1
        rows.append({"法人":label,"5日累計(張)":s5["sum"],"10日累計(張)":s10["sum"],"20日累計(張)":s20["sum"],
                     "20日買超天數":s20["buy_days"],"20日賣超天數":s20["sell_days"],"連續方向天數":s5["streak"],"分數":round(component)})
    score=int(round(max(0,min(100,score))))
    state="明顯偏多" if score>=70 else "稍偏多" if score>=58 else "分歧" if score>=43 else "稍偏空" if score>=30 else "明顯偏空"
    veto=None
    foreign20=details.get("外資",{}).get(20,{})
    trust20=details.get("投信",{}).get(20,{})
    if score<=25 and foreign20.get("sell_days",0)>=14 and trust20.get("sum",0)<0:
        veto="外資長期偏賣且投信同步轉賣，禁止把單日反彈視為籌碼翻多。"
    return {"score":score,"state":state,"quality":"HIGH","foreign":details.get("外資",{}),"trust":details.get("投信",{}),
            "dealer":details.get("自營商",{}),"positive":positive[:4],"negative":negative[:4],
            "warning_points":warning_points,"veto":veto,"rows":rows}


def build_volume_engine(res: dict) -> dict:
    """價量品質引擎。盤中成交量無效時，只用已完成日線，不以 0 量誤判。"""
    df=res.get("daily_df")
    if df is None or df.empty or len(df)<25:
        return {"score":50,"state":"資料不足","quality":"LOW","positive":[],"negative":["日線成交量不足。"],"warning_points":0,"veto":None}
    x=df.copy().sort_values("date")
    close=pd.to_numeric(x["close"],errors="coerce")
    vol=pd.to_numeric(x["vol"],errors="coerce")
    ma20=vol.rolling(20).mean()
    last_ret=float(close.pct_change().iloc[-1] or 0)
    ret5=float(close.pct_change(5).iloc[-1] or 0)
    vr=float(vol.iloc[-1]/ma20.iloc[-1]) if ma20.iloc[-1]>0 else 1.0
    upvol=float(vol.where(close>close.shift(1),0).tail(20).sum())
    downvol=float(vol.where(close<close.shift(1),0).tail(20).sum())
    pressure_ratio=upvol/max(downvol,1.0)
    score=50.0; positive=[]; negative=[]; warning_points=0; veto=None
    if last_ret>0 and vr>=1.3:
        state="放量上漲"; score+=20; positive.append(f"最近完成日上漲且量比 {vr:.2f}，上漲有量能支持。")
    elif last_ret<0 and vr>=1.3:
        state="放量下跌"; score-=25; negative.append(f"最近完成日下跌且量比 {vr:.2f}，賣壓明顯放大。")
        warning_points+=3
        if vr>=1.8 and ret5<0: veto="放量下跌且近5日報酬為負，禁止把反彈直接判定為轉強。"
    elif ret5<0 and float(vol.tail(5).mean()/ma20.iloc[-1])<=0.85:
        state="量縮整理"; score+=8; positive.append("近5日回檔量縮，賣壓未同步擴大。")
    elif ret5>0 and float(vol.tail(5).mean()/ma20.iloc[-1])<0.75:
        state="無量反彈"; score-=10; negative.append("近5日上漲但量能不足，反彈可信度偏低。")
        warning_points+=1
    else:
        state="量價中性"
    if pressure_ratio>=1.25:
        score+=10; positive.append(f"20日上漲日量／下跌日量為 {pressure_ratio:.2f}，買方量能占優。")
    elif pressure_ratio<=0.80:
        score-=12; negative.append(f"20日上漲日量／下跌日量為 {pressure_ratio:.2f}，賣方量能占優。")
        warning_points+=2
    score=int(round(max(0,min(100,score))))
    return {"score":score,"state":state,"quality":"HIGH","volume_ratio":vr,"pressure_ratio":pressure_ratio,
            "positive":positive[:3],"negative":negative[:3],"warning_points":warning_points,"veto":veto}


def build_edge_engine(snapshot: dict, user_holding: bool) -> dict:
    """用單一價格引擎計算剩餘報酬與風險；不預測，只比較目前策略的報酬風險。"""
    lv=snapshot.get("levels",{}) or {}
    current=safe_float(lv.get("current"),0)
    target=safe_float(lv.get("target1"),0)
    protective=safe_float(lv.get("protective_stop"),0)
    structure=safe_float(lv.get("structure_stop"),0)
    risk_line=protective if user_holding and 0<protective<current else structure
    upside=max(0,(target/current-1)*100) if current>0 and target>current else 0
    downside=max(0,(current/risk_line-1)*100) if current>0 and 0<risk_line<current else 0
    rr=upside/downside if downside>0 else None
    score=50
    if rr is not None:
        score=int(max(0,min(100,round(25+rr*22))))
    state="有優勢" if score>=70 else "普通" if score>=45 else "缺乏優勢"
    return {"score":score,"state":state,"upside_pct":upside,"downside_pct":downside,"rr":rr,"risk_line":risk_line,
            "note":"Edge 只比較既定目標與風險線，不代表股價一定到達目標。"}

def build_strategy_state_machine(res: dict, decision_snapshot: dict, user_holding: bool, user_cost: float) -> dict:
    """以歷史日線重建正式趨勢，再用目前大盤、籌碼與風險決定執行動作。

    正式趨勢不依賴本機資料庫，也不因單日小幅分數變化翻轉。
    一般升降級需連續兩日；重大結構破壞可立即降級。
    """
    df = res.get("daily_df")
    if df is None or df.empty:
        return {
            "state":"RANGE","state_label":"資料不足","state_days":0,"trend_score":50,
            "action":"等待","action_code":"WAIT","color":"#64748B",
            "positive_evidence":[],"negative_evidence":["日線資料不足，無法重建正式趨勢。"],
            "warnings":1,"warning_threshold":3,"changed":False,"change_note":"正式趨勢無法建立。",
            "trigger_rows":[],"today_change":"資料不足，正式策略不下方向結論。",
        }
    x = df.copy().sort_values("date").tail(120).reset_index(drop=True)
    x["_trend_score"] = x.apply(_daily_trend_score, axis=1)
    raw_states = [_score_bucket(int(v)) for v in x["_trend_score"]]
    order = ["BEAR","BEAR_RALLY","RANGE","BULL_PULLBACK","STRONG_BULL"]
    state = raw_states[0]
    up_count = down_count = 0
    states = []
    for i, (raw_state, score) in enumerate(zip(raw_states, x["_trend_score"])):
        current_idx = order.index(state); raw_idx = order.index(raw_state)
        row = x.iloc[i]
        close = safe_float(row.get("close"), 0); ma60 = safe_float(row.get("MA60"), 0)
        slope60 = safe_float(row.get("MA60_SLOPE"), 0)
        structural_break = ma60 > 0 and close < ma60 and slope60 < 0 and score < 35
        if structural_break:
            state = "BEAR" if score < 25 else "BEAR_RALLY"
            up_count = down_count = 0
        elif raw_idx > current_idx:
            up_count += 1; down_count = 0
            if up_count >= 2:
                state = order[min(current_idx + 1, len(order)-1)]
                up_count = 0
        elif raw_idx < current_idx:
            down_count += 1; up_count = 0
            if down_count >= 2:
                state = order[max(current_idx - 1, 0)]
                down_count = 0
        else:
            up_count = max(0, up_count-1); down_count = max(0, down_count-1)
        states.append(state)
    current_state = states[-1]
    state_days = 1
    for s in reversed(states[:-1]):
        if s == current_state: state_days += 1
        else: break
    changed = len(states) >= 2 and states[-1] != states[-2]
    trend_score = int(x["_trend_score"].iloc[-1])

    market = decision_snapshot.get("market", {}) or {}
    regime = decision_snapshot.get("regime", {}) or {}
    levels = decision_snapshot.get("levels", {}) or {}
    holding_value = decision_snapshot.get("holding_value", {}) or {}
    chip_engine = decision_snapshot.get("chip_engine", {}) or {}
    volume_engine = decision_snapshot.get("volume_engine", {}) or {}
    edge_engine = decision_snapshot.get("edge_engine", {}) or {}
    ta = res.get("trend_analysis", {}) or {}
    inst = res.get("institutional_summary", {}) or {}
    current = safe_float(res.get("current_price"), 0)
    protective = safe_float(levels.get("protective_stop"), 0)
    structure_stop = safe_float(levels.get("structure_stop"), 0)
    突破確認價 = safe_float(levels.get("突破確認價"), 0)
    target = safe_float(levels.get("target1"), 0)

    positive, negative = [], []
    if current_state in ["STRONG_BULL","BULL_PULLBACK"]:
        positive.append(f"正式趨勢為{_state_label(current_state)}，已由歷史日線連續確認 {state_days} 個交易日。")
    else:
        negative.append(f"正式趨勢為{_state_label(current_state)}，尚未形成可積極承擔風險的上升結構。")
    if safe_float(ta.get("slope20"), 0) > 0: positive.append("MA20 斜率仍向上。")
    else: negative.append("MA20 斜率未向上。")
    if safe_float(ta.get("slope60"), 0) > 0: positive.append("MA60 斜率仍向上。")
    else: negative.append("MA60 斜率未向上。")
    consensus = str(chip_engine.get("state", inst.get("consensus_label", "資料不足")))
    for item in chip_engine.get("positive", []): positive.append(item)
    for item in chip_engine.get("negative", []): negative.append(item)
    if chip_engine.get("veto"): negative.append("籌碼否決：" + str(chip_engine.get("veto")))
    gate = str(regime.get("gate", "CAUTION"))
    if gate == "OPEN": positive.append(f"大盤環境為{regime.get('state','正常')}，允許順勢操作。")
    elif gate in ["NO_NEW_BUY","RISK_OFF","PANIC"]: negative.append(f"大盤風險閘門為 {gate}，限制新增或要求優先風控。")
    else: negative.append(f"大盤環境為{regime.get('state','保守')}，目前只允許保守操作。")
    for item in volume_engine.get("positive", []): positive.append(item)
    for item in volume_engine.get("negative", []): negative.append(item)
    if volume_engine.get("veto"): negative.append("量價否決：" + str(volume_engine.get("veto")))

    warning_points = 0
    warning_points += 2 if safe_float(ta.get("slope20"), 0) <= 0 else 0
    warning_points += int(chip_engine.get("warning_points", 0) or 0)
    warning_points += int(volume_engine.get("warning_points", 0) or 0)
    warning_points += 4 if gate in ["RISK_OFF","PANIC"] else 2 if gate == "NO_NEW_BUY" else 0
    warning_points += 2 if edge_engine.get("state") == "缺乏優勢" else 0
    warning_threshold = 6
    warnings = warning_points

    # 決策優先順序：結構風險 > 大盤閘門 > 正式趨勢 > 籌碼量能 > 價格位置。
    if structure_stop > 0 and current <= structure_stop:
        action_code, action, color = "EXIT", "退出", "#DC2626"
    elif protective > 0 and current <= protective and user_holding:
        action_code, action, color = "REDUCE", "部分減碼", "#F97316"
    elif gate in ["PANIC","RISK_OFF"]:
        action_code, action, color = ("REDUCE","部分減碼","#F97316") if user_holding else ("WAIT","等待","#64748B")
    elif current_state == "BEAR":
        action_code, action, color = ("EXIT","退出","#DC2626") if user_holding else ("WAIT","等待","#64748B")
    elif current_state == "BEAR_RALLY":
        action_code, action, color = ("REDUCE","反彈減碼","#F97316") if user_holding else ("WAIT","等待","#64748B")
    elif current_state == "RANGE":
        if user_holding and holding_value.get("grade") == "不值得":
            action_code, action, color = "REDUCE", "部分減碼", "#F97316"
        else:
            action_code, action, color = ("HOLD_NO_ADD","續抱不加碼","#D97706") if user_holding else ("WAIT","等待","#64748B")
    elif current_state == "BULL_PULLBACK":
        if user_holding:
            action_code, action, color = "HOLD_NO_ADD", "續抱不加碼", "#2563EB"
        elif gate == "OPEN" and market.get("market_score",0) >= 60:
            action_code, action, color = "ESTABLISH", "建立第一筆", "#16A34A"
        else:
            action_code, action, color = "WAIT", "等待", "#64748B"
    else:  # STRONG_BULL
        if user_holding:
            action_code, action, color = "HOLD", "續抱", "#16A34A"
        elif gate == "OPEN":
            action_code, action, color = "ESTABLISH", "建立第一筆", "#16A34A"
        else:
            action_code, action, color = "WAIT", "等待", "#64748B"

    # 多項警訊可讓執行降一級，但不直接竄改正式趨勢。
    if warnings >= warning_threshold and action_code == "HOLD":
        action_code, action, color = "HOLD_NO_ADD", "續抱不加碼", "#D97706"
    elif warnings >= warning_threshold and action_code == "HOLD_NO_ADD" and user_holding and (holding_value.get("grade") == "不值得" or edge_engine.get("state") == "缺乏優勢"):
        action_code, action, color = "REDUCE", "部分減碼", "#F97316"

    trigger_rows = []
    if 突破確認價 > 0:
        trigger_rows.append({"condition":f"收盤站上 {突破確認價:.2f} 元並維持", "effect":"解除警訊／評估升級策略"})
    if protective > 0:
        trigger_rows.append({"condition":f"收盤跌破 {protective:.2f} 元", "effect":"部分減碼或提高現金部位"})
    if structure_stop > 0:
        trigger_rows.append({"condition":f"收盤跌破 {structure_stop:.2f} 元", "effect":"正式趨勢失效，退出剩餘波段部位"})
    if target > current and current_state in ["STRONG_BULL","BULL_PULLBACK"]:
        trigger_rows.append({"condition":f"接近第一目標 {target:.2f} 元", "effect":"分批停利，不一次全部賣出"})

    if changed:
        change_note = f"正式趨勢已由 {_state_label(states[-2])} 變更為 {_state_label(current_state)}。"
    else:
        change_note = f"正式趨勢維持 {_state_label(current_state)}，沒有因單日變化改口。"
    today_change = f"目前累積風險證據 {warnings}/{warning_threshold}；" + ("已達執行降級門檻。" if warnings >= warning_threshold else "尚不足以單獨改變正式趨勢。")
    return {
        "state":current_state,"state_label":_state_label(current_state),"state_days":state_days,
        "trend_score":trend_score,"action":action,"action_code":action_code,"color":color,
        "positive_evidence":positive[:6],"negative_evidence":negative[:6],
        "warnings":warnings,"warning_threshold":warning_threshold,"changed":changed,
        "change_note":change_note,"today_change":today_change,"trigger_rows":trigger_rows,
        "method_note":"正式趨勢由最近120個交易日日線重新推演；一般升降級需連續兩日，重大結構破壞可立即降級。",
        "current":current,"突破確認價":突破確認價,"protective_stop":protective,"structure_stop":structure_stop,"target":target,
    }




def build_strategy_stability_validation(res: dict) -> dict:
    """驗證狀態機是否降低跨日反覆翻轉。

    只使用已完成日線；比較每日原始分數分桶與「連續兩日確認、重大結構破壞立即降級」後的正式狀態。
    此結果是模型穩定度驗證，不是報酬回測。
    """
    df = res.get("daily_df")
    if df is None or df.empty or len(df) < 65:
        return {
            "available": False,
            "note": "至少需要 65 個完成交易日才能驗證策略穩定度。",
        }
    x = df.copy().sort_values("date").tail(120).reset_index(drop=True)
    needed = ["MA20", "MA60", "MA20_SLOPE", "MA60_SLOPE", "MACD_HIST", "RSI14", "PLUS_DI", "MINUS_DI", "ADX14"]
    for col in needed:
        if col not in x.columns:
            x[col] = 0.0
    x["_trend_score"] = x.apply(_daily_trend_score, axis=1)
    raw_states = [_score_bucket(int(v)) for v in x["_trend_score"].tolist()]

    order = ["BEAR", "BEAR_RALLY", "RANGE", "BULL_PULLBACK", "STRONG_BULL"]
    state = raw_states[0]
    up_count = 0
    down_count = 0
    confirmed = [state]
    for i in range(1, len(x)):
        candidate = raw_states[i]
        close = safe_float(x.iloc[i].get("close"), 0)
        ma20 = safe_float(x.iloc[i].get("MA20"), 0)
        ma60 = safe_float(x.iloc[i].get("MA60"), 0)
        # 重大破壞：收盤同時跌破 MA20 與 MA60，正式狀態可立即降至弱勢空頭。
        structural_break = ma20 > 0 and ma60 > 0 and close < ma20 and close < ma60
        if structural_break:
            state = "BEAR"
            up_count = down_count = 0
        else:
            cur_i = order.index(state)
            cand_i = order.index(candidate)
            if cand_i > cur_i:
                up_count += 1
                down_count = 0
                if up_count >= 2:
                    state = order[min(cur_i + 1, len(order) - 1)]
                    up_count = 0
            elif cand_i < cur_i:
                down_count += 1
                up_count = 0
                if down_count >= 2:
                    state = order[max(cur_i - 1, 0)]
                    down_count = 0
            else:
                up_count = max(0, up_count - 1)
                down_count = max(0, down_count - 1)
        confirmed.append(state)

    def flip_count(states):
        return sum(1 for a, b in zip(states, states[1:]) if a != b)

    def one_day_reversals(states):
        # A→B→A，代表只維持一天便反轉。
        return sum(1 for i in range(1, len(states)-1) if states[i-1] == states[i+1] and states[i] != states[i-1])

    runs = []
    start = 0
    for i in range(1, len(confirmed)+1):
        if i == len(confirmed) or confirmed[i] != confirmed[start]:
            runs.append(i-start)
            start = i
    raw_flips = flip_count(raw_states)
    confirmed_flips = flip_count(confirmed)
    reduction = (1 - confirmed_flips / raw_flips) * 100 if raw_flips > 0 else 0.0
    dates = pd.to_datetime(x.get("date"), errors="coerce")
    last_change_idx = 0
    for i in range(1, len(confirmed)):
        if confirmed[i] != confirmed[i-1]:
            last_change_idx = i
    last_change_date = dates.iloc[last_change_idx].strftime("%Y-%m-%d") if not pd.isna(dates.iloc[last_change_idx]) else "—"
    return {
        "available": True,
        "sample_days": len(x),
        "raw_flips": raw_flips,
        "confirmed_flips": confirmed_flips,
        "flip_reduction_pct": round(reduction, 1),
        "raw_one_day_reversals": one_day_reversals(raw_states),
        "confirmed_one_day_reversals": one_day_reversals(confirmed),
        "average_state_days": round(sum(runs) / len(runs), 1) if runs else 0.0,
        "median_state_days": round(float(pd.Series(runs).median()), 1) if runs else 0.0,
        "current_state": confirmed[-1],
        "current_state_label": _state_label(confirmed[-1]),
        "last_change_date": last_change_date,
        "note": "只驗證正式趨勢的穩定性，不代表買賣績效；狀態機一般變更需連續兩日，重大結構破壞可立即降級。",
    }


def build_strategy_outcome_validation(res: dict) -> dict:
    """以正式趨勢狀態驗證後續 5／20 日表現。

    這是狀態辨識驗證，不模擬實際下單、部位大小、滑價或交易成本。
    目的在檢查多頭狀態後續是否相對較強、空頭狀態後續是否相對較弱。
    """
    df = res.get("daily_df")
    if df is None or df.empty or len(df) < 90:
        return {"available": False, "note": "至少需要 90 個完成交易日才能驗證趨勢狀態後續表現。"}
    x = df.copy().sort_values("date").tail(260).reset_index(drop=True)
    needed = ["MA20", "MA60", "MA20_SLOPE", "MA60_SLOPE", "MACD_HIST", "RSI14", "PLUS_DI", "MINUS_DI", "ADX14"]
    for col in needed:
        if col not in x.columns:
            x[col] = 0.0
    x["_trend_score"] = x.apply(_daily_trend_score, axis=1)
    raw_states = [_score_bucket(int(v)) for v in x["_trend_score"].tolist()]
    order = ["BEAR", "BEAR_RALLY", "RANGE", "BULL_PULLBACK", "STRONG_BULL"]
    state = raw_states[0]
    up_count = down_count = 0
    confirmed = []
    for i, candidate in enumerate(raw_states):
        row = x.iloc[i]
        close = safe_float(row.get("close"), 0)
        ma20 = safe_float(row.get("MA20"), 0)
        ma60 = safe_float(row.get("MA60"), 0)
        score = safe_float(row.get("_trend_score"), 50)
        structural_break = ma20 > 0 and ma60 > 0 and close < ma20 and close < ma60 and score < 35
        if structural_break:
            state = "BEAR" if score < 25 else "BEAR_RALLY"
            up_count = down_count = 0
        else:
            cur_i = order.index(state); cand_i = order.index(candidate)
            if cand_i > cur_i:
                up_count += 1; down_count = 0
                if up_count >= 2:
                    state = order[min(cur_i + 1, len(order)-1)]
                    up_count = 0
            elif cand_i < cur_i:
                down_count += 1; up_count = 0
                if down_count >= 2:
                    state = order[max(cur_i - 1, 0)]
                    down_count = 0
            else:
                up_count = max(0, up_count-1); down_count = max(0, down_count-1)
        confirmed.append(state)
    x["_state"] = confirmed
    close = pd.to_numeric(x["close"], errors="coerce")
    x["_ret5"] = (close.shift(-5) / close - 1) * 100
    x["_ret20"] = (close.shift(-20) / close - 1) * 100
    # 未來 20 日最大不利走勢：從訊號日收盤起算。
    future_adverse = []
    for i in range(len(x)):
        base = safe_float(close.iloc[i], 0)
        if base <= 0 or i + 1 >= len(x):
            future_adverse.append(float('nan')); continue
        window = pd.to_numeric(x.loc[i+1:min(i+20, len(x)-1), "close"], errors="coerce").dropna()
        future_adverse.append(((window.min()/base)-1)*100 if not window.empty else float('nan'))
    x["_mae20"] = future_adverse
    labels = {s: _state_label(s) for s in order}
    rows=[]
    for s in order:
        g=x[x["_state"]==s]
        r5=g["_ret5"].dropna(); r20=g["_ret20"].dropna(); mae=g["_mae20"].dropna()
        if len(r5)==0 and len(r20)==0:
            continue
        rows.append({
            "正式趨勢": labels[s],
            "樣本數": int(max(len(r5), len(r20))),
            "5日勝率": round(float((r5>0).mean()*100),1) if len(r5) else None,
            "5日平均報酬": round(float(r5.mean()),2) if len(r5) else None,
            "20日勝率": round(float((r20>0).mean()*100),1) if len(r20) else None,
            "20日平均報酬": round(float(r20.mean()),2) if len(r20) else None,
            "20日平均最大不利": round(float(mae.mean()),2) if len(mae) else None,
        })
    if not rows:
        return {"available": False, "note": "目前沒有足夠的可用樣本。"}
    # 基本方向檢查：多頭狀態 20 日平均應高於空頭狀態。
    rowmap={r["正式趨勢"]:r for r in rows}
    bull_vals=[r["20日平均報酬"] for r in rows if r["正式趨勢"] in ["強勢多頭","多頭整理"] and r["20日平均報酬"] is not None]
    bear_vals=[r["20日平均報酬"] for r in rows if r["正式趨勢"] in ["弱勢空頭","空頭反彈"] and r["20日平均報酬"] is not None]
    separation = (sum(bull_vals)/len(bull_vals) - sum(bear_vals)/len(bear_vals)) if bull_vals and bear_vals else None
    return {
        "available": True,
        "sample_days": len(x),
        "rows": rows,
        "separation_pct": round(float(separation),2) if separation is not None else None,
        "direction_ok": bool(separation is not None and separation > 0),
        "note": "只驗證正式趨勢狀態後續表現，不代表實際交易績效；未計手續費、交易稅、滑價與部位管理。",
    }

def build_strategy_consistency_audit(snapshot: dict, strategy: dict, user_holding: bool) -> dict:
    """策略完成後的最終稽核。只檢查一致性，不重新產生另一套決策。"""
    lv = snapshot.get("levels", {}) or {}
    regime = snapshot.get("regime", {}) or {}
    chip = snapshot.get("chip_engine", {}) or {}
    volume = snapshot.get("volume_engine", {}) or {}
    current = safe_float(lv.get("current"), 0)
    confirm = safe_float(lv.get("突破確認價"), 0)
    protective = safe_float(lv.get("protective_stop"), 0)
    structural = safe_float(lv.get("structure_stop"), 0)
    target = safe_float(lv.get("target1"), 0)
    action = str(strategy.get("action_code", "WAIT"))
    state = str(strategy.get("state", "RANGE"))
    gate = str(regime.get("gate", "CAUTION"))

    checks = []
    checks.append(("價格順序合理", current > 0 and structural < protective < current < target))
    checks.append(("確認價為有效正數", confirm > 0))
    checks.append(("未持股不會收到續抱或減碼指令", user_holding or action not in {"HOLD", "HOLD_NO_ADD", "REDUCE", "EXIT"}))
    checks.append(("弱勢空頭不會建立新部位", not (state == "BEAR" and action == "ESTABLISH")))
    checks.append(("大盤風險關閉不會建立新部位", not (gate in {"PANIC", "RISK_OFF", "NO_NEW_BUY"} and action == "ESTABLISH")))
    checks.append(("結構跌破不會續抱", not (current <= structural and action in {"HOLD", "HOLD_NO_ADD", "ESTABLISH"})))
    checks.append(("籌碼否決不會積極建立", not (chip.get("veto") and action == "ESTABLISH")))
    checks.append(("量價否決不會積極建立", not (volume.get("veto") and action == "ESTABLISH")))
    checks.append(("正式趨勢與動作使用同一快照", snapshot.get("strategy") is strategy))
    checks.append(("工作階段記憶未覆寫正式趨勢", "歷史日線" in str(snapshot.get("stability", {}).get("note", ""))))

    failed = [name for name, ok in checks if not ok]
    return {
        "passed": sum(1 for _, ok in checks if ok),
        "total": len(checks),
        "ok": not failed,
        "failed": failed,
        "checks": checks,
    }

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
        ("確認價", lv.get("突破確認價")),
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
    hv = snapshot.get("holding_value", {}) or {}
    if user_holding and hv.get("available"):
        rr_text = f"風險報酬比 {hv['rr']:.2f}" if hv.get("rr") is not None else "風險報酬比資料不足"
        parts.append(f"持股價值評為「{hv.get('grade','—')}」：上漲空間 {hv.get('upside_pct',0):.1f}%、下跌風險 {hv.get('downside_pct',0):.1f}%、{rr_text}。")
    parts.append(f"收盤站上 {lv['突破確認價']:.2f} 元，才視為確認成功。")
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
        {"price": lv["突破確認價"], "condition": "收盤站上", "yes": success, "no": "檢查下一道保護價"},
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
# Beta v2：資金池不再作為個股進出判斷依據，後端僅保留相容預設值。
capital = 100.0
risk_pct = 1.0

# Beta v8.5：自選股票
# 以網址參數保存代碼，重新整理或沿用同一網址開啟時不必重新輸入。
def _normalize_stock_code(value):
    value = str(value or "").strip().upper()
    value = value.replace(".TW", "").replace(".TWO", "")
    return value

def _read_watchlist_from_query():
    try:
        raw = st.query_params.get("wl", "")
    except Exception:
        raw = ""
    if isinstance(raw, list):
        raw = raw[-1] if raw else ""
    items = []
    for item in str(raw or "").split(","):
        code = _normalize_stock_code(item)
        if code and code not in items:
            items.append(code)
    return items[:30]

def _save_watchlist_to_query(items):
    clean = []
    for item in items:
        code = _normalize_stock_code(item)
        if code and code not in clean:
            clean.append(code)
    try:
        if clean:
            st.query_params["wl"] = ",".join(clean)
        elif "wl" in st.query_params:
            del st.query_params["wl"]
    except Exception:
        pass
    return clean

if "_watchlist_codes" not in st.session_state:
    st.session_state["_watchlist_codes"] = _read_watchlist_from_query()

if "_watchlist_names" not in st.session_state:
    st.session_state["_watchlist_names"] = {}

if "_stock_input_widget" not in st.session_state:
    try:
        _initial_stock = _normalize_stock_code(st.query_params.get("stock", ""))
    except Exception:
        _initial_stock = ""
    if not _initial_stock:
        _initial_stock = (
            st.session_state["_watchlist_codes"][0]
            if st.session_state["_watchlist_codes"]
            else "3037"
        )
    st.session_state["_stock_input_widget"] = _initial_stock

with st.sidebar:
    st.header("⚙️ 操作設定")
    slip_input = st.slider("預估滑價 (Ticks)", 0, 5, 1)
    sector_panic_toggle = st.checkbox("🔥 同族群龍頭同步重挫", value=False)
    auto_refresh = st.checkbox("🔄 盤中每 15 秒更新報價", value=False)
    show_evidence_default = False
    debug_mode = False

    st.divider()
    st.subheader("⭐ 自選股票")

    _current_sidebar_code = _normalize_stock_code(
        st.session_state.get("_stock_input_widget", "")
    )
    _already_saved = _current_sidebar_code in st.session_state["_watchlist_codes"]

    if st.button(
        "✅ 已在自選" if _already_saved else "➕ 將目前個股加入自選",
        disabled=(not _current_sidebar_code) or _already_saved,
        use_container_width=True,
        key="_watch_add_current",
    ):
        if _current_sidebar_code:
            _new_list = list(st.session_state["_watchlist_codes"])
            if _current_sidebar_code not in _new_list:
                _new_list.append(_current_sidebar_code)
            st.session_state["_watchlist_codes"] = _save_watchlist_to_query(_new_list)
            st.rerun()

    if not st.session_state["_watchlist_codes"]:
        st.caption("尚未加入自選股票。")
    else:
        for _wl_code in list(st.session_state["_watchlist_codes"]):
            _wl_name = st.session_state["_watchlist_names"].get(_wl_code, "")
            _wl_label = f"{_wl_code} {_wl_name}".strip()

            _wl_c1, _wl_c2 = st.columns([4, 1], gap="small")
            with _wl_c1:
                if st.button(
                    _wl_label,
                    key=f"_watch_select_{_wl_code}",
                    use_container_width=True,
                ):
                    st.session_state["_stock_input_widget"] = _wl_code
                    try:
                        st.query_params["stock"] = _wl_code
                    except Exception:
                        pass
                    st.rerun()

            with _wl_c2:
                if st.button("×", key=f"_watch_remove_{_wl_code}"):
                    _new_list = [
                        x for x in st.session_state["_watchlist_codes"]
                        if x != _wl_code
                    ]
                    st.session_state["_watchlist_codes"] = _save_watchlist_to_query(_new_list)
                    st.session_state["_watchlist_names"].pop(_wl_code, None)
                    st.rerun()

    st.caption("自選清單會寫入目前網址；建議把這個網址加入瀏覽器書籤。")

st.markdown("## 🧭 StockPilot Beta v9.17.1｜個股操作決策")
st.caption("直接回答：現在要不要進場、持有、加碼、減碼或退出。")
stock_input = st.text_input(
    "請輸入核心目標個股代碼：",
    key="_stock_input_widget",
).strip()
stock_input = _normalize_stock_code(stock_input)

try:
    if stock_input:
        st.query_params["stock"] = stock_input
except Exception:
    pass

u_col1, u_col2 = st.columns(2)
with u_col1:
    user_holding = st.checkbox("📊 我手中「已持有」此個股", value=False)
with u_col2:
    user_cost = st.number_input(
        "每股真實持股成本 (元)",
        value=0.0,
        step=1.0,
        min_value=0.0,
        disabled=not user_holding,
    )
user_shares = st.number_input(
    "目前持有股數",
    value=0,
    step=100,
    min_value=0,
    disabled=not user_holding,
    help="用於計算目前部位風險與可否再加碼；若未持有可維持 0。",
)

if stock_input:
    res = evaluate_stock(stock_input, capital, risk_pct, slip_input, is_holding=user_holding, 進場區_cost=user_cost, sector_panic=sector_panic_toggle)
    if res is None:
        st.error("無法取得這檔股票的日線資料。程式已依序嘗試 Yahoo 上市、Yahoo 上櫃與 FinMind；請確認代碼，或稍後再重新整理。")
        st.caption(f"本次查詢代碼：{stock_input}。3274 為上櫃股，程式會優先查詢 3274.TWO。")
    else:
        _resolved_name = str(res.get("stock_name", "") or "").strip()
        if _resolved_name:
            st.session_state["_watchlist_names"][stock_input] = _resolved_name

        bp_data = res["tactical_blueprint"]
        bp = bp_data["blueprint"]
        missing_text = "、".join(res["missing_data"]) if res["missing_data"] else "無"
        st.info(f"資料完整度：{res['data_quality_score']:.0f}%｜缺少：{missing_text}。資料不足的項目不納入方向判斷。")
        st.caption(f"資料更新時間：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}（台北時間）｜報價來源：{res.get('rt_source', res.get('quote_source', '依目前可用資料'))}")

        # 0. Project Compass 首頁：先回答該怎麼做，再展開證據
        compass = build_compass_home_summary(res, user_holding)
        committee_seed = build_ai_investment_committee(res, compass)
        decision_snapshot = build_decision_snapshot(res, compass, committee_seed, user_holding, user_cost)
        strategy_state = build_strategy_state_machine(res, decision_snapshot, user_holding, user_cost)
        decision_snapshot["strategy"] = strategy_state
        decision_snapshot["strategy_stability_validation"] = build_strategy_stability_validation(res)
        decision_snapshot["strategy_outcome_validation"] = build_strategy_outcome_validation(res)
        decision_snapshot["strategy_audit"] = build_strategy_consistency_audit(decision_snapshot, strategy_state, user_holding)

        # StockPilot 4.0 Shadow：只做平行比對，不改寫 3.3 正式策略。
        shadow_v4 = None
        shadow_v4_error = None

        if ENABLE_STOCKPILOT4_SHADOW:
            try:
                shadow_market_df = get_shadow_market_index_df(
                    res.get("market_type", "TSE")
                )
                shadow_margin_df = get_shadow_margin_df(res["stock_id"])

                _legacy_levels_shadow = dict(
                    decision_snapshot.get("levels", {}) or {}
                )
                _legacy_moving = float(
                    _legacy_levels_shadow.get("protective_stop", 0) or 0
                )
                _legacy_structural = float(
                    _legacy_levels_shadow.get("structure_stop", 0) or 0
                )

                # App 自己的 build_price_level_engine 已保證 structure_stop < protective_stop。
                # 再把同一組合法價位以 Shadow/PriceEngine 使用的命名一起傳入。
                if _legacy_moving > 0:
                    _legacy_levels_shadow["moving_protection"] = _legacy_moving
                if _legacy_structural > 0:
                    _legacy_levels_shadow["structural_exit"] = _legacy_structural

                # 最後一道防線：若來源異常，直接按台股 tick 校正。
                if (
                    _legacy_moving > 0
                    and _legacy_structural > 0
                    and _legacy_structural >= _legacy_moving
                ):
                    _lg_tick = tick_size(_legacy_moving)
                    _legacy_structural = floor_to_tick(
                        max(_lg_tick, _legacy_moving - _lg_tick),
                        _lg_tick
                    )
                    _legacy_levels_shadow["structure_stop"] = _legacy_structural
                    _legacy_levels_shadow["structural_exit"] = _legacy_structural

                shadow_payload = build_legacy_payload_from_app(
                    res,
                    market_index_df=shadow_market_df,
                    margin_df=shadow_margin_df,
                    legacy_levels=_legacy_levels_shadow,
                )

                # v8.9d：adapter 產生 payload 後再遞迴掃描一次真正送進 Price Engine 的物件。
                # 在 Price Engine 驗證前先正規化，確保 structural_exit < moving_protection。
                shadow_payload, _shadow_price_order_fixes = normalize_shadow_price_order(
                    shadow_payload
                )
                st.session_state["_stockpilot_shadow_price_order_fixes"] = (
                    _shadow_price_order_fixes
                )
                st.session_state["_stockpilot_shadow_price_order_input"] = {
                    "moving_protection": _legacy_levels_shadow.get("moving_protection"),
                    "structural_exit": _legacy_levels_shadow.get("structural_exit"),
                    "protective_stop": _legacy_levels_shadow.get("protective_stop"),
                    "structure_stop": _legacy_levels_shadow.get("structure_stop"),
                }

                # Sprint 19.1：未持股時，成本欄即使殘留舊值也不得送進 Shadow。
                # Decision Engine 對 non-holder 的 cost 必須是 0。
                _shadow_user_cost = float(user_cost or 0) if user_holding else 0.0

                # v9.17.1：Price Engine 某版本的 structural_exit / moving_protection
                # 驗證器會在「35.65 < 37.45」這種本來就正確的價格順序下仍誤拋錯誤。
                # 先照正確語意送入；若只遇到這個已知矛盾驗證錯誤，
                # 不讓整個最新操作模組失敗，改以 Shadow unavailable 降級處理。
                try:
                    shadow_v4 = ShadowIntegration().run(
                        shadow_payload,
                        is_holding=user_holding,
                        cost=_shadow_user_cost,
                        legacy_action=strategy_state.get("action"),
                    )
                except Exception as _shadow_exc_v911:
                    _shadow_msg_v911 = str(_shadow_exc_v911)
                    if (
                        "structural_exit must be lower than moving_protection"
                        in _shadow_msg_v911
                    ):
                        log_error("shadow_price_order_validator_v911", _shadow_exc_v911)
                        shadow_v4 = None
                        st.session_state["_stockpilot_shadow_price_order_warning"] = (
                            "Price Engine 價格層級驗證器發生已知矛盾；"
                            "本輪已停用 Shadow 輔助判斷，主決策仍照既有正式邏輯執行。"
                        )
                    else:
                        raise

                # v9.17.1：若 Shadow 因已知 validator bug 被停用，
                # 後續不得覆蓋正式決策；建立空結果代理僅供相容既有顯示流程。
                if shadow_v4 is None:
                    # v9.17.1：fallback 必須完整符合後續 Governance 讀取介面。
                    # v9.17.1 只補了 action 等欄位，但後續會直接讀 snapshot，
                    # 因此造成 AttributeError。這裡補齊 snapshot 與其 enum-like .value。
                    class _ShadowValueV912:
                        def __init__(self, value):
                            self.value = str(value or "neutral")

                    class _ShadowSnapshotFallbackV912:
                        def __init__(self):
                            _fallback_state = str(strategy_state.get("state", "") or "").lower()
                            _fallback_action = str(strategy_state.get("action", "") or "").lower()

                            # 只做相容映射，不反向改寫正式 strategy_state。
                            _trend_map = {
                                "strong_bull": "strong_uptrend",
                                "bull": "uptrend",
                                "weak_bull": "uptrend",
                                "neutral": "neutral",
                                "weak": "weak",
                                "weak_bear": "weak",
                                "bear": "bearish",
                                "strong_bear": "bearish",
                            }
                            _trend_value = _trend_map.get(_fallback_state, "neutral")

                            self.market_state = _ShadowValueV912("neutral")
                            self.trend_state = _ShadowValueV912(_trend_value)
                            self.strategy = _ShadowValueV912(
                                _fallback_action if _fallback_action else "hold"
                            )

                    class _ShadowFallbackV912:
                        def __init__(self):
                            self.snapshot = _ShadowSnapshotFallbackV912()
                            self.__dict__.update({
                                "action": None,
                                "decision": None,
                                "score": None,
                                "confidence": None,
                                "reasons": [],
                                "warnings": [
                                    "Shadow 輔助判斷因 Price Engine 驗證器異常而停用"
                                ],
                                "available": False,
                            })

                        def model_dump(self):
                            return {
                                "action": self.action,
                                "decision": self.decision,
                                "score": self.score,
                                "confidence": self.confidence,
                                "reasons": self.reasons,
                                "warnings": self.warnings,
                                "available": self.available,
                            }

                    shadow_v4 = _ShadowFallbackV912()

                # Sprint 12：Action Governance
                # Trend 是慢狀態；Action 是快狀態。治理層不改 Trend，只限制「現在能不能進場」。
                _s12 = shadow_v4.snapshot
                _s12_price = float(res.get("current_price", 0) or 0)
                _s12_levels = decision_snapshot.get("levels", {}) or {}
                _s12_confirm = float(_s12_levels.get("突破確認價", 0) or 0)
                _s12_stop = float(_s12_levels.get("protective_stop", 0) or 0)

                _s12_daily = res.get("daily_df")
                _s12_vol_ratio = None
                if isinstance(_s12_daily, pd.DataFrame) and not _s12_daily.empty:
                    _vcol = "vol" if "vol" in _s12_daily.columns else (
                        "Trading_Volume" if "Trading_Volume" in _s12_daily.columns else None
                    )
                    if _vcol:
                        _vs = pd.to_numeric(_s12_daily[_vcol], errors="coerce").dropna()
                        if len(_vs) >= 20 and float(_vs.tail(20).mean()) > 0:
                            _s12_vol_ratio = float(_vs.iloc[-1]) / float(_vs.tail(20).mean())

                _s12_inst = res.get("institutional_df")
                _s12_foreign5 = None
                _s12_trust5 = None
                if isinstance(_s12_inst, pd.DataFrame) and not _s12_inst.empty:
                    if "外資(張)" in _s12_inst.columns:
                        _x = pd.to_numeric(_s12_inst["外資(張)"], errors="coerce").dropna()
                        if len(_x):
                            _s12_foreign5 = float(_x.tail(5).sum())
                    if "投信(張)" in _s12_inst.columns:
                        _x = pd.to_numeric(_s12_inst["投信(張)"], errors="coerce").dropna()
                        if len(_x):
                            _s12_trust5 = float(_x.tail(5).sum())

                _s12_market = str(_s12.market_state.value).lower()
                _s12_raw_strategy = str(_s12.strategy.value).lower()
                _s12_reasons = []

                # Sprint 14：允許建立部位 分層進場治理
                # Trend 只回答方向；Governance 回答「現在是否適合進場、適合做到哪一層」。
                _s14_進場區_low = float(_s12_levels.get("進場區_low", 0) or 0)
                _s14_進場區_high = float(_s12_levels.get("進場區_high", 0) or 0)
                _s14_進場區 = float(_s12_levels.get("進場區", 0) or 0)

                _s14_labels = {
                    "strong_uptrend": "強勢多頭",
                    "uptrend": "多頭趨勢",
                    "neutral": "中性整理",
                    "weak": "偏弱",
                    "bearish": "空頭",
                    "risk_off": "風險偏高",
                    "允許建立部位": "允許建立部位",
                    "WAIT_BETTER_ENTRY": "等待較佳進場位置",
                    "PROBE": "試探性進場",
                    "允許建立部位_BASE": "建立基本部位",
                    "ADD_ON_CONFIRMATION": "突破確認後加碼",
                    "等待突破確認": "等待進場條件確認",
                    "HOLD": "持有",
                    "REDUCE": "減碼",
                    "EXIT": "退出",
                    "WAIT": "等待",
                }

                _s12_action = _s12_raw_strategy.upper()

                if _s12_raw_strategy == "build":
                    _s14_in_進場區_zone = (
                        _s14_進場區_low > 0
                        and _s14_進場區_high > 0
                        and _s14_進場區_low <= _s12_price <= _s14_進場區_high
                    )
                    _s14_above_進場區 = (
                        _s14_進場區_high > 0 and _s12_price > _s14_進場區_high
                    )
                    _s14_confirmed = (
                        _s12_confirm > 0 and _s12_price >= _s12_confirm
                    )

                    # 支持條件採「計分」而不是單一條件一票否決。
                    _s14_support_score = 0
                    _s14_support_total = 0

                    if _s12_vol_ratio is not None:
                        _s14_support_total += 1
                        if _s12_vol_ratio >= 0.80:
                            _s14_support_score += 1
                        elif _s12_vol_ratio < 0.60:
                            _s12_reasons.append(
                                f"目前成交量僅約20日均量的 {_s12_vol_ratio:.2f} 倍，量能偏低"
                            )

                    if _s12_foreign5 is not None and _s12_trust5 is not None:
                        _s14_support_total += 1
                        if _s12_foreign5 >= 0 or _s12_trust5 >= 0:
                            _s14_support_score += 1
                        else:
                            _s12_reasons.append(
                                f"外資近5日 {_s12_foreign5:+,.0f} 張、"
                                f"投信近5日 {_s12_trust5:+,.0f} 張，兩者同步偏賣"
                            )

                    _s14_support_total += 1
                    if _s12_market in {"strong_uptrend", "uptrend", "bullish"}:
                        _s14_support_score += 1
                    elif _s12_market == "neutral":
                        _s12_reasons.append("大盤目前為中性整理，尚未提供明確順風條件")
                    else:
                        _s12_reasons.append(
                            f"大盤狀態為 {_s14_labels.get(_s12_market, _s12.market_state.value)}，"
                            "不利於積極建立新部位"
                        )

                    # Sprint 15：回檔品質評估
                    # 目的：進入理想進場區不代表自動可買；先分辨健康回檔與結構轉弱。
                    _s15_pullback_score = 0
                    _s15_pullback_checks = 0
                    _s15_pullback_reasons = []

                    _s15_ret5 = None
                    if isinstance(_s12_daily, pd.DataFrame) and not _s12_daily.empty:
                        _close_col15 = "close" if "close" in _s12_daily.columns else (
                            "Close" if "Close" in _s12_daily.columns else None
                        )
                        if _close_col15:
                            _cs15 = pd.to_numeric(_s12_daily[_close_col15], errors="coerce").dropna()
                            if len(_cs15) >= 6 and float(_cs15.iloc[-6]) > 0:
                                _s15_ret5 = (float(_cs15.iloc[-1]) / float(_cs15.iloc[-6]) - 1) * 100

                    # A. 均線結構：價格仍守在 MA60 上方，且 MA20 > MA60，視為結構尚未破壞。
                    _s15_ma20 = float(res.get("ma20_val", 0) or 0)
                    _s15_ma60 = float(res.get("ma60_val", 0) or 0)
                    if _s15_ma20 > 0 and _s15_ma60 > 0:
                        _s15_pullback_checks += 1
                        if _s12_price >= _s15_ma60 and _s15_ma20 >= _s15_ma60:
                            _s15_pullback_score += 1
                        else:
                            _s15_pullback_reasons.append("價格／均線結構已出現轉弱跡象")

                    # B. 回檔量能：回檔時量比 <= 0.80 視為相對健康；爆量不加分。
                    if _s12_vol_ratio is not None:
                        _s15_pullback_checks += 1
                        if _s12_vol_ratio <= 0.80:
                            _s15_pullback_score += 1
                        elif _s12_vol_ratio >= 1.30:
                            _s15_pullback_reasons.append(
                                f"成交量約為20日均量 {_s12_vol_ratio:.2f} 倍，回檔伴隨明顯放量"
                            )

                    # C. 近5日跌幅：溫和整理可接受；急跌不視為健康低接。
                    if _s15_ret5 is not None:
                        _s15_pullback_checks += 1
                        if _s15_ret5 >= -6.0:
                            _s15_pullback_score += 1
                        elif _s15_ret5 < -10.0:
                            _s15_pullback_reasons.append(
                                f"近5日跌幅 {_s15_ret5:.2f}%，屬快速下跌，不宜只因進入價格區間而承接"
                            )

                    # D. 法人：至少一方近5日未持續賣超才加分；雙賣則不加分。
                    if _s12_foreign5 is not None and _s12_trust5 is not None:
                        _s15_pullback_checks += 1
                        if _s12_foreign5 >= 0 or _s12_trust5 >= 0:
                            _s15_pullback_score += 1
                        else:
                            _s15_pullback_reasons.append("外資與投信近5日仍同步賣超")

                    # E. 大盤：中性以上不視為逆風；明顯弱勢則不加分。
                    _s15_pullback_checks += 1
                    if _s12_market in {"neutral", "strong_uptrend", "uptrend", "bullish"}:
                        _s15_pullback_score += 1
                    else:
                        _s15_pullback_reasons.append("大盤環境偏弱，回檔承接風險較高")

                    _s15_pullback_ratio = (
                        _s15_pullback_score / _s15_pullback_checks
                        if _s15_pullback_checks else 0
                    )

                    # 只有價格接近或進入理想進場區時，回檔品質才具有操作意義。
                    _s16_進場區_near = (
                        _s14_進場區_low > 0
                        and _s14_進場區_high > 0
                        and _s12_price <= _s14_進場區_high * 1.03
                    )
                    _s16_pullback_active = bool(_s14_in_進場區_zone or _s16_進場區_near)

                    if not _s16_pullback_active:
                        _s15_pullback_quality = "NOT_APPLICABLE"
                        _s15_pullback_quality_zh = "尚未進入回檔評估區"
                    elif _s15_pullback_ratio >= 0.80:
                        _s15_pullback_quality = "HEALTHY"
                        _s15_pullback_quality_zh = "健康回檔"
                    elif _s16_pullback_active and _s15_pullback_ratio >= 0.60:
                        _s15_pullback_quality = "ACCEPTABLE"
                        _s15_pullback_quality_zh = "可觀察的正常整理"
                    elif _s16_pullback_active:
                        _s15_pullback_quality = "WEAKENING"
                    else:
                        _s15_pullback_quality = "NOT_APPLICABLE"
                        _s15_pullback_quality_zh = "回檔品質不足／可能轉弱"

                    # 第一層：突破確認價後，且至少有部分外部條件支持，才允許加碼。
                    if _s14_confirmed:
                        if _s14_support_score >= 2:
                            _s12_action = "ADD_ON_CONFIRMATION"
                            _s12_reasons.insert(
                                0,
                                f"股價已達／突破 {_s12_confirm:,.2f} 元確認價，"
                                "且量能、法人、大盤至少有兩項提供支持"
                            )
                        else:
                            _s12_action = "等待突破確認"
                            _s12_reasons.insert(
                                0,
                                f"股價雖已達 {_s12_confirm:,.2f} 元確認價，"
                                "但量能、法人與大盤的配合仍不足，暫不追價"
                            )

                    # 第二層：位於既有 理想進場區，外部條件普通可試單，較佳可建基本部位。
                    elif _s14_in_進場區_zone:
                        if _s15_pullback_quality == "WEAKENING":
                            _s12_action = "等待突破確認"
                            _s12_reasons.insert(
                                0,
                                f"現價雖進入理想進場區 {_s14_進場區_low:,.2f}～"
                                f"{_s14_進場區_high:,.2f} 元，但回檔品質不足，不能只因價格變便宜就承接"
                            )
                        elif _s15_pullback_quality == "HEALTHY" and _s14_support_score >= 2:
                            _s12_action = "允許建立部位_BASE"
                            _s12_reasons.insert(
                                0,
                                f"現價位於理想進場區 {_s14_進場區_low:,.2f}～"
                                f"{_s14_進場區_high:,.2f} 元，回檔結構健康且外部條件已有一定支持"
                            )
                        elif _s15_pullback_quality in {"HEALTHY", "ACCEPTABLE"} and _s14_support_score >= 1:
                            _s12_action = "PROBE"
                            _s12_reasons.insert(
                                0,
                                f"現價位於理想進場區 {_s14_進場區_low:,.2f}～"
                                f"{_s14_進場區_high:,.2f} 元，回檔尚未破壞主要結構，"
                                "但確認條件未完全到位，只適合小部位試探"
                            )
                        else:
                            _s12_action = "等待突破確認"
                            _s12_reasons.insert(
                                0,
                                f"現價雖位於理想進場區 {_s14_進場區_low:,.2f}～"
                                f"{_s14_進場區_high:,.2f} 元，但回檔品質或外部條件仍不足"
                            )

                    # 第三層：已高於 理想進場區、尚未突破 Confirmation。
                    # 這裡不因 Trend 看多就追價。
                    elif _s14_above_進場區:
                        _s12_action = "WAIT_BETTER_ENTRY"
                        _s12_reasons.insert(
                            0,
                            f"現價 {_s12_price:,.2f} 元已高於既有理想進場區 "
                            f"{_s14_進場區_low:,.2f}～{_s14_進場區_high:,.2f} 元，"
                            f"但尚未突破 {_s12_confirm:,.2f} 元確認價，暫不追價"
                        )

                    # 第四層：低於 理想進場區，不把「便宜」直接當買點。
                    else:
                        _s12_action = "等待突破確認"
                        _s12_reasons.insert(
                            0,
                            f"現價尚未進入可確認的進場結構，先等待價格與趨勢重新取得一致"
                        )

                _s12_governance = {
                    "trend_state": _s12.trend_state.value,
                    "trend_state_zh": _s14_labels.get(_s12.trend_state.value, _s12.trend_state.value),
                    "raw_strategy": _s12_raw_strategy.upper(),
                    "raw_strategy_zh": _s14_labels.get(_s12_raw_strategy.upper(), _s12_raw_strategy.upper()),
                    "governed_action": _s12_action,
                    "governed_action_zh": _s14_labels.get(_s12_action, _s12_action),
                    "price": _s12_price,
                    "突破確認價": _s12_confirm,
                    "protective_stop": _s12_stop,
                    "進場區_low": _s14_進場區_low,
                    "進場區_high": _s14_進場區_high,
                    "volume_ratio": _s12_vol_ratio,
                    "foreign_5d": _s12_foreign5,
                    "trust_5d": _s12_trust5,
                    "market_state": _s12.market_state.value,
                    "market_state_zh": _s14_labels.get(_s12_market, _s12.market_state.value),
                    "support_score": locals().get("_s14_support_score", 0),
                    "support_total": locals().get("_s14_support_total", 0),
                    "pullback_quality": locals().get("_s15_pullback_quality", "UNKNOWN"),
                    "pullback_quality_zh": locals().get("_s15_pullback_quality_zh", "資料不足"),
                    "pullback_score": locals().get("_s15_pullback_score", 0),
                    "pullback_checks": locals().get("_s15_pullback_checks", 0),
                    "pullback_ret5": locals().get("_s15_ret5", None),
                    "pullback_reasons": locals().get("_s15_pullback_reasons", []),
                    "pullback_active": locals().get("_s16_pullback_active", False),
                    "reasons": _s12_reasons,
                }
                st.session_state["_stockpilot4_s12_governance"] = _s12_governance

                # Sprint 18：部位決策引擎（Shadow）
                # 先把既有「單筆最大風險承受」轉成實際可承受金額，再依動作層級分配。
                _s18_capital_ntd = max(float(capital or 0), 0) * 10000.0
                _s18_max_risk_ntd = _s18_capital_ntd * max(float(risk_pct or 0), 0) / 100.0
                _s18_tick = tick_size(_s12_price) if _s12_price > 0 else 0.01
                _s18_stop_fill = max(
                    0.0,
                    _s12_stop - max(int(slip_input or 0), 0) * _s18_tick
                )
                _s18_entry_price = _s12_price
                _s18_risk_per_share = max(_s18_entry_price - _s18_stop_fill, 0.0)

                _s18_action_fraction = {
                    "WAIT": 0.00,
                    "WAIT_CONFIRMATION": 0.00,
                    "WAIT_BETTER_ENTRY": 0.00,
                    "PROBE": 0.25,
                    "BUILD_BASE": 0.50,
                    "ADD_ON_CONFIRMATION": 1.00,
                    "BUILD": 0.50,
                    "HOLD": 0.00,
                    "REDUCE": 0.00,
                    "EXIT": 0.00,
                }.get(_s12_action, 0.00)

                _s18_target_risk_ntd = _s18_max_risk_ntd * _s18_action_fraction

                if _s18_risk_per_share > 0 and _s18_target_risk_ntd > 0:
                    _s18_shares_by_risk = int(_s18_target_risk_ntd // _s18_risk_per_share)
                    _s18_shares_by_cash = int(_s18_capital_ntd // _s18_entry_price) if _s18_entry_price > 0 else 0
                    _s18_target_shares = max(0, min(_s18_shares_by_risk, _s18_shares_by_cash))
                else:
                    _s18_shares_by_risk = 0
                    _s18_shares_by_cash = 0
                    _s18_target_shares = 0

                _s18_current_shares = int(user_shares or 0) if user_holding else 0
                _s18_additional_shares = max(0, _s18_target_shares - _s18_current_shares)

                # 若已持有，但使用者沒有輸入股數，不能假裝知道可加碼數量。
                _s18_holding_quantity_missing = bool(user_holding and _s18_current_shares <= 0)
                if _s18_holding_quantity_missing and _s12_action in {
                    "PROBE", "BUILD_BASE", "ADD_ON_CONFIRMATION", "BUILD"
                }:
                    _s18_additional_shares = None

                _s18_target_amount = _s18_target_shares * _s18_entry_price
                _s18_additional_amount = (
                    _s18_additional_shares * _s18_entry_price
                    if isinstance(_s18_additional_shares, int)
                    else None
                )

                _s18_current_position_risk = None
                if user_holding and _s18_current_shares > 0 and user_cost > 0:
                    # 成本基準風險：若保護價已高於成本，可能為 0。
                    _s18_current_position_risk = max(
                        float(user_cost) - _s18_stop_fill,
                        0.0
                    ) * _s18_current_shares

                # Sprint 18.2：三種持倉風險分離
                # A. 本金虧損風險：以成本價與「含滑價後風險價」比較。
                # B. 獲利回吐風險：以現價到風險價可能回吐的獲利衡量。
                # C. 資金集中度：以持倉市值 / 核心資金池衡量。
                _s182_cost_basis = float(user_cost or 0)
                _s182_principal_loss_per_share = (
                    max(_s182_cost_basis - _s18_stop_fill, 0.0)
                    if user_holding and _s18_current_shares > 0 and _s182_cost_basis > 0
                    else 0.0
                )
                _s182_principal_loss_risk = (
                    _s182_principal_loss_per_share * _s18_current_shares
                )

                _s182_profit_locked_per_share = (
                    max(_s18_stop_fill - _s182_cost_basis, 0.0)
                    if user_holding and _s18_current_shares > 0 and _s182_cost_basis > 0
                    else 0.0
                )
                _s182_locked_profit_at_stop = (
                    _s182_profit_locked_per_share * _s18_current_shares
                )

                _s182_profit_giveback_per_share = (
                    max(_s12_price - max(_s18_stop_fill, _s182_cost_basis), 0.0)
                    if user_holding and _s18_current_shares > 0 and _s182_cost_basis > 0
                    else 0.0
                )
                _s182_profit_giveback_risk = (
                    _s182_profit_giveback_per_share * _s18_current_shares
                )

                if not user_holding or _s18_current_shares <= 0:
                    _s182_principal_risk_status = "未持有"
                elif _s182_principal_loss_risk <= 0:
                    _s182_principal_risk_status = "目前保護價高於成本，本金風險低"
                elif _s182_principal_loss_risk <= _s18_max_risk_ntd:
                    _s182_principal_risk_status = "本金風險在單筆上限內"
                else:
                    _s182_principal_risk_status = "本金風險超過單筆上限"

                if not user_holding or _s18_current_shares <= 0:
                    _s182_giveback_status = "未持有"
                elif _s182_profit_giveback_risk <= _s18_max_risk_ntd:
                    _s182_giveback_status = "獲利回吐幅度可控"
                elif _s182_profit_giveback_risk <= _s18_max_risk_ntd * 3:
                    _s182_giveback_status = "獲利回吐風險偏高"
                else:
                    _s182_giveback_status = "獲利回吐風險高"

                # Sprint 18.1：現有部位必須用「從現在到保護價」衡量曝險，
                # 不能因為成本很低、保護價高於成本，就把風險誤算成 0。
                _s181_market_value = _s18_current_shares * _s12_price
                _s181_cost_value = (
                    _s18_current_shares * float(user_cost or 0)
                    if user_holding else 0.0
                )
                _s181_unrealized_pnl = (
                    _s181_market_value - _s181_cost_value
                    if user_holding and _s18_current_shares > 0 and user_cost > 0
                    else None
                )
                _s181_exposure_pct = (
                    _s181_market_value / _s18_capital_ntd * 100.0
                    if _s18_capital_ntd > 0 else None
                )

                # 從目前價格跌到含滑價後保護價，可能回吐的市值。
                _s181_mark_to_stop_risk_per_share = max(
                    _s12_price - _s18_stop_fill,
                    0.0
                )
                _s181_mark_to_stop_risk = (
                    _s181_mark_to_stop_risk_per_share * _s18_current_shares
                )

                # 資金池硬上限：單一個股市值不能超過整個核心資金池。
                _s181_max_shares_by_capital = (
                    int(_s18_capital_ntd // _s12_price)
                    if _s18_capital_ntd > 0 and _s12_price > 0 else 0
                )

                # 風險上限：依「從現價到保護價」的每股風險反推最大股數。
                _s181_max_shares_by_risk = (
                    int(_s18_max_risk_ntd // _s181_mark_to_stop_risk_per_share)
                    if _s18_max_risk_ntd > 0 and _s181_mark_to_stop_risk_per_share > 0
                    else 0
                )

                _s181_effective_max_shares = min(
                    x for x in [
                        _s181_max_shares_by_capital,
                        _s181_max_shares_by_risk,
                    ] if x >= 0
                ) if (_s181_max_shares_by_capital or _s181_max_shares_by_risk) else 0

                _s181_excess_shares = max(
                    0,
                    _s18_current_shares - _s181_effective_max_shares
                )

                # 個人部位判斷與股票方向分開。
                # Sprint 18.3：個股操作判斷不再看核心資金池集中度。
                if not user_holding or _s18_current_shares <= 0:
                    _s181_position_status = "未持有"
                elif _s182_principal_loss_risk > _s18_max_risk_ntd and _s18_max_risk_ntd > 0:
                    _s181_position_status = "本金風險偏高"
                else:
                    _s181_position_status = "依個股條件管理"

                # Sprint 19.5：起漲模組安全數值轉換，避免單一欄位型別異常讓整塊決策消失。
                def _s19_safe_float(value, default=0.0):
                    try:
                        if value is None:
                            return default
                        return float(value)
                    except Exception:
                        return default

                def _s19_safe_int(value, default=0):
                    try:
                        if value is None:
                            return default
                        return int(float(value))
                    except Exception:
                        return default

                # Sprint 19.4：未持股起漲偵測
                # 直接共用「主決策引擎」已經算好的資料，不再從 Shadow 自己另抓一套。
                _s19_price = _s19_safe_float(res.get("current_price", 0), 0.0)
                _s19_ma20 = _s19_safe_float(res.get("ma20_val", 0), 0.0)
                _s19_ma60 = _s19_safe_float(res.get("ma60_val", 0), 0.0)
                _s19_ta = res.get("trend_analysis", {}) or {}
                _s19_strategy_state = str(strategy_state.get("state", "RANGE") or "RANGE")
                _s19_strategy_label = str(strategy_state.get("state_label", "資料不足") or "資料不足")
                _s19_strategy_score = _s19_safe_int(strategy_state.get("trend_score", 50), 50)

                _s19_regime = decision_snapshot.get("regime", {}) or {}
                _s19_gate = str(_s19_regime.get("gate", "CAUTION") or "CAUTION").upper()
                _s19_market_state_label = str(_s19_regime.get("state", "保守") or "保守")
                _s19_market_score = _s19_safe_float((decision_snapshot.get("market", {}) or {}).get("market_score", 50), 50.0)

                _s19_chip = decision_snapshot.get("chip_engine", {}) or {}
                _s19_volume = decision_snapshot.get("volume_engine", {}) or {}

                _s19_slope20 = _s19_safe_float(_s19_ta.get("slope20", 0), 0.0)
                _s19_slope60 = _s19_safe_float(_s19_ta.get("slope60", 0), 0.0)
                # Sprint 19.8：量能來源分流
                # 正式趨勢若關閉盤中量比，trend_analysis["volume_ratio"] 會刻意為 0；
                # 起漲偵測不可把這個 0 誤判為「極度無量」。
                _s19_volume_ratio = None
                _s19_volume_source = "資料不足"

                _s19_market_data = res.get("market_data", {}) or {}
                _s19_intraday_volume_valid = bool(
                    _s19_market_data.get("volume_valid", False)
                    and _s19_market_data.get("volume_ratio_enabled", False)
                )

                if _s19_intraday_volume_valid:
                    _vr19 = _s19_ta.get("volume_ratio", None)
                    if _vr19 is not None:
                        _s19_volume_ratio = _s19_safe_float(_vr19, None)
                        if _s19_volume_ratio is not None and _s19_volume_ratio > 0:
                            _s19_volume_source = "盤中成交量／20日均量"

                # 若盤中量比停用或不可用，改用最近完整交易日。
                if _s19_volume_ratio is None or _s19_volume_ratio <= 0:
                    _s19_df_vol = res.get("daily_df")
                    if (
                        isinstance(_s19_df_vol, pd.DataFrame)
                        and not _s19_df_vol.empty
                        and "vol" in _s19_df_vol.columns
                    ):
                        _v19 = pd.to_numeric(_s19_df_vol["vol"], errors="coerce").dropna()
                        if len(_v19) >= 21:
                            _last_vol19 = float(_v19.iloc[-1])
                            _avg20_vol19 = float(_v19.iloc[-21:-1].mean())
                            if _avg20_vol19 > 0 and _last_vol19 >= 0:
                                _s19_volume_ratio = _last_vol19 / _avg20_vol19
                                _s19_volume_source = "最近完整交易日成交量／前20日均量"
                        elif len(_v19) >= 20:
                            _last_vol19 = float(_v19.iloc[-1])
                            _avg20_vol19 = float(_v19.tail(20).mean())
                            if _avg20_vol19 > 0 and _last_vol19 >= 0:
                                _s19_volume_ratio = _last_vol19 / _avg20_vol19
                                _s19_volume_source = "最近交易日成交量／20日均量"

                _s19_daily = res.get("daily_df")
                _s19_ret3 = None
                _s19_ret5 = None
                _s20_today_pct = _s19_safe_float(res.get("stock_daily_pct", 0), 0.0)
                _s20_ret2_live = None
                _s20_reclaim_2d = False
                _s20_ma5 = _s19_safe_float(res.get("ma5_val", 0), 0.0)

                if isinstance(_s19_daily, pd.DataFrame) and not _s19_daily.empty and "close" in _s19_daily.columns:
                    _c19 = pd.to_numeric(_s19_daily["close"], errors="coerce").dropna()
                    if len(_c19) >= 4 and float(_c19.iloc[-4]) > 0:
                        _s19_ret3 = (float(_c19.iloc[-1]) / float(_c19.iloc[-4]) - 1) * 100
                    if len(_c19) >= 6 and float(_c19.iloc[-6]) > 0:
                        _s19_ret5 = (float(_c19.iloc[-1]) / float(_c19.iloc[-6]) - 1) * 100

                    # 用即時價檢查「今天是否正在反轉」，避免三日累積報酬慢一拍。
                    if len(_c19) >= 2 and float(_c19.iloc[-2]) > 0:
                        _s20_ret2_live = (_s19_price / float(_c19.iloc[-2]) - 1) * 100
                    if len(_c19) >= 2:
                        _s20_recent_2d_high = float(_c19.tail(2).max())
                        _s20_reclaim_2d = _s19_price > _s20_recent_2d_high

                _s19_checks = []

                def _s19_add_check(name, available, passed, value_text, reason, core=False):
                    _s19_checks.append({
                        "條件": name,
                        "資料狀態": "有資料" if available else "缺資料",
                        "實際值": value_text,
                        "是否通過": "✅ 通過" if (available and passed) else ("❌ 未通過" if available else "⚪ 不計分"),
                        "用途": reason,
                        "核心條件": "是" if core else "否",
                        "_available": bool(available),
                        "_passed": bool(available and passed),
                        "_core": bool(core),
                    })

                # 核心一：價格必須站上 MA20
                _s19_add_check(
                    "股價站上20日均線",
                    _s19_ma20 > 0,
                    _s19_price > _s19_ma20 if _s19_ma20 > 0 else False,
                    f"現價 {_s19_price:,.2f}／MA20 {_s19_ma20:,.2f}" if _s19_ma20 > 0 else "MA20 缺資料",
                    "起漲至少要先脫離20日均線下方弱勢區",
                    core=True,
                )

                # 核心二：正式趨勢為慢狀態；起漲模組允許「反轉起漲結構」提早通過。
                _s19_formal_trend_bull = _s19_strategy_state in {"BULL_PULLBACK", "STRONG_BULL"}

                _s19_reversal_structure = (
                    _s19_ma20 > 0
                    and _s19_ma60 > 0
                    and _s19_price > _s19_ma20
                    and _s19_ma20 >= _s19_ma60
                    and _s19_slope20 > 0
                    and _s19_slope60 >= 0
                )

                _s19_trend_pass = bool(
                    _s19_formal_trend_bull or _s19_reversal_structure
                )

                if _s19_formal_trend_bull:
                    _s19_trend_gate_text = (
                        f"正式趨勢已翻多：{_s19_strategy_label}／趨勢分數 {_s19_strategy_score}"
                    )
                elif _s19_reversal_structure:
                    _s19_trend_gate_text = (
                        f"正式趨勢仍為{_s19_strategy_label}（{_s19_strategy_score}分），"
                        "但價格與均線已形成反轉起漲結構"
                    )
                else:
                    _s19_trend_gate_text = (
                        f"正式趨勢仍為{_s19_strategy_label}／趨勢分數 {_s19_strategy_score}，"
                        "且尚未形成完整反轉結構"
                    )

                _s19_add_check(
                    "趨勢已翻多或形成反轉起漲結構",
                    True,
                    _s19_trend_pass,
                    _s19_trend_gate_text,
                    "正式趨勢是慢狀態；若價格、MA20、MA60與斜率已形成反轉結構，可提前進入試單評估",
                    core=True,
                )

                # 核心三：大盤不可明確禁止新增
                _s19_market_pass = _s19_gate not in {"PANIC", "RISK_OFF", "NO_NEW_BUY"}
                _s19_add_check(
                    "大盤允許新部位",
                    True,
                    _s19_market_pass,
                    f"{_s19_market_state_label}／風險閘門 {_s19_gate}／市場分數 {_s19_market_score:.0f}",
                    "OPEN 或 CAUTION 可以評估起漲試單；明確風險閘門則不新增",
                    core=True,
                )

                # 加分條件：MA20斜率
                _s19_add_check(
                    "20日均線向上",
                    True,
                    _s19_slope20 > 0,
                    f"斜率 {_s19_slope20:+.2f}%",
                    "確認不是只靠單日價格彈升",
                )

                # 加分條件：MA20 > MA60
                _s19_add_check(
                    "短中期均線排列偏多",
                    _s19_ma20 > 0 and _s19_ma60 > 0,
                    _s19_ma20 >= _s19_ma60 if (_s19_ma20 > 0 and _s19_ma60 > 0) else False,
                    f"MA20 {_s19_ma20:,.2f}／MA60 {_s19_ma60:,.2f}" if (_s19_ma20 > 0 and _s19_ma60 > 0) else "均線資料不足",
                    "確認短期均線沒有落在中期均線下方",
                )

                # Sprint 20：短線動能轉折
                # 不再要求三日累積報酬一定先翻正；今天若已明顯轉強，也可提早辨識。
                _s20_momentum_turn = bool(
                    (_s20_today_pct >= 1.0 and (_s20_ma5 <= 0 or _s19_price >= _s20_ma5))
                    or (_s20_today_pct > 0 and _s20_reclaim_2d)
                    or (_s19_ret3 is not None and 0.3 <= _s19_ret3 <= 8.0)
                )

                _s20_momentum_text_parts = [f"今日 {_s20_today_pct:+.2f}%"]
                if _s20_ret2_live is not None:
                    _s20_momentum_text_parts.append(f"即時2日 {_s20_ret2_live:+.2f}%")
                if _s19_ret3 is not None:
                    _s20_momentum_text_parts.append(f"近3日 {_s19_ret3:+.2f}%")
                if _s20_reclaim_2d:
                    _s20_momentum_text_parts.append("已收復近2日高點")
                if _s20_ma5 > 0:
                    _s20_momentum_text_parts.append(
                        f"現價{'站上' if _s19_price >= _s20_ma5 else '低於'}MA5 {_s20_ma5:,.2f}"
                    )
                _s20_momentum_text = "／".join(_s20_momentum_text_parts)

                _s19_add_check(
                    "短線動能出現轉折",
                    True,
                    _s20_momentum_turn,
                    _s20_momentum_text,
                    "三日報酬尚未翻正時，只要今日明顯轉強或收復近2日高點，也能提早辨識起漲",
                )

                # 加分條件：量能，不需要爆量，至少不能極度無量
                _s19_add_check(
                    "成交量不是極度低迷",
                    _s19_volume_ratio is not None,
                    _s19_volume_ratio >= 0.55 if _s19_volume_ratio is not None else False,
                    (
                        f"{_s19_volume_ratio:.2f} 倍（{_s19_volume_source}）"
                        if _s19_volume_ratio is not None else "缺資料"
                    ),
                    "起漲可以先於爆量；盤中量比停用時，以最近完整交易日量能替代",
                )

                # 籌碼採主系統 chip_engine：有 veto 直接不加分；沒有 veto 且 warning 不高視為可接受
                _s19_chip_veto = bool(_s19_chip.get("veto"))
                _s19_chip_warning = _s19_safe_int(_s19_chip.get("warning_points", 0), 0)
                _s19_chip_state = str(_s19_chip.get("state", "資料不足") or "資料不足")
                _s19_add_check(
                    "籌碼沒有明確否決",
                    True,
                    (not _s19_chip_veto) and _s19_chip_warning < 4,
                    f"{_s19_chip_state}／警訊 {_s19_chip_warning}",
                    "不要求法人一定大買，但不能已有強烈籌碼否決",
                )

                # 量價引擎也不可 veto
                _s19_volume_veto = bool(_s19_volume.get("veto"))
                _s19_volume_warning = _s19_safe_int(_s19_volume.get("warning_points", 0), 0)
                _s19_add_check(
                    "量價沒有明確否決",
                    True,
                    (not _s19_volume_veto) and _s19_volume_warning < 4,
                    f"警訊 {_s19_volume_warning}",
                    "避免在量價結構已惡化時提早進場",
                )

                _s19_scored_checks = [x for x in _s19_checks if x["_available"]]
                _s19_early_total = len(_s19_scored_checks)
                _s19_early_score = sum(1 for x in _s19_scored_checks if x["_passed"])
                _s19_early_ratio = (
                    _s19_early_score / _s19_early_total
                    if _s19_early_total else 0.0
                )
                _s19_early_reasons = [
                    x["條件"] + "：" + x["實際值"]
                    for x in _s19_scored_checks if x["_passed"]
                ]

                _s19_core_checks = [x for x in _s19_checks if x["_core"]]
                _s19_core_ok = bool(_s19_core_checks) and all(
                    x["_available"] and x["_passed"] for x in _s19_core_checks
                )

                # 防追高：用 ATR 兼容高波動股；無 ATR 時才用固定 12%。
                _s19_atr = _s19_safe_float(res.get("atr", 0), 0.0)
                _s19_ma20_distance_pct = (
                    (_s19_price / _s19_ma20 - 1) * 100
                    if _s19_ma20 > 0 else None
                )
                _s19_extension_limit_pct = 12.0
                if _s19_atr > 0 and _s19_price > 0:
                    _s19_atr_pct = _s19_atr / _s19_price * 100
                    _s19_extension_limit_pct = max(8.0, min(18.0, _s19_atr_pct * 2.0))
                _s19_not_extended = (
                    _s19_ma20_distance_pct is not None
                    and _s19_ma20_distance_pct <= _s19_extension_limit_pct
                )

                # Beta v4：完整未持股決策鏈
                _s20_market_blocked = _s19_gate in {"PANIC", "RISK_OFF", "NO_NEW_BUY"}
                _s20_price_weak = bool(_s19_ma20 > 0 and _s19_price < _s19_ma20)

                # 低檔試單：不等正式趨勢翻多。
                # 價格需靠近低風險區/MA20，且至少有一個早期止跌轉強訊號。
                _s21_in_entry_zone = bool(
                    _s14_進場區_low > 0
                    and _s14_進場區_high > 0
                    and _s14_進場區_low <= _s19_price <= _s14_進場區_high
                )
                _s21_near_ma20 = bool(
                    _s19_ma20 > 0
                    and abs(_s19_price / _s19_ma20 - 1) <= 0.035
                )
                _s21_above_ma60_buffer = bool(
                    _s19_ma60 <= 0 or _s19_price >= _s19_ma60 * 0.98
                )
                _s21_no_hard_veto = bool(
                    (not _s19_chip_veto)
                    and (not _s19_volume_veto)
                    and (not _s20_market_blocked)
                )
                _s21_early_turn = bool(
                    _s20_momentum_turn
                    or _s19_slope20 > 0
                    or (_s20_today_pct > 0 and _s20_reclaim_2d)
                )

                # Beta v8.7：低位轉折偵測
                # 正式趨勢仍可維持空頭，但若股價已跌到成本區下方，
                # 先辨識「低位觀察」，再等止跌訊號升級為小部位試單。
                _s87_recent_low = 0.0
                if (
                    isinstance(_s19_daily, pd.DataFrame)
                    and not _s19_daily.empty
                ):
                    _s87_low_col = None
                    if "low" in _s19_daily.columns:
                        _s87_low_col = pd.to_numeric(
                            _s19_daily["low"], errors="coerce"
                        ).dropna()
                    elif "close" in _s19_daily.columns:
                        _s87_low_col = pd.to_numeric(
                            _s19_daily["close"], errors="coerce"
                        ).dropna()
                    if _s87_low_col is not None and len(_s87_low_col):
                        _s87_recent_low = float(_s87_low_col.tail(10).min())

                _s87_low_zone_low = _s87_recent_low
                _s87_low_zone_high = 0.0
                if _s87_recent_low > 0:
                    _s87_low_buffer = max(
                        _s19_atr * 0.45 if _s19_atr > 0 else 0,
                        _s87_recent_low * 0.035,
                        tick_size(_s87_recent_low),
                    )
                    _s87_low_zone_high = _s87_recent_low + _s87_low_buffer
                    if _s14_進場區_low > 0:
                        _s87_low_zone_high = min(
                            _s87_low_zone_high,
                            _s14_進場區_low
                        )

                _s87_below_old_entry = bool(
                    _s14_進場區_low > 0
                    and _s19_price < _s14_進場區_low
                )
                _s87_in_low_zone = bool(
                    _s87_low_zone_low > 0
                    and _s87_low_zone_high > 0
                    and _s87_low_zone_low <= _s19_price <= _s87_low_zone_high
                )

                # Beta v8.9：更早一層的「止跌雷達」
                # 目的：不要等到站回 MA5 或收復兩日高點後才注意到轉折。
                _s89_today_low = 0.0
                _s89_prev_low = 0.0
                _s89_prev2_low = 0.0
                _s89_prev_close = _s19_safe_float(
                    res.get("previous_close", res.get("prev_close", 0)),
                    0.0
                )

                if isinstance(_s19_daily, pd.DataFrame) and not _s19_daily.empty:
                    if "low" in _s19_daily.columns:
                        _s89_lows = pd.to_numeric(
                            _s19_daily["low"], errors="coerce"
                        ).dropna()
                        if len(_s89_lows) >= 1:
                            _s89_prev_low = float(_s89_lows.iloc[-1])
                        if len(_s89_lows) >= 2:
                            _s89_prev2_low = float(_s89_lows.iloc[-2])

                # 若即時資料有當日最低價則優先使用；沒有時用現價作保守替代。
                _s89_today_low = _s19_safe_float(
                    res.get("day_low", res.get("low", _s19_price)),
                    _s19_price
                )

                _s89_intraday_rebound_pct = 0.0
                if _s89_today_low > 0 and _s19_price > 0:
                    _s89_intraday_rebound_pct = (
                        (_s19_price / _s89_today_low) - 1
                    ) * 100

                _s89_decline_narrowing = False
                if _s89_prev_close > 0:
                    _s89_decline_narrowing = bool(
                        _s20_today_pct > -3.0
                        or (
                            _s19_ret3 is not None
                            and _s19_ret3 > -5.0
                            and _s20_today_pct > _s19_ret3
                        )
                    )

                _s89_no_new_low = bool(
                    _s89_prev_low > 0
                    and _s89_today_low >= _s89_prev_low
                )

                _s89_low_rising = bool(
                    _s89_prev_low > 0
                    and _s89_prev2_low > 0
                    and _s89_prev_low >= _s89_prev2_low
                )

                _s89_rebound_from_low = bool(
                    _s89_intraday_rebound_pct >= 1.5
                )

                _s89_volume_stabilizing = bool(
                    _s19_volume_ratio is not None
                    and _s19_volume_ratio >= 0.40
                )

                _s89_stabilization_signals = {
                    "今日未再創近期新低": _s89_no_new_low,
                    "短線低點開始墊高": _s89_low_rising,
                    "盤中自低點拉回至少1.5%": _s89_rebound_from_low,
                    "跌幅開始明顯收斂": _s89_decline_narrowing,
                    "量能未低於20日均量40%": _s89_volume_stabilizing,
                }
                _s89_stabilization_score = sum(
                    1 for _v in _s89_stabilization_signals.values() if _v
                )

                # v8.9a：止跌雷達不能引用稍後才建立的 _s87_low_watch。
                # 直接以目前價格是否位於低位承接區判斷。
                _s89_low_zone_active = bool(
                    (_s87_low_zone_low > 0 and _s87_low_zone_high > 0)
                    and (_s87_low_zone_low <= _s19_price <= _s87_low_zone_high)
                )
                _s89_stabilizing = bool(
                    _s89_low_zone_active
                    and _s89_stabilization_score >= 2
                )

                _s87_bottom_signals = {
                    "今日收紅或盤中轉強": _s20_today_pct > 0,
                    "站回5日均線": (_s20_ma5 > 0 and _s19_price >= _s20_ma5),
                    "收復近2日高點": _s20_reclaim_2d,
                    "近3日跌勢已明顯收斂": (
                        _s19_ret3 is not None and _s19_ret3 >= -1.5
                    ),
                    "量能不極度萎縮": (
                        _s19_volume_ratio is not None
                        and _s19_volume_ratio >= 0.55
                    ),
                }
                _s87_bottom_score = sum(
                    1 for _v in _s87_bottom_signals.values() if _v
                )

                _s87_low_watch = bool(
                    _s87_below_old_entry
                    and (
                        _s87_in_low_zone
                        or (
                            _s19_ma20_distance_pct is not None
                            and -18.0 <= _s19_ma20_distance_pct < -3.5
                        )
                    )
                )

                # 低位「觀察」可在風險尚高時成立；
                # 但真正「試單」仍必須通過硬風險否決。
                # Beta v8.8：低位試單先形成候選，不因單一分鐘直接升級。
                _s88_low_probe_candidate = bool(
                    _s87_low_watch
                    and _s21_no_hard_veto
                    and _s87_bottom_score >= 3
                    and (
                        _s20_today_pct > 0
                        or _s20_reclaim_2d
                        or (_s20_ma5 > 0 and _s19_price >= _s20_ma5)
                    )
                )

                # 低位轉折穩定器：同一檔、同一交易日維持候選計數與鎖定狀態。
                _s88_stock_key = str(res.get("stock_id", stock_input) or stock_input)
                _s88_trade_date = str(pd.Timestamp.now(tz="Asia/Taipei").date())
                _s88_session_key = f"stockpilot_v88_low_{_s88_stock_key}_{_s88_trade_date}"
                _s88_prev = st.session_state.get(_s88_session_key, {}) or {}

                _s88_candidate_count = int(_s88_prev.get("candidate_count", 0) or 0)
                _s88_low_probe_latched = bool(_s88_prev.get("low_probe_latched", False))

                if _s88_low_probe_candidate:
                    _s88_candidate_count += 1
                else:
                    _s88_candidate_count = 0

                # 4/5 以上且有明確價格轉強可立即升級；
                # 3/5 則至少連續兩次更新都成立，避免一分鐘雜訊翻單。
                _s88_immediate_probe = bool(
                    _s87_bottom_score >= 4
                    and (
                        _s20_reclaim_2d
                        or (_s20_ma5 > 0 and _s19_price >= _s20_ma5)
                    )
                )

                if _s88_low_probe_candidate and (
                    _s88_candidate_count >= 2 or _s88_immediate_probe
                ):
                    _s88_low_probe_latched = True

                # 失效：硬風險重新出現，或跌破低位承接區下緣/正式防守價。
                _s88_low_invalidation = 0.0
                _s88_candidates = [
                    float(x) for x in [_s87_low_zone_low, _s12_stop]
                    if float(x or 0) > 0
                ]
                if _s88_candidates:
                    _s88_low_invalidation = min(_s88_candidates)

                _s88_low_invalid_now = bool(
                    (not _s21_no_hard_veto)
                    or (
                        _s88_low_invalidation > 0
                        and _s19_price < _s88_low_invalidation
                    )
                )

                if _s88_low_invalid_now:
                    _s88_low_probe_latched = False
                    _s88_candidate_count = 0

                st.session_state[_s88_session_key] = {
                    "candidate_count": _s88_candidate_count,
                    "low_probe_latched": _s88_low_probe_latched,
                    "last_score": int(_s87_bottom_score),
                    "last_price": float(_s19_price or 0),
                    "invalidation": float(_s88_low_invalidation or 0),
                }

                _s87_low_probe = bool(_s88_low_probe_latched)

                _s21_low_probe = bool(
                    (
                        _s21_no_hard_veto
                        and _s21_above_ma60_buffer
                        and (_s21_in_entry_zone or _s21_near_ma20)
                        and _s21_early_turn
                    )
                    or _s87_low_probe
                )

                # Beta v8.4c：拉回轉強旗標必須在任何使用前先建立。
                _beta7_reclaim_triggered = False

                # 轉強試單：反轉結構已成形，動能也轉強，但還不到正式確認。
                _s21_turn_probe = bool(
                    (
                        _s19_core_ok
                        and _s19_not_extended
                        and _s20_momentum_turn
                        and _s19_early_ratio >= 0.62
                    )
                    or (
                        _beta7_reclaim_triggered
                        and _s21_no_hard_veto
                        and _s19_not_extended
                    )
                )

                # 正式進場：條件完整度較高，或已完成突破確認。
                _s21_formal_entry = bool(
                    (
                        _s19_core_ok
                        and _s19_not_extended
                        and _s20_momentum_turn
                        and _s19_early_ratio >= 0.78
                    )
                    or (
                        _s12_confirm > 0
                        and _s19_price >= _s12_confirm
                        and _s19_market_pass
                    )
                )

                # Beta v8.8：未持股狀態梯度
                # 價格已進低位時，不再被「弱勢空頭」完全蓋掉；
                # 但真正買進仍需穩定器確認。
                if _s20_market_blocked and _s87_low_watch:
                    _s19_early_state = "LOW_WATCH"
                    _s19_early_state_zh = "低位觀察・暫不試單"
                elif _s20_market_blocked:
                    _s19_early_state = "NO_ENTRY"
                    _s19_early_state_zh = "不宜進場"
                elif _s21_formal_entry:
                    _s19_early_state = "FORMAL_ENTRY"
                    _s19_early_state_zh = "正式進場"
                elif _s19_core_ok and (not _s19_not_extended):
                    _s19_early_state = "WAIT_PULLBACK"
                    _s19_early_state_zh = "等待拉回"
                elif _s21_turn_probe:
                    _s19_early_state = "TURN_PROBE"
                    _s19_early_state_zh = "轉強試單"
                elif _s87_low_probe:
                    _s19_early_state = "LOW_PROBE"
                    _s19_early_state_zh = "低位試單"
                elif _s87_low_watch and _s87_bottom_score >= 2:
                    _s19_early_state = "LOW_CONFIRM"
                    _s19_early_state_zh = "低位轉折確認中"
                elif _s89_stabilizing:
                    _s19_early_state = "STABILIZING"
                    _s19_early_state_zh = "止跌形成"
                elif _s87_low_watch:
                    _s19_early_state = "LOW_WATCH"
                    _s19_early_state_zh = (
                        "低位觀察・暫不試單"
                        if not _s21_no_hard_veto
                        else "低位觀察"
                    )
                elif _s20_price_weak and (not _s19_reversal_structure) and (not _s21_low_probe):
                    _s19_early_state = "NO_ENTRY"
                    _s19_early_state_zh = "不宜進場"
                else:
                    _s19_early_state = "WAIT_CONFIRM"
                    _s19_early_state_zh = "繼續等待"

                # Sprint 18.3：最終只回答四件事：進場、退場、加碼、減碼。
                _s183_governed = str(_s12_governance.get("governed_action") or "").upper()
                _s183_trend = str(_s12_governance.get("trend_state") or "").lower()
                _s183_price_in_entry = bool(
                    _s14_進場區_low <= _s12_price <= _s14_進場區_high
                )

                if not user_holding or _s18_current_shares <= 0:
                    if _s19_early_state == "FORMAL_ENTRY":
                        _s183_trade_decision = "正式進場"
                        _s183_trade_reason = (
                            "價格、趨勢結構、短線動能與外部條件已達正式進場標準；"
                            "不必再等待更遠的確認價才第一次建立部位。"
                        )
                    elif _s19_early_state == "TURN_PROBE":
                        _s183_trade_decision = "轉強試單"
                        _s183_trade_reason = (
                            "反轉起漲結構已形成，短線動能也開始轉強；"
                            "可先用小部位卡位，後續若正式確認再加碼。"
                        )
                    elif _s19_early_state == "LOW_PROBE":
                        _s183_trade_decision = "低位試單"
                        _s183_trade_reason = (
                            f"股價位於低位區，止跌轉折條件 {_s87_bottom_score}/5 項成立，"
                            "且訊號已通過穩定確認；可用很小部位試單，但正式趨勢尚未翻多。"
                        )
                    elif _s19_early_state == "LOW_CONFIRM":
                        _s183_trade_decision = "低位轉折確認中"
                        _s183_trade_reason = (
                            f"股價已在低位承接區，轉折條件 {_s87_bottom_score}/5 項成立；"
                            "訊號正在累積確認，暫不因單一分鐘轉強就立即買進。"
                        )
                    elif _s19_early_state == "STABILIZING":
                        _s183_trade_decision = "止跌形成"
                        _s183_trade_reason = (
                            f"股價仍在低位承接區，但更早期的止跌訊號已出現 "
                            f"{_s89_stabilization_score}/5 項；"
                            "目前先列入高度觀察，不立即買進，等低位轉折訊號接手確認。"
                        )
                    elif _s19_early_state == "LOW_WATCH":
                        _s183_trade_decision = (
                            "低位觀察・暫不試單"
                            if not _s21_no_hard_veto
                            else "低位觀察"
                        )
                        _s183_trade_reason = (
                            "股價已進入低位承接區，值得開始監看；"
                            + (
                                "但目前仍存在大盤／籌碼／量能硬風險，因此先不試單。"
                                if not _s21_no_hard_veto
                                else "目前等待止跌、站回短均線或收復短期高點後再升級為試單。"
                            )
                        )
                    elif _s19_early_state == "WAIT_PULLBACK":
                        _s183_trade_decision = "等待拉回"
                        _s183_trade_reason = (
                            "起漲結構已出現，但目前價格離短期成本區過遠；"
                            "此時追價的風險報酬較差，等待拉回或重新形成低風險切入點。"
                        )
                    elif _s19_early_state == "NO_ENTRY":
                        _s183_trade_decision = "不宜進場"
                        _s183_trade_reason = (
                            "目前仍有明確的大盤或價格結構風險，暫不建立新部位。"
                        )
                    else:
                        _s183_trade_decision = "繼續等待"
                        _s183_trade_reason = (
                            "已接近起漲條件，但短線動能或確認訊號尚未形成；"
                            "持續觀察，不因完成度高就提前追價。"
                        )
                else:
                    if _s183_governed == "EXIT":
                        _s183_trade_decision = "退場"
                        _s183_trade_reason = "個股風控／趨勢條件已進入退出狀態。"
                    elif _s183_governed == "REDUCE":
                        _s183_trade_decision = "減碼"
                        _s183_trade_reason = "個股條件轉弱，先降低持股曝險。"
                    elif _s183_governed in {"ADD_ON_CONFIRMATION", "BUILD_BASE", "BUILD", "PROBE"}:
                        _s183_trade_decision = "加碼"
                        _s183_trade_reason = "既有持股仍成立，且個股條件允許增加部位。"
                    else:
                        _s183_trade_decision = "持有"
                        _s183_trade_reason = "目前尚未出現退場或減碼條件，也未形成新的加碼條件。"

                # 保護價跌破具有最高優先權。
                if user_holding and _s18_current_shares > 0 and _s12_price <= _s18_stop_fill:
                    _s183_trade_decision = "退場"
                    _s183_trade_reason = "現價已觸及或跌破風控保護價。"

                _s18_action_zh = _s12_governance.get(
                    "governed_action_zh",
                    _s12_action
                )

                # Beta v5：未持股三層價格路徑
                _beta5_entry_low = float(_s14_進場區_low or 0)
                _beta5_entry_high = float(_s14_進場區_high or 0)
                _beta5_confirm = float(_s12_confirm or 0)
                _beta5_ma20 = float(_s19_ma20 or 0)
                _beta5_atr = float(_s19_atr or 0)

                # 拉回觀察區：不只看舊 entry zone，也允許靠近 MA20 的較高一層觀察帶。
                _beta5_pullback_low = _beta5_entry_low
                _beta5_pullback_high = _beta5_entry_high
                if _beta5_ma20 > 0:
                    _beta5_ma20_band_low = _beta5_ma20 * 0.99
                    _beta5_ma20_band_high = _beta5_ma20 * 1.05
                    if _beta5_pullback_low <= 0:
                        _beta5_pullback_low = _beta5_ma20_band_low
                    else:
                        _beta5_pullback_low = min(_beta5_pullback_low, _beta5_ma20_band_low)

                    if _beta5_pullback_high <= 0:
                        _beta5_pullback_high = _beta5_ma20_band_high
                    else:
                        _beta5_pullback_high = max(_beta5_pullback_high, _beta5_ma20_band_high)

                # Beta v6：試單觸發價必須高於「拉回觀察區上緣」。
                # 否則會出現觀察區 930~1003，但試單價 985 的矛盾。
                # 這個價格代表「拉回後重新轉強」，不是靜態低點買入價。
                _beta5_tick = tick_size(_s19_price) if _s19_price > 0 else 1.0

                _beta6_reversal_buffer = max(
                    _beta5_tick,
                    _beta5_atr * 0.15 if _beta5_atr > 0 else 0,
                    _beta5_pullback_high * 0.01 if _beta5_pullback_high > 0 else 0,
                )

                _beta5_probe_trigger = (
                    ceil_to_tick(
                        _beta5_pullback_high + _beta6_reversal_buffer,
                        _beta5_tick
                    )
                    if _beta5_pullback_high > 0 else 0
                )

                # 試單觸發價必須低於強勢突破價；若兩者過近，取消中間試單層。
                _beta6_probe_available = True
                if _beta5_confirm > 0 and _beta5_probe_trigger >= _beta5_confirm:
                    _beta5_probe_trigger = 0
                    _beta6_probe_available = False

                # Beta v7：試單觸發必須具備順序
                # 不能因為「目前價格本來就在觸發價上方」就算觸發。
                # 必須先確認近期真的回到拉回觀察區，之後才重新站上觸發價。
                _beta7_pullback_seen = False
                _beta7_reclaim_triggered = False
                _beta7_pullback_days_ago = None

                _beta7_df = res.get("daily_df")
                if (
                    isinstance(_beta7_df, pd.DataFrame)
                    and not _beta7_df.empty
                    and _beta5_pullback_high > 0
                ):
                    # 優先使用 low；沒有 low 才退回 close。
                    _beta7_price_col = "low" if "low" in _beta7_df.columns else "close"
                    if _beta7_price_col in _beta7_df.columns:
                        _beta7_series = pd.to_numeric(
                            _beta7_df[_beta7_price_col], errors="coerce"
                        ).dropna()

                        # 只看最近 8 個完整交易日，避免很久以前的回檔誤算成這一波。
                        _beta7_recent = _beta7_series.tail(8)
                        if len(_beta7_recent):
                            _beta7_touch_mask = (
                                (_beta7_recent >= _beta5_pullback_low)
                                & (_beta7_recent <= _beta5_pullback_high)
                            )
                            if bool(_beta7_touch_mask.any()):
                                _beta7_pullback_seen = True
                                _beta7_touch_positions = [
                                    idx for idx, ok in enumerate(_beta7_touch_mask.tolist()) if ok
                                ]
                                _beta7_last_touch_pos = _beta7_touch_positions[-1]
                                _beta7_pullback_days_ago = (
                                    len(_beta7_recent) - 1 - _beta7_last_touch_pos
                                )

                # 若近期曾進入觀察區，且現在重新站上試單觸發價，才算真正的「拉回轉強」。
                # Beta v8.4c：此處開始計算真正的「先拉回、後站回」狀態。
                _beta7_reclaim_triggered = bool(_beta7_reclaim_triggered)

                # Beta v8.4d：真正的「重新站上」必須是穿越事件，
                # 不能只因為目前本來就在觸發價上方就視為今天觸發。
                _beta84d_prev_price = 0.0

                # 優先使用即時資料提供的昨收；沒有時，再使用完整日線最後一筆收盤。
                _beta84d_prev_price = _s19_safe_float(
                    res.get("previous_close", res.get("prev_close", 0)),
                    0.0
                )

                if (
                    _beta84d_prev_price <= 0
                    and isinstance(_beta7_df, pd.DataFrame)
                    and not _beta7_df.empty
                    and "close" in _beta7_df.columns
                ):
                    _beta84d_close = pd.to_numeric(
                        _beta7_df["close"], errors="coerce"
                    ).dropna()
                    if len(_beta84d_close) >= 1:
                        _beta84d_prev_price = float(_beta84d_close.iloc[-1])

                _beta84d_cross_up = bool(
                    _beta5_probe_trigger > 0
                    and _beta84d_prev_price > 0
                    and _beta84d_prev_price < _beta5_probe_trigger
                    and _s19_price >= _beta5_probe_trigger
                )

                if (
                    _beta6_probe_available
                    and _beta7_pullback_seen
                    and _beta84d_cross_up
                ):
                    _beta7_reclaim_triggered = True

                # Beta v8.4 CLEAN：決策穩定器
                # 主趨勢仍由完整日線策略決定；即時價格只負責觸發。
                # 鎖定以「股票 + 台北日期」為單位，不跨交易日延續。
                _beta84_stock_key = str(res.get("stock_id", stock_input) or stock_input)
                _beta84_trade_date = str(pd.Timestamp.now(tz="Asia/Taipei").date())
                # Beta v8.4e：更換穩定器版本 key。
                # 舊版曾用較寬鬆規則鎖定的狀態不可沿用。
                _beta84_session_key = (
                    f"stockpilot_v84e_{_beta84_stock_key}_{_beta84_trade_date}"
                )

                _beta84_prev = st.session_state.get(_beta84_session_key, {}) or {}
                _beta84_latched_probe = bool(
                    _beta84_prev.get("probe_latched", False)
                )

                # 真正完成「先拉回、後重新站上試單價」後，當日鎖定。
                if _beta7_reclaim_triggered:
                    _beta84_latched_probe = True

                # 未持股進場失效價：正式防守價與進場區下緣兩者取較低者。
                _beta84_entry_low = float(_s14_進場區_low or 0)
                _beta84_stop = float(_s12_stop or 0)
                _beta84_tick = (
                    tick_size(_beta84_entry_low)
                    if _beta84_entry_low > 0 else 1.0
                )
                if _beta84_entry_low > 0 and _beta84_stop > 0:
                    _beta84_invalidation = min(
                        _beta84_stop,
                        _beta84_entry_low - _beta84_tick
                    )
                elif _beta84_stop > 0:
                    _beta84_invalidation = _beta84_stop
                elif _beta84_entry_low > 0:
                    _beta84_invalidation = max(
                        _beta84_entry_low - _beta84_tick, 0
                    )
                else:
                    _beta84_invalidation = 0.0

                # 只有跌破真正的進場失效價才解除，不因觸發價附近震盪解除。
                _beta84_invalid_now = bool(
                    _s19_price > 0
                    and _beta84_invalidation > 0
                    and _s19_price <= _beta84_invalidation
                )
                if _beta84_invalid_now:
                    _beta84_latched_probe = False

                # 已鎖定時，讓本日拉回轉強狀態維持成立。
                if _beta84_latched_probe:
                    _beta7_reclaim_triggered = True

                st.session_state[_beta84_session_key] = {
                    "probe_latched": _beta84_latched_probe,
                    "last_price": float(_s19_price or 0),
                    "invalidation": float(_beta84_invalidation or 0),
                    "invalid_now": _beta84_invalid_now,
                    "cross_up_today": bool(_beta84d_cross_up),
                    "prev_price": float(_beta84d_prev_price or 0),
                    "trigger_price": float(_beta5_probe_trigger or 0),
                }

                # Beta v8.4c：價格路徑在決策鏈後段才完成，
                # 因此若「拉回後轉強」成立，要把結果回寫到未持股決策。
                if (
                    (not user_holding or _s18_current_shares <= 0)
                    and _beta7_reclaim_triggered
                    and _s21_no_hard_veto
                    and _s19_not_extended
                    and _s19_early_state not in {"FORMAL_ENTRY", "WAIT_PULLBACK", "NO_ENTRY"}
                ):
                    _s21_turn_probe = True
                    _s19_early_state = "TURN_PROBE"
                    _s19_early_state_zh = "轉強試單"
                    _s183_trade_decision = "轉強試單"
                    _s183_trade_reason = (
                        "近期已先回到拉回觀察區，之後重新站上試單觸發價；"
                        "拉回轉強路徑成立，可用小部位試單，正式確認後再加碼。"
                    )

                _s18_position_plan = {
                    "action": _s12_action,
                    "action_zh": _s18_action_zh,
                    "capital_ntd": _s18_capital_ntd,
                    "risk_pct": float(risk_pct or 0),
                    "max_risk_ntd": _s18_max_risk_ntd,
                    "action_fraction": _s18_action_fraction,
                    "target_risk_ntd": _s18_target_risk_ntd,
                    "entry_price": _s18_entry_price,
                    "protective_stop": _s12_stop,
                    "structural_exit": float(_s12_levels.get("structure_stop", 0) or 0),
                    "add_confirm_price": float(_s12_confirm or 0),
                    "target1": float(_s12_levels.get("target1", 0) or 0),
                    "target2": float(_s12_levels.get("target2", 0) or 0),
                    "estimated_stop_fill": _s18_stop_fill,
                    "risk_per_share": _s18_risk_per_share,
                    "target_shares": _s18_target_shares,
                    "target_amount": _s18_target_amount,
                    "current_shares": _s18_current_shares,
                    "additional_shares": _s18_additional_shares,
                    "additional_amount": _s18_additional_amount,
                    "current_position_risk": _s18_current_position_risk,
                    "holding_quantity_missing": _s18_holding_quantity_missing,
                    "market_value": _s181_market_value,
                    "cost_value": _s181_cost_value,
                    "unrealized_pnl": _s181_unrealized_pnl,
                    "exposure_pct": _s181_exposure_pct,
                    "mark_to_stop_risk_per_share": _s181_mark_to_stop_risk_per_share,
                    "mark_to_stop_risk": _s181_mark_to_stop_risk,
                    "max_shares_by_capital": _s181_max_shares_by_capital,
                    "max_shares_by_risk": _s181_max_shares_by_risk,
                    "effective_max_shares": _s181_effective_max_shares,
                    "excess_shares": _s181_excess_shares,
                    "position_status": _s181_position_status,
                    "principal_loss_per_share": _s182_principal_loss_per_share,
                    "principal_loss_risk": _s182_principal_loss_risk,
                    "principal_risk_status": _s182_principal_risk_status,
                    "locked_profit_at_stop": _s182_locked_profit_at_stop,
                    "profit_giveback_per_share": _s182_profit_giveback_per_share,
                    "profit_giveback_risk": _s182_profit_giveback_risk,
                    "profit_giveback_status": _s182_giveback_status,
                    "trade_decision": _s183_trade_decision,
                    "trade_reason": _s183_trade_reason,
                    "early_entry_state": _s19_early_state,
                    "early_entry_state_zh": _s19_early_state_zh,
                    "early_entry_score": _s19_early_score,
                    "early_entry_total": _s19_early_total,
                    "early_entry_ratio": _s19_early_ratio,
                    "early_entry_reasons": _s19_early_reasons,
                    "ma20_distance_pct": _s19_ma20_distance_pct,
                    "ret3": _s19_ret3,
                    "ret5": _s19_ret5,
                    "early_entry_checks": _s19_checks,
                    "early_core_ok": _s19_core_ok,
                    "early_not_extended": _s19_not_extended,
                    "early_strategy_state": _s19_strategy_state,
                    "early_strategy_label": _s19_strategy_label,
                    "early_strategy_score": _s19_strategy_score,
                    "early_market_gate": _s19_gate,
                    "early_market_state": _s19_market_state_label,
                    "early_market_score": _s19_market_score,
                    "early_ma20": _s19_ma20,
                    "early_ma60": _s19_ma60,
                    "early_extension_limit_pct": _s19_extension_limit_pct,
                    "early_volume_ratio": _s19_volume_ratio,
                    "early_volume_source": _s19_volume_source,
                    "early_formal_trend_bull": _s19_formal_trend_bull,
                    "early_reversal_structure": _s19_reversal_structure,
                    "early_trend_gate_text": _s19_trend_gate_text,
                    "short_momentum_turn": _s20_momentum_turn,
                    "short_momentum_text": _s20_momentum_text,
                    "today_pct": _s20_today_pct,
                    "ret2_live": _s20_ret2_live,
                    "reclaim_2d": _s20_reclaim_2d,
                    "low_probe": _s21_low_probe,
                    "low_watch": _s87_low_watch,
                    "low_hard_veto": (not _s21_no_hard_veto),
                    "low_turn_score": _s87_bottom_score,
                    "low_probe_candidate": _s88_low_probe_candidate,
                    "low_probe_candidate_count": _s88_candidate_count,
                    "low_probe_latched": _s88_low_probe_latched,
                    "low_probe_invalidation": _s88_low_invalidation,
                    "low_probe_invalid_now": _s88_low_invalid_now,
                    "stabilization_score": _s89_stabilization_score,
                    "stabilization_signals": _s89_stabilization_signals,
                    "stabilizing": _s89_stabilizing,
                    "stabilization_details": [
                        {
                            "項目": "今日是否未再創近期新低",
                            "實際值": (
                                f"今日低 {_s89_today_low:,.2f}／前一低 {_s89_prev_low:,.2f}"
                                if _s89_prev_low > 0 else "近期低點資料不足"
                            ),
                            "成立門檻": "今日低點 ≥ 前一交易日低點",
                            "通過": bool(_s89_no_new_low),
                        },
                        {
                            "項目": "短線低點是否開始墊高",
                            "實際值": (
                                f"前一低 {_s89_prev_low:,.2f}／前二低 {_s89_prev2_low:,.2f}"
                                if _s89_prev_low > 0 and _s89_prev2_low > 0
                                else "近期低點資料不足"
                            ),
                            "成立門檻": "前一日低點 ≥ 前二日低點",
                            "通過": bool(_s89_low_rising),
                        },
                        {
                            "項目": "盤中是否自低點明顯拉回",
                            "實際值": f"{_s89_intraday_rebound_pct:+.2f}%",
                            "成立門檻": "現價較今日低點反彈 ≥ 1.50%",
                            "通過": bool(_s89_rebound_from_low),
                        },
                        {
                            "項目": "跌幅是否開始收斂",
                            "實際值": f"今日 {_s20_today_pct:+.2f}%",
                            "成立門檻": "今日跌幅 > -3% 或跌勢較近3日明顯改善",
                            "通過": bool(_s89_decline_narrowing),
                        },
                        {
                            "項目": "量能是否維持最低活性",
                            "實際值": (
                                f"{_s19_volume_ratio:.2f} 倍"
                                if _s19_volume_ratio is not None else "量能比缺資料"
                            ),
                            "成立門檻": "成交量 ≥ 20日均量的 0.40 倍",
                            "通過": bool(_s89_volume_stabilizing),
                        },
                    ],
                    "low_turn_signals": _s87_bottom_signals,
                    "low_turn_details": [
                        {
                            "項目": "今日是否轉強",
                            "實際值": f"{_s20_today_pct:+.2f}%",
                            "成立門檻": "今日漲跌幅 > 0%",
                            "通過": bool(_s20_today_pct > 0),
                        },
                        {
                            "項目": "是否站回5日均線",
                            "實際值": (
                                f"現價 {_s19_price:,.2f}／MA5 {_s20_ma5:,.2f}"
                                if _s20_ma5 > 0 else "MA5 缺資料"
                            ),
                            "成立門檻": "現價 ≥ MA5",
                            "通過": bool(_s20_ma5 > 0 and _s19_price >= _s20_ma5),
                        },
                        {
                            "項目": "是否收復近2日高點",
                            "實際值": (
                                f"現價 {_s19_price:,.2f}／近2日高點 {_s20_recent_2d_high:,.2f}"
                                if '_s20_recent_2d_high' in locals() else f"現價 {_s19_price:,.2f}"
                            ),
                            "成立門檻": "現價 > 最近2個交易日高點",
                            "通過": bool(_s20_reclaim_2d),
                        },
                        {
                            "項目": "近3日跌勢是否收斂",
                            "實際值": (
                                f"{_s19_ret3:+.2f}%"
                                if _s19_ret3 is not None else "近3日資料不足"
                            ),
                            "成立門檻": "近3日報酬 ≥ -1.50%",
                            "通過": bool(
                                _s19_ret3 is not None and _s19_ret3 >= -1.5
                            ),
                        },
                        {
                            "項目": "量能是否不極度萎縮",
                            "實際值": (
                                f"{_s19_volume_ratio:.2f} 倍"
                                if _s19_volume_ratio is not None else "量能比缺資料"
                            ),
                            "成立門檻": "成交量 ≥ 20日均量的 0.55 倍",
                            "通過": bool(
                                _s19_volume_ratio is not None
                                and _s19_volume_ratio >= 0.55
                            ),
                        },
                    ],
                    "low_zone_low": _s87_low_zone_low,
                    "low_zone_high": _s87_low_zone_high,
                    "in_low_zone": _s87_in_low_zone,
                    "turn_probe": _s21_turn_probe,
                    "formal_entry": _s21_formal_entry,
                    "in_entry_zone": _s21_in_entry_zone,
                    "near_ma20": _s21_near_ma20,
                    "early_turn": _s21_early_turn,
                    # Beta v2：未持股關鍵價位
                    "beta_entry_low": _s14_進場區_low,
                    "beta_entry_high": _s14_進場區_high,
                    "beta_confirm_price": _s12_confirm,
                    "beta_invalidation_price": (
                        min(
                            float(_s12_stop or 0),
                            float(_s14_進場區_low or 0) - tick_size(float(_s14_進場區_low or 0))
                        )
                        if float(_s14_進場區_low or 0) > 0
                        else float(_s12_stop or 0)
                    ),
                    "beta_pullback_low": _beta5_pullback_low,
                    "beta_pullback_high": _beta5_pullback_high,
                    "beta_probe_trigger": _beta5_probe_trigger,
                    "beta_probe_available": _beta6_probe_available,
                    "beta_pullback_seen": _beta7_pullback_seen,
                    "beta_pullback_days_ago": _beta7_pullback_days_ago,
                    "beta_reclaim_triggered": _beta7_reclaim_triggered,
                    "beta_prev_price": _beta84d_prev_price,
                    "beta_cross_up_today": _beta84d_cross_up,
                    "beta_probe_latched": _beta84_latched_probe,
                    "beta_intraday_invalid": _beta84_invalid_now,
                    "beta_stable_invalidation": _beta84_invalidation,
                    "beta_strong_breakout": _beta5_confirm,
                }
                # v8.9b：每檔股票各自保存最新版 position plan，避免切換自選股時沿用上一檔舊介面/舊資料。
                _s18_position_stock_key = str(res.get("stock_id", stock_input) or stock_input).strip()
                st.session_state["_stockpilot4_s18_position"] = _s18_position_plan
                st.session_state[f"_stockpilot4_s18_position_{_s18_position_stock_key}"] = _s18_position_plan

                st.session_state["_stockpilot4_shadow"] = shadow_v4
                st.session_state["_stockpilot4_shadow_error"] = None

            except Exception as exc:
                _price_fix_note = ""
                _price_fix_rows = st.session_state.get(
                    "_stockpilot_shadow_price_order_fixes", []
                ) or []
                if _price_fix_rows:
                    _price_fix_note = (
                        f" | price-order fixes applied: {len(_price_fix_rows)}"
                    )
                _price_input_dbg = st.session_state.get(
                    "_stockpilot_shadow_price_order_input", {}
                ) or {}
                _price_input_note = (
                    " | input "
                    f"structural_exit={_price_input_dbg.get('structural_exit')}, "
                    f"moving_protection={_price_input_dbg.get('moving_protection')}"
                    if _price_input_dbg else ""
                )
                shadow_v4_error = (
                    f"{type(exc).__name__}: {exc}"
                    f"{_price_fix_note}{_price_input_note}"
                )
                log_error("StockPilot 4.0 shadow hook", exc)
                shadow_v4 = None
                st.session_state["_stockpilot4_shadow"] = None
                st.session_state["_stockpilot4_shadow_error"] = shadow_v4_error
                st.session_state["_stockpilot4_s18_position"] = {}
                _s18_error_stock_key = str(res.get("stock_id", stock_input) or stock_input).strip()
                st.session_state[f"_stockpilot4_s18_position_{_s18_error_stock_key}"] = {}

        # 3.3 正式 Decision Center：永遠執行，不依賴 Shadow 成敗。
        compass = decision_snapshot["compass"]
        decision_engine = decision_snapshot["market"]
        portfolio_engine = decision_snapshot["portfolio"]

        compass["decision"] = strategy_state["action"]
        compass["strategy"] = "正式趨勢：" + strategy_state["state_label"]
        compass["action"] = strategy_state["change_note"]
        compass["today"] = strategy_state["today_change"]
        compass["confidence"] = strategy_state["trend_score"]

        decision_color = strategy_state["color"]

        # v8.9b：只讀取「目前這一檔股票」自己的最新版操作資料。
        # 不再使用上一檔股票殘留的全域 position plan，確保所有自選股切換後都進入同一套新版 UI。
        _stock_name_beta = str(res.get("stock_name", "") or "").strip()
        _stock_id_beta = str(res.get("stock_id", stock_input) or stock_input).strip()
        _p18 = st.session_state.get(
            f"_stockpilot4_s18_position_{_stock_id_beta}",
            {}
        ) or {}
        _current_price_beta = float(res.get("current_price", 0) or 0)

        st.markdown(
            f"""
            <div style="
                border:1px solid #d9dee8;
                border-radius:14px;
                padding:18px 22px;
                margin:8px 0 20px 0;
                background:#ffffff;
            ">
              <div style="font-size:13px;color:#64748b;font-weight:700;">目前分析個股</div>
              <div style="font-size:30px;font-weight:900;color:#0f172a;margin-top:4px;">
                {_stock_name_beta} <span style="color:#2563eb;">({_stock_id_beta})</span>
              </div>
              <div style="font-size:17px;color:#475569;margin-top:6px;">
                現價 <b style="font-size:22px;color:#0f172a;">{_current_price_beta:,.2f}</b> 元
              </div>
            </div>
            """,
            unsafe_allow_html=True
        )

        st.markdown("## 最新操作建議")

        if not _p18:
            _decision_module_error = str(
                st.session_state.get("_stockpilot4_shadow_error", "")
                or "未知錯誤：決策模組未產生 position plan"
            )
            st.error(
                "最新操作模組計算失敗。以下為真正錯誤：\n\n"
                + _decision_module_error
            )
            with st.expander("查看完整決策模組錯誤"):
                st.code(_decision_module_error, language="text")
                st.caption(
                    "請直接把這一段錯誤訊息貼回來；不需要再到 Streamlit Manage app 找 logs。"
                )
        else:
            _decision_now = str(_p18.get("trade_decision", "等待"))
            _decision_reason = str(_p18.get("trade_reason", "等待更多確認。"))
            _state_now = str(_p18.get("early_entry_state_zh", ""))

            c1, c2, c3 = st.columns([1.3, 1, 1])
            with c1:
                st.metric("目前操作", _decision_now)
            with c2:
                st.metric("個股", f"{_stock_name_beta}（{_stock_id_beta}）")
            with c3:
                st.metric("資料完整度", f"{int(decision_snapshot.get('data_reliability', 0) or 0)}%")

            st.info(f"一句話判斷：{_decision_reason}")

            # 未持股：只顯示進場相關資訊
            if not user_holding or int(_p18.get("current_shares", 0) or 0) <= 0:
                _b_low = float(_p18.get("beta_entry_low", 0) or 0)
                _b_high = float(_p18.get("beta_entry_high", 0) or 0)
                _b_confirm = float(_p18.get("beta_confirm_price", 0) or 0)
                _b_invalid = float(_p18.get("beta_invalidation_price", 0) or 0)
                _er = float(_p18.get("early_entry_ratio", 0) or 0)

                st.markdown("### 未持股進場判斷")
                u1, u2, u3, u4 = st.columns(4)
                u1.metric("進場狀態", _state_now or _decision_now)
                u2.metric("短線進場條件完成度", f"{_er*100:.0f}%")
                u3.metric(
                    "止跌雷達",
                    f"{int(_p18.get('stabilization_score', 0) or 0)}/5"
                )
                u4.metric(
                    "低位轉折訊號",
                    f"{int(_p18.get('low_turn_score', 0) or 0)}/5"
                )

                st.caption(
                    "「止跌雷達」是最早期警報；"
                    "「低位轉折訊號」確認止跌後是否開始轉強；"
                    "「短線進場條件完成度」表示目前短線條件距可執行進場還差多少；主趨勢仍由日線判斷。三者用途不同。"
                )

                if _state_now == "等待拉回":
                    st.warning("目前已有起漲結構，但位置偏高，不適合追價。")
                elif _state_now in {"低位觀察", "低位觀察・暫不試單"}:
                    st.warning("股價已進入低位承接區，但目前仍以觀察為主，尚未形成可執行的試單訊號。")
                elif _state_now == "止跌形成":
                    st.info("股價仍在低位區，但已開始出現較早期的止跌訊號；先提高注意，不急著下單。")
                elif _state_now == "低位轉折確認中":
                    st.info("低位轉折訊號正在累積確認；目前先不下單，避免因單一分鐘反彈就追進。")
                elif _state_now == "低位試單":
                    st.success("低位轉折訊號已通過穩定確認，可用很小部位試單；正式趨勢尚未翻多。")
                elif _state_now == "轉強試單":
                    st.success("反轉結構與短線動能已轉強，可小部位試單，正式確認後再加碼。")
                elif _state_now == "正式進場":
                    st.success("目前已達正式進場條件，可建立第一筆主要部位。")
                elif _state_now == "不宜進場":
                    st.warning(
                        "目前暫不進場；短線條件已接近成熟，但仍有風控條件尚未解除。"
                    )
                else:
                    st.info("目前繼續觀察，等待新的價格或動能觸發。")

                _pb_low = float(_p18.get("beta_pullback_low", _b_low) or 0)
                _pb_high = float(_p18.get("beta_pullback_high", _b_high) or 0)
                _probe_trigger = float(_p18.get("beta_probe_trigger", 0) or 0)
                _strong_breakout = float(_p18.get("beta_strong_breakout", _b_confirm) or 0)

                # v9.17.1：未持股顯示分級。
                # 不改正式決策引擎，只把「不宜進場」拆成硬否決與接近觸發兩種情況，
                # 避免條件已接近成熟時仍顯示過度負面的文字。
                _entry_display_state_v98 = _state_now or _decision_now
                # v9.17.1：硬風險否決改為「原因清單」，不能只顯示 True/False。
                _entry_veto_reasons_v99 = []

                if _p18.get("beta_intraday_invalid"):
                    _entry_veto_reasons_v99.append("盤中進場條件已失效")

                if (
                    _b_invalid > 0
                    and _current_price_beta > 0
                    and _current_price_beta < _b_invalid
                ):
                    _entry_veto_reasons_v99.append(
                        f"現價跌破進場失效價 {_b_invalid:,.2f} 元"
                    )

                # low_hard_veto 是綜合否決旗標，往下拆成目前可辨識的實際來源。
                if _p18.get("low_hard_veto"):
                    _formal_trend_state_v99 = str(
                        strategy_state.get("state", "") or ""
                    )
                    _formal_trend_label_v99 = str(
                        strategy_state.get("state_label", "") or ""
                    )

                    if _formal_trend_state_v99 in {"BEAR", "STRONG_BEAR", "WEAK_BEAR"}:
                        _entry_veto_reasons_v99.append(
                            f"日線主趨勢仍為「{_formal_trend_label_v99 or '弱勢空頭'}」"
                        )

                    _chip_v99 = decision_snapshot.get("chip_engine", {}) or {}
                    if bool(_chip_v99.get("veto")):
                        _entry_veto_reasons_v99.append("籌碼條件仍有否決訊號")

                    _vol_v99 = decision_snapshot.get("volume_engine", {}) or {}
                    if bool(_vol_v99.get("veto")):
                        _entry_veto_reasons_v99.append("量價條件仍有否決訊號")

                    _regime_v99 = decision_snapshot.get("regime", {}) or {}
                    _regime_gate_v99 = str(_regime_v99.get("gate", "") or "")
                    if _regime_gate_v99 in {"PANIC", "RISK_OFF"}:
                        _entry_veto_reasons_v99.append(
                            f"大盤風險門目前為 {_regime_gate_v99}"
                        )

                    if not _entry_veto_reasons_v99:
                        _entry_veto_reasons_v99.append(
                            "綜合低位試單風控尚未解除"
                        )

                # 去重複，保留顯示順序
                _entry_veto_reasons_v99 = list(
                    dict.fromkeys(_entry_veto_reasons_v99)
                )
                _entry_hard_veto_v98 = bool(_entry_veto_reasons_v99)
                _entry_near_probe_v98 = bool(
                    _probe_trigger > 0
                    and _current_price_beta > 0
                    and _current_price_beta < _probe_trigger
                    and ((_probe_trigger / _current_price_beta) - 1) <= 0.03
                )
                _entry_early_ok_v98 = bool(
                    int(_p18.get("stabilization_score", 0) or 0) >= 3
                    and int(_p18.get("low_turn_score", 0) or 0) >= 3
                )
                if (
                    _entry_display_state_v98 == "不宜進場"
                    and not _entry_hard_veto_v98
                    and _entry_near_probe_v98
                    and _entry_early_ok_v98
                ):
                    _entry_display_state_v98 = "等待轉強確認・暫不進場"

                if (_state_now or _decision_now) == "正式進場":
                    _formal_trend_upper_v915 = str(strategy_state.get("state", "") or "").upper()
                    _formal_trend_label_v915 = str(strategy_state.get("state_label", "") or "")
                    _entry_path_v915 = (
                        "逆勢轉強進場"
                        if _formal_trend_upper_v915 in {"BEAR", "STRONG_BEAR", "WEAK_BEAR"}
                        or "空頭" in _formal_trend_label_v915
                        else "順勢確認進場"
                    )
                    st.success(
                        f"進場型態：{_entry_path_v915}。"
                        "主決策已符合正式進場條件；低位承接雷達僅作為後續拉回低接／加碼參考。"
                    )

                st.markdown("### 決策穩定器")
                _stable_trend_label = str(
                    strategy_state.get("state_label", "資料不足") or "資料不足"
                )
                _stable_strategy_label = _entry_display_state_v98
                if _p18.get("beta_probe_latched"):
                    _stable_trigger_label = "已觸發・本日鎖定"
                elif _p18.get("beta_intraday_invalid"):
                    _stable_trigger_label = "失效條件成立"
                else:
                    _stable_trigger_label = "尚未觸發"

                _st1, _st2, _st3 = st.columns(3)
                _st1.metric("主趨勢（日線）", _stable_trend_label)
                _st2.metric("目前策略", _stable_strategy_label)
                _st3.metric("盤中觸發", _stable_trigger_label)

                if _entry_display_state_v98 != (_state_now or _decision_now):
                    st.info(
                        f"目前策略細分：{_entry_display_state_v98}。"
                        "早期止跌與低位轉折條件已接近成熟，但尚未完成試單確認。"
                    )

                st.caption(
                    "趨勢決定方向，盤中價格只決定是否到達執行點。"
                    "一旦拉回轉強試單成立，當日不因觸發價附近的小幅震盪反覆改變；"
                    "只有跌破進場失效價才解除。"
                )
                st.caption(
                    "短線進場條件完成度與低位轉折訊號分開計算："
                    "前者偏向正式進場，後者用來避免錯過低點附近的早期止跌。"
                )
                if _p18.get("beta_probe_latched"):
                    st.caption("本日鎖定來源：今天已確認由觸發價下方重新站上。")
                else:
                    st.caption("本日尚未建立新的試單鎖定。")

                _formal_entry_v915 = (_state_now or _decision_now) == "正式進場"
                if _formal_entry_v915:
                    st.markdown("### 正式進場交易計畫")

                    _entry_low_v915 = float(_b_low or _pb_low or _current_price_beta or 0)
                    _entry_high_v915 = float(_b_high or _pb_high or _current_price_beta or 0)
                    _chase_cap_v915 = float(
                        _probe_trigger if _probe_trigger > _entry_high_v915
                        else (_strong_breakout if _strong_breakout > _entry_high_v915 else _entry_high_v915)
                    )
                    _raw_exit1_v917 = float(_p18.get("target1", 0) or 0)
                    _raw_exit2_v917 = float(_p18.get("target2", 0) or 0)
                    _structural_exit_v917 = float(
                        _p18.get("structural_exit", 0) or _b_invalid or 0
                    )
                    _raw_moving_v917 = float(_p18.get("moving_protection", 0) or 0)

                    # v9.17.1.1：先建立進場區中位數，再進行目標價合理性檢查。
                    _entry_mid_v915 = (
                        (_entry_low_v915 + _entry_high_v915) / 2
                        if _entry_low_v915 > 0 and _entry_high_v915 > 0
                        else float(_current_price_beta or 0)
                    )

                    # 正式進場後，獲利目標必須高於可執行買進基準。
                    # 若舊 target1 已被現價突破，不再把它當作「未來出場價」。
                    _entry_reference_v917 = max(
                        float(_current_price_beta or 0),
                        float(_entry_mid_v915 or 0),
                    )
                    _exit_candidates_v917 = sorted({
                        float(v) for v in (
                            _raw_exit1_v917,
                            _raw_exit2_v917,
                            float(_strong_breakout or 0),
                        )
                        if float(v or 0) > _entry_reference_v917
                    })
                    _exit1_v915 = _exit_candidates_v917[0] if _exit_candidates_v917 else 0.0
                    _exit2_v915 = _exit_candidates_v917[1] if len(_exit_candidates_v917) > 1 else 0.0
                    _old_target_passed_v917 = bool(
                        _raw_exit1_v917 > 0
                        and _entry_reference_v917 >= _raw_exit1_v917
                    )

                    # 交易防守與結構失效分離。交易防守優先使用較近的移動防守；
                    # 若它不存在或離正式進場基準過遠，改以進場區下緣附近建立風控參考。
                    _trade_defense_candidates_v917 = [
                        float(v) for v in (
                            _raw_moving_v917,
                            float(_entry_low_v915 or 0) * 0.985 if _entry_low_v915 > 0 else 0,
                        )
                        if 0 < float(v or 0) < _entry_reference_v917
                    ]
                    _trade_defense_v917 = (
                        max(_trade_defense_candidates_v917)
                        if _trade_defense_candidates_v917 else 0.0
                    )
                    _defense_v915 = _trade_defense_v917
                    _force_exit_v915 = _structural_exit_v917

                    _risk_v915 = _entry_reference_v917 - _defense_v915 if _entry_reference_v917 > _defense_v915 > 0 else 0
                    _reward_v915 = _exit1_v915 - _entry_reference_v917 if _exit1_v915 > _entry_reference_v917 > 0 else 0
                    _rr_v915 = _reward_v915 / _risk_v915 if _risk_v915 > 0 and _reward_v915 > 0 else 0

                    q1,q2,q3 = st.columns(3)
                    q1.metric("建議進場區間", f"{_entry_low_v915:,.2f}～{_entry_high_v915:,.2f} 元" if _entry_low_v915 > 0 and _entry_high_v915 > 0 else "待建立")
                    q2.metric("追價上限", f"{_chase_cap_v915:,.2f} 元" if _chase_cap_v915 > 0 else "待建立")
                    q3.metric("目前價格", f"{_current_price_beta:,.2f} 元" if _current_price_beta > 0 else "待取得")

                    x1,x2,x3 = st.columns(3)
                    x1.metric(
                        "第一出場價",
                        f"{_exit1_v915:,.2f} 元" if _exit1_v915 > 0
                        else ("原第一目標已突破・待建立新目標" if _old_target_passed_v917 else "待建立")
                    )
                    x2.metric("第二出場價", f"{_exit2_v915:,.2f} 元" if _exit2_v915 > _exit1_v915 > 0 else "待建立")
                    x3.metric("交易防守價", f"{_defense_v915:,.2f} 元" if _defense_v915 > 0 else "待建立")

                    if _force_exit_v915 > 0:
                        st.caption(f"結構失效價：{_force_exit_v915:,.2f} 元（中期結構完全破壞的底線，與交易防守價分開）")
                    if _old_target_passed_v917:
                        st.info(
                            f"原第一目標 {_raw_exit1_v917:,.2f} 元已被目前價格突破，"
                            "不再作為新進場部位的獲利出場價；系統改採下一個仍高於目前價格的有效目標。"
                        )

                    if _entry_low_v915 > 0 and _entry_high_v915 > 0 and _current_price_beta > 0:
                        if _entry_low_v915 <= _current_price_beta <= _entry_high_v915:
                            _entry_position_v915 = "目前位於建議進場區，可建立第一筆部位。"
                        elif _current_price_beta < _entry_low_v915:
                            _entry_position_v915 = "目前低於建議進場區，先確認沒有持續破底，再評估進場。"
                        elif _chase_cap_v915 > 0 and _current_price_beta <= _chase_cap_v915:
                            _entry_position_v915 = "目前高於理想進場區、但尚未超過追價上限；不宜一次追滿部位。"
                        else:
                            _entry_position_v915 = "目前已超過追價上限；正式進場訊號仍成立，但先不要追價。"
                        st.info("目前位置：" + _entry_position_v915)

                    if _rr_v915 > 0:
                        st.caption(f"第一目標風險報酬比約 1：{_rr_v915:.2f}")
                    if _force_exit_v915 > 0 and _force_exit_v915 != _defense_v915:
                        st.caption(f"結構強制失效價：{_force_exit_v915:,.2f} 元")

                    st.caption(
                        "正式進場後，試單觸發價退居參考，不再作為買進前置門檻；"
                        "主畫面改以進場區間、追價上限、獲利出場與防守價格管理交易。"
                    )
                else:
                    st.markdown("### 關鍵價位")

                # v8.6：價格門檻與「是否可進場」分開顯示。
                # 現價碰到某個價位，只代表價格路徑到位；正式策略仍由日線趨勢、
                # 動能、風控與既有決策穩定器共同決定，避免盤中來回翻單。
                _price_now_v86 = float(_current_price_beta or 0)

                if _pb_low > 0 and _pb_high > 0:
                    if _price_now_v86 < _pb_low:
                        _pullback_status_v86 = "尚未進入・位於區間下方"
                    elif _price_now_v86 <= _pb_high:
                        _pullback_status_v86 = "已進入觀察區"
                    else:
                        _pullback_status_v86 = "已高於觀察區"
                else:
                    _pullback_status_v86 = "待建立"

                if _strong_breakout > 0 and _price_now_v86 > 0:
                    _break_price_hit_v86 = _price_now_v86 >= _strong_breakout
                    _break_price_status_v86 = "已突破價格門檻" if _break_price_hit_v86 else "尚未突破價格門檻"
                else:
                    _break_price_hit_v86 = False
                    _break_price_status_v86 = "待建立"

                # 價格突破不等於有效突破；只有正式進場/轉強試單或本日已鎖定，
                # 才把突破有效性標成已確認。
                _break_effective_v86 = bool(
                    _break_price_hit_v86
                    and (
                        _state_now in {"正式進場", "轉強試單"}
                        or _p18.get("beta_probe_latched")
                    )
                )
                if not _break_price_hit_v86:
                    _break_effective_status_v86 = "尚未到價"
                elif _break_effective_v86:
                    _break_effective_status_v86 = "有效性已確認"
                else:
                    _break_effective_status_v86 = "價格已過・有效性未確認"

                if not _formal_entry_v915:
                    p1, p2, p3 = st.columns(3)
                    _low_zone_low_v87 = float(_p18.get("low_zone_low", 0) or 0)
                    _low_zone_high_v87 = float(_p18.get("low_zone_high", 0) or 0)

                    if (
                        _low_zone_low_v87 > 0
                        and _low_zone_high_v87 > 0
                        and _price_now_v86 < _pb_low
                    ):
                        p1.metric(
                            "低位承接區",
                            f"{_low_zone_low_v87:,.2f}～{_low_zone_high_v87:,.2f} 元"
                        )
                        p1.caption(
                            "目前狀態："
                            + ("已進入低位區" if _p18.get("in_low_zone") else "接近低位區")
                        )
                        st.caption(
                            f"原轉強觀察區：{_pb_low:,.2f}～{_pb_high:,.2f} 元"
                        )
                    else:
                        p1.metric(
                            "拉回觀察區",
                            f"{_pb_low:,.2f}～{_pb_high:,.2f} 元"
                            if _pb_low > 0 and _pb_high > 0 else "待建立"
                        )
                        p1.caption(f"目前狀態：{_pullback_status_v86}")

                    _probe_available_v916 = bool(
                        _p18.get("beta_probe_available", True)
                    )
                    _probe_ready_v916 = bool(
                        _probe_available_v916 and _probe_trigger > 0
                    )
                    _probe_not_needed_v916 = bool(
                        (_state_now or _decision_now) == "正式進場"
                        or _p18.get("formal_entry")
                    )

                    p2.metric(
                        "試單觸發價",
                        f"{_probe_trigger:,.2f} 元"
                        if _probe_ready_v916
                        else (
                            "本策略不需要"
                            if _probe_not_needed_v916
                            else "尚未建立"
                        )
                    )

                    if _probe_ready_v916:
                        if _p18.get("beta_probe_latched"):
                            p2.caption("目前狀態：已完成拉回後重新站上・本日鎖定")
                        elif _p18.get("beta_pullback_seen"):
                            p2.caption("目前狀態：曾拉回，等待真正重新站上")
                        else:
                            p2.caption("目前狀態：尚未完成先拉回、後站回的順序")
                    elif _probe_not_needed_v916:
                        p2.caption(
                            "目前狀態：主策略已進入正式進場，"
                            "不再需要中間試單層。"
                        )
                    else:
                        p2.caption(
                            "目前狀態：尚未形成試單條件；"
                            "待止跌／轉強條件改善後，系統才會建立試單觸發價。"
                        )

                    p3.metric(
                        "突破價格門檻",
                        f"{_strong_breakout:,.2f} 元" if _strong_breakout > 0 else "待建立"
                    )
                    p3.caption(
                        f"目前狀態：{_break_price_status_v86}；{_break_effective_status_v86}"
                    )

                    st.caption(
                        f"進場條件失效價：{_b_invalid:,.2f} 元"
                        if _b_invalid > 0 else "進場條件失效價：待建立"
                    )

                    # v9.17.1：直接告訴使用者距離下一個可執行門檻還有多少。
                    if _probe_trigger > 0 and _price_now_v86 > 0 and _price_now_v86 < _probe_trigger:
                        _probe_gap_pct_v98 = (_probe_trigger / _price_now_v86 - 1) * 100
                        st.info(
                            f"距下一步：距試單確認價 {_probe_trigger:,.2f} 元尚差 "
                            f"{_probe_gap_pct_v98:.2f}%｜"
                            f"若站穩 {_probe_trigger:,.2f} 元，且低位轉折維持 ≥3/5，"
                            "再升級為試單評估。"
                        )
                    elif _probe_trigger > 0 and _price_now_v86 >= _probe_trigger and not _p18.get("beta_probe_latched"):
                        st.info(
                            f"距下一步：現價已到達 {_probe_trigger:,.2f} 元以上，"
                            "但仍須完成拉回後重新站上與穩定確認，才升級為試單評估。"
                        )

                if _entry_hard_veto_v98 and (_state_now or _decision_now) != "正式進場":
                    st.error(
                        "目前否決原因："
                        + "；".join(_entry_veto_reasons_v99)
                    )

                    # v9.17.1：升級條件改成「已完成 / 待完成」雙清單，
                    # 不再把現價已經達成的價格條件重複列成待完成。
                    _entry_done_v913 = []
                    _entry_pending_v913 = []

                    _trend_veto_present_v913 = any(
                        "日線主趨勢仍為" in str(_r)
                        for _r in _entry_veto_reasons_v99
                    )
                    _chip_veto_present_v913 = any(
                        "籌碼條件仍有否決訊號" in str(_r)
                        for _r in _entry_veto_reasons_v99
                    )
                    _volume_veto_present_v913 = any(
                        "量價條件仍有否決訊號" in str(_r)
                        for _r in _entry_veto_reasons_v99
                    )
                    _market_veto_present_v913 = any(
                        "大盤風險門目前為" in str(_r)
                        for _r in _entry_veto_reasons_v99
                    )

                    # 日線趨勢否決
                    if _trend_veto_present_v913:
                        _entry_pending_v913.append("日線弱勢空頭否決解除")
                    else:
                        _entry_done_v913.append("日線趨勢無硬否決")

                    # 籌碼 / 量價 / 大盤否決
                    if _chip_veto_present_v913:
                        _entry_pending_v913.append("籌碼否決訊號解除")
                    else:
                        _entry_done_v913.append("籌碼無硬否決")

                    if _volume_veto_present_v913:
                        _entry_pending_v913.append("量價否決訊號解除")
                    else:
                        _entry_done_v913.append("量價無硬否決")

                    if _market_veto_present_v913:
                        _entry_pending_v913.append("大盤風險門恢復至可承作狀態")
                    else:
                        _entry_done_v913.append("大盤風險門未關閉")

                    # 試單確認價：只有真的低於觸發價才列待完成
                    if _probe_trigger > 0 and _price_now_v86 > 0:
                        if _price_now_v86 >= _probe_trigger:
                            _entry_done_v913.append(
                                f"價格已站上試單確認價 {_probe_trigger:,.2f} 元"
                            )
                        else:
                            _entry_pending_v913.append(
                                f"站穩試單確認價 {_probe_trigger:,.2f} 元"
                            )

                    # 低位轉折
                    _low_turn_now_v913 = int(_p18.get("low_turn_score", 0) or 0)
                    if _low_turn_now_v913 >= 3:
                        _entry_done_v913.append(
                            f"低位轉折訊號 {_low_turn_now_v913}/5，已達至少 3/5"
                        )
                    else:
                        _entry_pending_v913.append(
                            f"低位轉折訊號由 {_low_turn_now_v913}/5 提升至至少 3/5"
                        )

                    # 止跌雷達
                    _stabilization_now_v913 = int(
                        _p18.get("stabilization_score", 0) or 0
                    )
                    if _stabilization_now_v913 >= 3:
                        _entry_done_v913.append(
                            f"止跌雷達 {_stabilization_now_v913}/5，已達至少 3/5"
                        )
                    else:
                        _entry_pending_v913.append(
                            f"止跌雷達由 {_stabilization_now_v913}/5 提升至至少 3/5"
                        )

                    # 盤中失效 / 進場失效價
                    if _p18.get("beta_intraday_invalid"):
                        _entry_pending_v913.append(
                            "盤中失效條件解除並重新形成有效觸發"
                        )

                    if (
                        _b_invalid > 0
                        and _price_now_v86 > 0
                        and _price_now_v86 < _b_invalid
                    ):
                        _entry_pending_v913.append(
                            f"重新站回進場失效價 {_b_invalid:,.2f} 元以上"
                        )

                    # 去重
                    _entry_done_v913 = list(dict.fromkeys(_entry_done_v913))
                    _entry_pending_v913 = list(dict.fromkeys(_entry_pending_v913))

                    if _entry_done_v913:
                        st.success(
                            "✅ 已完成："
                            + "；".join(_entry_done_v913)
                        )

                    if _entry_pending_v913:
                        st.warning(
                            "❌ 待完成："
                            + "；".join(_entry_pending_v913)
                        )
                        st.info(
                            "目前真正還差："
                            + "；".join(_entry_pending_v913)
                            + " → 全部完成後，才進入低位試單評估。"
                        )
                    else:
                        if (_state_now or _decision_now) == "正式進場":
                            st.success(
                                "正式進場條件已成立；低位承接條件僅供後續加碼機會判斷。"
                            )
                        else:
                            st.success(
                                "目前升級條件已全部完成，可進一步進入低位試單評估。"
                            )

                
                # v8.9e：所有未持股股票都顯示同一套完整新版雷達介面。
                # 是否進入低位區只影響判斷，不再影響 UI 是否出現。
                _low_score_v87 = int(_p18.get("low_turn_score", 0) or 0)
                _low_sig_v87 = _p18.get("low_turn_signals", {}) or {}
                _passed_v87 = [k for k, v in _low_sig_v87.items() if v]
                _low_count_v88 = int(_p18.get("low_probe_candidate_count", 0) or 0)
                _low_latched_v88 = bool(_p18.get("low_probe_latched"))
                _low_invalid_v88 = bool(_p18.get("low_probe_invalid_now"))
                _in_low_context_v89e = bool(
                    _p18.get("low_watch")
                    or _p18.get("low_probe")
                    or _p18.get("in_low_zone")
                )

                _low_context_note_v89e = (
                    "｜目前已進入低位監控區。"
                    if _in_low_context_v89e
                    else (
                        "｜目前未形成額外低位承接／加碼機會；不影響既有正式進場判斷。"
                        if (_state_now or _decision_now) == "正式進場"
                        else "｜目前尚未進入低位承接區，雷達持續監控。"
                    )
                )

                _low_radar_label_v914 = (
                    "低位承接機會："
                    if (_state_now or _decision_now) == "正式進場"
                    else "低位轉折雷達："
                )

                st.info(
                    _low_radar_label_v914
                    + f"{_low_score_v87}/5 項成立。"
                    + (
                        " 已出現：" + "、".join(_passed_v87)
                        if _passed_v87 else " 尚未出現明確轉折訊號"
                    )
                    + _low_context_note_v89e
                    + (
                        "｜目前仍有硬風險否決：暫不試單。"
                        if _p18.get("low_hard_veto") else ""
                    )
                    + (
                        ""
                        if (_state_now or _decision_now) == "正式進場"
                        else (
                            "｜低位試單訊號已鎖定。"
                            if _low_latched_v88
                            else (
                                f"｜候選連續確認 {_low_count_v88}/2。"
                                if _p18.get("low_probe_candidate") else ""
                            )
                        )
                    )
                    + (
                        "｜目前已觸發低位試單失效條件。"
                        if _low_invalid_v88 else ""
                    )
                )

                _stabilization_details_v89 = _p18.get("stabilization_details", []) or []
                with st.expander("查看止跌雷達 5 項明細"):
                    if _stabilization_details_v89:
                        _st_rows_v89 = []
                        for _item in _stabilization_details_v89:
                            _st_rows_v89.append({
                                "條件": str(_item.get("項目", "")),
                                "目前實際值": str(_item.get("實際值", "")),
                                "成立門檻": str(_item.get("成立門檻", "")),
                                "結果": "✅ 通過" if bool(_item.get("通過")) else "❌ 未通過",
                            })
                        st.dataframe(
                            pd.DataFrame(_st_rows_v89),
                            use_container_width=True,
                            hide_index=True,
                        )
                    else:
                        st.caption("目前沒有可用的止跌雷達明細資料。")
                    st.caption(
                        "止跌雷達是最早期警報，只負責發現跌勢可能正在停止；"
                        "分數提高不代表可以直接買進。"
                    )

                _low_details_v88a = _p18.get("low_turn_details", []) or []
                with st.expander("查看低位轉折 5 項明細"):
                    if _low_details_v88a:
                        _low_rows_v88a = []
                        for _item in _low_details_v88a:
                            _low_rows_v88a.append({
                                "條件": str(_item.get("項目", "")),
                                "目前實際值": str(_item.get("實際值", "")),
                                "成立門檻": str(_item.get("成立門檻", "")),
                                "結果": "✅ 通過" if bool(_item.get("通過")) else "❌ 未通過",
                            })
                        st.dataframe(
                            pd.DataFrame(_low_rows_v88a),
                            use_container_width=True,
                            hide_index=True,
                        )
                    else:
                        st.caption("目前沒有可用的低位轉折明細資料。")
                    st.caption(
                        "低位轉折訊號用來確認止跌後是否開始轉強；"
                        "不是正式趨勢分數，也不代表單一條件通過就能買進。"
                    )

                if _break_price_hit_v86 and not _break_effective_v86:
                    st.info(
                        "價格雖已站上突破門檻，但這不等於買進訊號。"
                        f"目前主趨勢為「{_stable_trend_label}」、策略為「{_stable_strategy_label}」，"
                        "仍須由日線趨勢、動能與風控條件確認；不因盤中價格單獨越過門檻就追價。"
                    )
                elif _pullback_status_v86 == "已進入觀察區" and _state_now not in {"低檔試單", "轉強試單", "正式進場"}:
                    st.info(
                        "現價已進入拉回觀察區，但「進入觀察區」只代表開始監看，"
                        "尚未等於進場；仍等待止跌、重新轉強或正式確認條件。"
                    )

                if _p18.get("beta_probe_available", True) and _probe_trigger > 0:
                    if _p18.get("beta_pullback_seen"):
                        _days_ago = _p18.get("beta_pullback_days_ago")
                        st.caption(
                            f"拉回路徑：最近 8 個交易日內已進入過觀察區"
                            + (
                                f"（約 {_days_ago} 個交易日前）"
                                if _days_ago is not None else ""
                            )
                            + "；重新站上試單觸發價後才算轉強試單。"
                        )
                    else:
                        st.caption(
                            "拉回路徑：近期尚未真正進入拉回觀察區；"
                            "即使現價高於試單觸發價，也不會直接視為已觸發試單。"
                        )

                if _state_now == "等待拉回" and _pb_low > 0 and _pb_high > 0:
                    if _p18.get("beta_probe_available", True) and _probe_trigger > 0:
                        st.write(
                            f"**下一步：**先不要追價。若回到 **{_pb_low:,.2f}～{_pb_high:,.2f} 元**附近，進入拉回觀察；"
                            f"若拉回後重新站上 **{_probe_trigger:,.2f} 元**，可評估小部位試單；"
                            f"若完全不回檔而直接走強，則以 **{_strong_breakout:,.2f} 元**作為強勢突破路徑。"
                        )
                    else:
                        st.write(
                            f"**下一步：**先不要追價。若回到 **{_pb_low:,.2f}～{_pb_high:,.2f} 元**附近重新評估；"
                            f"目前沒有足夠空間建立中間試單價，因此若不拉回，直接觀察 "
                            f"**{_strong_breakout:,.2f} 元**的強勢突破。"
                        )
                elif _state_now == "低檔試單":
                    st.write("**下一步：**只用很小部位試單；若短線動能、量價與趨勢持續改善，再升級為轉強試單或正式進場。")
                elif _state_now == "轉強試單":
                    st.write("**下一步：**可建立小部位；若後續正式突破或條件完整度提升，再加碼。")
                elif _state_now == "正式進場":
                    st.write(
                        "**下一步：**依上方正式進場交易計畫的建議進場區建立第一筆部位；"
                        "若已超過追價上限則不追價。進場後依第一／第二出場價分批獲利，"
                        "跌破防守／失效價則執行風控。"
                    )
                elif _entry_display_state_v98 == "等待轉強確認・暫不進場":
                    if _probe_trigger > 0:
                        st.write(
                            f"**下一步：**暫不下單；先等站穩 **{_probe_trigger:,.2f} 元**，"
                            "並確認低位轉折維持 ≥3/5，再進入試單評估。"
                        )
                    else:
                        st.write("**下一步：**暫不下單，等待轉強條件完成後再進入試單評估。")
                elif _state_now == "不宜進場":
                    _next_parts_v99 = []

                    if (
                        _probe_trigger > 0
                        and _price_now_v86 > 0
                        and _price_now_v86 < _probe_trigger
                    ):
                        _next_parts_v99.append(
                            f"站穩 **{_probe_trigger:,.2f} 元**"
                        )

                    if int(_p18.get("low_turn_score", 0) or 0) < 3:
                        _next_parts_v99.append("低位轉折提升至至少 **3/5**")

                    if int(_p18.get("stabilization_score", 0) or 0) < 3:
                        _next_parts_v99.append("止跌雷達提升至至少 **3/5**")

                    if _entry_veto_reasons_v99:
                        if any(
                            "日線主趨勢仍為" in str(_r)
                            for _r in _entry_veto_reasons_v99
                        ):
                            _next_parts_v99.append("日線弱勢空頭否決解除")
                        else:
                            _next_parts_v99.append("目前否決原因解除")

                    _next_parts_v99 = list(dict.fromkeys(_next_parts_v99))

                    if _next_parts_v99:
                        st.write(
                            "**下一步：**暫不下單；"
                            + "、".join(_next_parts_v99)
                            + "，再升級為低位試單評估。"
                        )
                    else:
                        st.write(
                            "**下一步：**主要升級條件已完成，等待系統確認有效觸發後進入低位試單評估。"
                        )
                else:
                    st.write("**下一步：**維持觀察，等待新的短線動能或價格觸發。")

                _md = _p18.get("ma20_distance_pct")
                if _md is not None:
                    st.caption(
                        f"目前股價距20日均線約 {float(_md):+.2f}%；"
                        f"本檔防追價上限約 {float(_p18.get('early_extension_limit_pct', 12) or 12):.1f}%。"
                    )

            # v9.0：已持股完整操作中心
            else:
                st.markdown("### 已持股操作判斷")

                _hold_shares = int(_p18.get("current_shares", 0) or 0)
                _hold_cost = float(user_cost or 0)
                _hold_price = float(_current_price_beta or 0)
                _hold_pnl = _p18.get("unrealized_pnl")
                _hold_pnl_pct = (
                    ((_hold_price / _hold_cost) - 1) * 100
                    if _hold_cost > 0 and _hold_price > 0 else None
                )

                _hold_protect = float(_p18.get("protective_stop", 0) or 0)
                _hold_structural = float(_p18.get("structural_exit", 0) or 0)
                _hold_add = float(_p18.get("add_confirm_price", 0) or 0)
                _hold_target1 = float(_p18.get("target1", 0) or 0)
                _hold_target2 = float(_p18.get("target2", 0) or 0)

                # 持股健康度與訊號一致度：全部沿用同一份正式決策快照。
                _hold_regime = decision_snapshot.get("regime", {}) or {}
                _hold_chip = decision_snapshot.get("chip_engine", {}) or {}
                _hold_volume = decision_snapshot.get("volume_engine", {}) or {}

                _hold_signal_checks = [
                    (
                        "日線趨勢",
                        str(strategy_state.get("state", "")) in {"STRONG_BULL", "BULL_PULLBACK"}
                        or int(strategy_state.get("trend_score", 0) or 0) >= 60
                    ),
                    (
                        "價格仍在保護價上方",
                        _hold_protect <= 0 or _hold_price > _hold_protect
                    ),
                    (
                        "大盤環境未關閉風險",
                        str(_hold_regime.get("gate", "CAUTION")) not in {"PANIC", "RISK_OFF"}
                    ),
                    (
                        "籌碼未出現否決",
                        not bool(_hold_chip.get("veto"))
                    ),
                    (
                        "量價未出現否決",
                        not bool(_hold_volume.get("veto"))
                    ),
                ]
                _hold_signal_score = sum(1 for _, ok in _hold_signal_checks if ok)
                _hold_signal_total = len(_hold_signal_checks)

                if _decision_now == "退場" or (
                    _hold_structural > 0 and _hold_price <= _hold_structural
                ):
                    _hold_health = "危險"
                elif _decision_now == "減碼" or (
                    _hold_protect > 0 and _hold_price <= _hold_protect
                ):
                    _hold_health = "防守"
                elif _hold_signal_score >= 4:
                    _hold_health = "健康"
                elif _hold_signal_score >= 3:
                    _hold_health = "注意"
                else:
                    _hold_health = "防守"

                # 第一列：真正需要先看到的持股資訊
                h1, h2, h3, h4 = st.columns(4)
                h1.metric("目前操作", _decision_now)
                h2.metric("持股健康度", _hold_health)
                h3.metric("訊號一致度", f"{_hold_signal_score}/{_hold_signal_total}")
                h4.metric(
                    "未實現報酬",
                    f"{_hold_pnl_pct:+.2f}%"
                    if _hold_pnl_pct is not None else "未提供成本"
                )

                # 第二列：持股基本資料
                b1, b2, b3, b4 = st.columns(4)
                b1.metric("目前持股", f"{_hold_shares:,} 股")
                b2.metric(
                    "持股成本",
                    f"{_hold_cost:,.2f} 元" if _hold_cost > 0 else "未提供"
                )
                b3.metric("目前股價", f"{_hold_price:,.2f} 元")
                b4.metric(
                    "未實現損益",
                    f"{float(_hold_pnl):+,.0f} 元"
                    if _hold_pnl is not None else "未提供成本"
                )

                # 持股健康度拆解
                _health_parts = []
                for _name, _ok in _hold_signal_checks:
                    _health_parts.append(f"{'✅' if _ok else '⚠️'} {_name}")
                st.caption("持股健康度依據：" + "｜".join(_health_parts))

                # v9.1：已持股改為「出場價格」導向
                # 第一/第二出場價屬獲利了結路徑；防守/強制出場價屬風控路徑。
                _hold_exit1 = _hold_target1 if _hold_target1 > 0 else 0.0
                _hold_exit2 = _hold_target2 if _hold_target2 > _hold_exit1 else 0.0

                # v9.2：加碼價與第一出場價互斥。
                # 加碼後若到第一出場價的剩餘空間不足 5%，不提供加碼訊號，
                # 避免「剛加碼就準備出場」的矛盾。
                _hold_add_raw = _hold_add
                _hold_add_room_pct = (
                    ((_hold_exit1 / _hold_add_raw) - 1.0) * 100.0
                    if _hold_add_raw > 0 and _hold_exit1 > _hold_add_raw else 0.0
                )
                _hold_add_blocked = bool(
                    _hold_add_raw > 0
                    and _hold_exit1 > 0
                    and _hold_add_room_pct < 5.0
                )
                if _hold_add_blocked:
                    _hold_add = 0.0

                # 最終出場價採動態移動停利概念：趨勢延續時由保護價向上跟隨，
                # 不用預先硬算一個永遠不變的遠端價格。
                _hold_final_exit_label = (
                    "動態移動停利"
                    if _hold_protect > 0 else "待建立"
                )

                st.markdown("### 出場價格")
                k1, k2, k3, k4, k5 = st.columns(5)
                k1.metric(
                    "第一出場價",
                    f"{_hold_exit1:,.2f} 元" if _hold_exit1 > 0 else "待建立"
                )
                k2.metric(
                    "第二出場價",
                    f"{_hold_exit2:,.2f} 元" if _hold_exit2 > 0 else "待建立"
                )
                k3.metric("最終出場", _hold_final_exit_label)
                k4.metric(
                    "防守出場價",
                    f"{_hold_protect:,.2f} 元" if _hold_protect > 0 else "待建立"
                )
                k5.metric(
                    "強制出場價",
                    f"{_hold_structural:,.2f} 元" if _hold_structural > 0 else "待建立"
                )

                # 額外保留加碼資訊，但與出場價格做互斥檢查。
                if _hold_add_blocked:
                    st.caption(
                        f"加碼：目前不適用｜原確認價 {_hold_add_raw:,.2f} 元距第一出場價"
                        f"僅剩 {_hold_add_room_pct:.2f}% 空間，風險報酬不足。"
                    )
                elif _hold_add > 0:
                    st.caption(f"加碼確認價：{_hold_add:,.2f} 元")

                # 現價相對關鍵價位的位置
                _position_notes = []
                if _hold_cost > 0:
                    _position_notes.append(
                        f"現價較成本 {_hold_pnl_pct:+.2f}%"
                    )
                if _hold_protect > 0 and _hold_price > 0:
                    _position_notes.append(
                        f"距移動保護價 {((_hold_price / _hold_protect) - 1) * 100:+.2f}%"
                    )
                if _hold_structural > 0 and _hold_price > 0:
                    _position_notes.append(
                        f"距結構失效價 {((_hold_price / _hold_structural) - 1) * 100:+.2f}%"
                    )
                if _hold_target1 > _hold_price > 0:
                    _position_notes.append(
                        f"距第一出場價 {((_hold_target1 / _hold_price) - 1) * 100:+.2f}%"
                    )
                if _position_notes:
                    st.info("目前位置：" + "｜".join(_position_notes))

                # 操作狀態提示
                if _hold_exit1 > 0:
                    st.info(
                        f"出場重點：目前 {_decision_now}｜第一出場價 {_hold_exit1:,.2f} 元"
                        + (
                            f"｜第二出場價 {_hold_exit2:,.2f} 元"
                            if _hold_exit2 > 0 else ""
                        )
                        + (
                            f"｜防守出場價 {_hold_protect:,.2f} 元"
                            if _hold_protect > 0 else ""
                        )
                    )

                if _decision_now == "加碼":
                    st.success(
                        "目前持股條件允許增加部位；仍須確認股價位置沒有過度乖離，"
                        "且加碼後風險仍在可接受範圍內。"
                    )
                elif _decision_now == "減碼":
                    st.warning("目前條件已轉弱，先降低持股曝險，不等到結構完全破壞才處理。")
                elif _decision_now == "退場":
                    st.error("目前已進入退場條件，持股理由已失效或已觸發核心風控。")
                else:
                    st.success("目前沒有減碼或退場訊號，持股條件仍可維持。")

                # v9.3：出場比例不再固定 30%，改由持股健康度與訊號一致度動態決定。
                # 健康且一致度高：讓獲利奔跑；轉弱或一致度下降：提早收回較多部位。
                # v9.17.1：出場比例直接使用畫面「訊號一致度」實際顯示的同一個計數。
                # 畫面顯示 _hold_signal_score/_hold_signal_total，
                # 因此出場引擎也只能讀 _hold_signal_score，不再使用舊的 _hold_agree。
                _exit_signal_count = int(_hold_signal_score or 0)
                if _hold_health == "健康" and _exit_signal_count >= 4:
                    _exit_pct1, _exit_pct2 = 20, 30
                    _exit_style = "趨勢偏強"
                elif _hold_health == "健康" and _exit_signal_count >= 3:
                    _exit_pct1, _exit_pct2 = 30, 30
                    _exit_style = "趨勢正常"
                elif _hold_health == "注意" or _exit_signal_count == 2:
                    _exit_pct1, _exit_pct2 = 40, 30
                    _exit_style = "訊號轉弱"
                else:
                    _exit_pct1, _exit_pct2 = 50, 30
                    _exit_style = "風險升高"

                _exit_pct_final = max(0, 100 - _exit_pct1 - _exit_pct2)

                # v9.17.1：所有出場比例文字一律從同一組變數產生，禁止任何 UI 區塊另寫固定百分比。
                _exit_plan_text = (
                    f"出場配置：{_exit_style}｜第一段 {_exit_pct1}%｜"
                    f"第二段 {_exit_pct2}%｜剩餘 {_exit_pct_final}% 採動態移動停利"
                )
                st.caption(_exit_plan_text)
                # 下一步操作劇本：與上方出場配置共用同一組比例，避免畫面互相矛盾。
                st.markdown("### 下一步操作劇本")
                _script_parts = []

                if _decision_now == "退場":
                    _script_parts.append("目前：已進入退出條件，優先處理剩餘持股。")
                elif _decision_now == "減碼":
                    _script_parts.append("目前：先分批減碼，降低曝險。")
                elif _decision_now == "加碼":
                    _script_parts.append("目前：可評估加碼，但仍以出場價與風控價管理。")
                else:
                    _script_parts.append("目前：維持持有，等待第一出場價或風控條件觸發。")

                if _hold_exit1 > 0:
                    _script_parts.append(
                        f"第一出場：到 {_hold_exit1:,.2f} 元附近，建議先減碼約 {_exit_pct1}%。"
                    )
                if _hold_exit2 > 0:
                    _script_parts.append(
                        f"第二出場：若續強到 {_hold_exit2:,.2f} 元附近，再減碼約 {_exit_pct2}%。"
                    )
                if _hold_protect > 0:
                    _script_parts.append(
                        f"剩餘部位：保留約 {_exit_pct_final}% 採動態移動停利；目前保護基準為 {_hold_protect:,.2f} 元，"
                        "後續若股價創高，保護價應隨趨勢向上調整，不固定停在目前價位。"
                    )
                if _hold_structural > 0:
                    _script_parts.append(
                        f"強制出場：有效跌破 {_hold_structural:,.2f} 元，視為持股結構失效，退出剩餘部位。"
                    )
                if _hold_add_blocked:
                    _script_parts.append(
                        f"加碼：目前不適用。原加碼確認價 {_hold_add_raw:,.2f} 元距第一出場價"
                        f" {_hold_exit1:,.2f} 元僅 {_hold_add_room_pct:.2f}% 空間，不為了追價而硬加碼。"
                    )
                elif _hold_add > 0:
                    _script_parts.append(
                        f"加碼：只有站穩 {_hold_add:,.2f} 元且趨勢／籌碼／量價未出現新的否決訊號時才評估。"
                    )

                for _line in _script_parts:
                    st.write("• " + _line)

                st.caption(
                    "成本價只用來計算損益與調整持股節奏；"
                    "不會因為目前有獲利或虧損而改寫股票本身的趨勢判斷。"
                )

            with st.expander("查看判斷依據", expanded=False):
                if not user_holding or int(_p18.get("current_shares", 0) or 0) <= 0:
                    st.write(
                        "未持股決策鏈：**低檔試單 → 轉強試單 → 正式進場 → 等待拉回／不宜進場**"
                    )
                    st.caption(
                        f"低檔條件={'成立' if _p18.get('low_probe') else '未成立'}｜"
                        f"轉強條件={'成立' if _p18.get('turn_probe') else '未成立'}｜"
                        f"正式進場={'成立' if _p18.get('formal_entry') else '未成立'}"
                    )
                    _route_probe = (
                        f"{float(_p18.get('beta_probe_trigger', 0) or 0):,.2f}"
                        if _p18.get("beta_probe_available", True)
                        and float(_p18.get("beta_probe_trigger", 0) or 0) > 0
                        else "不適用"
                    )
                    st.caption(
                        f"價格路徑：拉回觀察區 {float(_p18.get('beta_pullback_low', 0) or 0):,.2f}～"
                        f"{float(_p18.get('beta_pullback_high', 0) or 0):,.2f}｜"
                        f"試單觸發 {_route_probe}｜"
                        f"強勢突破 {float(_p18.get('beta_strong_breakout', 0) or 0):,.2f}"
                    )
                    st.caption(
                        "路徑狀態："
                        + ("已曾拉回觀察區" if _p18.get("beta_pullback_seen") else "尚未拉回觀察區")
                        + "｜"
                        + ("已重新站上試單觸發價" if _p18.get("beta_reclaim_triggered") else "尚未完成拉回後轉強")
                    )
                    st.caption(
                        "決策穩定："
                        + (
                            "試單訊號已鎖定，本交易日盤中震盪不取消"
                            if _p18.get("beta_probe_latched")
                            else "試單訊號尚未鎖定"
                        )
                        + f"｜失效價 {float(_p18.get('beta_stable_invalidation', 0) or 0):,.2f}"
                    )
                    st.caption(
                        "觸發檢查："
                        + (
                            "今天確實由觸發價下方重新站上"
                            if _p18.get("beta_cross_up_today")
                            else "今天沒有新的由下往上穿越事件"
                        )
                        + f"｜前一有效價格 {float(_p18.get('beta_prev_price', 0) or 0):,.2f}"
                    )
                _checks = _p18.get("early_entry_checks", []) or []
                if _checks:
                    _df = pd.DataFrame([
                        {
                            "條件": x.get("條件"),
                            "實際數值": x.get("實際值"),
                            "結果": x.get("是否通過"),
                            "說明": x.get("用途"),
                        }
                        for x in _checks
                    ])
                    st.dataframe(_df, use_container_width=True, hide_index=True)
                else:
                    st.write("目前沒有額外條件表。")

        # Beta v2：以下舊版分析、Shadow 比對與歷史測試區不再顯示。
        st.stop()

        st.markdown("### ① 目前市場狀態（State）")
        st.markdown(f"""
        <div style="background:#FFFFFF;border:1px solid #E2E8F0;border-left:10px solid {strategy_state['color']};padding:22px;border-radius:14px;box-shadow:0 3px 12px rgba(15,23,42,.06);">
          <div style="font-size:13px;color:#64748B;font-weight:850;">正式趨勢</div>
          <div style="font-size:34px;color:{strategy_state['color']};font-weight:950;margin-top:4px;">{strategy_state['state_label']}</div>
          <div style="font-size:16px;color:#334155;margin-top:7px;">已由歷史日線維持 {strategy_state['state_days']} 個交易日｜日線趨勢分數 {strategy_state['trend_score']}/100</div>
          <div style="font-size:15px;color:#0F172A;margin-top:12px;line-height:1.7;"><b>{strategy_state['change_note']}</b><br>{strategy_state['today_change']}</div>
        </div>
        """, unsafe_allow_html=True)
        st.caption(strategy_state["method_note"])

        st.markdown("### ② 目前動作（Action）")
        st.markdown(f"""
        <div style="background:linear-gradient(135deg,#0F172A 0%,#1E293B 100%);padding:23px;border-radius:14px;color:white;border:1px solid #334155;">
          <div style="font-size:12px;color:#94A3B8;font-weight:850;letter-spacing:.08em;">CURRENT STRATEGY</div>
          <div style="font-size:39px;color:{strategy_state['color']};font-weight:950;margin-top:5px;">{strategy_state['action']}</div>
          <div style="font-size:15px;color:#E2E8F0;margin-top:7px;">現價 {strategy_state['current']:.2f} 元｜正式趨勢 {_state_label(strategy_state['state'])}｜大盤 {decision_snapshot['regime']['state']}</div>
        </div>
        """, unsafe_allow_html=True)
        if user_holding and user_cost > 0:
            pnl = (strategy_state['current']/user_cost-1)*100 if strategy_state['current']>0 else 0
            st.info(f"持股成本 {user_cost:.2f} 元、帳面報酬 {pnl:+.1f}%。成本只影響執行節奏，不改變正式趨勢。")

        st.markdown("### ③ 為什麼（Evidence）")
        ev1, ev2 = st.columns(2)
        with ev1:
            st.markdown("#### 支持目前策略")
            if strategy_state['positive_evidence']:
                for item in strategy_state['positive_evidence']:
                    st.write("✅ " + str(item))
            else:
                st.write("目前沒有足夠的正向證據。")
        with ev2:
            st.markdown("#### 反對目前策略／風險")
            if strategy_state['negative_evidence']:
                for item in strategy_state['negative_evidence']:
                    st.write("⚠️ " + str(item))
            else:
                st.write("目前沒有額外重大反證。")
        st.caption(f"風險證據 {strategy_state['warnings']}/{strategy_state['warning_threshold']}。一般證據會累積後才影響動作，重大結構風險才可立即處理。")

        st.markdown("### ④ 什麼情況會改變（條件）")
        if strategy_state['trigger_rows']:
            st.dataframe(pd.DataFrame(strategy_state['trigger_rows']).rename(columns={'condition':'條件','effect':'策略改變'}), use_container_width=True, hide_index=True)
        else:
            st.info("目前沒有可可靠計算的策略切換價位。")

        st.markdown("### 決策引擎儀表板")
        eng1,eng2,eng3,eng4,eng5=st.columns(5)
        eng1.metric("正式趨勢", f"{strategy_state['trend_score']}/100", strategy_state['state_label'])
        eng2.metric("法人籌碼", f"{decision_snapshot['chip_engine']['score']}/100", decision_snapshot['chip_engine']['state'])
        eng3.metric("量價品質", f"{decision_snapshot['volume_engine']['score']}/100", decision_snapshot['volume_engine']['state'])
        eng4.metric("大盤環境", f"{decision_snapshot['regime']['score']}/100", decision_snapshot['regime']['state'])
        eng5.metric("報酬風險", f"{decision_snapshot['edge_engine']['score']}/100", decision_snapshot['edge_engine']['state'])
        with st.expander("🏦 籌碼與量價引擎明細", expanded=False):
            chip_rows=pd.DataFrame(decision_snapshot['chip_engine'].get('rows',[]))
            if not chip_rows.empty:
                st.dataframe(chip_rows.style.format({'5日累計(張)':'{:+,.0f}','10日累計(張)':'{:+,.0f}','20日累計(張)':'{:+,.0f}'}),use_container_width=True,hide_index=True)
            else:
                st.info("三大法人資料不足，籌碼未納入方向判斷。")
            ve=decision_snapshot['volume_engine']
            st.write(f"量價狀態：**{ve.get('state')}**｜完成日量比 {ve.get('volume_ratio',0):.2f}｜20日上漲量／下跌量 {ve.get('pressure_ratio',0):.2f}")
            if ve.get('veto'): st.warning(ve.get('veto'))
            if decision_snapshot['chip_engine'].get('veto'): st.warning(decision_snapshot['chip_engine'].get('veto'))

        with st.expander("✅ 策略一致性與安全檢查", expanded=False):
            sa = decision_snapshot.get("strategy_audit", {}) or {}
            st.write(f"通過 {sa.get('passed',0)} / {sa.get('total',0)} 項")
            for name, ok in sa.get("checks", []):
                st.write(("✅ " if ok else "❌ ") + str(name))
            if not sa.get("ok", False):
                st.error("策略快照存在不一致，請不要依此版本執行交易；失敗項目：" + "、".join(sa.get("failed", [])))
            else:
                st.success("State、Action、條件、籌碼、量價與大盤閘門目前一致。")

        with st.expander("🧪 跨日趨勢穩定性驗證", expanded=False):
            sv = decision_snapshot.get("strategy_stability_validation", {}) or {}
            if not sv.get("available"):
                st.info(sv.get("note", "目前無法驗證。"))
            else:
                v1, v2, v3, v4 = st.columns(4)
                v1.metric("驗證交易日", f"{sv.get('sample_days',0)} 日")
                v2.metric("原始訊號翻轉", f"{sv.get('raw_flips',0)} 次")
                v3.metric("正式趨勢翻轉", f"{sv.get('confirmed_flips',0)} 次", f"減少 {sv.get('flip_reduction_pct',0):.1f}%")
                v4.metric("平均維持", f"{sv.get('average_state_days',0):.1f} 日")
                st.write(f"• 原始訊號一日反轉：{sv.get('raw_one_day_reversals',0)} 次")
                st.write(f"• 正式趨勢一日反轉：{sv.get('confirmed_one_day_reversals',0)} 次")
                st.write(f"• 目前正式趨勢：{sv.get('current_state_label','—')}｜最近一次狀態變更：{sv.get('last_change_date','—')}")
                st.caption(sv.get("note", ""))

        with st.expander("📈 正式趨勢後續表現驗證", expanded=False):
            ov = decision_snapshot.get("strategy_outcome_validation", {}) or {}
            if not ov.get("available"):
                st.info(ov.get("note", "目前無法驗證。"))
            else:
                o1,o2,o3 = st.columns(3)
                o1.metric("驗證交易日", f"{ov.get('sample_days',0)} 日")
                sep = ov.get('separation_pct')
                o2.metric("多頭－空頭 20日差", f"{sep:+.2f}%" if sep is not None else "—")
                o3.metric("方向區分", "通過" if ov.get('direction_ok') else "待調整")
                odf = pd.DataFrame(ov.get("rows", []))
                if not odf.empty:
                    st.dataframe(odf.style.format({
                        "5日勝率":"{:.1f}%", "5日平均報酬":"{:+.2f}%",
                        "20日勝率":"{:.1f}%", "20日平均報酬":"{:+.2f}%",
                        "20日平均最大不利":"{:+.2f}%"
                    }, na_rep="—"), use_container_width=True, hide_index=True)
                if ov.get('direction_ok'):
                    st.success("多頭正式狀態的後續 20 日表現高於空頭狀態，狀態機具備基本方向區分。")
                else:
                    st.warning("目前樣本未顯示清楚的多空區分；應先調整狀態門檻，不宜只因狀態穩定就提高信任。")
                st.caption(ov.get("note", ""))

        if user_holding:
            hv = decision_snapshot.get("holding_value", {}) or {}
            with st.expander("📈 持股價值與風險報酬", expanded=False):
                h1,h2,h3,h4=st.columns(4)
                h1.metric("持股價值", hv.get('grade','—'))
                h2.metric("剩餘上漲空間", f"{hv.get('upside_pct',0):.1f}%")
                h3.metric("第一層下跌風險", f"{hv.get('downside_pct',0):.1f}%")
                h4.metric("風險報酬比", f"{hv.get('rr'):.2f}" if hv.get('rr') is not None else "—")
                st.write(hv.get('conclusion',''))
                for reason in hv.get('reasons',[]): st.write("• "+str(reason))

        with st.expander("🌐 大盤、價格與資料透明度", expanded=False):
            c1,c2,c3,c4=st.columns(4)
            c1.metric("大盤環境", decision_snapshot['regime']['state'], f"{decision_snapshot['regime']['score']}/100")
            c2.metric("市場分數", f"{decision_engine['market_score']}/100")
            c3.metric("資料可信度", f"{decision_snapshot['data_reliability']}%")
            c4.metric("訊號一致度", f"{decision_snapshot['agreement']['score']}%")
            st.write(f"• 確認價：{lv['突破確認價']:.2f} 元｜{lv['sources']['突破確認價']}")
            st.write(f"• 移動保護價：{lv['protective_stop']:.2f} 元｜{lv['sources']['protective_stop']}")
            st.write(f"• 結構退出價：{lv['structure_stop']:.2f} 元｜{lv['sources']['structure_stop']}")
            ctx=decision_snapshot['regime'].get('context',{}) or {}
            st.write(f"• 參考市場：{ctx.get('benchmark_name','—')}（{ctx.get('benchmark','—')}），資料日期 {ctx.get('raw_date') or '資料不足'}")
            factors=pd.DataFrame(decision_snapshot['regime'].get('factor_rows',[]))
            if not factors.empty:
                factors=factors.rename(columns={'factor':'面向','raw':'原始數據','score':'分數','weight':'權重(%)','contribution':'加權貢獻','rule':'規則'})
                st.dataframe(factors,use_container_width=True,hide_index=True)

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
                <b>目前進場方式：</b>{ta['進場區_model']}｜<b>條件是否完整：</b>{'已確認' if ta['進場區_ready'] else '尚未確認'}<br>
                <b>白話解讀：</b>{'目前已符合此進場方式的主要條件，但仍建議分批。' if ta['進場區_ready'] else '目前只有部分條件成立，先等待止跌、放量或突破確認。'}
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
