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
st.set_page_config(page_title="SOP v11.3 終極整合系統", layout="wide")

# ============ 2. 智慧市場狀態判斷 ============
def get_detailed_market_status(last_trade_date_str):
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    today_str = now.strftime('%Y-%m-%d')
    weekday = now.weekday() 
    current_time = now.time()
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    if weekday >= 5: return "CLOSED_WEEKEND", "市場休市 (週末)"
    if today_str != last_trade_date_str and current_time > datetime.strptime("10:00", "%H:%M").time():
        return "CLOSED_HOLIDAY", "市場休市 (國定假日)"
    if current_time < start_time: return "PRE_MARKET", "盤前準備中"
    elif start_time <= current_time <= end_time: return "OPEN", "市場交易中"
    else: return "POST_MARKET", "今日已收盤"

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
    if np.isnan(x) or t == 0: return 0.0
    return round(x / t) * t

# ============ 4. 權限與 Token ============
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

# ============ 5. 主介面 ============
st.title("🦅 SOP v11.3 全方位策略整合引擎")

with st.sidebar:
    st.header("⚙️ 實戰風控與門檻")
    total_capital = st.number_input("操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆最大風險 (%)", 1.0, 5.0, 2.0)
    st.divider()
    st.header("🛡️ 硬性門檻 (Gates)")
    liq_gate = st.number_input("流動性：MA20成交額(億) ≥", value=2.0, step=0.5)
    slip_ticks = st.number_input("滑價 Buffer (ticks)", value=3, step=1)
    st.info("💡 v11.3 更新：1. 正統 ATR 算法 2. 修正成交額單位 3. 加入 OBV 量價背離警示。")

with st.form("query_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        stock_id = st.text_input("股票代號", value="2330").strip()
    with col2:
        submitted = st.form_submit_button("啟動全方位診斷", type="primary")

# ============ 6. 核心數據處理 ============
if submitted:
    with st.spinner("正在執行工業級數據校準與背離偵測..."):
        try:
            api = DataLoader()
            if FINMIND_TOKEN: api.login_by_token(FINMIND_TOKEN)
            
            # 1. 數據抓取
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            short_start = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
            df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
            df_inst = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=short_start)
            df_rev = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d'))
            
            df_info = api.taiwan_stock_info()
            match = df_info[df_info['stock_id'] == stock_id]
            stock_name = match['stock_name'].values[0] if not match.empty else "未知"
            industry = match['industry_category'].values[0] if not match.empty else "未知產業"

            if df_raw is None or df_raw.empty:
                st.error("❌ 無法取得歷史資料"); st.stop()

            # --- 數據清洗 (單位修正與指標加固) ---
            df = df_raw.copy()
            df.columns = [c.strip() for c in df.columns]
            mapping = {"Trading_Volume": "vol", "max": "high", "min": "low", "close": "close", "date": "date"}
            for old, new in mapping.items():
                if old in df.columns: df = df.rename(columns={old: new})
            
            for c in ["close", "high", "low", "vol"]:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
            
            # --- 核心改善：工業級指標 ---
            # A. 正統 ATR (Wilder's True Range)
            prev_close = df["close"].shift(1)
            tr = pd.concat([(df["high"] - df["low"]), (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1).max(axis=1)
            df["ATR14"] = tr.ewm(alpha=1/14, adjust=False).mean()

            # B. 修正成交金額 (億)：FinMind vol 是股，成交額 = (價 * 股) / 1e8
            df["MA20"] = df["close"].rolling(20).mean()
            df["MA20_Amount"] = (df["close"] * df["vol"] / 1e8).rolling(20).mean()
            
            # C. OBV 與 背離偵測
            df['OBV'] = (np.where(df['close'].diff() > 0, 1, np.where(df['close'].diff() < 0, -1, 0)) * df['vol']).cumsum()
            df['OBV_MA10'] = df['OBV'].rolling(10).mean()
            # 背離邏輯：股價 10 日內創新高，但 OBV 未創 10 日高
            is_div = (df['close'].iloc[-1] >= df['close'].tail(10).max()) and (df['OBV'].iloc[-1] < df['OBV'].tail(10).max())

            hist_last = df.iloc[-1]
            last_trade_date_str = str(hist_last["date"])
            m_code, m_desc = get_detailed_market_status(last_trade_date_str)
            current_price = float(hist_last["close"])

            # --- Step 8: 進階診斷邏輯 (保留並增強) ---
            score = 0
            sig_chips, sig_tech = [], []
            
            ma20_val = safe_float(hist_last.get("MA20"))
            ma20_slope = "UP" if ma20_val > df["MA20"].iloc[-6] else "DOWN"
            
            if ma20_slope == "UP": 
                sig_tech.append("📈 **趨勢方向**：MA20 均線向上 (多頭助漲)"); score += 1
            else: 
                sig_tech.append("📉 **趨勢方向**：均線走平或向下 (動能偏弱)")

            if hist_last['OBV'] > hist_last['OBV_MA10']: 
                sig_tech.append("🟢 **量能配合**：OBV 位於均線之上 (買盤穩定)"); score += 1
            if is_div:
                sig_tech.append("⚠️ **量價背離**：股價創高但 OBV 動能不足 (警惕假突破)")

            # 籌碼診斷 (保留原邏輯)
            trust_5d = 0
            if df_inst is not None and not df_inst.empty:
                df_inst['net'] = (pd.to_numeric(df_inst['buy'], errors='coerce').fillna(0) - pd.to_numeric(df_inst['sell'], errors='coerce').fillna(0)) / 1000
                trust_5d = df_inst[df_inst['name'] == 'Investment_Trust'].tail(5)['net'].sum()
                if trust_5d > 50: sig_chips.append(f"🟢 **投信認養**：近5日買超 {int(trust_5d)} 張"); score += 1

            # 基本面 (保留原邏輯)
            rev_yoy = safe_float(df_rev.iloc[-1].get('revenue_year_growth_rate')) if not df_rev.empty else 0

            # --- Step 9: 決策結論 ---
            pivot = float(df.tail(60)["high"].max()) # 改用 60 日高點更具實戰意義
            atr = float(hist_last["ATR14"])
            t = tick_size(current_price)
            slip = slip_ticks * t

            if current_price >= pivot * 0.98:
                action, clr = "🚀 突破進攻：挑戰高點壓力中", "orange" if is_div else "red"
            elif current_price <= ma20_val * 1.03:
                action, clr = "💎 黃金買點：回測均線支撐位", "green"
            else:
                action, clr = "⏳ 盤整觀察：目前位階中性", "blue"

            # --- Step 10: UI 呈現 ---
            st.divider()
            top1, top2, top3 = st.columns([2, 1, 1])
            with top1: 
                st.header(f"{stock_name} ({stock_id})")
                st.subheader(f"產業：{industry}")
            with top2: 
                st.metric("目前現價", f"{current_price}")
            with top3: 
                st.subheader(f":gray[{m_desc}]")

            st.info(f"### 🎯 策略整合結論 -> :{clr}[**{action}**]")
            
            c_sig1, c_sig2 = st.columns(2)
            with c_sig1:
                st.markdown("#### 📋 趨勢與技術診斷")
                for s in sig_tech: st.markdown(s)
            with c_sig2:
                st.markdown("#### 🛡️ 硬性門檻檢查 (Gates)")
                st.write(f"{'✅' if hist_last['MA20_Amount'] >= liq_gate else '❌'} 流動性 (20D均量 {hist_last['MA20_Amount']:.1f} 億)")
                st.write(f"{'✅' if (atr/current_price) <= 0.07 else '❌'} 波動度 (ATR% {(atr/current_price)*100:.1f}%)")
                st.write(f"{'✅' if not is_div else '⚠️'} 量價配合 (無背離現象)")

            # 交易計畫 Tab
            st.divider()
            tab1, tab2 = st.tabs(["⚔️ 實戰交易計畫", "📈 趨勢觀測圖"])
            
            with tab1:
                col_brk, col_pb = st.columns(2)
                risk_amt = total_capital * 10000 * (risk_per_trade / 100)
                
                with col_brk:
                    # 方案 A: 追高突破
                    entry_brk = round_to_tick(pivot + t, t)
                    stop_brk = round_to_tick(entry_brk - 1.5 * atr - slip, t)
                    tp1_brk = round_to_tick(entry_brk + 2.5 * atr, t)
                    lots_brk = int(risk_amt / ((entry_brk - stop_brk) * 1000)) if (entry_brk-stop_brk)>0 else 0
                    
                    st.error(f"### ① Breakout 方案 (突破前高)")
                    st.write(f"- **進場觸發**: `{entry_brk:.2f}`")
                    st.write(f"- **防守停損**: `{stop_brk:.2f}`")
                    st.write(f"- **獲利 TP1**: `{tp1_brk:.2f}`")
                    st.markdown(f"🛡️ **建議部位**: <span style='font-size:20px; color:red;'>**{lots_brk}**</span> 張", unsafe_allow_html=True)

                with col_pb:
                    # 方案 B: 拉回買入
                    entry_pb = round_to_tick(ma20_val + 0.2 * atr, t)
                    stop_pb = round_to_tick(entry_pb - 1.2 * atr - slip, t)
                    tp_pb = round_to_tick(pivot, t)
                    lots_pb = int(risk_amt / ((entry_pb - stop_pb) * 1000)) if (entry_pb-stop_pb)>0 else 0
                    
                    st.success(f"### ② Pullback 方案 (拉回均線)")
                    st.write(f"- **建議買區**: `{entry_pb:.2f}` 附近")
                    st.write(f"- **防守停損**: `{stop_pb:.2f}`")
                    st.write(f"- **預期目標**: `{tp_pb:.2f}`")
                    st.markdown(f"🛡️ **建議部位**: <span style='font-size:20px; color:green;'>**{lots_pb}**</span> 張", unsafe_allow_html=True)

            with tab2:
                chart_df = df.tail(120).copy()
                chart_df["date"] = pd.to_datetime(chart_df["date"])
                base = alt.Chart(chart_df).encode(x='date:T')
                line_p = base.mark_line(color='#2962FF').encode(y=alt.Y('close:Q', scale=alt.Scale(zero=False)))
                line_ma = base.mark_line(color='orange', strokeDash=[5,5], opacity=0.5).encode(y='MA20:Q')
                st.altair_chart((line_p + line_ma).interactive(), use_container_width=True)
                st.caption("藍線：收盤價 | 橘虛線：MA20 | 可縮放觀察趨勢")

        except Exception as e:
            st.error(f"系統運行異常: {e}")
