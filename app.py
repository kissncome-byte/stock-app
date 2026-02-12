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
st.set_page_config(page_title="SOP v5.2 全功能終極版", layout="wide")

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
st.title("🦅 SOP v5.2 全方位專業操盤系統")

with st.sidebar:
    st.header("⚙️ 風險管理與本金設定")
    total_capital = st.number_input("總操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆交易風險 (%)", 1.0, 5.0, 2.0)
    st.caption("註：單筆風險 2% 代表若停損則損失本金的 2%。")
    st.divider()
    st.info("💡 提示：本系統會自動判斷目前是否休市，並給出突破與拉回兩種交易方案。")

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

            if df_raw is None or df_raw.empty:
                st.error("❌ 無法取得歷史資料"); st.stop()

            # --- 數據清洗 ---
            df = df_raw.copy()
            df.columns = [c.strip() for c in df.columns]
            mapping = {"Trading_Volume": "vol", "Trading_Money": "amount", "max": "high", "min": "low", "close": "close", "date": "date"}
            for old, new in mapping.items():
                if old in df.columns: df = df.rename(columns={old: new})
            if "amount" not in df.columns or df["amount"].sum() == 0:
                df["amount"] = df["close"] * df["vol"] * 1000
            for c in ["close", "high", "low", "vol", "amount"]:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
            df = df[df['vol'] > 0].copy()

            hist_last = df.iloc[-1]
            last_trade_date_str = str(hist_last["date"])
            m_code, m_desc, m_clr = get_detailed_market_status(last_trade_date_str)
            st.subheader(f"市場狀態：:{m_clr}[{m_desc}]")

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

            # --- 籌碼與估值 ---
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

            current_pe = 0.0
            if df_per is not None and not df_per.empty:
                df_per.columns = [c.upper().strip() for c in df_per.columns]
                pe_col = next((c for c in ["PE", "PER", "P/E"] if c in df_per.columns), None)
                if pe_col: current_pe = safe_float(df_per.iloc[-1][pe_col])

            # --- 大盤指標 ---
            idx_5d, m_trend, m_ma20 = 0, "未知", 0
            if df_index is not None and not df_index.empty:
                df_index["close"] = pd.to_numeric(df_index["close"], errors='coerce')
                m_ma20 = df_index["close"].rolling(20).mean().iloc[-1]
                idx_l = df_index.iloc[-1]
                m_trend = "多頭" if idx_l["close"] > m_ma20 else "空頭"
                if len(df_index) > 5:
                    p_idx = df_index.iloc[-6]["close"]
                    idx_5d = ((idx_l["close"] - p_idx) / p_idx) * 100

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
    ma20, avg_amt, atr = safe_float(hist_last.get("MA20")), safe_float(hist_last.get("MA20_Amount")), safe_float(hist_last.get("ATR14"))
    bias_20 = ((current_price - ma20) / ma20 * 100) if ma20 != 0 else 0
    s_5d = 0
    if len(df) > 5:
        p_s = float(df.iloc[-6]["close"])
        s_5d = ((current_price - p_s) / p_s) * 100

    t = tick_size(current_price)
    pivot = float(df.tail(252)["high"].max())
    
    # 策略點位
    # 1. 突破方案
    brk_entry = round_to_tick(pivot + max(0.2 * atr, t), t)
    brk_stop = round_to_tick(brk_entry - 1.0 * atr, t)
    
    # 2. 拉回方案 (低買機會)
    pb_zone_high = round_to_tick(max(ma20, current_price - 0.2 * atr), t)
    pb_zone_low = round_to_tick(max(ma20 - 0.5 * atr, current_price - 0.8 * atr), t)
    pb_stop = round_to_tick(pb_zone_low - 1.0 * atr, t)

    # 風控計算
    risk_amount = total_capital * 10000 * (risk_per_trade / 100)
    stop_dist = brk_entry - brk_stop
    suggested_lots = int(risk_amount / (stop_dist * 1000)) if stop_dist > 0 else 0

    # --- Step 9: UI 呈現 ---
    st.divider()
    
    # 9.1 市場與機會雷達
    st.markdown("### 📡 市場與機會雷達 (Market & Opportunity Radar)")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("大盤趨勢", m_trend, delta=f"MA20: {m_ma20:.0f}", delta_color="off")
    m2.metric("相對強度 (RS)", "強於大盤 🔥" if s_5d > idx_5d else "弱於大盤 ❄️", delta=f"{s_5d:.1f}% vs {idx_5d:.1f}%")
    
    # 買點距離 (v5.1 新增)
    dist_to_buy = bias_20
    r_label = "黃金買區 ✅" if 0 <= dist_to_buy <= 3 else ("超跌機會" if dist_to_buy < -5 else "等待拉回")
    m3.metric("買點距離/位階", r_label, delta=f"{dist_to_buy:.1f}%", delta_color="normal" if 0 <= dist_to_buy <= 3 else "off")
    
    m4.metric("日均成交額", f"{avg_amt:.2f} 億")

    # 9.2 籌碼與基本面體檢 (v5.0 核心)
    st.markdown("### 🧬 籌碼與基本面體檢")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("投信 5D", f"{int(trust_5d)} 張", delta="法人動向")
    k2.metric("外資 5D", f"{int(foreign_5d)} 張", delta="大金主動向")
    k3.metric("融資增減", f"{int(margin_1d)} 張", delta="散戶動向", delta_color="inverse")
    
    rev_yoy = safe_float(df_rev.iloc[-1].get('revenue_year_growth_rate')) if df_rev is not None and not df_rev.empty else 0
    k4.metric("營收 YoY / PE", f"{rev_yoy:.1f}%", delta=f"PE: {current_pe:.1f}")

    # 9.3 綜合診斷訊號
    st.markdown("### 🤖 綜合診斷訊號")
    sig_a, sig_b, sig_c = st.columns(3)
    
    # 籌碼診斷
    if trust_5d > 500 and margin_1d < 0: sig_a.success("🌟 籌碼完美：投信鎖碼 + 散戶退場")
    elif margin_1d > 1000: sig_a.warning("⚠️ 籌碼警示：融資過熱，小心洗盤")
    else: sig_a.info("💡 籌碼狀態：中性穩定")
    
    # 量價診斷
    obv_up = float(hist_last.get("OBV", 0)) > float(hist_last.get("OBV_MA10", 0))
    if current_price >= brk_entry and obv_up: sig_b.error("🚀 攻擊訊號：帶量突破關鍵壓力")
    elif pb_zone_low <= current_price <= pb_zone_high: sig_b.success("💎 買點機會：處於黃金拉回區")
    else: sig_b.info("⏳ 狀態：等待突破或拉回")

    # 動能診斷
    if rev_yoy > 20: sig_c.success(f"📈 動能強勁：營收 YoY {rev_yoy:.1f}%")
    else: sig_c.info(f"📊 基本面：估值 PE {current_pe:.1f}")

    st.info(f"### 系統結論：{current_price} (資料來源: {data_source})")

    # 9.4 雙路徑交易計畫 (v5.0 + v5.1)
    tab1, tab2, tab3 = st.tabs(["⚔️ 交易計畫書 (必看)", "📈 趨勢觀測圖", "📊 詳細數據表"])
    
    with tab1:
        col_brk, col_pb = st.columns(2)
        with col_brk:
            st.error("### ① Breakout (突破進攻方案)")
            st.markdown(f"""
            - **關鍵壓力 (Pivot)**: `{pivot:.2f}`
            - **進場觸發價**: `{brk_entry:.2f}`
            - **停損出場價**: `{brk_stop:.2f}`
            - **目標 TP1 (+2ATR)**: `{round_to_tick(brk_entry + 2*atr, t):.2f}`
            - **目標 TP2 (+4ATR)**: `{round_to_tick(brk_entry + 4*atr, t):.2f}`
            """)
            st.warning(f"**🛡️ 風控建議**: 建議最大部位 **{suggested_lots}** 張")

        with col_pb:
            st.success("### ② Pullback (拉回守備方案) - 捕捉低價")
            st.markdown(f"""
            - **黃金買進區間**: `{pb_zone_low:.2f}` ~ `{pb_zone_high:.2f}`
            - **停損出場價**: `{pb_stop:.2f}`
            - **目標 TP1 (前高)**: `{pivot:.2f}`
            - **目標 TP2 (突破延展)**: `{round_to_tick(brk_entry + 2*atr, t):.2f}`
            """)
            st.caption("註：適合在股價回測 MA20 附近時，分批布局低價部位。")

    with tab2:
        chart_df = df.tail(100).copy()
        chart_df["date"] = pd.to_datetime(chart_df["date"])
        base = alt.Chart(chart_df).encode(x='date:T')
        line_p = base.mark_line(color='#2962FF').encode(y=alt.Y('close:Q', scale=alt.Scale(zero=False), title='股價'))
        line_o = base.mark_line(color='#FF6D00').encode(y=alt.Y('OBV:Q', scale=alt.Scale(zero=False), title='OBV'))
        line_ma = base.mark_line(color='rgba(0,0,0,0.3)', strokeDash=[5,5]).encode(y='MA20:Q')
        st.altair_chart(alt.layer(line_p, line_o, line_ma).resolve_scale(y='independent').interactive(), use_container_width=True)

    with tab3:
        st.write("### 營收趨勢")
        if df_rev is not None: st.dataframe(df_rev.tail(6))
        st.write("### 法人詳細動態 (近10日)")
        if df_inst is not None: st.dataframe(df_inst.tail(10))
