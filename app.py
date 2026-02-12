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

# ============ 1. 專業級計算模組 ============

def calculate_technical_indicators(df):
    """
    修正後的指標計算：包含正統 ATR 與 穩健 MA 斜率
    """
    # A. 正統 ATR (Wilder's TR)
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    # 使用 alpha=1/14 的 RMA (Wilder 常用平滑方式)
    df['ATR14'] = df['tr'].ewm(alpha=1/14, adjust=False).mean()
    
    # B. 穩健 MA20 與 斜率 (解決 iloc 偏移問題)
    df['MA20'] = df['close'].rolling(20).mean()
    df['MA20_Vol'] = df['vol'].rolling(20).mean()
    df['MA20_Amount'] = (df['close'] * df['vol'] * 1000 / 1e8).rolling(20).mean() # 單位：億
    
    # C. OBV 趨勢
    df['OBV'] = (np.where(df['close'].diff() > 0, 1, np.where(df['close'].diff() < 0, -1, 0)) * df['vol']).cumsum()
    df['OBV_MA10'] = df['OBV'].rolling(10).mean()
    
    return df

def get_market_status():
    """修正後的市場判斷邏輯"""
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    if now.weekday() >= 5: return "CLOSED", "市場休市 (週末)"
    
    current_time = now.time()
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()
    
    if current_time < start_time: return "PRE", "盤前準備"
    if start_time <= current_time <= end_time: return "OPEN", "市場交易中"
    return "POST", "今日已收盤"

def tick_size(p: float) -> float:
    if p >= 1000: return 5.0
    if p >= 500:  return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.01
    return 0.001

def round_to_tick(x: float, t: float) -> float:
    return round(x / t) * t if not np.isnan(x) else 0.0

# ============ 2. 核心決策引擎 (Gates & Logic) ============

def pick_targets(stock_data: pd.DataFrame):
    """
    用不同週期的壓力當作「可達成目標」來源，避免 Breakout reward 用 ATR 亂抓
    """
    h60  = float(stock_data['high'].tail(60).max())
    h120 = float(stock_data['high'].tail(120).max()) if len(stock_data) >= 120 else h60
    h252 = float(stock_data['high'].tail(252).max()) if len(stock_data) >= 252 else h120
    return {"pivot_60": h60, "res_120": h120, "res_252": h252}


def generate_trade_plan(stock_data, current_price, total_capital, risk_per_trade,
                        liquidity_min_20d_amount=2.0,  # 億
                        vol_gate_breakout=0.06,
                        vol_gate_pullback=0.05,
                        rr_gate_breakout=2.0,
                        rr_gate_pullback=3.0,
                        slippage_ticks=3):
    """
    Gate -> Setup -> RR -> Position
    - 方案若 Gate 或 RR 不合格：enabled=False, lots=0
    """
    hist_last = stock_data.iloc[-1]

    # --- 基本取值（含 NaN 防護） ---
    ma20 = float(hist_last.get('MA20', np.nan))
    atr  = float(hist_last.get('ATR14', np.nan))
    obv  = float(hist_last.get('OBV', np.nan))
    obv_ma10 = float(hist_last.get('OBV_MA10', np.nan))
    amt20 = float(hist_last.get('MA20_Amount', np.nan))  # 億

    if np.isnan(ma20) or np.isnan(atr) or np.isnan(amt20):
        return {"error": "INDICATOR_NAN", "message": "指標不足（MA20/ATR/Amount 出現 NaN），請確認資料長度與欄位。"}

    t = tick_size(float(current_price))
    slip = slippage_ticks * t  # 保守滑價緩衝（沒拿 bid/ask 時用 tick 估）
    targets = pick_targets(stock_data)

    pivot = targets["pivot_60"]
    next_res = max(targets["res_120"], pivot)  # 至少不低於突破位
    far_res  = max(targets["res_252"], next_res)

    # --- Gate（硬門檻） ---
    gates = {
        "History": len(stock_data) >= 120,                      # 至少 120 日讓壓力/型態更可靠
        "Liquidity": amt20 >= liquidity_min_20d_amount,         # 20D 均量(億)門檻
    }

    # 波動 Gate（分方案）
    vol_ratio = atr / float(current_price) if current_price else 1.0
    gates_breakout = {**gates, "Volatility": vol_ratio <= vol_gate_breakout}
    gates_pullback = {**gates, "Volatility": vol_ratio <= vol_gate_pullback}

    # --- Setup（型態：成立/不成立，不用天然偏多分數） ---
    # Breakout 成立條件：站上 pivot 且量能/OBV 方向確認（這裡用 OBV>MA10 做簡化）
    breakout_setup = (float(current_price) >= pivot + t) and (obv > obv_ma10) and (float(current_price) > ma20)

    # Pullback 成立條件：趨勢向上（ma20 上揚、價在 ma20 上方附近）且沒有跌破 ma20 太遠
    ma20_prev = float(stock_data['MA20'].iloc[-6]) if len(stock_data) > 6 and not np.isnan(stock_data['MA20'].iloc[-6]) else ma20
    trend_up = (ma20 > ma20_prev)
    pullback_setup = trend_up and (float(current_price) >= ma20) and (float(current_price) <= ma20 + 1.0*atr)

    # --- 風險資金 ---
    risk_amt = total_capital * 10000 * (risk_per_trade / 100)

    # ========= 方案 A：Breakout =========
    entry_brk = round_to_tick(pivot + t, t)
    stop_brk  = round_to_tick(entry_brk - 1.5*atr - slip, t)  # 加入滑價緩衝
    R_brk = entry_brk - stop_brk

    # Breakout 目標：先用 next_res / far_res（避免 ATR 亂抓）
    # 若 entry 已經接近 next_res，reward 會很小，RR 會自然不過
    target_brk = round_to_tick(next_res, t) if next_res > entry_brk else round_to_tick(far_res, t)
    reward_brk = target_brk - entry_brk
    rr_brk = (reward_brk / R_brk) if R_brk > 0 else 0

    brk_enabled = all(gates_breakout.values()) and breakout_setup and (rr_brk >= rr_gate_breakout) and (reward_brk > 0)

    lots_brk = int(risk_amt / (R_brk * 1000)) if brk_enabled and R_brk > 0 else 0

    # ========= 方案 B：Pullback =========
    # 進場：靠近 MA20（趨勢回測），不要用 current-0.5*atr 亂飄
    entry_pb = round_to_tick(ma20 + 0.2*atr, t)
    stop_pb  = round_to_tick(entry_pb - 1.2*atr - slip, t)
    R_pb = entry_pb - stop_pb

    # Pullback 目標：回到 pivot（突破位/箱頂），或更遠壓力
    target_pb = round_to_tick(pivot, t) if pivot > entry_pb else round_to_tick(next_res, t)
    reward_pb = target_pb - entry_pb
    rr_pb = (reward_pb / R_pb) if R_pb > 0 else 0

    pb_enabled = all(gates_pullback.values()) and pullback_setup and (rr_pb >= rr_gate_pullback) and (reward_pb > 0)

    lots_pb = int(risk_amt / (R_pb * 1000)) if pb_enabled and R_pb > 0 else 0

    return {
        "market": {
            "current_price": float(current_price),
            "tick": t,
            "slip_buffer": slip,
            "atr_pct": vol_ratio,
            "targets": targets
        },
        "gates": {
            "base": gates,
            "breakout": gates_breakout,
            "pullback": gates_pullback
        },
        "setups": {
            "breakout_setup": breakout_setup,
            "pullback_setup": pullback_setup
        },
        "plans": {
            "breakout": {
                "enabled": brk_enabled,
                "entry": entry_brk,
                "stop": stop_brk,
                "target": target_brk,
                "rr": rr_brk,
                "lots": lots_brk
            },
            "pullback": {
                "enabled": pb_enabled,
                "entry": entry_pb,
                "stop": stop_pb,
                "target": target_pb,
                "rr": rr_pb,
                "lots": lots_pb
            }
        }
    }


# ============ 3. Streamlit UI 介面 ============

st.set_page_config(page_title="SOP v11.0 決策引擎", layout="wide")
st.title("🦅 SOP v11.0 量化決策重構版")

# 側邊欄設定
with st.sidebar:
    st.header("🛡️ 風控中心")
    total_capital = st.number_input("操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆最大風險 (%)", 1.0, 5.0, 2.0)
    st.divider()
    token = st.text_input("FinMind Token", type="password")

# 查詢表單
with st.form("query"):
    col_id, col_btn = st.columns([3,1])
    stock_id = col_id.text_input("輸入股票代碼", "2330")
    submitted = col_btn.form_submit_button("執行深度診斷")

if submitted:
    try:
        api = DataLoader()
        if token: api.login_by_token(token)
        
        # 抓取數據
        df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=365)).strftime('%Y-%m-%d'))
        if df_raw.empty: st.error("查無資料"); st.stop()
        
        # 指標計算
        df = calculate_technical_indicators(df_raw.copy())
        m_code, m_desc = get_market_status()
        
        # 取得現價 (簡化版：實戰建議對接即時 API)
        current_price = df.iloc[-1]['close']
        
        # 產出計畫
        plan_data = generate_trade_plan(df, current_price, total_capital, risk_per_trade)
        
        # --- UI 呈現 ---
        st.subheader(f"📊 診斷對象：{stock_id} | 市場狀態：{m_desc}")
        
        # 1. Gate 檢查 (視覺化)
        cols = st.columns(len(plan_data['gates']))
        for i, (name, passed) in enumerate(plan_data['gates'].items()):
            cols[i].metric(name, "通過" if passed else "未達標", delta=None, delta_color="normal")
            if not passed: st.warning(f"⚠️ {name} 未通過硬門檻，請謹慎操作。")

        # 2. 交易計畫卡片
        st.divider()
        c1, c2 = st.columns(2)
        
        with c1:
            p = plan_data['plans']['breakout']
            st.error(f"### 🚀 突破方案 (RR: {p['rr']:.1f})")
            if p['rr'] < 2: st.caption("❌ 盈虧比過低，不符交易規範")
            else:
                st.write(f"**進場點**: {p['entry']} | **停損點**: {p['stop']}")
                st.write(f"**建議張數**: :red[{p['lots']}] 張")

        with c2:
            p = plan_data['plans']['pullback']
            st.success(f"### 💎 拉回方案 (RR: {p['rr']:.1f})")
            if p['rr'] < 3: st.caption("❌ 空間不足，等待更好買點")
            else:
                st.write(f"**進場區**: {p['entry']} 附近")
                st.write(f"**建議張數**: :green[{p['lots']}] 張")

        # 3. 圖表
        chart_df = df.tail(100).reset_index()
        base = alt.Chart(chart_df).encode(x='date:T')
        line = base.mark_line().encode(y=alt.Y('close:Q', scale=alt.Scale(zero=False)))
        ma20 = base.mark_line(color='orange').encode(y='MA20')
        st.altair_chart((line + ma20).interactive(), use_container_width=True)

    except Exception as e:
        st.error(f"系統錯誤: {e}")
