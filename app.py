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
st.set_page_config(page_title="SOP v7.2 趨勢動能整合版", layout="wide")

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
    if np.isnan(x): return 0.0
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
st.title("🦅 SOP v7.2 全方位策略整合引擎")

with st.sidebar:
    st.header("⚙️ 實戰風控設定")
    total_capital = st.number_input("操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆最大風險 (%)", 1.0, 20.0, 2.0)
    st.divider()
    st.info("💡 趨勢分析：系統會自動計算均線斜率與量價背離。")

with st.form("query_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        stock_id = st.text_input("股票代號", value="2330").strip()
    with col2:
        submitted = st.form_submit_button("啟動全方位診斷", type="primary")

# ============ 6. 核心數據處理 ============
if submitted:
    with st.spinner("策略引擎正在『讀取趨勢圖』並整合全維度因子..."):
        try:
            api = DataLoader()
            api.login_by_token(FINMIND_TOKEN)
            
            # 數據抓取
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            short_start = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
            df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
            df_index = api.taiwan_stock_daily(stock_id='TAIEX', start_date=start_date)
            df_inst = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=short_start)
            df_margin = api.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=short_start)
            df_rev = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d'))
            df_per = api.taiwan_stock_per_pbr(stock_id=stock_id, start_date=short_start)
            df_info = api.taiwan_stock_info()
            stock_name = df_info[df_info['stock_id'] == stock_id]['stock_name'].values[0] if not df_info[df_info['stock_id'] == stock_id].empty else "未知"

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
            m_code, m_desc = get_detailed_market_status(last_trade_date_str)

            # --- 指標計算 ---
            win = min(20, len(df))
            df["MA20"] = df["close"].rolling(win).mean()
            df["MA20_Amount"] = (df["amount"] / 1e8).rolling(win).mean()
            df["ATR14"] = (df["high"] - df["low"]).rolling(min(14, len(df))).mean()
            df['OBV'] = (np.where(df['close'].diff() > 0, 1, np.where(df['close'].diff() < 0, -1, 0)) * df['vol']).cumsum()
            df['OBV_MA10'] = df['OBV'].rolling(min(10, len(df))).mean()

            # --- 即時報價 ---
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
                        if z: current_price, rt_success = z, True; rt_diff = current_price - safe_float(info.get("y"))
                except: pass

            # --- Step 8: 趨勢與量價深度分析 (SOP v7.2 核心) ---
            score = 0
            sig_chips, sig_fund, sig_trend, sig_pos = [], [], [], []
            
            # 1. 均線斜率判定 (Trend Direction)
            ma20_now = df["MA20"].iloc[-1]
            ma20_prev = df["MA20"].iloc[-5] if len(df) > 5 else ma20_now
            ma20_slope = "UP" if ma20_now > ma20_prev else "DOWN"
            if ma20_slope == "UP":
                sig_trend.append("📈 **趨勢方向**：MA20 均線斜率向上 (多頭助漲)"); score += 1
            else:
                sig_trend.append("📉 **趨勢方向**：MA20 均線斜率走平或向下 (趨勢偏弱)")

            # 2. 量價背離判定 (OBV Divergence)
            obv_now = df['OBV'].iloc[-1]
            obv_ma = df['OBV_MA10'].iloc[-1]
            price_new_high = current_price >= df['close'].tail(10).max()
            obv_new_high = obv_now >= df['OBV'].tail(10).max()
            
            if price_new_high and not obv_new_high:
                sig_trend.append("⚠️ **量價背離**：股價創新高但 OBV 沒跟上 (防假突破)"); score -= 1
            elif obv_now > obv_ma:
                sig_trend.append("🟢 **量能配合**：OBV 位於均線之上 (買盤穩定)"); score += 1

            # 3. 籌碼維度 (保留 v7.1)
            trust_5d, foreign_5d, margin_1d = 0, 0, 0
            if df_inst is not None and not df_inst.empty:
                df_inst['net'] = (pd.to_numeric(df_inst['buy'], errors='coerce').fillna(0) - pd.to_numeric(df_inst['sell'], errors='coerce').fillna(0)) / 1000
                trust_5d = df_inst[df_inst['name'] == 'Investment_Trust'].tail(5)['net'].sum()
                foreign_5d = df_inst[df_inst['name'] == 'Foreign_Investor'].tail(5)['net'].sum()
                if trust_5d > 100 and foreign_5d > 500: sig_chips.append("🌟 **完美籌碼**：雙資同步大買超"); score += 2
                elif trust_5d > 50: sig_chips.append(f"🟢 **投信認養**：近5日買超 {int(trust_5d)} 張"); score += 1
            
            if df_margin is not None and not df_margin.empty:
                df_margin['MarginPurchaseLimit'] = pd.to_numeric(df_margin['MarginPurchaseLimit'], errors='coerce')
                margin_1d = df_margin['MarginPurchaseLimit'].diff().iloc[-1] if len(df_margin) > 1 else 0
                if margin_1d < 0: sig_chips.append("🟢 **籌碼安定**：融資減肥，散戶退場"); score += 1

            # 4. 基本面與大盤
            rev_yoy = safe_float(df_rev.iloc[-1].get('revenue_year_growth_rate')) if df_rev is not None and not df_rev.empty else 0
            if rev_yoy > 20: sig_fund.append(f"🚀 **營收爆發**：YoY {rev_yoy:.1f}% 強勁成長"); score += 1
            
            # --- Step 9: 決策結論 (整合趨勢圖結論) ---
            ma20_val = safe_float(hist_last.get("MA20"))
            bias_20 = ((current_price - ma20_val) / ma20_val * 100) if ma20_val != 0 else 0
            pivot = float(df.tail(252)["high"].max())
            atr = max(safe_float(hist_last.get("ATR14")), current_price * 0.025)
            
            is_breaking = current_price >= pivot
            is_pulling_back = (0 <= bias_20 <= 3)

            # 最終結論邏輯：加入趨勢斜率判定
            if is_breaking:
                if score >= 5 and ma20_slope == "UP": action, clr = "🔥 強力突破：量價斜率共振，極佳進攻點", "red"
                elif score >= 3: action, clr = "🚀 突破進攻：技術面轉強，建議小量試單", "orange"
                else: action, clr = "⚠️ 弱勢突破：趨勢斜率不足或量能背離，慎防假突破", "gray"
            elif is_pulling_back:
                if score >= 4 and ma20_slope == "UP": action, clr = "💎 黃金買點：強勢波段回測，支撐力道強", "green"
                else: action, clr = "🟡 觀察拉回：支撐測試中，等待量縮止跌", "orange"
            else:
                action, clr = "⏳ 盤整觀察：目前位階不明確，等待價格表態", "blue"

            if "CLOSED" in m_code: action = f"🌙 [休市功課] {action}"

            # --- Step 10: UI 呈現 ---
            st.divider()
            top1, top2, top3 = st.columns([2, 1, 1])
            with top1: st.header(f"{stock_name} ({stock_id})")
            with top2: st.metric("目前現價", f"{current_price}", delta=f"{rt_diff:.2f}" if rt_success else "昨日收盤")
            with top3: st.subheader(f":gray[{m_desc}]")

            st.info(f"### 🎯 策略整合結論 -> :{clr}[**{action}**]")
            
            # 10.2 診斷報告
            st.write("#### 📋 趨勢與維度診斷報告")
            c_sig1, c_sig2 = st.columns(2)
            with c_sig1:
                st.markdown("**【趨勢與技術面】**")
                for s in sig_trend: st.markdown(s)
                st.markdown(f"🔥 **相對強度**：{'強於大盤' if bias_20 > 0 else '弱於大盤'}")
            with c_sig2:
                st.markdown("**【籌碼與基本面】**")
                for s in sig_chips + sig_fund: st.markdown(s)

            # 10.3 核心雷達
            st.divider()
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("投信 5D", f"{int(trust_5d)} 張")
            r2.metric("營收 YoY", f"{rev_yoy:.1f}%")
            r3.metric("買點距離", f"{bias_20:.1f}%")
            r4.metric("MA20 斜率", "向上 ↗️" if ma20_slope == "UP" else "向下 ↘️")

            # 10.4 交易計畫
            st.divider()
            tab1, tab2, tab3 = st.tabs(["⚔️ 實戰交易計畫", "📈 趨勢觀測圖 (藍:價/橘:量)", "📋 詳細報表"])
            
            with tab1:
                col_brk, col_pb = st.columns(2)
                t = tick_size(current_price)
                with col_brk:
                    entry = round_to_tick(pivot + max(0.2 * atr, t), t)
                    stop = round_to_tick(entry - 1.0 * atr, t)
                    st.error("### ① Breakout 方案")
                    st.write(f"- 進場觸發: **{entry:.2f}**")
                    st.write(f"- 停損價位: **{stop:.2f}**")
                    st.write(f"- 目標 TP1: **{round_to_tick(entry + (3.0 if score>=5 else 2.0)*atr, t):.2f}**")
                    risk_amt = total_capital * 10000 * (risk_per_trade / 100)
                    lots = int(risk_amt / ((entry - stop) * 1000)) if (entry-stop)>0 else 0
                    st.write(f"🛡️ **建議部位**: **{lots}** 張")
                with col_pb:
                    pb_l = round_to_tick(max(ma20_val, current_price - 0.8 * atr), t)
                    pb_h = round_to_tick(max(pb_l + t, current_price - 0.2 * atr), t)
                    st.success("### ② Pullback 方案")
                    st.write(f"- 黃金買區: **{pb_l:.2f} ~ {pb_h:.2f}**")
                    st.write(f"- 停損價位: **{round_to_tick(pb_l - 1.2 * atr, t):.2f}**")
                    st.write(f"- 目標價位: **{pivot:.2f}**")

            with tab2:
                chart_df = df.tail(120).copy()
                chart_df["date"] = pd.to_datetime(chart_df["date"])
                base = alt.Chart(chart_df).encode(x=alt.X('date:T', title='日期'))
                line_p = base.mark_line(color='#2962FF').encode(y=alt.Y('close:Q', scale=alt.Scale(zero=False), title='股價'))
                line_ma = base.mark_line(color='rgba(0,0,0,0.3)', strokeDash=[5,5]).encode(y='MA20:Q')
                line_o = base.mark_line(color='#FF6D00').encode(y=alt.Y('OBV:Q', scale=alt.Scale(zero=False), title='OBV'))
                st.altair_chart(alt.layer(line_ma, line_p, line_o).resolve_scale(y='independent').interactive(), use_container_width=True)

            with tab3:
                c_a, c_b = st.columns(2)
                with c_a:
                    st.write("### 法人詳細動態")
                    if df_inst is not None: st.dataframe(df_inst.tail(10))
                with c_b:
                    st.write("### 歷史月營收")
                    if df_rev is not None: st.dataframe(df_rev.tail(6))

        except Exception as e:
            st.error(f"數據處理失敗: {e}"); st.stop()
