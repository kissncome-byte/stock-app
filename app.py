import os
import time
import requests
import certifi
import pandas as pd
import numpy as np
import streamlit as st
import altair as alt
from datetime import datetime, timedelta
import pytz
from FinMind.data import DataLoader

# ============ 1. Page Config ============
st.set_page_config(page_title="SOP v16 三層市場掃描架構", layout="wide")

# ============ 2. Global ============
TZ = pytz.timezone("Asia/Taipei")


# ============ 3. Helper ============
def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ["-", "", "None", "nan"]:
            return default
        return float(str(x).replace(",", ""))
    except Exception:
        return default


def tick_size(p: float) -> float:
    if p >= 1000:
        return 5.0
    if p >= 500:
        return 1.0
    if p >= 100:
        return 0.5
    if p >= 50:
        return 0.1
    if p >= 10:
        return 0.01
    return 0.001


def round_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t == 0:
        return 0.0
    return round(x / t) * t


def fmt_space(x) -> str:
    if x is None or pd.isna(x) or np.isinf(x):
        return "無更高壓力位"
    return f"{x:.2f}"


def get_market_status_label(rt_success: bool, last_trade_date_str: str):
    now = datetime.now(TZ)
    weekday = now.weekday()
    current_time = now.time()

    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    if weekday >= 5:
        return "CLOSED_WEEKEND", f"市場休市 (週末) | 數據日期: {last_trade_date_str}", "gray"

    is_trading_hours = start_time <= current_time <= end_time

    if rt_success:
        if is_trading_hours:
            return "OPEN", "市場交易中 (即時更新)", "red"
        elif current_time < start_time:
            return "PRE_MARKET", "盤前準備中 (即時連線正常)", "blue"
        else:
            return "POST_MARKET", "今日已收盤 (即時報價)", "green"
    else:
        if is_trading_hours:
            return "API_WAIT", f"連線受限，改用歷史價 | 歷史日期: {last_trade_date_str}", "orange"
        elif current_time < start_time:
            return "PRE_MARKET", f"盤前準備中 | 歷史日期: {last_trade_date_str}", "blue"
        else:
            if current_time > datetime.strptime("10:00", "%H:%M").time() and last_trade_date_str != now.strftime("%Y-%m-%d"):
                return "CLOSED_HOLIDAY", f"市場休市 (國定假日) | 數據日期: {last_trade_date_str}", "gray"
            return "POST_MARKET", f"今日已收盤 | 數據日期: {last_trade_date_str}", "green"


def next_resistance_above(price: float, levels):
    above = [lv for lv in levels if lv > price]
    return min(above) if above else float("inf")


def detect_style(result: dict) -> str:
    brk_score = 0
    pb_score = 0

    if result["breakout_setup"]:
        brk_score += 3
    if result["pullback_setup"]:
        pb_score += 3

    if result["space_ok_brk"]:
        brk_score += 2
    if result["space_ok_pb"]:
        pb_score += 2

    if result["rr1_brk"] >= 2.0:
        brk_score += 2
    if result["rr1_pb"] >= 3.0:
        pb_score += 2

    if result["brk_tradeable"]:
        brk_score += 3
    if result["pb_tradeable"]:
        pb_score += 3

    if brk_score > pb_score:
        return "突破型"
    if pb_score > brk_score:
        return "拉回型"

    if result["current_price"] >= result["pivot"]:
        return "突破型"
    return "拉回型"


def judge_market_regime_from_df(df: pd.DataFrame) -> dict:
    if df is None or df.empty or len(df) < 30:
        return {
            "regime": "資料不足",
            "preferred_style": "拉回型",
            "reason": "資料不足，預設偏防守。"
        }

    x = df.copy()
    x["MA20"] = x["close"].rolling(20).mean()
    x = x.dropna(subset=["MA20"]).copy()
    if x.empty or len(x) < 6:
        return {
            "regime": "資料不足",
            "preferred_style": "拉回型",
            "reason": "資料不足，預設偏防守。"
        }

    price = float(x["close"].iloc[-1])
    ma20 = float(x["MA20"].iloc[-1])
    ma20_prev = float(x["MA20"].iloc[-6])
    slope_up = ma20 > ma20_prev

    high_60 = float(x.tail(60)["high"].max()) if len(x) >= 60 else float(x["high"].max())
    atr14 = float((x["high"] - x["low"]).rolling(14).mean().iloc[-1]) if len(x) >= 14 else 0.0

    near_high = price >= (high_60 - 0.5 * atr14) if atr14 > 0 else price >= high_60 * 0.98
    above_ma20 = price >= ma20

    if slope_up and above_ma20 and near_high:
        return {
            "regime": "強勢盤",
            "preferred_style": "突破型",
            "reason": "均線上彎、價格站上 MA20 且接近區間高點。"
        }

    if slope_up and above_ma20:
        return {
            "regime": "震盪偏強盤",
            "preferred_style": "拉回型",
            "reason": "均線仍上彎，但尚未明顯突破高點，偏向等回檔切入。"
        }

    return {
        "regime": "偏弱盤",
        "preferred_style": "拉回型",
        "reason": "價格/均線結構較弱，偏防守。"
    }


# ============ 4. Auth ============
APP_PASSWORD = os.getenv("APP_PASSWORD", "") or st.secrets.get("APP_PASSWORD", "")
if APP_PASSWORD and "authed" not in st.session_state:
    st.session_state["authed"] = False

if APP_PASSWORD and not st.session_state["authed"]:
    st.title("🔐 系統登入")
    pw = st.text_input("Access Password", type="password")
    if st.button("Login"):
        if pw == APP_PASSWORD:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("密碼錯誤")
    st.stop()

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")


# ============ 5. Cached API ============
@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try:
            api.login_by_token(FINMIND_TOKEN)
        except Exception:
            pass
    return api


@st.cache_data(ttl=3600)
def get_stock_info_df():
    api = get_api()
    df = api.taiwan_stock_info()
    if df is None or df.empty:
        return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category"])
    df = df.copy()
    if "stock_id" in df.columns:
        df["stock_id"] = df["stock_id"].astype(str).str.strip()
    return df


@st.cache_data(ttl=900)
def get_daily_df(stock_id: str, days: int = 365):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
    if df_raw is None or df_raw.empty:
        return None

    df = df_raw.copy()
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={
        "Trading_Volume": "vol",
        "Trading_money": "amount",
        "max": "high",
        "min": "low",
    })

    for c in ["close", "high", "low", "vol", "amount"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["close", "high", "low", "vol"]).copy()
    df = df[df["vol"] > 0].copy()
    return df


@st.cache_data(ttl=900)
def get_inst_df(stock_id: str, days: int = 60):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
    return df if df is not None else pd.DataFrame()


@st.cache_data(ttl=900)
def get_rev_df(stock_id: str, days: int = 220):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=start_date)
    return df if df is not None else pd.DataFrame()


# ============ 6. Core ============
def prepare_indicator_df(df: pd.DataFrame):
    if df is None or df.empty:
        return None

    x = df.copy()
    x["ATR14"] = (x["high"] - x["low"]).rolling(14).mean()
    x["MA20"] = x["close"].rolling(20).mean()

    if "amount" in x.columns:
        x["MA20_Amount"] = (x["amount"] / 1e8).rolling(20).mean()
    else:
        x["MA20_Amount"] = (x["close"] * x["vol"] / 1e8).rolling(20).mean()

    direction = np.where(x["close"].diff() > 0, 1, np.where(x["close"].diff() < 0, -1, 0))
    x["OBV"] = (direction * x["vol"]).cumsum()
    x["OBV_MA10"] = x["OBV"].rolling(10).mean()

    x = x.dropna(subset=["ATR14", "MA20", "MA20_Amount", "OBV_MA10"]).copy()
    if x.empty:
        return None
    return x


def compute_live_price(stock_id: str, hist_last_close: float):
    rt_price = None
    rt_success = False
    rt_y_price = 0.0
    rt_source = "歷史收盤"

    # 引擎 A：TWSE MIS
    try:
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        session.get(
            "https://mis.twse.com.tw/stock/index.jsp",
            headers=headers,
            timeout=3,
            verify=certifi.where(),
        )

        ts = int(time.time() * 1000)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
        r = session.get(url, headers=headers, timeout=3, verify=certifi.where())

        if r.status_code == 200:
            data = r.json()
            if "msgArray" in data and len(data["msgArray"]) > 0:
                info = data["msgArray"][0]

                # z: 最新成交價
                # y: 昨收/參考價
                z = safe_float(info.get("z"))
                y = safe_float(info.get("y"))
                rt_y_price = y

                # 只有 z > 0 才算真正即時
                if z > 0:
                    rt_price = z
                    rt_success = True
                    rt_source = "TWSE MIS 即時"
    except Exception:
        pass

    # 引擎 B：Yahoo 備援
    if not rt_success:
        try:
            for suffix in [".TW", ".TWO"]:
                yh_url = f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}"
                yh_r = requests.get(
                    yh_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=3,
                    verify=certifi.where(),
                )

                if yh_r.status_code == 200:
                    meta = yh_r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                    p = safe_float(meta.get("regularMarketPrice"))
                    prev_close = safe_float(meta.get("previousClose"))

                    if prev_close > 0:
                        rt_y_price = prev_close

                    if p > 0:
                        rt_price = p
                        rt_success = True
                        rt_source = f"Yahoo 即時/延遲 {suffix}"
                        break
        except Exception:
            pass

    final_price = rt_price if rt_success else hist_last_close
    return final_price, rt_success, rt_y_price, rt_source


def evaluate_stock(
    stock_id: str,
    total_capital: float,
    risk_per_trade: float,
    liq_gate: float,
    slip_ticks: int,
    space_atr_mult: float,
    space_tick_buffer: int,
):
    df_raw = get_daily_df(stock_id, days=365)
    if df_raw is None or df_raw.empty:
        return None

    df = prepare_indicator_df(df_raw)
    if df is None or df.empty:
        return None

    info_df = get_stock_info_df()
    match = info_df[info_df["stock_id"] == stock_id]
    stock_name = match["stock_name"].values[0] if ("stock_name" in match.columns and not match.empty) else "未知"
    industry = match["industry_category"].values[0] if ("industry_category" in match.columns and not match.empty) else "未知產業"

    hist_last = df.iloc[-1]
    last_trade_date_str = str(hist_last["date"])

    current_price, rt_success, rt_y_price, rt_source = compute_live_price(
        stock_id, float(hist_last["close"])
    )
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

    ma20_val = float(hist_last["MA20"])
    atr = float(hist_last["ATR14"]) if not np.isnan(hist_last["ATR14"]) else current_price * 0.03
    t = tick_size(current_price)
    slip = float(slip_ticks) * t
    risk_amt = float(total_capital) * 10000 * (float(risk_per_trade) / 100)

    pivot = float(df.tail(60)["high"].max())
    res_120 = float(df.tail(120)["high"].max()) if len(df) >= 120 else pivot
    res_252 = float(df.tail(252)["high"].max()) if len(df) >= 252 else res_120
    res_504 = float(df.tail(504)["high"].max()) if len(df) >= 504 else res_252
    levels = [pivot, res_120, res_252, res_504]

    ma20_prev = float(df["MA20"].iloc[-6]) if len(df) > 6 else ma20_val
    ma20_slope_up = ma20_val > ma20_prev
    obv_up = df["OBV"].iloc[-1] > df["OBV_MA10"].iloc[-1]
    price_10d_max = df["close"].tail(10).max()
    obv_10d_max = df["OBV"].tail(10).max()
    is_div = (current_price >= price_10d_max) and (df["OBV"].iloc[-1] < obv_10d_max)
    avg_vol_20 = float(df["vol"].rolling(20).mean().iloc[-1])
    current_vol = float(hist_last["vol"])
    vol_ratio = current_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0

    liq_ok = float(hist_last["MA20_Amount"]) >= float(liq_gate)
    breakout_setup = (current_price >= pivot) and obv_up
    pullback_setup = ma20_slope_up and (ma20_val <= current_price <= ma20_val + 1.2 * atr)

    def calc_breakout_targets(entry, r120, r252, atr_val, t_val):
        tp1 = r120 if r120 > entry else entry + 2.0 * atr_val
        tp2 = r252 if r252 > tp1 else tp1 + 3.0 * atr_val
        return round_to_tick(tp1, t_val), round_to_tick(tp2, t_val)

    def calc_pullback_targets(entry, pivot_val, r120, atr_val, t_val):
        tp1 = pivot_val if pivot_val > entry else entry + 2.0 * atr_val
        tp2 = r120 if r120 > tp1 else tp1 + 2.0 * atr_val
        return round_to_tick(tp1, t_val), round_to_tick(tp2, t_val)

    entry_brk = round_to_tick(pivot + t, t)
    stop_brk = round_to_tick(entry_brk - 1.5 * atr - slip, t)
    tp1_brk, tp2_brk = calc_breakout_targets(entry_brk, res_120, res_252, atr, t)

    entry_pb = round_to_tick(current_price if pullback_setup else ma20_val + 0.3 * atr, t)
    stop_pb = round_to_tick(entry_pb - 1.2 * atr - slip, t)
    tp1_pb, tp2_pb = calc_pullback_targets(entry_pb, pivot, res_120, atr, t)

    space_buf = float(space_tick_buffer) * t

    next_res_brk = next_resistance_above(entry_brk, levels)
    space_to_res_brk = (next_res_brk - entry_brk) if np.isfinite(next_res_brk) else float("inf")
    space_ok_brk = space_to_res_brk >= (float(space_atr_mult) * atr + space_buf)

    next_res_pb = next_resistance_above(entry_pb, levels)
    space_to_res_pb = (next_res_pb - entry_pb) if np.isfinite(next_res_pb) else float("inf")
    space_ok_pb = space_to_res_pb >= (float(space_atr_mult) * atr + space_buf)

    R_brk = entry_brk - stop_brk
    rr1_brk = ((tp1_brk - entry_brk) / R_brk) if R_brk > 0 else 0.0
    rr2_brk = ((tp2_brk - entry_brk) / R_brk) if R_brk > 0 else 0.0
    brk_tradeable = liq_ok and space_ok_brk and (rr1_brk >= 2.0)

    R_pb = entry_pb - stop_pb
    rr1_pb = ((tp1_pb - entry_pb) / R_pb) if R_pb > 0 else 0.0
    rr2_pb = ((tp2_pb - entry_pb) / R_pb) if R_pb > 0 else 0.0
    pb_tradeable = liq_ok and space_ok_pb and (rr1_pb >= 3.0)

    style = detect_style({
        "breakout_setup": breakout_setup,
        "pullback_setup": pullback_setup,
        "space_ok_brk": space_ok_brk,
        "space_ok_pb": space_ok_pb,
        "rr1_brk": rr1_brk,
        "rr1_pb": rr1_pb,
        "brk_tradeable": brk_tradeable,
        "pb_tradeable": pb_tradeable,
        "current_price": current_price,
        "pivot": pivot,
    })

    regime = judge_market_regime_from_df(df)

    strong_stock = (
        ma20_slope_up
        and current_price >= ma20_val
        and obv_up
        and liq_ok
    )

    trend_score = int(ma20_slope_up) + int(current_price >= ma20_val)
    momentum_score = int(obv_up) + int(current_price >= price_10d_max)
    liquidity_score = 2 if liq_ok else 0

    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "industry": industry,
        "df": df,
        "last_trade_date_str": last_trade_date_str,
        "current_price": current_price,
        "rt_success": rt_success,
        "rt_y_price": rt_y_price,
        "rt_source": rt_source,
        "quote_status": "即時/近即時" if rt_success else "歷史/非即時",
        "m_code": m_code,
        "m_desc": m_desc,
        "m_color": m_color,
        "ma20_val": ma20_val,
        "atr": atr,
        "t": t,
        "slip": slip,
        "risk_amt": risk_amt,
        "pivot": pivot,
        "res_120": res_120,
        "res_252": res_252,
        "res_504": res_504,
        "levels": levels,
        "ma20_slope_up": ma20_slope_up,
        "obv_up": obv_up,
        "is_div": is_div,
        "vol_ratio": vol_ratio,
        "liq_ok": liq_ok,
        "ma20_amount": float(hist_last["MA20_Amount"]),
        "breakout_setup": breakout_setup,
        "pullback_setup": pullback_setup,
        "entry_brk": entry_brk,
        "stop_brk": stop_brk,
        "tp1_brk": tp1_brk,
        "tp2_brk": tp2_brk,
        "space_to_res_brk": space_to_res_brk,
        "space_ok_brk": space_ok_brk,
        "rr1_brk": rr1_brk,
        "rr2_brk": rr2_brk,
        "brk_tradeable": brk_tradeable,
        "entry_pb": entry_pb,
        "stop_pb": stop_pb,
        "tp1_pb": tp1_pb,
        "tp2_pb": tp2_pb,
        "space_to_res_pb": space_to_res_pb,
        "space_ok_pb": space_ok_pb,
        "rr1_pb": rr1_pb,
        "rr2_pb": rr2_pb,
        "pb_tradeable": pb_tradeable,
        "個股型態": style,
        "市場環境": regime["regime"],
        "市場偏好型態": regime["preferred_style"],
        "regime_reason": regime["reason"],
        "strong_stock": strong_stock,
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "liquidity_score": liquidity_score,
    }


# ============ 7. Universe ============
@st.cache_data(ttl=3600)
def get_finmind_universe():
    info = get_stock_info_df().copy()
    if info.empty:
        return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category"])

    info["stock_id"] = info["stock_id"].astype(str).str.strip()
    info = info[info["stock_id"].str.fullmatch(r"\d{4}", na=False)].copy()

    keep_cols = ["stock_id"]
    if "stock_name" in info.columns:
        keep_cols.append("stock_name")
    if "industry_category" in info.columns:
        keep_cols.append("industry_category")

    return info[keep_cols].drop_duplicates("stock_id").reset_index(drop=True)


# ============ 8. Session ============
if "screen_df" not in st.session_state:
    st.session_state["screen_df"] = None
if "screen_ts" not in st.session_state:
    st.session_state["screen_ts"] = ""
if "picked_stock" not in st.session_state:
    st.session_state["picked_stock"] = "2330"
if "stock_input" not in st.session_state:
    st.session_state["stock_input"] = st.session_state["picked_stock"]

# ============ 9. Main UI ============
st.title("🦅 SOP v16 三層市場掃描架構")
st.caption("Layer1 全市場快速掃描 → Layer2 熱門產業 → Layer3 深度策略掃描。")

with st.sidebar:
    st.header("⚙️ 實戰風控設定")
    total_capital = st.number_input("操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆最大風險 (%)", 1.0, 20.0, 2.0)

    st.divider()
    st.header("🛡️ 硬性門檻")
    liq_gate = st.number_input("流動性：MA20成交額(億) ≥", value=2.0, step=0.5)
    slip_ticks = st.number_input("滑價 Buffer (ticks)", value=3, step=1, min_value=0)

    st.divider()
    st.header("🧠 Space Gate")
    space_atr_mult = st.number_input("到下一壓力至少 ≥ ATR ×", value=2.0, step=0.5, min_value=0.0)
    space_tick_buffer = st.number_input("壓力位 Tick Buffer", value=2, step=1, min_value=0)

    st.divider()
    st.header("🔥 掃描模式")
    scan_mode = st.selectbox("市場掃描模式", ["快速模式", "標準模式", "完整模式"], index=1)
    if scan_mode == "快速模式":
        market_scan_limit_default = 1200
        hot_industry_top_default = 3
    elif scan_mode == "標準模式":
        market_scan_limit_default = 1800
        hot_industry_top_default = 5
    else:
        market_scan_limit_default = 2500
        hot_industry_top_default = 8

    market_scan_limit = st.number_input("Layer1 全市場快速掃描股數", value=market_scan_limit_default, step=100, min_value=200, max_value=5000)
    hot_industry_top_n = st.number_input("Layer2 熱門產業前幾名", value=hot_industry_top_default, step=1, min_value=1, max_value=30)
    deep_scan_limit = st.number_input("Layer3 深度掃描上限", value=9999, step=100, min_value=50, max_value=99999)

    strong_only = st.checkbox("市場掃描只看強勢股", value=True)
    trend_filter = st.checkbox("只顯示 MA20 上升股票", value=True)
    adapt_to_regime = st.checkbox("依市場環境偏好排序型態", value=True)

tab_a, tab_b = st.tabs(["📌 個股分析", "🔎 市場掃描"])


# ============ 10. Render ============
def render_plan(
    container,
    name,
    entry,
    stop,
    tp1,
    tp2,
    rr_gate,
    setup_ok,
    accent,
    liq_ok,
    risk_amt,
    slip,
    space_ok,
    rr2_gate_bonus=1.0,
):
    R = entry - stop
    risk_per_share = abs(entry - stop) + slip

    rr1 = ((tp1 - entry) / R) if R > 0 else 0.0
    rr2 = ((tp2 - entry) / R) if R > 0 else 0.0

    rr1_ok = rr1 >= rr_gate
    rr2_ok = rr2 >= (rr_gate + rr2_gate_bonus)

    tradeable = liq_ok and space_ok and rr1_ok

    total_lots = int(risk_amt / (risk_per_share * 1000)) if (tradeable and risk_per_share > 0) else 0
    tp1_lots = total_lots // 2
    runner_lots = total_lots - tp1_lots

    with container:
        st.markdown(f"### {accent} {name}")
        st.write(
            f"Setup {'✅' if setup_ok else '❌'} | "
            f"Liquidity {'✅' if liq_ok else '❌'} | "
            f"Space {'✅' if space_ok else '❌'} | "
            f"RR1 {rr1:.2f} {'✅' if rr1_ok else '❌'} | "
            f"RR2 {rr2:.2f} {'✅' if rr2_ok else '❌'}"
        )
        st.write(f"**Tradeable {'✅YES' if tradeable else '❌NO'}**")
        st.write(f"🔹 進場 `{entry:.2f}`  |  🛑 停損 `{stop:.2f}`")
        st.write(f"🎯 目標1 `{tp1:.2f}` | 🚀 目標2 `{tp2:.2f}`")

        m1, m2, m3 = st.columns(3)
        m1.metric("建議總張數", f"{total_lots}")
        m2.metric("TP1 賣出", f"{tp1_lots}")
        m3.metric("Runner", f"{runner_lots}")

        if not tradeable:
            st.caption("⚠️ 未通過 Tradeable（流動性 / 空間 / RR1 任一不足）。")


def render_single_stock_result(result: dict):
    st.divider()
    top1, top2, top3 = st.columns([2.2, 1, 1.5])
    with top1:
        st.header(f"{result['stock_name']} {result['stock_id']}")
        st.caption(f"產業：{result['industry']} | 個股型態：{result['個股型態']} | 資料來源：{'即時' if result['rt_success'] else '歷史'}")
    with top2:
        diff = result["current_price"] - (result["rt_y_price"] if result["rt_y_price"] > 0 else result["df"].iloc[-1]["close"])
        st.metric("目前現價", f"{result['current_price']:.2f}", delta=f"{diff:.2f}")
        st.caption(f"報價狀態：{result['quote_status']}｜來源：{result['rt_source']}")
    with top3:
        st.subheader(f":{result['m_color']}[{result['m_desc']}]")

    st.markdown("### 🌦️ 市場環境判讀")
    st.write(f"**市場環境**：{result['市場環境']}")
    st.write(f"**市場偏好型態**：{result['市場偏好型態']}")
    st.write(f"**原因**：{result['regime_reason']}")

    st.markdown("### 🧬 價量與型態深度解析")
    c1, c2 = st.columns(2)
    with c1:
        if result["ma20_slope_up"]:
            st.success("📈 **均線趨勢**：MA20 向上，具備多頭保護力道")
        else:
            st.warning("📉 **均線趨勢**：MA20 向下或走平，動能偏弱")

        if result["obv_up"]:
            st.success("🟢 **量能配合**：OBV 位於均線之上，買盤穩定")
        else:
            st.warning("⚪ **量能配合**：OBV 低於均線，資金退潮中")

        if result["is_div"]:
            st.error("⚠️ **型態警示**：出現量價背離！慎防假突破。")
        elif result["vol_ratio"] > 1.5:
            st.success(f"🔥 **攻擊量能**：今日成交量達均量 {result['vol_ratio']:.1f} 倍！")

    with c2:
        st.write(f"**突破 Setup**：{'✅成立' if result['breakout_setup'] else '❌不成立'}")
        st.write(f"**拉回 Setup**：{'✅成立' if result['pullback_setup'] else '❌不成立'}")
        st.write(f"**流動性**：{'✅合格' if result['liq_ok'] else '❌不足'} ({result['ma20_amount']:.2f}億)")
        st.write(f"**個股型態**：{result['個股型態']}")
        st.write(f"**強勢股**：{'✅' if result['strong_stock'] else '❌'}")

    st.markdown("### 🧠 Space Gate（以 Entry 為基準）")
    st.write(f"**Breakout Space**：{'✅' if result['space_ok_brk'] else '❌'} ｜距離下一壓力 `{fmt_space(result['space_to_res_brk'])}`")
    st.write(f"**Pullback Space**：{'✅' if result['space_ok_pb'] else '❌'} ｜距離下一壓力 `{fmt_space(result['space_to_res_pb'])}`")

    st.divider()
    st.subheader("⚔️ 多階層交易計畫")
    col_brk, col_pb = st.columns(2)

    with col_brk:
        render_plan(
            st.container(border=True),
            "Breakout 突破方案",
            result["entry_brk"],
            result["stop_brk"],
            result["tp1_brk"],
            result["tp2_brk"],
            2.0,
            result["breakout_setup"],
            "🚀",
            result["liq_ok"],
            result["risk_amt"],
            result["slip"],
            result["space_ok_brk"],
            rr2_gate_bonus=1.0,
        )

    with col_pb:
        render_plan(
            st.container(border=True),
            "Pullback 拉回方案",
            result["entry_pb"],
            result["stop_pb"],
            result["tp1_pb"],
            result["tp2_pb"],
            3.0,
            result["pullback_setup"],
            "💎",
            result["liq_ok"],
            result["risk_amt"],
            result["slip"],
            result["space_ok_pb"],
            rr2_gate_bonus=1.0,
        )

    st.divider()
    st.markdown("### 📈 趨勢觀測 (藍線:價 / 橘線:OBV)")
    chart_df = result["df"].tail(100).copy()
    chart_df["date"] = pd.to_datetime(chart_df["date"])
    base = alt.Chart(chart_df).encode(x=alt.X("date:T", title="日期"))
    lp = base.mark_line(color="#2962FF").encode(y=alt.Y("close:Q", scale=alt.Scale(zero=False), title="價格 (藍)"))
    lma = base.mark_line(color="rgba(0,0,0,0.3)", strokeDash=[5, 5]).encode(y="MA20:Q")
    lo = base.mark_line(color="#FF6D00").encode(y=alt.Y("OBV:Q", scale=alt.Scale(zero=False), title="OBV (橘)"))
    st.altair_chart(alt.layer(lma, lp, lo).resolve_scale(y="independent").interactive(), use_container_width=True)

    df_inst = get_inst_df(result["stock_id"], days=60)
    df_rev = get_rev_df(result["stock_id"], days=220)
    with st.expander("📋 詳細數據"):
        ti, trr = st.tabs(["法人動態", "月營收"])
        with ti:
            if df_inst is not None and not df_inst.empty:
                st.dataframe(df_inst.tail(10))
            else:
                st.write("無資料")
        with trr:
            if df_rev is not None and not df_rev.empty:
                st.dataframe(df_rev.tail(6))
            else:
                st.write("無資料")


# ===================== TAB B: Market Scanner =====================
with tab_b:
    st.subheader("市場掃描（v16）")

    info_df = get_finmind_universe()
    industry_options = (
        sorted(info_df["industry_category"].dropna().astype(str).unique().tolist())
        if not info_df.empty and "industry_category" in info_df.columns
        else []
    )

    industry_mode = st.radio(
        "產業鎖定模式",
        ["全部產業", "手動指定產業", "自動熱門產業"],
        horizontal=True
    )
    selected_industries = []
    if industry_mode == "手動指定產業":
        selected_industries = st.multiselect("選擇產業", industry_options)

    top_show = st.number_input("輸出候選數", value=30, step=10, min_value=10, max_value=200, key="top_show")

    run_scan = st.button("🚦 開始市場掃描", type="primary", key="run_scan")

    if run_scan:
        with st.spinner("正在執行三層掃描..."):
            try:
                universe = get_finmind_universe()
                if universe is None or universe.empty:
                    st.error("❌ 無法取得股票清單")
                else:
                    if industry_mode == "手動指定產業" and selected_industries:
                        universe = universe[universe["industry_category"].astype(str).isin(selected_industries)].copy()

                    # ===== Layer 1 =====
                    layer1_rows = []
                    total = min(len(universe), int(market_scan_limit))
                    prog = st.progress(0)

                    for i, (_, row) in enumerate(universe.head(total).iterrows(), start=1):
                        sid = str(row["stock_id"]).strip()

                        try:
                            result = evaluate_stock(
                                stock_id=sid,
                                total_capital=total_capital,
                                risk_per_trade=risk_per_trade,
                                liq_gate=liq_gate,
                                slip_ticks=slip_ticks,
                                space_atr_mult=space_atr_mult,
                                space_tick_buffer=space_tick_buffer,
                            )
                        except Exception:
                            prog.progress(i / total)
                            continue

                        if result is None:
                            prog.progress(i / total)
                            continue

                        if trend_filter and not result["ma20_slope_up"]:
                            prog.progress(i / total)
                            continue

                        if strong_only and not result["strong_stock"]:
                            prog.progress(i / total)
                            continue

                        layer1_rows.append(
                            {
                                "stock_id": result["stock_id"],
                                "stock_name": result["stock_name"],
                                "industry": result["industry"],
                                "個股型態": result["個股型態"],
                                "市場環境": result["市場環境"],
                                "市場偏好型態": result["市場偏好型態"],
                                "price": result["current_price"],
                                "quote_status": result["quote_status"],
                                "rt_source": result["rt_source"],
                                "liq20E": result["ma20_amount"],
                                "strong_stock": result["strong_stock"],
                                "trend_score": result["trend_score"],
                                "momentum_score": result["momentum_score"],
                                "liquidity_score": result["liquidity_score"],
                                "result_obj": result,
                            }
                        )
                        prog.progress(i / total)

                    layer1_df = pd.DataFrame(layer1_rows)
                    if layer1_df.empty:
                        st.warning("Layer1 沒有掃出符合條件的強勢股。")
                    else:
                        st.subheader("Layer 1｜全市場快速掃描完成")
                        st.write(f"掃描完成筆數：{len(layer1_df)}")

                        # ===== Layer 2 =====
                        hot_industry_df = (
                            layer1_df.groupby("industry", dropna=False)
                            .agg(
                                strong_count=("strong_stock", "sum"),
                                avg_liq=("liq20E", "mean"),
                                avg_trend=("trend_score", "mean"),
                                avg_momentum=("momentum_score", "mean"),
                            )
                            .reset_index()
                        )
                        hot_industry_df["industry_score"] = (
                            hot_industry_df["strong_count"] * 3
                            + hot_industry_df["avg_liq"] * 0.2
                            + hot_industry_df["avg_trend"] * 1.0
                            + hot_industry_df["avg_momentum"] * 1.0
                        )
                        hot_industry_df = hot_industry_df.sort_values(
                            by=["industry_score", "strong_count", "avg_liq"],
                            ascending=False
                        ).reset_index(drop=True)

                        if industry_mode == "自動熱門產業":
                            hot_list = hot_industry_df["industry"].head(int(hot_industry_top_n)).astype(str).tolist()
                            layer2_df = layer1_df[layer1_df["industry"].astype(str).isin(hot_list)].copy()
                        else:
                            layer2_df = layer1_df.copy()

                        st.subheader("Layer 2｜熱門產業排行榜")
                        st.dataframe(hot_industry_df.head(int(hot_industry_top_n)), use_container_width=True)

                        # ===== Layer 3 =====
                        layer2_df = layer2_df.head(int(deep_scan_limit)).copy()

                        deep_rows = []
                        for _, r in layer2_df.iterrows():
                            result = r["result_obj"]

                            tier = "C"
                            if result["liq_ok"] and (result["space_ok_brk"] or result["space_ok_pb"]):
                                tier = "B"
                            if result["brk_tradeable"] or result["pb_tradeable"]:
                                tier = "A"

                            preferred_bonus = 0
                            if adapt_to_regime and result["個股型態"] == result["市場偏好型態"]:
                                preferred_bonus = 1

                            deep_rows.append(
                                {
                                    "stock_id": result["stock_id"],
                                    "stock_name": result["stock_name"],
                                    "industry": result["industry"],
                                    "個股型態": result["個股型態"],
                                    "市場環境": result["市場環境"],
                                    "市場偏好型態": result["市場偏好型態"],
                                    "price": result["current_price"],
                                    "quote_status": result["quote_status"],
                                    "rt_source": result["rt_source"],
                                    "liq20E": result["ma20_amount"],
                                    "strong_stock": result["strong_stock"],
                                    "brk_setup": result["breakout_setup"],
                                    "brk_space": result["space_ok_brk"],
                                    "brk_rr1": result["rr1_brk"],
                                    "brk_rr2": result["rr2_brk"],
                                    "brk_tradeable": result["brk_tradeable"],
                                    "pb_setup": result["pullback_setup"],
                                    "pb_space": result["space_ok_pb"],
                                    "pb_rr1": result["rr1_pb"],
                                    "pb_rr2": result["rr2_pb"],
                                    "pb_tradeable": result["pb_tradeable"],
                                    "tier": tier,
                                    "preferred_bonus": preferred_bonus,
                                }
                            )

                        out = pd.DataFrame(deep_rows)
                        if out.empty:
                            st.warning("Layer3 沒有深度掃描結果。")
                        else:
                            out["tier_rank"] = out["tier"].map({"A": 1, "B": 2, "C": 3})
                            out["style_rank"] = out["個股型態"].map({"突破型": 1, "拉回型": 2})
                            out = out.sort_values(
                                by=["tier_rank", "preferred_bonus", "style_rank", "brk_rr2", "pb_rr2", "liq20E"],
                                ascending=[True, False, True, False, False, False],
                            ).reset_index(drop=True)
                            out = out.drop(columns=["tier_rank", "style_rank"])

                            st.session_state["screen_df"] = out.copy()
                            st.session_state["screen_ts"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

                            a_count = int((out["tier"] == "A").sum())
                            b_count = int((out["tier"] == "B").sum())
                            c_count = int((out["tier"] == "C").sum())
                            breakout_count = int((out["個股型態"] == "突破型").sum())
                            pullback_count = int((out["個股型態"] == "拉回型").sum())

                            st.subheader("市場溫度計")
                            c1, c2, c3, c4, c5 = st.columns(5)
                            c1.metric("A級", a_count)
                            c2.metric("B級", b_count)
                            c3.metric("C級", c_count)
                            c4.metric("突破型", breakout_count)
                            c5.metric("拉回型", pullback_count)

                            st.subheader("🥇 A級交易機會")
                            a_df = out[out["tier"] == "A"].copy()
                            st.dataframe(a_df.head(int(top_show)), use_container_width=True) if not a_df.empty else st.write("無")

                            st.subheader("🥈 B級強候選")
                            b_df = out[out["tier"] == "B"].copy()
                            st.dataframe(b_df.head(int(top_show)), use_container_width=True) if not b_df.empty else st.write("無")

                            st.subheader("🥉 C級候選")
                            c_df = out[out["tier"] == "C"].copy()
                            st.dataframe(c_df.head(int(top_show)), use_container_width=True) if not c_df.empty else st.write("無")

                            st.subheader("⭐ 今日最佳候選（Top Picks）")
                            topk = out.head(10).copy()
                            st.dataframe(
                                topk[
                                    [
                                        "stock_id",
                                        "stock_name",
                                        "industry",
                                        "個股型態",
                                        "市場環境",
                                        "市場偏好型態",
                                        "price",
                                        "quote_status",
                                        "rt_source",
                                        "liq20E",
                                        "brk_rr1",
                                        "pb_rr1",
                                        "tier",
                                    ]
                                ],
                                use_container_width=True,
                            )

                            pick_list = out["stock_id"].head(int(top_show)).tolist()
                            if pick_list:
                                picked = st.selectbox("帶入個股分析", pick_list, key="picked_from_scan")
                                st.caption("選好後按「帶入個股分析」，再切到個股分析分頁執行診斷。")
                                if st.button("帶入個股分析", key="btn_use_pick"):
                                    st.session_state["picked_stock"] = picked
                                    st.session_state["stock_input"] = picked
                                    st.rerun()

            except Exception as e:
                st.error(f"市場掃描執行出錯: {type(e).__name__}: {e}")

    if st.session_state.get("screen_df") is not None and not run_scan:
        st.subheader("✅ 上次掃描結果（常駐）")
        st.caption(f"更新時間：{st.session_state.get('screen_ts', '')}")
        st.dataframe(st.session_state["screen_df"].head(int(top_show)), use_container_width=True)


# ===================== TAB A: Single Stock =====================
with tab_a:
    st.subheader("個股分析")

    with st.form("single_stock_form"):
        col1, col2 = st.columns([3, 1])
        with col1:
            stock_id = st.text_input("股票代號", key="stock_input").strip()
        with col2:
            submitted = st.form_submit_button("啟動旗艦診斷", type="primary")

    if submitted:
        with st.spinner("正在執行旗艦級大數據掃描..."):
            try:
                result = evaluate_stock(
                    stock_id=stock_id,
                    total_capital=total_capital,
                    risk_per_trade=risk_per_trade,
                    liq_gate=liq_gate,
                    slip_ticks=slip_ticks,
                    space_atr_mult=space_atr_mult,
                    space_tick_buffer=space_tick_buffer,
                )
                if result is None:
                    st.error("❌ 無法取得資料或指標不足")
                else:
                    render_single_stock_result(result)
            except Exception as e:
                st.error(f"系統執行出錯: {type(e).__name__}: {e}")
