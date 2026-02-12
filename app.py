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
st.set_page_config(page_title="SOP v6.0 策略整合引擎", layout="wide")

# ============ 2. 市場狀態判斷 (台北時區) ============
def get_detailed_market_status(last_trade_date_str):
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    today_str = now.strftime('%Y-%m-%d')
    weekday = now.weekday() 
    current_time = now.time()
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    if weekday >= 5: return "CLOSED_WEEKEND", "市場休市 (週末)", "gray"
    if today_str != last_trade_date_str and current_time > datetime.strptime("10:00", "%H:%M").time():
        return "CLOSED_HOLIDAY", "市場休市 (國定假日)", "gray"
    if current_time < start_time: return "PRE_MARKET", "盤前準備中", "blue"
    elif start_time <= current_time <= end_time: return "OPEN", "市場交易中", "red"
    else: return "POST_MARKET", "今日已收盤", "green"

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
st.title("🦅 SOP v6.0 全方位策略整合引擎")

with st.sidebar:
    st.header("⚙️ 資金與風險管理")
    total_capital = st.number_input("總操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆交易風險 (%)", 1.0, 5.0, 2.0)
    st.divider()
    st.caption("策略引擎會根據籌碼強度自動調整目標價位。")

with st.form("query_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        stock_id = st.text_input("股票代號", value="2330").strip()
    with col2:
        submitted = st.form_submit_button("啟動全方位診斷", type="primary")

# ============ 6. 核心數據處理 ============
if submitted:
    with st.spinner("策略引擎運算中，正在整合籌碼、技術、基本面因子..."):
        try:
            api = DataLoader()
            api.login_by_token(FINMIND_TOKEN)
            
            # 1. 抓取所有維度數據
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            short_start = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
            
            df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
            df_index = api.taiwan_stock_daily(stock_id='TAIEX', start_date=start_date)
            df_inst = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=short_start)
            df_margin = api.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=short_start)
            df_rev = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d'))
            df_per = api.taiwan_stock_per_pbr(stock_id=stock_id, start_date=short_start)
            
            df_info = api.taiwan_stock_info()
            stock_name = df_info[df_info['stock_id'] == stock_id]['stock_name'].values[0] if not df_info[df_info['stock_id'] == stock_id].empty else "未知股票"

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
            last_date_str = str(hist_last["date"])
            m_code, m_desc, m_clr = get_detailed_market_status(last_date_str)

            # --- 指標計算 ---
            win = min(20, len(df))
            df["MA20"] = df["close"].rolling(win).mean()
            df["MA20_Amount"] = (df["amount"] / 1e8).rolling(win).mean()
            
            # ATR & OBV
            df["H-L"] = df["high"] - df["low"]
            df["H-PC"] = (df["high"] - df["close"].shift(1)).abs()
            df["L-PC"] = (df["low"] - df["close"].shift(1)).abs()
            df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
            df["ATR14"] = df["TR"].rolling(min(14, len(df))).mean()
            
            df['change'] = df['close'].diff()
            df['direction'] = np.where(df['change'] > 0, 1, np.where(df['change'] < 0, -1, 0))
            df['OBV'] = (df['direction'] * df['vol']).cumsum()
            df['OBV_MA10'] = df['OBV'].rolling(min(10, len(df))).mean()

            # --- 籌碼權重分 ---
            score = 0
            signals = []
            
            trust_5d, foreign_5d, margin_1d = 0, 0, 0
            if df_inst is not None and not df_inst.empty:
                df_inst.columns = [c.strip() for c in df_inst.columns]
                df_inst['net'] = (pd.to_numeric(df_inst['buy']) - pd.to_numeric(df_inst['sell'])) / 1000
                trust_5d = df_inst[df_inst['name'] == 'Investment_Trust'].tail(5)['net'].sum()
                foreign_5d = df_inst[df_inst['name'] == 'Foreign_Investor'].tail(5)['net'].sum()
                if trust_5d > 0: score += 1; signals.append("✅ 投信近5日買超")
                if foreign_5d > 0: score += 1; signals.append("✅ 外資近5日買超")
            
            if df_margin is not None and not df_margin.empty:
                df_margin['MarginPurchaseLimit'] = pd.to_numeric(df_margin['MarginPurchaseLimit'], errors='coerce')
                margin_1d = df_margin['MarginPurchaseLimit'].diff().iloc[-1] if len(df_margin) > 1 else 0
                if margin_1d < 0: score += 1; signals.append("✅ 融資減肥 (籌碼安定)")
                elif margin_1d > 1000: score -= 1; signals.append("❌ 融資過熱 (散戶進場)")

            # --- 基本面分 ---
            rev_yoy = safe_float(df_rev.iloc[-1].get('revenue_year_growth_rate')) if df_rev is not None and not df_rev.empty else 0
            if rev_yoy > 20: score += 1; signals.append("✅ 營收動能強勁")
            
            current_pe = 0.0
            if df_per is not None and not df_per.empty:
                df_per.columns = [c.upper().strip() for c in df_per.columns]
                pe_col = next((c for c in ["PE", "PER", "P/E"] if c in df_per.columns), None)
                if pe_col: 
                    current_pe = safe_float(df_per.iloc[-1][pe_col])
                    if 0 < current_pe < 20: score += 1; signals.append("✅ 估值合理 (PE < 20)")

            # --- 大盤分 ---
            idx_5d, m_trend = 0, "未知"
            if df_index is not None and not df_index.empty:
                df_index["close"] = pd.to_numeric(df_index["close"])
                m_ma20 = df_index["close"].rolling(20).mean().iloc[-1]
                idx_l = df_index.iloc[-1]["close"]
                m_trend = "多頭" if idx_l > m_ma20 else "空頭"
                if m_trend == "多頭": score += 1
                if len(df_index) > 5:
                    p_idx = df_index.iloc[-6]["close"]
                    idx_5d = ((idx_l - p_idx) / p_idx) * 100

        except Exception as e:
            st.error(f"數據處理失敗: {e}"); st.stop()

    # --- Step 7: 即時報價 ---
    rt_success, current_price, rt_diff = False, float(hist_last["close"]), 0.0
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
                    current_price, rt_success = z, True
                    rt_diff = current_price - safe_float(info.get("y"))
        except: pass

    # --- Step 8: 策略整合結論 (Strategy Engine) ---
    ma20, avg_amt, atr = safe_float(hist_last.get("MA20")), safe_float(hist_last.get("MA20_Amount")), safe_float(hist_last.get("ATR14"))
    bias_20 = ((current_price - ma20) / ma20 * 100) if ma20 != 0 else 0
    t = tick_size(current_price)
    pivot = float(df.tail(252)["high"].max())
    
    # 技術面判定
    is_breaking = current_price >= pivot
    is_pulling_back = (0 <= bias_20 <= 3)
    obv_up = float(hist_last.get("OBV", 0)) > float(hist_last.get("OBV_MA10", 0))
    if obv_up: score += 1; signals.append("✅ 量能(OBV)多頭排列")

    # 綜合結論邏輯
    final_action = ""
    action_color = "gray"
    
    if "CLOSED" in m_code:
        final_action = "休市中：請參考下方交易計畫做週末功課"
        action_color = "blue"
    elif is_breaking:
        if score >= 4:
            final_action = f"🔥 強力突破：籌碼、量能、基本面全數支持 (評分: {score})"
            action_color = "red"
        elif score >= 2:
            final_action = f"🚀 技術性突破：籌碼普通，建議小量參與 (評分: {score})"
            action_color = "orange"
        else:
            final_action = f"⚠️ 假突破疑慮：價格創新高但無籌碼支持，慎防拉回 (評分: {score})"
            action_color = "gray"
    elif is_pulling_back:
        if score >= 3:
            final_action = f"🟢 黃金買點：強勢股回測支撐，籌碼依然穩健 (評分: {score})"
            action_color = "green"
        else:
            final_action = f"🟡 觀察買點：回測支撐但動能不足，分批布局 (評分: {score})"
            action_color = "orange"
    else:
        final_action = f"⏳ 盤整觀察：目前位階不具備優勢，等待訊號 (評分: {score})"
        action_color = "blue"

    # --- Step 9: UI 呈現 ---
    st.divider()
    
    # 9.0 置頂儀表板
    top1, top2, top3 = st.columns([2, 1, 1])
    with top1: st.header(f"{stock_name} ({stock_id})")
    with top2: st.metric("目前現價", f"{current_price}", delta=f"{rt_diff:.2f}" if rt_success else "昨日收盤")
    with top3: st.subheader(f":{m_clr}[{m_desc}]")

    # 9.1 系統整合決策 (置頂強調)
    st.info(f"### 🎯 策略整合結論 -> :{action_color}[**{final_action}**]")
    with st.expander("🔍 查看評分組成訊號"):
        for s in signals: st.write(s)

    # 9.2 核心數據雷達
    st.markdown("### 📡 核心數據雷達")
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("相對強度 (RS)", "強於大盤" if bias_20 > 0 else "弱於大盤", delta=f"{bias_20:.1f}%")
    d2.metric("投信 5D", f"{int(trust_5d)} 張")
    d3.metric("外資 5D", f"{int(foreign_5d)} 張")
    d4.metric("營收 YoY", f"{rev_yoy:.1f}%")

    # 9.3 交易計畫 (動態調整)
    tab1, tab2, tab3 = st.tabs(["⚔️ 整合交易計畫", "📈 趨勢觀測", "📋 詳細數據"])
    
    # 根據籌碼強度調整目標價倍數
    tp_multiplier = 2.0 if score < 4 else 3.0 # 籌碼強，目標看更遠
    
    with tab1:
        col_brk, col_pb = st.columns(2)
        with col_brk:
            brk_entry = round_to_tick(pivot + max(0.2 * atr, t), t)
            brk_stop = round_to_tick(brk_entry - 1.0 * atr, t)
            st.error("### ① Breakout 進攻方案")
            st.write(f"- **進場觸發**: `{brk_entry:.2f}` (帶量突破)")
            st.write(f"- **停損價位**: `{brk_stop:.2f}`")
            st.write(f"- **目標 TP1**: `{round_to_tick(brk_entry + tp_multiplier*atr, t):.2f}`")
            
            # 風控計算
            risk_amt = total_capital * 10000 * (risk_per_trade / 100)
            lots = int(risk_amt / ((brk_entry - brk_stop) * 1000)) if (brk_entry-brk_stop)>0 else 0
            st.write(f"🛡️ **建議部位**: **{lots}** 張")

        with col_pb:
            pb_l = round_to_tick(max(ma20, current_price - 0.8 * atr), t)
            pb_h = round_to_tick(max(pb_l, current_price - 0.2 * atr), t)
            st.success("### ② Pullback 守備方案")
            st.write(f"- **黃金買區**: `{pb_l:.2f}` ~ `{pb_h:.2f}`")
            st.write(f"- **停損價位**: `{round_to_tick(pb_l - 1.2 * atr, t):.2f}`")
            st.write(f"- **目標價位**: `{pivot:.2f}`")

    with tab2:
        chart_df = df.tail(120).copy()
        chart_df["date"] = pd.to_datetime(chart_df["date"])
        base = alt.Chart(chart_df).encode(x='date:T')
        line_p = base.mark_line(color='#2962FF').encode(y=alt.Y('close:Q', scale=alt.Scale(zero=False)))
        line_ma = base.mark_line(color='rgba(0,0,0,0.3)', strokeDash=[5,5]).encode(y='MA20:Q')
        st.altair_chart(alt.layer(line_p, line_ma).interactive(), use_container_width=True)

    with tab3:
        c1, c2 = st.columns(2)
        with c1: 
            st.write("### 法人動態")
            if df_inst is not None: st.dataframe(df_inst.tail(10))
        with c2:
            st.write("### 融資增減")
            if df_margin is not None: st.dataframe(df_margin.tail(10))
