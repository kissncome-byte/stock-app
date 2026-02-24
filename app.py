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
st.set_page_config(page_title="SOP v11.3.3 實戰修正版", layout="wide")

# ============ 2. 智慧市場狀態判斷 ============
def get_detailed_market_status(last_trade_date_str: str, rt_success: bool):
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    today_str = now.strftime('%Y-%m-%d')
    weekday = now.weekday()
    current_time = now.time()
    
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    # 1. 週末判定
    if weekday >= 5:
        return "CLOSED_WEEKEND", "市場休市 (週末)"
    
    # 2. 如果即時報價成功，一定是開盤中或今日盤後
    if rt_success:
        if start_time <= current_time <= end_time:
            return "OPEN", "市場交易中 (即時更新)"
        elif current_time > end_time:
            return "POST_MARKET", "今日已收盤"
        else:
            return "PRE_MARKET", "盤前準備中"

    # 3. 如果即時報價失敗，且時間在盤中，才可能是國定假日
    if today_str != last_trade_date_str and current_time > datetime.strptime("10:00", "%H:%M").time():
        # 額外保險：如果是在 09:00-13:35 且 rt_success 為 False，極高機率是假日
        return "CLOSED_HOLIDAY", "市場休市 (國定假日/補休)"
        
    if current_time < start_time:
        return "PRE_MARKET", "盤前準備中"
    else:
        return "POST_MARKET", "今日已收盤"

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
    st.stop()

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

# ============ 5. 主介面 ============
st.title("🦅 SOP v11.3.3 全方位策略整合引擎")

with st.sidebar:
    st.header("⚙️ 實戰風控設定")
    total_capital = st.number_input("操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆最大風險 (%)", 1.0, 20.0, 2.0)
    st.divider()
    st.header("🛡️ 硬性門檻")
    liq_gate = st.number_input("流動性：MA20成交額(億) ≥", value=2.0, step=0.5)
    slip_ticks = st.number_input("滑價 Buffer (ticks)", value=3, step=1, min_value=0)

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
        st.write(f"Setup {'✅' if setup_ok else '❌'} | Liquidity {'✅' if liq_ok else '❌'} | RR {rr:.2f} {'✅' if rr_ok else '❌'}")
        st.write(f"**Tradeable {'✅YES' if tradeable else '❌NO'}**")
        st.write(f"🔹 進場 `{entry:.2f}`  |  🛑 停損 `{stop:.2f}`")
        st.write(f"🎯 目標1 `{tp1:.2f}` | 🚀 目標2 `{tp2:.2f}`")
        m1, m2, m3 = st.columns(3)
        m1.metric("建議張數", f"{total_lots}")
        m2.metric("TP1 賣出", f"{tp1_lots}")
        m3.metric("Runner", f"{runner_lots}")

# ============ 6. 核心處理 ============
if submitted:
    with st.spinner("正在同步即時報價與歷史數據..."):
        try:
            api = DataLoader()
            if FINMIND_TOKEN: api.login_by_token(FINMIND_TOKEN)

            # 1. 抓取歷史 (FinMind)
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
            df_inst = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=60)).strftime('%Y-%m-%d'))
            df_rev = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=200)).strftime('%Y-%m-%d'))
            
            df_info = api.taiwan_stock_info()
            match = df_info[df_info['stock_id'] == stock_id]
            stock_name = match['stock_name'].values[0] if not match.empty else "未知"
            industry = match['industry_category'].values[0] if not match.empty else "未知產業"

            if df_raw is None or df_raw.empty:
                st.error("❌ 無法取得歷史資料"); st.stop()

            # 2. 抓取即時 (TWSE MIS) - 解決休市誤判關鍵
            rt_price = None
            rt_success = False
            try:
                ts = int(time.time() * 1000)
                url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
                r = requests.get(url, timeout=3)
                data = r.json()
                if "msgArray" in data and len(data["msgArray"]) > 0:
                    info = data["msgArray"][0]
                    z = safe_float(info.get("z")) # 最近成交價
                    y = safe_float(info.get("y")) # 昨收
                    if z > 0:
                        rt_price = z
                        rt_success = True
            except: pass

            # 數據清洗
            df = df_raw.copy()
            df.columns = [c.strip() for c in df.columns]
            df = df.rename(columns={"Trading_Volume": "vol", "Trading_money": "amount", "max": "high", "min": "low"})
            for c in ["close", "high", "low", "vol", "amount"]:
                if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
            df = df[df["vol"] > 0].dropna(subset=["close"]).copy()

            # 指標計算
            df["ATR14"] = (df["high"] - df["low"]).rolling(14).mean() # 簡化穩定版
            df["MA20"] = df["close"].rolling(20).mean()
            df["MA20_Amount"] = (df["amount"] / 1e8).rolling(20).mean() if "amount" in df.columns else (df["close"] * df["vol"] / 1e8).rolling(20).mean()
            df["OBV"] = (np.where(df["close"].diff() > 0, 1, np.where(df["close"].diff() < 0, -1, 0)) * df["vol"]).cumsum()
            df["OBV_MA10"] = df["OBV"].rolling(10).mean()

            hist_last = df.iloc[-1]
            last_trade_date_str = str(hist_last["date"])
            
            # 市場狀態判定
            m_code, m_desc = get_detailed_market_status(last_trade_date_str, rt_success)
            
            # 決定目前價格 (即時優先)
            current_price = rt_price if rt_success else float(hist_last["close"])
            ma20_val = float(hist_last["MA20"])
            atr = float(hist_last["ATR14"]) if not np.isnan(hist_last["ATR14"]) else current_price * 0.03
            t = tick_size(current_price)
            slip = float(slip_ticks) * t
            risk_amt = float(total_capital) * 10000 * (float(risk_per_trade) / 100)

            # 策略邏輯
            pivot = float(df.tail(60)["high"].max())
            res_120 = float(df.tail(120)["high"].max()) if len(df) >= 120 else pivot
            res_252 = float(df.tail(252)["high"].max()) if len(df) >= 252 else res_120
            
            ma20_prev = float(df["MA20"].iloc[-6]) if len(df) > 6 else ma20_val
            trend_up = ma20_val > ma20_prev
            liq_ok = float(hist_last["MA20_Amount"]) >= float(liq_gate)
            
            breakout_setup = (current_price >= pivot) and (df["OBV"].iloc[-1] > df["OBV_MA10"].iloc[-1])
            pullback_setup = trend_up and (ma20_val <= current_price <= ma20_val + 1.2 * atr)

            # UI 呈現
            st.divider()
            top1, top2, top3 = st.columns([2.2, 1, 1])
            with top1:
                st.header(f"{stock_name} {stock_id}")
                st.caption(f"產業：{industry} | 資料日期：{last_trade_date_str}")
            with top2:
                st.metric("目前價格", f"{current_price:.2f}", delta=f"{current_price - float(hist_last['close']):.2f}" if rt_success else None)
            with top3:
                st.subheader(f":red[{m_desc}]" if "OPEN" in m_code else f":gray[{m_desc}]")

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("#### 📋 策略診斷")
                st.write(f"{'📈' if trend_up else '📉'} MA20 趨勢 {'(多頭)' if trend_up else '(空頭/盤整)'}")
                st.write(f"突破 Setup：{'✅成立' if breakout_setup else '❌不成立'}")
                st.write(f"拉回 Setup：{'✅成立' if pullback_setup else '❌不成立'}")
            with c2:
                st.markdown("#### 🛡️ 風控門檻")
                st.write(f"{'✅' if liq_ok else '❌'} 流動性 (MA20均額 {float(hist_last['MA20_Amount']):.2f} 億)")
                st.write(f"單筆風險預算：{risk_amt:,.0f} 元")

            # 交易計畫
            st.divider()
            st.subheader("⚔️ 多階層交易計畫")
            col_brk, col_pb = st.columns(2)

            # Breakout 方案
            entry_brk = round_to_tick(pivot + t, t)
            stop_brk  = round_to_tick(entry_brk - 1.5 * atr - slip, t)
            tp1_brk   = round_to_tick(res_120 if res_120 > entry_brk else res_252, t)
            tp2_brk   = round_to_tick(res_252 if res_252 > tp1_brk else tp1_brk + 3*atr, t)

            # Pullback 方案
            entry_pb = round_to_tick(current_price if pullback_setup else ma20_val + 0.3 * atr, t)
            stop_pb  = round_to_tick(entry_pb - 1.2 * atr - slip, t)
            tp1_pb   = round_to_tick(pivot, t)
            tp2_pb   = round_to_tick(res_120, t)

            with col_brk:
                render_plan(st.container(border=True), "突破方案", entry_brk, stop_brk, tp1_brk, tp2_brk, 2.0, breakout_setup, "🚀", liq_ok, risk_amt, slip)
            with col_pb:
                render_plan(st.container(border=True), "拉回方案", entry_pb, stop_pb, tp1_pb, tp2_pb, 3.0, pullback_setup, "💎", liq_ok, risk_amt, slip)

            # 圖表
            st.divider()
            chart_df = df.tail(100).copy()
            chart_df["date"] = pd.to_datetime(chart_df["date"])
            base = alt.Chart(chart_df).encode(x=alt.X("date:T", title="日期"))
            line = base.mark_line(color="#2962FF").encode(y=alt.Y("close:Q", scale=alt.Scale(zero=False), title="價格"))
            ma_line = base.mark_line(color="orange", strokeDash=[5, 5]).encode(y="MA20:Q")
            st.altair_chart((line + ma_line).interactive(), use_container_width=True)

            with st.expander("📋 詳細法人與營收數據"):
                tab_i, tab_r = st.tabs(["法人買賣超", "月營收趨勢"])
                with tab_i: st.dataframe(df_inst.tail(10)) if df_inst is not None else st.write("無資料")
                with tab_r: st.dataframe(df_rev.tail(6)) if df_rev is not None else st.write("無資料")

        except Exception as e:
            st.error(f"系統執行出錯: {e}")
