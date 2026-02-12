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
st.set_page_config(page_title="SOP v4.1 穩定專業版", layout="wide")

# ============ 2. 智慧市場狀態判斷 ============
def get_detailed_market_status(last_trade_date_str):
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    today_str = now.strftime('%Y-%m-%d')
    weekday = now.weekday() 
    current_time = now.time()
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    if weekday >= 5: return "CLOSED_WEEKEND", f"市場休市 (週末) - 顯示 {last_trade_date_str} 數據", "gray"
    if today_str != last_trade_date_str and current_time > datetime.strptime("10:00", "%H:%M").time():
        return "CLOSED_HOLIDAY", f"市場休市 (國定假日) - 顯示 {last_trade_date_str} 數據", "gray"
    if current_time < start_time: return "PRE_MARKET", f"盤前準備中 - 參考 {last_trade_date_str} 數據", "blue"
    elif start_time <= current_time <= end_time: return "OPEN", "市場交易中 (即時更新)", "red"
    else: return "POST_MARKET", f"今日已收盤 - 數據日期: {today_str}", "green"

# ============ 3. 輔助函式 ============
def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ["-", "", "None"]: return default
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
        st.title("🔐 專業系統登入")
        pw = st.text_input("Access Password", type="password")
        if st.button("Login"):
            if pw == APP_PASSWORD:
                st.session_state.authed = True
                st.rerun()
        st.stop()

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

# ============ 5. 主介面 ============
st.title("🦅 SOP v4.1 全方位專業操盤系統")

with st.sidebar:
    st.header("⚙️ 風險設定")
    total_capital = st.number_input("總操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆交易風險 (%)", 1.0, 5.0, 2.0)
    st.caption("註：單筆風險 2% 代表停損時僅損失本金的 2%。")

with st.form("query_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        stock_id = st.text_input("股票代號", value="2330").strip()
    with col2:
        submitted = st.form_submit_button("啟動全方位分析", type="primary")

# ============ 6. 核心邏輯 ============
if submitted:
    with st.spinner("正在同步歷史、籌碼、法人與估值數據..."):
        try:
            api = DataLoader()
            api.login_by_token(FINMIND_TOKEN)
            
            # 1. 抓取數據
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            short_start = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
            
            df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
            df_index = api.taiwan_stock_daily(stock_id='TAIEX', start_date=start_date)
            df_inst = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=short_start)
            df_margin = api.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=short_start)
            df_rev = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d'))
            df_per = api.taiwan_stock_per_pbr(stock_id=stock_id, start_date=short_start)

            if df_raw is None or df_raw.empty:
                st.error("❌ 無法取得歷史資料"); st.stop()

            # --- 數據清洗 (個股) ---
            df = df_raw.copy()
            df.columns = [c.strip() for c in df.columns]
            mapping = {"Trading_Volume": "vol", "Trading_Money": "amount", "max": "high", "min": "low", "close": "close", "date": "date"}
            for old, new in mapping.items():
                if old in df.columns: df = df.rename(columns={old: new})
            if "amount" not in df.columns: df["amount"] = df["close"] * df["vol"] * 1000
            for c in ["close", "high", "low", "vol", "amount"]:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
            df = df[df['vol'] > 0].copy()

            hist_last = df.iloc[-1]
            last_trade_date_str = str(hist_last["date"])
            m_code, m_desc, m_clr = get_detailed_market_status(last_trade_date_str)
            st.subheader(f"市場狀態：:{m_clr}[{m_desc}]")

            # --- 指標計算 ---
            df["MA20"] = df["close"].rolling(20).mean()
            df["MA20_Amount"] = (df["amount"] / 1e8).rolling(20).mean()
            df['change'] = df['close'].diff()
            df['direction'] = np.where(df['change'] > 0, 1, np.where(df['change'] < 0, -1, 0))
            df['OBV'] = (df['direction'] * df['vol']).cumsum()
            df['OBV_MA10'] = df['OBV'].rolling(10).mean()
            df["H-L"] = df["high"] - df["low"]
            df["H-PC"] = (df["high"] - df["close"].shift(1)).abs()
            df["L-PC"] = (df["low"] - df["close"].shift(1)).abs()
            df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
            df["ATR14"] = df["TR"].rolling(14).mean()

            # --- 籌碼計算 (外資+投信) ---
            trust_5d, foreign_5d = 0, 0
            if df_inst is not None and not df_inst.empty:
                df_inst.columns = [c.strip() for c in df_inst.columns]
                df_inst['buy'] = pd.to_numeric(df_inst['buy'], errors='coerce').fillna(0)
                df_inst['sell'] = pd.to_numeric(df_inst['sell'], errors='coerce').fillna(0)
                df_inst['net'] = (df_inst['buy'] - df_inst['sell']) / 1000
                trust_5d = df_inst[df_inst['name'] == 'Investment_Trust'].tail(5)['net'].sum()
                foreign_5d = df_inst[df_inst['name'] == 'Foreign_Investor'].tail(5)['net'].sum()
            
            # --- 🛠️ 估值計算 (修復 PE KeyError) ---
            current_pe = 0.0
            if df_per is not None and not df_per.empty:
                # 自動搜尋可能的 PE 欄位名稱
                df_per.columns = [c.upper().strip() for c in df_per.columns]
                pe_col = next((c for c in ["PE", "PER", "P/E"] if c in df_per.columns), None)
                if pe_col:
                    current_pe = safe_float(df_per.iloc[-1][pe_col])

        except Exception as e:
            st.error(f"數據處理失敗: {e}"); st.stop()

    # --- Step 7: 即時報價 ---
    rt_success, current_price, current_vol = False, float(hist_last["close"]), 0
    data_source = "歷史收盤數據"
    if "CLOSED" not in m_code:
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

    # --- Step 8: 數據融合與計畫計算 ---
    ma20, avg_amt, atr = float(hist_last["MA20"]), float(hist_last["MA20_Amount"]), float(hist_last["ATR14"])
    high_52w = float(df.tail(252)["high"].max())
    t = tick_size(current_price)
    
    pivot = high_52w
    brk_entry = round_to_tick(pivot + max(0.2 * atr, t), t)
    brk_stop = round_to_tick(brk_entry - 1.0 * atr, t)
    
    # 風控計算
    risk_amount = total_capital * 10000 * (risk_per_trade / 100)
    stop_distance = brk_entry - brk_stop
    suggested_lots = int(risk_amount / (stop_distance * 1000)) if stop_distance > 0 else 0

    # --- Step 9: UI 呈現 ---
    st.divider()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("估值位階", f"PE: {current_pe if current_pe > 0 else 'N/A'}", delta="基本面" if current_pe < 25 else "偏高")
    m2.metric("乖離率 (Bias)", f"{((current_price-ma20)/ma20*100):.1f}%", delta="過熱" if (current_price-ma20)/ma20*100 > 15 else "正常", delta_color="inverse")
    m3.metric("投信 5D", f"{int(trust_5d)} 張")
    m4.metric("外資 5D", f"{int(foreign_5d)} 張")

    f_obv = float(hist_last["OBV"]) + (current_vol if current_price > float(hist_last["close"]) else -current_vol if current_price < float(hist_last["close"]) else 0)
    obv_up = f_obv > float(hist_last["OBV_MA10"])
    
    if "CLOSED" in m_code: msg, clr = "休市中：基於最後交易日分析", "blue"
    elif current_price >= brk_entry and obv_up: msg, clr = "🔥 強勢突破訊號", "red"
    else: msg, clr = "🟡 盤整觀察中", "orange"

    st.info(f"### 系統診斷：{current_price} (來源: {data_source}) -> :{clr}[**{msg}**]")

    tab1, tab2, tab3 = st.tabs(["⚔️ 專業交易計畫", "📈 趨勢觀測", "📊 詳細數據"])
    with tab1:
        col_plan, col_risk = st.columns([2, 1])
        with col_plan:
            st.error("### 🚀 Breakout 進攻方案")
            st.markdown(f"""
            - **關鍵壓力 (Pivot)**: `{pivot:.2f}`
            - **進場觸發價**: `{brk_entry:.2f}`
            - **停損出場價**: `{brk_stop:.2f}`
            - **目標 TP1 (+2ATR)**: `{round_to_tick(brk_entry + 2*atr, t):.2f}`
            - **目標 TP2 (+4ATR)**: `{round_to_tick(brk_entry + 4*atr, t):.2f}`
            """)
        with col_risk:
            st.warning("### 🛡️ 風控建議")
            st.write(f"單筆承擔風險: `${int(risk_amount):,}`")
            st.write(f"建議最大部位: **{suggested_lots}** 張")
            st.caption("註：停損時僅損失設定的風險金額。")

    with tab2:
        chart_df = df.tail(100).copy()
        chart_df["date"] = pd.to_datetime(chart_df["date"])
        base = alt.Chart(chart_df).encode(x='date:T')
        line_p = base.mark_line(color='#2962FF').encode(y=alt.Y('close:Q', scale=alt.Scale(zero=False), title='股價'))
        line_o = base.mark_line(color='#FF6D00').encode(y=alt.Y('OBV:Q', scale=alt.Scale(zero=False), title='OBV'))
        st.altair_chart(alt.layer(line_p, line_o).resolve_scale(y='independent').interactive(), use_container_width=True)

    with tab3:
        c_a, c_b = st.columns(2)
        with c_a:
            st.write("### 營收趨勢")
            if df_rev is not None: st.dataframe(df_rev.tail(6))
        with c_b:
            st.write("### 法人詳細動態")
            if df_inst is not None: st.dataframe(df_inst.tail(10))
