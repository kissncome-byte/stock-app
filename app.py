import os
import requests
import pandas as pd
import streamlit as st
from FinMind.data import DataLoader

st.set_page_config(page_title="SOP v1.1（進攻型 2–8 週）", layout="wide")

# ============ Login ============
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
if "authed" not in st.session_state:
    st.session_state.authed = False

if not st.session_state.authed:
    st.title("🔐 存取保護")
    pw = st.text_input("請輸入密碼", type="password")
    if pw and APP_PASSWORD and pw == APP_PASSWORD:
        st.session_state.authed = True
        st.rerun()
    st.stop()

# ============ Settings ============
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
if not FINMIND_TOKEN:
    st.error("缺少 FINMIND_TOKEN（請設定環境變數 FINMIND_TOKEN）")
    st.stop()

st.title("📈 SOP v1.1 交易決策（進攻型｜2–8 週）")
stock_id = st.text_input("股票代號", value="2330").strip()

def safe_float(x, default=None):
    try:
        return float(x)
    except:
        return default

def estimate_turnover_yi(price: float, vol_lot: float) -> float:
    # 成交額（億）= 價格 * 張數 * 1000 / 1e8
    return (price * vol_lot * 1000.0) / 1e8

def tick_size(p: float) -> float:
    if p >= 1000: return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.01
    return 0.001

def round_to_tick(x: float, t: float) -> float:
    return round(x / t) * t

if st.button("查詢", type="primary"):
    if not stock_id.isdigit():
        st.error("代號格式不正確（請輸入純數字，如 2330）")
        st.stop()

    # --------- History (FinMind) ---------
    api = DataLoader()
    api.login_by_token(FINMIND_TOKEN)
    df = api.taiwan_stock_daily(stock_id=stock_id, start_date="2023-01-01")

    if df is None or len(df) < 260:
        st.error("歷史資料不足（少於260筆），無法計算 52W高/MA/ATR。")
        st.stop()

    close_col = "close"
    high_col = "max" if "max" in df.columns else ("high" if "high" in df.columns else None)
    low_col  = "min" if "min" in df.columns else ("low" if "low" in df.columns else None)
    if high_col is None or low_col is None:
        st.error(f"欄位不符，找不到 high/low 欄位。現有欄位：{list(df.columns)}")
        st.stop()

    df["MA20"] = df[close_col].rolling(20).mean()
    df["MA50"] = df[close_col].rolling(50).mean()

    df["H-L"]  = df[high_col] - df[low_col]
    df["H-PC"] = (df[high_col] - df[close_col].shift(1)).abs()
    df["L-PC"] = (df[low_col] - df[close_col].shift(1)).abs()
    df["TR"]   = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
    df["ATR14"] = df["TR"].rolling(14).mean()

    latest = df.iloc[-1]
    ma20 = float(latest["MA20"])
    ma50 = float(latest["MA50"])
    atr = float(latest["ATR14"])
    last_close = float(latest[close_col])
    high_52w = float(df.tail(252)[high_col].max())

    # --------- Realtime (TWSE MIS) ---------
    rt_price = rt_vol = None
    rt_date = rt_time = None
    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0"
        r = requests.get(url, timeout=10)
        data = r.json()
        if "msgArray" in data and len(data["msgArray"]) > 0:
            info = data["msgArray"][0]
            rt_price = safe_float(info.get("z"))
            rt_vol = safe_float(info.get("v"))  # 張
            rt_date = info.get("d")
            rt_time = info.get("t")
    except:
        pass

    # --------- Price Mode ---------
    if rt_price is not None and rt_time:
        is_close = (rt_time == "13:30:00")
        used_price = rt_price
        data_time = f"{rt_date} {rt_time}"
        data_type = "收盤價" if is_close else "盤中最近成交價"
        turnover_yi = estimate_turnover_yi(rt_price, rt_vol or 0.0) if rt_vol is not None else None
    else:
        used_price = last_close
        data_time = "（MIS 抓取失敗，改用日K收盤）"
        data_type = "收盤價（替代）"
        turnover_yi = None

    t = tick_size(used_price)

    # --------- Strategy (攻擊型) ---------
    pivot = high_52w

    # Breakout
    breakout_entry = round_to_tick(pivot + max(0.2 * atr, t), t)
    breakout_stop  = round_to_tick(breakout_entry - 1.0 * atr, t)
    tp1 = round_to_tick(breakout_entry + 2.0 * atr, t)
    tp2 = round_to_tick(breakout_entry + 3.0 * atr, t)
    tp3 = round_to_tick(breakout_entry + 4.0 * atr, t)

    # Pullback
    pb_low  = round_to_tick(max(ma20, used_price - 0.8 * atr), t)
    pb_high = round_to_tick(max(pb_low, used_price - 0.2 * atr), t)
    pb_stop = round_to_tick(pb_low - 1.2 * atr, t)
    pb_tp1  = round_to_tick(pivot, t)
    pb_tp2  = tp1
    pb_tp3  = tp2

    if used_price < pb_low:
        action = "觀察（低於 Pullback 區下緣，不追）"
    elif pb_low <= used_price <= pb_high:
        action = "可小倉 Pullback 試單（在區間內）"
    elif used_price < breakout_entry:
        action = "等待觸發（不追價；等 Pullback 或等突破）"
    else:
        action = "突破已觸發（依 Breakout 方案執行）"

    # --------- UI Output ---------
    st.subheader("🧾 資料快照")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("代號", stock_id)
    c2.metric("使用價格", f"{used_price:.2f}")
    c3.metric("資料性質", data_type)
    c4.metric("資料時間", data_time)

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("MA20", f"{ma20:.2f}")
    c6.metric("MA50", f"{ma50:.2f}")
    c7.metric("ATR14", f"{atr:.2f}")
    c8.metric("52W 前高", f"{high_52w:.2f}")

    if turnover_yi is not None:
        st.write(f"**成交額（估算）**：{turnover_yi:.2f} 億")
    else:
        st.write("成交額：**【資料不足，無法確認】**")

    st.subheader("🎯 交易建議（進攻型｜2–8 週）")
    st.success(f"system_action：**{action}**")

    L, R = st.columns(2)

    with L:
        st.markdown("### ① Pullback（逢低先買）")
        st.write(f"Entry 區：**{pb_low:.2f} – {pb_high:.2f}**")
        st.write(f"停損出場價：**{pb_stop:.2f}**")
        st.write(f"目標出場價：**TP1 {pb_tp1:.2f} / TP2 {pb_tp2:.2f} / TP3 {pb_tp3:.2f}**")

    with R:
        st.markdown("### ② Breakout（突破進攻）")
        st.write(f"Pivot（前高）：**{pivot:.2f}**")
        st.write(f"Breakout Entry：**{breakout_entry:.2f}**")
        st.write(f"停損出場價：**{breakout_stop:.2f}**")
        st.write(f"目標出場價：**TP1 {tp1:.2f} / TP2 {tp2:.2f} / TP3 {tp3:.2f}**")}")

