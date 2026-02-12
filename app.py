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
st.set_page_config(page_title="SOP v5.3 全功能全顯版", layout="wide")

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
        if x is None or str(x).strip() in ["-", "", "None", "nan"]: return default
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
st.title("🦅 SOP v5.3 全方位專業操盤系統")

with st.sidebar:
    st.header("⚙️ 風險管理與本金設定")
    total_capital = st.number_input("總操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆交易風險 (%)", 1.0, 5.0, 2.0)
    st.divider()
    st.info("💡 提示：現價會根據 MIS 即時系統自動更新。")

with st.form("query_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        stock_id = st.text_input("股票代號", value="2330").strip()
    with col2:
        submitted = st.form_submit_button("啟動全方位分析", type="primary")

# ============ 6. 核心邏輯 ============
if submitted:
    with st.spinner("正在同步全球數據、法人籌碼與估值動能..."):
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
            # 抓取股票名稱
            df_info = api.taiwan_stock_info()
            stock_name = df_info[df_info['stock_id'] == stock_id]['stock_name'].values[0] if not df_info[df_info['stock_id'] == stock_id].empty else ""

            if df_raw is None or df_raw.empty:
                st.error("❌ 無法取得歷史資料"); st.stop()

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

            hist_last = df.iloc[-1]
            last_trade_date_str = str(hist_last["date"])
            m_code, m_desc, m_clr = get_detailed_market_status(last_trade_date_str)

            # --- 指標計算 ---
            win = min(20, len(df))
            df["MA20"] = df["close"].rolling(win).mean()
            df["MA20_Amount"] = (df["amount"] / 1e8).rolling(win).mean()
            df['change'] = df['close'].diff()
            df['direction'] = np.where(df['change'] > 0, 1, np.where(df['change'] < 0, -1, 0))
            df['OBV'] = (df['direction'] * df['vol']).cumsum()
            df['OBV_MA10'] = df['OBV'].rolling(min(10, len(df))).mean()
            df["H-L"] = df["high"] - df["low"]
            df["H-PC"] = (df["high"] - df["close"].shift(1)).abs()
            df["L-PC"] = (df["low"] - df["close"].shift(1)).abs()
            df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
            df["ATR14"] = df["TR"].rolling(min(14, len(df))).mean()

            # --- 籌碼計算 ---
            trust_5d, foreign_5d, margin_1d = 0, 0, 0
            if df_inst is not None and not df_inst.empty:
                df_inst.columns = [c.strip() for c in df_inst.columns]
                df_inst['buy'] = pd.to_numeric(df_inst['buy'], errors='coerce').fillna(0)
                df_inst['sell'] = pd.to_numeric(df_inst['sell'], errors='coerce').fillna(0)
                df_inst['net'] = (df_inst['buy'] - df_inst['sell']) / 1000
                trust_5d = df_inst[df_inst['name'] == 'Investment_Trust'].tail(5)['net'].sum()
                foreign_5d = df_inst[df_inst['name'] == 'Foreign_Investor'].tail(5)['net'].sum()
            if df_margin is not None and not df_margin.empty:
                df_margin['MarginPurchaseLimit'] = pd.to_numeric(df_margin['MarginPurchaseLimit'], errors='coerce')
                margin_1d = df_margin['MarginPurchaseLimit'].diff().iloc[-1] if len(df_margin) > 1 else 0

        except Exception as e:
            st.error(f"數據處理失敗: {e}"); st.stop()

    # --- Step 7: 即時報價 (MIS) ---
    rt_success, current_price, current_vol = False, float(hist_last["close"]), 0
    data_source = "歷史收盤"
    rt_diff = 0.0
    if "CLOSED" not in m_code:
        try:
            ts = int(time.time() * 1000)
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
            r = requests.get(url, timeout=3)
            res = r.json().get("msgArray", [])
            if res:
                info = res[0]
                z = safe_float(info.get("z")) or safe_float(info.get("y"))
                if z:
                    current_price = z
                    current_vol = safe_float(info.get("v"))
                    rt_success = True
                    data_source = "即時報價"
                    rt_diff = current_price - safe_float(info.get("y"))
        except: pass

    # --- Step 8: 數據融合與計畫 ---
    ma20, avg_amt, atr = safe_float(hist_last.get("MA20")), safe_float(hist_last.get("MA20_Amount")), safe_float(hist_last.get("ATR14"))
    bias_20 = ((current_price - ma20) / ma20 * 100) if ma20 != 0 else 0
    t = tick_size(current_price)
    pivot = float(df.tail(252)["high"].max())
    
    # 計畫點位
    brk_entry = round_to_tick(pivot + max(0.2 * atr, t), t)
    brk_stop = round_to_tick(brk_entry - 1.0 * atr, t)
    pb_low = round_to_tick(max(ma20, current_price - 0.8 * atr), t)
    pb_high = round_to_tick(max(pb_low, current_price - 0.2 * atr), t)
    pb_stop = round_to_tick(pb_low - 1.2 * atr, t)

    risk_amount = total_capital * 10000 * (risk_per_trade / 100)
    stop_dist = brk_entry - brk_stop
    suggested_lots = int(risk_amount / (stop_dist * 1000)) if stop_dist > 0 else 0

    # --- Step 9: UI 呈現 (置頂顯示現價) ---
    st.divider()
    
    # 9.0 置頂核心資訊
    top_col1, top_col2, top_col3 = st.columns([2, 1, 1])
    with top_col1:
        st.header(f"{stock_name} ({stock_id})")
    with top_col2:
        st.metric("目前現價", f"{current_price}", delta=f"{rt_diff:.2f}" if rt_success else "收盤價")
    with top_col3:
        st.subheader(f":{m_clr}[{m_desc}]")

    # 9.1 市場雷達
    st.markdown("### 📡 市場與機會雷達")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("相對強度 (RS)", "強於大盤 🔥" if bias_20 > 0 else "弱於大盤 ❄️", delta=f"{bias_20:.1f}% (乖離)")
    m2.metric("買點距離", "黃金區 ✅" if 0 <= bias_20 <= 3 else "等待", delta=f"{bias_20:.1f}%")
    m3.metric("日均成交額", f"{avg_amt:.2f} 億")
    m4.metric("投信 5D", f"{int(trust_5d)} 張")

    # 9.2 綜合診斷
    st.markdown("### 🤖 系統綜合診斷")
    sig_a, sig_b, sig_c = st.columns(3)
    obv_up = float(hist_last.get("OBV", 0)) > float(hist_last.get("OBV_MA10", 0))
    
    if trust_5d > 500 and margin_1d < 0: sig_a.success("🌟 籌碼：投信鎖碼 + 散戶退場")
    elif margin_1d > 1000: sig_a.warning("⚠️ 籌碼：融資過熱")
    else: sig_a.info("💡 籌碼：穩定")
    
    if current_price >= brk_entry and obv_up: sig_b.error("🚀 突破：帶量衝過壓力位")
    elif pb_low <= current_price <= pb_high: sig_b.success("💎 機會：處於黃金拉回買點")
    else: sig_b.info("⏳ 狀態：等待機會")

    rev_yoy = safe_float(df_rev.iloc[-1].get('revenue_year_growth_rate')) if df_rev is not None and not df_rev.empty else 0
    if rev_yoy > 20: sig_c.success(f"📈 動能：營展開 YoY {rev_yoy:.1f}%")
    else: sig_c.info(f"📊 動能：YoY {rev_yoy:.1f}%")

    # 系統結論
    if "CLOSED" in m_code: msg, clr = "休市中：基於最後交易日分析", "blue"
    elif current_price >= brk_entry: msg, clr = "🔥 強勢突破訊號", "red"
    elif pb_low <= current_price <= pb_high: msg, clr = "🟢 處於黃金拉回區", "green"
    else: msg, clr = "🟡 盤整觀察中", "orange"

    st.info(f"### 系統結論：{stock_name} ({stock_id}) 目前價格 {current_price} -> :{clr}[**{msg}**]")

    # 9.3 交易計畫
    tab1, tab2, tab3 = st.tabs(["⚔️ 交易計畫書", "📈 趨勢圖", "📊 詳細數據"])
    with tab1:
        col_brk, col_pb = st.columns(2)
        with col_brk:
            st.error("### ① Breakout (突破進攻)")
            st.write(f"- **壓力位 (Pivot)**: `{pivot:.2f}`")
            st.write(f"- **進場價**: `{brk_entry:.2f}`")
            st.write(f"- **停損價**: `{brk_stop:.2f}`")
            st.write(f"- **建議部位**: **{suggested_lots}** 張")
        with col_pb:
            st.success("### ② Pullback (拉回低買)")
            st.write(f"- **理想買進區**: `{pb_low:.2f}` ~ `{pb_high:.2f}`")
            st.write(f"- **停損價**: `{pb_stop:.2f}`")
            st.write(f"- **目標價**: `{pivot:.2f}`")

    with tab2:
        chart_df = df.tail(100).copy()
        chart_df["date"] = pd.to_datetime(chart_df["date"])
        base = alt.Chart(chart_df).encode(x='date:T')
        line_p = base.mark_line(color='#2962FF').encode(y=alt.Y('close:Q', scale=alt.Scale(zero=False), title='股價'))
        line_o = base.mark_line(color='#FF6D00').encode(y=alt.Y('OBV:Q', scale=alt.Scale(zero=False), title='OBV'))
        st.altair_chart(alt.layer(line_p, line_o).resolve_scale(y='independent').interactive(), use_container_width=True)

    with tab3:
        if df_rev is not None: st.write("### 營收趨勢"), st.dataframe(df_rev.tail(6))
        if df_inst is not None: st.write("### 法人詳細動態"), st.dataframe(df_inst.tail(10))
