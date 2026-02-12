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
st.set_page_config(page_title="SOP v3.6 全方位操盤系統", layout="wide")

# ============ 2. 市場狀態判斷 (台北時區) ============
def get_market_status():
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    weekday = now.weekday() 
    current_time = now.time()
    
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    if weekday >= 5:
        return "WEEKEND", "市場休市 (週末)", "gray"
    elif current_time < start_time:
        return "PRE_MARKET", "盤前準備中", "blue"
    elif start_time <= current_time <= end_time:
        return "OPEN", "市場交易中 (即時更新)", "red"
    else:
        return "POST_MARKET", "今日已收盤", "green"

# ============ 3. 輔助函式 ============
def safe_float(x, default=None):
    try:
        if x is None or str(x).strip() in ["-", ""]: return default
        return float(str(x).replace(",", ""))
    except: return default

def tick_size(p: float) -> float:
    if p >= 1000: return 5.0
    if p >= 500:  return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.01
    return 0.001

def round_to_tick(x: float, t: float) -> float:
    return round(x / t) * t

# ============ 4. 權限認證 ============
APP_PASSWORD = os.getenv("APP_PASSWORD", "") or st.secrets.get("APP_PASSWORD", "")
if APP_PASSWORD:
    if "authed" not in st.session_state: st.session_state.authed = False
    if not st.session_state.authed:
        st.title("🔐 系統登入")
        pw = st.text_input("Access Password", type="password")
        if st.button("Login"):
            if pw == APP_PASSWORD:
                st.session_state.authed = True
                st.rerun()
        st.stop()

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

# ============ 5. 主介面 ============
market_code, market_desc, market_color = get_market_status()
st.title("🦅 SOP v3.6 全方位操盤系統")
st.subheader(f"市場狀態：:{market_color}[{market_desc}]")

with st.form("query_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        stock_id = st.text_input("股票代號", value="2330").strip()
    with col2:
        submitted = st.form_submit_button("啟動分析", type="primary")

# ============ 6. 核心邏輯 ============
if submitted:
    if not stock_id.isdigit():
        st.error("❌ 代號格式錯誤")
        st.stop()

    with st.spinner("正在同步全球數據與市場狀態..."):
        try:
            api = DataLoader()
            api.login_by_token(FINMIND_TOKEN)
            
            # 1. 抓取歷史資料
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
            df_index = api.taiwan_stock_daily(stock_id='TAIEX', start_date=start_date)
            
            if df_raw is None or df_raw.empty:
                st.error("❌ 無法取得個股歷史資料")
                st.stop()

            # --- 個股數據清洗 ---
            df = df_raw.copy()
            df.columns = [c.strip() for c in df.columns]
            mapping = {"Trading_Volume": "vol", "Trading_Money": "amount", "max": "high", "min": "low", "close": "close", "date": "date"}
            for old, new in mapping.items():
                if old in df.columns: df = df.rename(columns={old: new})
            
            if "amount" not in df.columns: df["amount"] = df["close"] * df["vol"] * 1000
            for c in ["close", "high", "low", "vol", "amount"]:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
            df = df[df['vol'] > 0].copy()

            # --- 指標計算 (修正語法錯誤位置) ---
            df["MA20"] = df["close"].rolling(20).mean()
            df["Amount_Yi"] = df["amount"] / 1e8
            df["MA20_Amount"] = df["Amount_Yi"].rolling(20).mean()
            
            # OBV
            df['change'] = df['close'].diff()
            df['direction'] = np.where(df['change'] > 0, 1, np.where(df['change'] < 0, -1, 0))
            df['OBV'] = (df['direction'] * df['vol']).cumsum()
            df['OBV_MA10'] = df['OBV'].rolling(10).mean()
            
            # ATR (這裡已經拆分，解決 SyntaxError)
            df["H-L"] = df["high"] - df["low"]
            df["H-PC"] = (df["high"] - df["close"].shift(1)).abs()
            df["L-PC"] = (df["low"] - df["close"].shift(1)).abs()
            df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
            df["ATR14"] = df["TR"].rolling(14).mean()

            hist_last = df.iloc[-1]
            
            # 2. 抓取營收
            df_rev = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d'))

            # 3. 大盤指標計算
            index_5d_change, market_trend, market_ma20 = 0, "未知", 0
            if df_index is not None and not df_index.empty:
                df_index["close"] = pd.to_numeric(df_index["close"], errors='coerce')
                df_index["MA20"] = df_index["close"].rolling(20).mean()
                idx_last = df_index.iloc[-1]
                market_ma20 = idx_last["MA20"]
                market_trend = "多頭 (Bull)" if idx_last["close"] > market_ma20 else "空頭 (Bear)"
                if len(df_index) > 5:
                    p_idx = df_index.iloc[-6]["close"]
                    index_5d_change = ((idx_last["close"] - p_idx) / p_idx) * 100

        except Exception as e:
            st.error(f"數據處理失敗: {e}")
            st.stop()

    # --- Step 7: 自動判斷數據源 ---
    rt_success, current_price, current_vol = False, float(hist_last["close"]), 0
    data_source_label = "歷史收盤數據"

    if market_code != "WEEKEND":
        try:
            ts = int(time.time() * 1000)
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
            r = requests.get(url, timeout=3)
            res = r.json().get("msgArray", [])
            if res:
                info = res[0]
                z = safe_float(info.get("z")) or safe_float(info.get("y"))
                v = safe_float(info.get("v"))
                if z:
                    current_price, current_vol, rt_success = z, v or 0, True
                    data_source_label = "即時報價系統"
        except: pass

    # --- Step 8: 數據融合 ---
    if not rt_success or current_vol == 0:
        final_vol, final_amount_yi, final_obv = float(hist_last["vol"]), float(hist_last["Amount_Yi"]), float(hist_last["OBV"])
    else:
        final_vol = current_vol
        final_amount_yi = (current_price * current_vol * 1000) / 1e8
        if current_price > float(hist_last["close"]): final_obv = float(hist_last["OBV"]) + current_vol
        elif current_price < float(hist_last["close"]): final_obv = float(hist_last["OBV"]) - current_vol
        else: final_obv = float(hist_last["OBV"])

    # --- Step 9: 指標判定與 UI ---
    ma20, avg_amount_20, atr = float(hist_last["MA20"]), float(hist_last["MA20_Amount"]), float(hist_last["ATR14"])
    high_52w = float(df.tail(252)["high"].max())
    bias_20 = ((current_price - ma20) / ma20) * 100
    
    stock_5d_change = 0
    if len(df) > 5:
        p_stock = float(df.iloc[-6]["close"])
        stock_5d_change = ((current_price - p_stock) / p_stock) * 100
    is_stronger = stock_5d_change > index_5d_change

    # UI 顯示
    st.markdown("### 📡 市場雷達 (Market Context)")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("大盤趨勢", market_trend, delta=f"MA20: {market_ma20:.0f}", delta_color="off")
    m2.metric("相對強度 (RS)", "強於大盤 🔥" if is_stronger else "弱於大盤 ❄️", delta=f"{stock_5d_change:.1f}% vs {index_5d_change:.1f}%")
    m3.metric("乖離率 (Bias)", f"{bias_20:.1f}%", delta="過熱" if bias_20 > 15 else "正常", delta_color="inverse")
    m4.metric("日均成交額", f"{avg_amount_20:.2f} 億")

    st.divider()
    
    t = tick_size(current_price)
    breakout_entry = round_to_tick(high_52w + max(0.2 * atr, t), t)
    pb_low = round_to_tick(max(ma20, current_price - 0.8 * atr), t)
    pb_high = round_to_tick(max(pb_low, current_price - 0.2 * atr), t)

    obv_up = final_obv > float(hist_last["OBV_MA10"])
    if market_code == "WEEKEND": msg, clr = "市場休市：顯示最後交易日結果", "blue"
    elif current_price >= breakout_entry and obv_up: msg, clr = "🔥 強勢突破訊號", "red"
    elif pb_low <= current_price <= pb_high: msg, clr = "🟢 處於 Pullback 買進區", "green"
    else: msg, clr = "🟡 盤整觀察中", "orange"

    st.info(f"### 系統診斷：{current_price} (資料來源: {data_source_label}) -> :{clr}[**{msg}**]")

    # 圖表
    chart_df = df.tail(100).copy()
    chart_df["date"] = pd.to_datetime(chart_df["date"])
    base = alt.Chart(chart_df).encode(x='date:T')
    line_p = base.mark_line(color='#2962FF').encode(y=alt.Y('close:Q', scale=alt.Scale(zero=False), title='股價'))
    line_o = base.mark_line(color='#FF6D00').encode(y=alt.Y('OBV:Q', scale=alt.Scale(zero=False), title='OBV'))
    st.altair_chart(alt.layer(line_p, line_o).resolve_scale(y='independent').interactive(), use_container_width=True)

    tab1, tab2 = st.tabs(["⚔️ 交易計畫", "📊 營收數據"])
    with tab1:
        col_a, col_b = st.columns(2)
        with col_a: st.success(f"**拉回買進區**: {pb_low} ~ {pb_high}")
        with col_b: st.error(f"**突破進場點**: {breakout_entry}")
    with tab2:
        if df_rev is not None and not df_rev.empty:
            st.write("### 最近月營收趨勢")
            st.dataframe(df_rev.tail(6))
        else: st.warning("暫無營收數據")
