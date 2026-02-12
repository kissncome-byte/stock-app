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


# ============ 1) 計算模組 ============

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

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # 欄位標準化（避免 KeyError）
    df = df.copy()
    df.columns = [c.strip() for c in df.columns]
    rename_map = {
        "Trading_Volume": "vol",
        "Trading_Money": "amount",
        "max": "high",
        "min": "low",
        "close": "close",
        "date": "date",
    }
    for k, v in rename_map.items():
        if k in df.columns and v not in df.columns:
            df = df.rename(columns={k: v})

    # 必要欄位檢查
    need = ["date", "close", "high", "low", "vol"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"缺少必要欄位: {missing}")

    # 型別
    for c in ["close", "high", "low", "vol"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # 移除無交易日
    df = df.dropna(subset=["close", "high", "low", "vol"])
    df = df[df["vol"] > 0].copy()

    # --- ATR (Wilder TR + RMA/EMA) ---
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    tr = tr.fillna(df["high"] - df["low"])
    df["ATR14"] = tr.ewm(alpha=1/14, adjust=False).mean()

    # --- MA / 量能 ---
    df["MA20"] = df["close"].rolling(20).mean()
    df["MA20_Vol"] = df["vol"].rolling(20).mean()
    # 成交金額（億）：close * vol * 1000 / 1e8
    df["MA20_Amount"] = (df["close"] * df["vol"] * 1000 / 1e8).rolling(20).mean()

    # --- OBV ---
    direction = np.where(df["close"].diff() > 0, 1, np.where(df["close"].diff() < 0, -1, 0))
    df["OBV"] = (direction * df["vol"]).cumsum()
    df["OBV_MA10"] = df["OBV"].rolling(10).mean()

    # 確保核心欄位足夠（避免最後一筆 NaN）
    df = df.dropna(subset=["MA20", "ATR14", "MA20_Amount"]).copy()
    return df


def get_market_status():
    tz = pytz.timezone("Asia/Taipei")
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return "CLOSED", "市場休市 (週末)"

    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time   = datetime.strptime("13:35", "%H:%M").time()
    t = now.time()

    if t < start_time: return "PRE",  "盤前準備"
    if start_time <= t <= end_time: return "OPEN", "市場交易中"
    return "POST", "今日已收盤"


def pick_targets(stock_data: pd.DataFrame):
    h60  = float(stock_data["high"].tail(60).max())
    h120 = float(stock_data["high"].tail(120).max()) if len(stock_data) >= 120 else h60
    h252 = float(stock_data["high"].tail(252).max()) if len(stock_data) >= 252 else h120
    return {"pivot_60": h60, "res_120": h120, "res_252": h252}


# ============ 2) 核心決策引擎（邏輯硬切） ============

def generate_trade_plan(
    stock_data: pd.DataFrame,
    current_price: float,
    total_capital: float,
    risk_per_trade: float,
    liquidity_min_20d_amount: float = 2.0,   # 億（建議 2~5）
    vol_gate_breakout: float = 0.06,
    vol_gate_pullback: float = 0.05,
    rr_gate_breakout: float = 2.0,
    rr_gate_pullback: float = 3.0,
    slippage_ticks: int = 3
):
    hist_last = stock_data.iloc[-1]

    ma20 = float(hist_last["MA20"])
    atr  = float(hist_last["ATR14"])
    obv  = float(hist_last["OBV"]) if "OBV" in stock_data.columns else np.nan
    obv_ma10 = float(hist_last["OBV_MA10"]) if "OBV_MA10" in stock_data.columns else np.nan
    amt20 = float(hist_last["MA20_Amount"])

    if any(np.isnan(x) for x in [ma20, atr, amt20]) or current_price <= 0:
        return {"error": "INVALID_INPUT", "message": "指標或現價無效，請確認資料完整性。"}

    t = tick_size(current_price)
    slip = slippage_ticks * t
    targets = pick_targets(stock_data)

    pivot = targets["pivot_60"]
    next_res = max(targets["res_120"], pivot)
    far_res  = max(targets["res_252"], next_res)

    # --- Base Gates ---
    base_gates = {
        "History(>=120d)": len(stock_data) >= 120,
        "Liquidity(MA20>=X億)": amt20 >= liquidity_min_20d_amount,
    }

    atr_pct = atr / current_price
    gates_breakout = {**base_gates, "Volatility(ATR%<=X)": atr_pct <= vol_gate_breakout}
    gates_pullback = {**base_gates, "Volatility(ATR%<=X)": atr_pct <= vol_gate_pullback}

    # --- Setup（成立/不成立）---
    # Breakout：站上突破位 + 趨勢(>MA20) + 量能方向(OBV>MA10)
    breakout_setup = (current_price >= pivot + t) and (current_price > ma20) and (obv > obv_ma10)

    # Pullback：MA20 上揚 + 價格貼近 MA20（不離太遠）+ 未跌破 MA20
    ma20_prev = float(stock_data["MA20"].iloc[-6]) if len(stock_data) > 6 else ma20
    trend_up = ma20 > ma20_prev
    pullback_setup = trend_up and (current_price >= ma20) and (current_price <= ma20 + 1.0 * atr)

    # --- 風險資金 ---
    risk_amt = total_capital * 10000 * (risk_per_trade / 100)

    # ========= Breakout Plan =========
    entry_brk = round_to_tick(pivot + t, t)
    stop_brk  = round_to_tick(entry_brk - 1.5 * atr - slip, t)
    R_brk = entry_brk - stop_brk

    target_brk = round_to_tick(next_res, t) if next_res > entry_brk else round_to_tick(far_res, t)
    reward_brk = target_brk - entry_brk
    rr_brk = (reward_brk / R_brk) if R_brk > 0 else 0.0

    brk_enabled = all(gates_breakout.values()) and breakout_setup and (rr_brk >= rr_gate_breakout) and (reward_brk > 0)
    lots_brk = int(risk_amt / (R_brk * 1000)) if brk_enabled and R_brk > 0 else 0

    # ========= Pullback Plan =========
    entry_pb = round_to_tick(ma20 + 0.2 * atr, t)
    stop_pb  = round_to_tick(entry_pb - 1.2 * atr - slip, t)
    R_pb = entry_pb - stop_pb

    target_pb = round_to_tick(pivot, t) if pivot > entry_pb else round_to_tick(next_res, t)
    reward_pb = target_pb - entry_pb
    rr_pb = (reward_pb / R_pb) if R_pb > 0 else 0.0

    pb_enabled = all(gates_pullback.values()) and pullback_setup and (rr_pb >= rr_gate_pullback) and (reward_pb > 0)
    lots_pb = int(risk_amt / (R_pb * 1000)) if pb_enabled and R_pb > 0 else 0

    return {
        "market": {
            "current_price": float(current_price),
            "tick": t,
            "slip_buffer": slip,
            "atr_pct": atr_pct,
            "targets": targets,
            "ma20_amount": amt20
        },
        "gates": {
            "base": base_gates,
            "breakout": gates_breakout,
            "pullback": gates_pullback
        },
        "setups": {
            "breakout_setup": breakout_setup,
            "pullback_setup": pullback_setup,
            "trend_up": trend_up
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


# ============ 3) Streamlit UI（不誤導：Enabled 才顯示進場價） ============

st.set_page_config(page_title="SOP v11.1 決策引擎（硬門檻版）", layout="wide")
st.title("🦅 SOP v11.1 Gate → Setup → RR → Position（硬門檻）")

with st.sidebar:
    st.header("🛡️ 風控中心")
    total_capital = st.number_input("操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆最大風險 (%)", 1.0, 5.0, 2.0)
    st.divider()
    token = st.text_input("FinMind Token", type="password")

    st.caption("建議：Liquidity(20D均量) 2~5 億；滑價 buffer 以 tick 設定。")
    liquidity_min = st.number_input("Liquidity Gate：MA20成交額(億) ≥", value=2.0, step=0.5)
    slippage_ticks = st.number_input("Slippage Buffer（ticks）", value=3, step=1, min_value=0)

with st.form("query"):
    col_id, col_btn = st.columns([3, 1])
    stock_id = col_id.text_input("輸入股票代碼", "2330").strip()
    submitted = col_btn.form_submit_button("執行深度診斷")

if submitted:
    try:
        api = DataLoader()
        if token:
            api.login_by_token(token)

        df_raw = api.taiwan_stock_daily(
            stock_id=stock_id,
            start_date=(datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        )
        if df_raw is None or df_raw.empty:
            st.error("查無資料")
            st.stop()

        df = calculate_technical_indicators(df_raw)

        m_code, m_desc = get_market_status()

        # 現價：這裡仍用收盤（你之後可接 TWSE 即時）
        current_price = float(df.iloc[-1]["close"])

        plan = generate_trade_plan(
            df, current_price, total_capital, risk_per_trade,
            liquidity_min_20d_amount=float(liquidity_min),
            slippage_ticks=int(slippage_ticks)
        )

        if "error" in plan:
            st.error(plan["message"])
            st.stop()

        st.subheader(f"📊 {stock_id}｜市場狀態：{m_desc}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("現價", f"{plan['market']['current_price']:.2f}")
        m2.metric("ATR%", f"{plan['market']['atr_pct']*100:.2f}%")
        m3.metric("20D均量(億)", f"{plan['market']['ma20_amount']:.2f}")
        m4.metric("Tick / Slip", f"{plan['market']['tick']:.3g} / {plan['market']['slip_buffer']:.3g}")

        # ---- Gate + Setup 總覽（避免只看 Gate 就誤判可交易）----
        st.divider()
        st.markdown("### ✅ Gate / Setup 狀態總覽")

        gcol1, gcol2, gcol3 = st.columns(3)
        with gcol1:
            st.markdown("**Base Gates**")
            for k, v in plan["gates"]["base"].items():
                st.write(f"- {k}: {'✅' if v else '❌'}")

        with gcol2:
            st.markdown("**Breakout Gates + Setup**")
            for k, v in plan["gates"]["breakout"].items():
                st.write(f"- {k}: {'✅' if v else '❌'}")
            st.write(f"- Setup成立: {'✅' if plan['setups']['breakout_setup'] else '❌'}")

        with gcol3:
            st.markdown("**Pullback Gates + Setup**")
            for k, v in plan["gates"]["pullback"].items():
                st.write(f"- {k}: {'✅' if v else '❌'}")
            st.write(f"- Setup成立: {'✅' if plan['setups']['pullback_setup'] else '❌'}")

        # ---- 方案卡片：Enabled 才顯示價格與張數 ----
        st.divider()
        c1, c2 = st.columns(2)

        def render_plan(title, pdata, rr_gate, style="error"):
            enabled = pdata["enabled"]
            rr = pdata["rr"]

            if style == "error":
                st.error(f"### {title} (RR: {rr:.2f}, Gate: ≥{rr_gate})")
            else:
                st.success(f"### {title} (RR: {rr:.2f}, Gate: ≥{rr_gate})")

            if not enabled:
                st.caption("❌ 本方案未啟用：未通過 Gate / Setup / RR（硬門檻）。")
                st.write("- 建議：等待條件改善（量、突破成立、或拉回更貼近支撐）。")
                return

            st.write(f"**進場**: `{pdata['entry']:.2f}`")
            st.write(f"**停損**: `{pdata['stop']:.2f}`")
            st.write(f"**目標**: `{pdata['target']:.2f}`")
            st.write(f"**建議張數**: **{pdata['lots']} 張**")

        with c1:
            render_plan("🚀 突破方案（Breakout）", plan["plans"]["breakout"], rr_gate=2.0, style="error")

        with c2:
            render_plan("💎 拉回方案（Pullback）", plan["plans"]["pullback"], rr_gate=3.0, style="success")

        # ---- 圖表 ----
        st.divider()
        chart_df = df.tail(120).copy()
        chart_df["date"] = pd.to_datetime(chart_df["date"])
        base = alt.Chart(chart_df).encode(x=alt.X("date:T", title="日期"))

        line = base.mark_line().encode(y=alt.Y("close:Q", scale=alt.Scale(zero=False), title="價格"))
        ma20 = base.mark_line(color="orange").encode(y="MA20:Q")

        st.altair_chart((line + ma20).interactive(), use_container_width=True)
        st.caption("提示：OPEN 時應接 TWSE 即時（含 bid/ask）才能做 Spread/Slippage Gate 更精準。")

    except Exception as e:
        st.error(f"系統錯誤: {e}")
