import os
import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
import altair as alt
from datetime import datetime, timedelta
import pytz
from FinMind.data import DataLoader

# ============ 1. Page Config ============
st.set_page_config(page_title="SOP v11.3.4 連線突破版", layout="wide")

# ============ 2. 智慧市場狀態判斷 (全新重寫) ============
def get_market_status_label(is_market_open_today: bool, rt_success: bool, last_trade_date_str: str):
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    current_time = now.time()
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    if not rt_success:
        # 如果 API 連線失敗，用時間粗略判斷並誠實顯示
        weekday = now.weekday()
        if weekday >= 5:
            return "CLOSED_WEEKEND", f"市場休市 (週末) | 歷史日期 {last_trade_date_str}"
        elif current_time > end_time:
            return "POST_MARKET", f"今日已收盤 | 歷史日期 {last_trade_date_str}"
        elif start_time <= current_time <= end_time:
            return "API_ERROR", f"即時連線異常 (請稍後再試) | 歷史日期 {last_trade_date_str}"
        else:
            return "PRE_MARKET", f"盤前準備中 | 歷史日期 {last_trade_date_str}"

    # API 連線成功，根據證交所回傳的日期精準判斷
    if is_market_open_today:
        if current_time < start_time:
            return "PRE_MARKET", "盤前準備中 (即時連線正常)"
        elif start_time <= current_time <= end_time:
            return "OPEN", "市場交易中 (即時報價)"
        else:
            return "POST_MARKET", "今日已收盤 (即時報價)"
    else:
        weekday = now.weekday()
        if weekday >= 5:
            return "CLOSED_WEEKEND", f"市場休市 (週末) | 歷史日期 {last_trade_date_str}"
        else:
            return "CLOSED_HOLIDAY", f"市場休市 (假日) | 歷史日期 {last_trade_date_str}"

# ============ 3. 輔助函式 ============
def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ["-", "", "None", "nan"]:
            return default
        return float(str(x).replace(",", ""))
    except:
        return default

def tick_size(p: float) -> float:
    if p >= 1000: return 5.0
    if p >= 500:  return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.01
    return 0.001

def round_to_tick(x: float, t: float) -> float:
    if x is None or np.isnan(x) or t == 0:
        return 0.0
    return round(x / t) * t

# ============ 4. 權限認證 ============
APP_PASSWORD = os.getenv("APP_PASSWORD", "") or st.secrets.get("APP_PASSWORD", "")
if APP_PASSWORD and "authed" not in st.session_state:
    st.session_state.authed = False
if APP_PASSWORD and not st.session_state.authed:
    st.title("🔐 系統登入")
    pw = st.text_input("Access Password", type="password")
    if st.button("Login"):
        if pw == APP_PASSWORD:
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("密碼錯誤")
    st.stop()

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

# ============ 5. 主介面 ============
st.title("🦅 SOP v11.3.4 全方位策略整合引擎")

with st.sidebar:
    st.header("⚙️ 實戰風控設定")
    total_capital = st.number_input("操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆最大風險 (%)", 1.0, 20.0, 2.0)
    st.divider()

    st.header("🛡️ 硬性門檻")
    liq_gate = st.number_input("流動性：MA20成交額(億) ≥", value=2.0, step=0.5)
    slip_ticks = st.number_input("滑價 Buffer (ticks)", value=3, step=1, min_value=0)

    st.info("💡 v11.3.4：突破證交所反爬蟲機制，精準判斷開盤與休市狀態。")

with st.form("query_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        stock_id = st.text_input("股票代號", value="2330").strip()
    with col2:
        submitted = st.form_submit_button("啟動全方位診斷", type="primary")

def render_plan(container, name, entry, stop, tp1, tp2, rr_gate, setup_ok, accent, liq_ok, risk_amt, slip):
    R = entry - stop
    risk_per_share = abs(entry - stop) + slip

    rr = ((tp1 - entry) / R) if R > 0 else 0.0
    rr_ok = rr >= rr_gate

    tradeable = liq_ok and rr_ok
    total_lots = int(risk_amt / (risk_per_share * 1000)) if (tradeable and risk_per_share > 0) else 0

    tp1_lots = total_lots // 2
    runner_lots = total_lots - tp1_lots

    with container:
        st.markdown(f"### {accent} {name}")

        st.write(
            f"Setup {'✅' if setup_ok else '❌'} | "
            f"Liquidity {'✅' if liq_ok else '❌'} | "
            f"RR {rr:.2f} {'✅' if rr_ok else '❌'} | "
            f"Tradeable {'✅YES' if tradeable else '❌NO'}"
        )

        st.write(f"🔹 進場 `{entry:.2f}`  |  🛑 停損 `{stop:.2f}`")
        st.write(f"🎯 目標1 `{tp1
