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
st.set_page_config(page_title="SOP v3.7 全方位操盤系統", layout="wide")

# ============ 2. 市場狀態判斷 (台北時區) ============
def get_market_status():
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    weekday = now.weekday() 
    current_time = now.time()
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    if weekday >= 5: return "WEEKEND", "市場休市 (週末)", "gray"
    elif current_time < start_time: return "PRE_MARKET", "盤前準備中", "blue"
    elif start_time <= current_time <= end_time: return "OPEN", "市場交易中 (即時更新)", "red"
    else: return "POST_MARKET", "今日已收盤", "green"

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
st.title("🦅 SOP v3.7 全方位操盤系統")
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

    with st.spinner("正在同步全球數據、法人籌碼與營收動能..."):
        try:
            api = DataLoader()
            api.login_by_token(FINMIND_TOKEN)
            
            # 1. 抓取歷史與大盤
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            short_start = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
            
            df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
            df_index = api.taiwan_stock_daily(stock_id='TAIEX', start_date=start_date)
            
            # 2. 抓取籌碼 (三大法人 & 融資)
            df_inst = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=short_start)
            df_margin = api.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=short_start)
            
            # 3. 抓取營收
            df_rev = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d'))

            if df_raw is None or df_raw.empty:
                st.error("❌ 無法取得歷史資料")
                st.stop()

            # --- 數據清洗 ---
            df = df_raw.copy()
            df.columns = [c.strip() for c in df.columns]
            mapping = {"Trading_Volume": "vol", "Trading_Money": "amount", "max": "high", "min": "low", "close": "close", "date": "date"}
            for old, new in mapping.items():
                if old in df.columns: df = df.rename(columns={old: new})
            
            if "amount" not in df.columns: df["amount"] = df["close"] * df["vol"] * 1000
            for c in ["close", "high", "low", "vol", "amount"]:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
            df = df[df['vol'] > 0].copy()

            # --- 指標計算 ---
            df["MA20"] = df["close"].rolling(20).mean()
            df["Amount_Yi"] = df["amount"] / 1e8
            df["MA20_Amount"] = df["Amount_Yi"].rolling(20).mean()
            
            df['change'] = df['close'].diff()
            df['direction'] = np.where(df['change'] > 0, 1, np.where(df['change'] < 0, -1, 0))
            df['OBV'] = (df['direction'] * df['vol']).cumsum()
            df['OBV_MA10'] = df['OBV'].rolling(10).mean()
            
            df["H-L"] = df["high"] - df["low"]
            df["H-PC"] = (df["high"] - df["close"].shift(1)).abs()
            df["L-PC"] = (df["low"] - df["close"].shift(1)).abs()
            df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
            df["ATR14"] = df["TR"].rolling(14).mean()

            hist_last = df.iloc[-1]

            # --- 籌碼計算 ---
            trust_5d_net, margin_1d_change = 0, 0
            if df_inst is not None and not df_inst.empty:
                df_inst['buy'] = pd.to_numeric(df_inst['buy'], errors='coerce').fillna(0)
                df_inst['sell'] = pd.to_numeric(df_inst['sell'], errors='coerce').fillna(0)
                trust = df_inst[df_inst['name'] == 'Investment_Trust'].copy()
                if not trust.empty:
                    trust['net'] = (trust['buy'] - trust['sell']) / 1000
                    trust_5d_net = trust.tail(5)['net'].sum()
            
            if df_margin is not None and not df_margin.empty:
                df_margin['MarginPurchaseLimit'] = pd.to_numeric(df_margin['MarginPurchaseLimit'], errors='coerce')
                if len(df_margin) >= 2:
                    margin_1d_change = df_margin['MarginPurchaseLimit'].diff().iloc[-1]

            # --- 大盤指標 ---
            idx_5d, m_trend, m_ma20 = 0, "未知", 0
            if df_index is not None and not df_index.empty:
                df_index["close"] = pd.to_numeric(df_index["close"], errors='coerce')
                df_index["MA20"] = df_index["close"].rolling(20).mean()
                idx_l = df_index.iloc[-1]
                m_ma20 = idx_l["MA20"]
                m_trend = "多頭 (Bull)" if idx_l["close"] > m_ma20 else "空頭 (Bear)"
                if len(df_index) > 5:
                    p_idx = df_index.iloc[-6]["close"]
                    idx_5d = ((idx_l["close"] - p_idx) / p_idx) * 100

        except Exception as e:
            st.error(f"數據處理失敗: {e}")
            st.stop()

    # --- Step 7: 即時報價 ---
    rt_success, current_price, current_vol = False, float(hist_last["close"]), 0
    data_source = "歷史收盤數據"
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
                if z: current_price, current_vol, rt_success, data_source = z, v or 0, True, "即時報價系統"
        except: pass

    # --- Step 8: 數據融合 ---
    if not rt_success or current_vol == 0:
        f_vol, f_obv = float(hist_last["vol"]), float(hist_last["OBV"])
    else:
        f_vol = current_vol
        if current_price > float(hist_last["close"]): f_obv = float(hist_last["OBV"]) + current_vol
        elif current_price < float(hist_last["close"]): f_obv = float(hist_last["OBV"]) - current_vol
        else: f_obv = float(hist_last["OBV"])

    # --- Step 9: UI 呈現 ---
    ma20, avg_amt, atr = float(hist_last["MA20"]), float(hist_last["MA20_Amount"]), float(hist_last["ATR14"])
    bias_20 = ((current_price - ma20) / ma20) * 100
    s_5d = 0
    if len(df) > 5:
        p_s = float(df.iloc[-6]["close"])
        s_5d = ((current_price - p_s) / p_s) * 100
    
    st.markdown("### 📡 市場雷達 (Market Context)")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("大盤趨勢", m_trend, delta=f"MA20: {m_ma20:.0f}", delta_color="off")
    m2.metric("相對強度 (RS)", "強於大盤 🔥" if s_5d > idx_5d else "弱於大盤 ❄️", delta=f"{s_5d:.1f}% vs {idx_5d:.1f}%")
    m3.metric("乖離率 (Bias)", f"{bias_20:.1f}%", delta="過熱" if bias_20 > 15 else "正常", delta_color="inverse")
    m4.metric("日均成交額", f"{avg_amt:.2f} 億")

    st.divider()

    # 籌碼與基本面列
    st.markdown("### 🧬 籌碼與基本面體檢")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("投信近5日", f"{int(trust_5d_net)} 張", delta="法人動向")
    k2.metric("融資單日增減", f"{int(margin_1d_change)} 張", delta="散戶動向", delta_color="inverse")
    
    rev_yoy = 0
    if df_rev is not None and not df_rev.empty: rev_yoy = safe_float(df_rev.iloc[-1].get('revenue_year_growth_rate'), 0)
    k3.metric("最新月營收 YoY", f"{rev_yoy:.1f}%", delta="成長動能")
    
    obv_up = f_obv > float(hist_last["OBV_MA10"])
    k4.metric("OBV 狀態", "多頭排列" if obv_up else "轉弱/背離", delta="量能指標")

    # 系統診斷
    high_52w = float(df.tail(252)["high"].max())
    t = tick_size(current_price)
    breakout_entry = round_to_tick(high_52w + max(0.2 * atr, t), t)
    pb_low = round_to_tick(max(ma20, current_price - 0.8 * atr), t)
    pb_high = round_to_tick(max(pb_low, current_price - 0.2 * atr), t)

    if market_code == "WEEKEND": msg, clr = "市場休市：顯示最後交易日結果", "blue"
    elif current_price >= breakout_entry and obv_up: msg, clr = "🔥 強勢突破訊號", "red"
    elif pb_low <= current_price <= pb_high: msg, clr = "🟢 處於 Pullback 買進區", "green"
    else: msg, clr = "🟡 盤整觀察中", "orange"

    st.info(f"### 系統診斷：{current_price} (來源: {data_source}) -> :{clr}[**{msg}**]")

    # 圖表
    chart_df = df.tail(100).copy()
    chart_df["date"] = pd.to_datetime(chart_df["date"])
    base = alt.Chart(chart_df).encode(x='date:T')
    line_p = base.mark_line(color='#2962FF').encode(y=alt.Y('close:Q', scale=alt.Scale(zero=False), title='股價'))
    line_o = base.mark_line(color='#FF6D00').encode(y=alt.Y('OBV:Q', scale=alt.Scale(zero=False), title='OBV'))
    st.altair_chart(alt.layer(line_p, line_o).resolve_scale(y='independent').interactive(), use_container_width=True)

    tab1, tab2 = st.tabs(["⚔️ 交易計畫", "📊 詳細數據"])
    with tab1:
        col_a, col_b = st.columns(2)
        with col_a: st.success(f"**拉回買進區**: {pb_low} ~ {pb_high}")
        with col_b: st.error(f"**突破進場點**: {breakout_entry}")
    with tab2:
        c_a, c_b = st.columns(2)
        with c_a:
            st.write("### 營收趨勢")
            if df_rev is not None: st.dataframe(df_rev.tail(6))
        with c_b:
            st.write("### 法人買賣超")
            if df_inst is not None: st.dataframe(df_inst.tail(10))
