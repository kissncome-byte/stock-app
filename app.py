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
st.set_page_config(page_title="SOP v11.3.1 終極整合系統", layout="wide")

# ============ 2. 智慧市場狀態判斷 ============
def get_detailed_market_status(last_trade_date_str: str):
    """
    注意：holiday 判斷用 last_trade_date 推論仍可能因資料延遲誤判。
    本系統保留你的原邏輯，但建議之後改用 TWSE 開市資訊/即時回傳作最終裁決。
    """
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    today_str = now.strftime('%Y-%m-%d')
    weekday = now.weekday()
    current_time = now.time()
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    if weekday >= 5:
        return "CLOSED_WEEKEND", "市場休市 (週末)"
    if today_str != last_trade_date_str and current_time > datetime.strptime("10:00", "%H:%M").time():
        return "CLOSED_HOLIDAY", "市場休市 (國定假日)"
    if current_time < start_time:
        return "PRE_MARKET", "盤前準備中"
    elif start_time <= current_time <= end_time:
        return "OPEN", "市場交易中"
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

# ============ 4. Token ============
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

# ============ 5. 主介面 ============
st.title("🦅 SOP v11.3.1 全方位策略整合引擎（方案照給｜Tradeable 硬切）")

with st.sidebar:
    st.header("⚙️ 實戰風控設定")
    total_capital = st.number_input("操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆最大風險 (%)", 1.0, 5.0, 2.0)
    st.divider()

    st.header("🛡️ 硬性門檻 (Gates)")
    liq_gate = st.number_input("流動性：MA20成交額(億) ≥", value=2.0, step=0.5)
    slip_ticks = st.number_input("滑價 Buffer (ticks)", value=3, step=1, min_value=0)

    st.info("💡 v11.3.1：修正成交額單位(×1000)、target 改壓力位、stop 含滑價，Setup 不硬切但會提示；Tradeable=流動性+RR。")

with st.form("query_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        stock_id = st.text_input("股票代號", value="2330").strip()
    with col2:
        submitted = st.form_submit_button("啟動全方位診斷", type="primary")

# ============ 6. 核心處理 ============
if submitted:
    with st.spinner("正在執行工業級數據校準與背離偵測..."):
        try:
            api = DataLoader()
            if FINMIND_TOKEN:
                api.login_by_token(FINMIND_TOKEN)

            # ---- 1) 數據抓取 ----
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
                st.error("❌ 無法取得歷史資料")
                st.stop()

            # ---- 2) 數據清洗 / 欄位標準化 ----
            df = df_raw.copy()
            df.columns = [c.strip() for c in df.columns]
            mapping = {
                "Trading_Volume": "vol",
                "max": "high",
                "min": "low",
                "close": "close",
                "date": "date",
            }
            for old, new in mapping.items():
                if old in df.columns and new not in df.columns:
                    df = df.rename(columns={old: new})

            need_cols = ["date", "close", "high", "low", "vol"]
            missing = [c for c in need_cols if c not in df.columns]
            if missing:
                st.error(f"❌ 缺少必要欄位: {missing}")
                st.stop()

            for c in ["close", "high", "low", "vol"]:
                df[c] = pd.to_numeric(df[c], errors='coerce')

            df = df.dropna(subset=["close", "high", "low", "vol"]).copy()
            df = df[df["vol"] > 0].copy()

            # ---- 3) 指標（正統 ATR + 成交額單位修正 + MA/OBV） ----
            prev_close = df["close"].shift(1)
            tr = pd.concat([
                (df["high"] - df["low"]),
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs()
            ], axis=1).max(axis=1).fillna(df["high"] - df["low"])

            df["ATR14"] = tr.ewm(alpha=1/14, adjust=False).mean()
            df["MA20"] = df["close"].rolling(20).mean()

            # ✅ 成交額（億）：close * vol(張) * 1000(股/張) / 1e8
            df["MA20_Amount"] = (df["close"] * df["vol"] * 1000 / 1e8).rolling(20).mean()

            direction = np.where(df["close"].diff() > 0, 1, np.where(df["close"].diff() < 0, -1, 0))
            df["OBV"] = (direction * df["vol"]).cumsum()
            df["OBV_MA10"] = df["OBV"].rolling(10).mean()

            # 確保最後一筆指標不是 NaN
            df = df.dropna(subset=["ATR14", "MA20", "MA20_Amount", "OBV_MA10"]).copy()
            if df.empty:
                st.error("❌ 指標不足（資料長度太短或缺漏）")
                st.stop()

            hist_last = df.iloc[-1]
            last_trade_date_str = str(hist_last["date"])
            m_code, m_desc = get_detailed_market_status(last_trade_date_str)

            # ---- 4) 核心取值 ----
            current_price = float(hist_last["close"])  # 你可自行改成 TWSE 即時價
            ma20_val = float(hist_last["MA20"])
            atr = float(hist_last["ATR14"])
            t = tick_size(current_price)
            slip = float(slip_ticks) * t
            risk_amt = float(total_capital) * 10000 * (float(risk_per_trade) / 100)

            # 壓力位（用於 target，避免 ATR 倍數假象）
            pivot = float(df.tail(60)["high"].max())
            res_120 = float(df.tail(120)["high"].max()) if len(df) >= 120 else pivot
            res_252 = float(df.tail(252)["high"].max()) if len(df) >= 252 else res_120

            # ---- 5) 診斷訊號（含背離；背離僅提示） ----
            is_div = (df["close"].iloc[-1] >= df["close"].tail(10).max()) and (df["OBV"].iloc[-1] < df["OBV"].tail(10).max())

            # Setup（只提示，不硬切）
            ma20_prev = float(df["MA20"].iloc[-6]) if len(df) > 6 else ma20_val
            trend_up = ma20_val > ma20_prev

            breakout_setup = (current_price >= pivot + t) and (current_price > ma20_val) and (df["OBV"].iloc[-1] > df["OBV_MA10"].iloc[-1])
            pullback_setup = trend_up and (current_price >= ma20_val) and (current_price <= ma20_val + 1.0 * atr)

            # Gate（硬）：流動性
            liq_ok = float(hist_last["MA20_Amount"]) >= float(liq_gate)

            # ---- 6) UI Header ----
            st.divider()
            top1, top2, top3 = st.columns([2.2, 1, 1])
            with top1:
                st.header(f"{stock_name} ({stock_id})")
                st.caption(f"產業：{industry}")
            with top2:
                st.metric("目前現價", f"{current_price:.2f}")
            with top3:
                st.caption(m_desc)

            # ---- 7) 診斷區 ----
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("#### 📋 趨勢/量能診斷（提示）")
                st.write(f"{'📈' if trend_up else '📉'} MA20 趨勢（比較 -6 日）")
                st.write(f"{'🟢' if df['OBV'].iloc[-1] > df['OBV_MA10'].iloc[-1] else '⚪'} OBV vs OBV_MA10")
                st.write(f"{'⚠️ 量價背離(提示)' if is_div else '✅ 量價無明顯背離'}")
                st.write(f"Setup(突破)：{'✅成立' if breakout_setup else '❌不成立'}")
                st.write(f"Setup(拉回)：{'✅成立' if pullback_setup else '❌不成立'}")

            with c2:
                st.markdown("#### 🛡️ 風控門檻（硬）")
                st.write(f"{'✅' if liq_ok else '❌'} 流動性 Gate：MA20成交額 = {float(hist_last['MA20_Amount']):.2f} 億（門檻 {liq_gate:.2f}）")
                st.write(f"Tick = {t:g}｜Slip buffer = {slip:g}（{slip_ticks} ticks）")
                st.write(f"單筆風險金額 = {risk_amt:,.0f} 元")

            # ---- 8) 交易計畫：方案照給，但 Tradeable=Liquidity+RR；張數僅 tradeable 才給 ----
            st.divider()
            st.subheader("⚔️ 多階層交易實戰計畫（方案照給｜Tradeable 硬切）")
            col_brk, col_pb = st.columns(2)

            def breakout_targets(entry: float):
                tp1 = res_120 if res_120 > entry else res_252
                tp2 = res_252
                return tp1, tp2

            def pullback_targets(entry: float):
                tp1 = pivot
                tp2 = res_120 if res_120 > tp1 else res_252
                return tp1, tp2

            def render_plan(name, entry, stop, tp1, tp2, rr_gate, setup_ok, color_hex):
                # 風險（含滑價）
                R = (entry - stop)
                risk_per_share = abs(entry - stop) + slip

                # RR（用 TP1 作主要 reward）
                rr = ((tp1 - entry) / R) if R > 0 else 0.0
                rr_ok = rr >= rr_gate

                # ✅ 依你要求：Tradeable 不包含 Setup（方案照給）
                tradeable = liq_ok and rr_ok

                # 張數：tradeable 才給，不然 0（避免誤導下單）
                total_lots = int(risk_amt / (risk_per_share * 1000)) if (tradeable and risk_per_share > 0) else 0

                # 分批（50%/50%）
                tp1_lots = total_lots // 2
                runner_lots = total_lots - tp1_lots

                with st.container():
                    st.markdown(
                        f"<div style='border:2px solid {color_hex}; padding:15px; border-radius:10px;'>",
                        unsafe_allow_html=True
                    )
                    st.markdown(f"<h3 style='color:{color_hex};'>{name}</h3>", unsafe_allow_html=True)

                    st.write(
                        f"**Setup**: {'✅成立' if setup_ok else '❌不成立'}  |  "
                        f"**Liquidity**: {'✅' if liq_ok else '❌'}  |  "
                        f"**RR**: {rr:.2f} ({'✅' if rr_ok else '❌'} ≥{rr_gate})  |  "
                        f"**Tradeable**: {'✅YES' if tradeable else '❌NO（預案）'}"
                    )

                    # 方案永遠給（你要的）
                    st.write(f"🔹 **進場點**: `{entry:.2f}` | 🛑 **停損點**: `{stop:.2f}`")
                    st.write(f"🎯 **目標 1 (TP1)**: `{tp1:.2f}`")
                    st.write(f"🚀 **目標 2 (Runner)**: `{tp2:.2f}`")

                    m1, m2, m3 = st.columns(3)
                    m1.metric("建議張數", f"{total_lots}")
                    m2.metric("TP1 賣出(50%)", f"{tp1_lots}")
                    m3.metric("留倉(Runner)", f"{runner_lots}")

                    if not tradeable:
                        st.caption("⚠️ 目前僅為『預案』：未通過 Tradeable（流動性或 RR 不足）。")

                    st.markdown("</div>", unsafe_allow_html=True)

            # Breakout
            with col_brk:
                entry_brk = round_to_tick(pivot + t, t)
                stop_brk = round_to_tick(entry_brk - 1.5 * atr - slip, t)

                tp1_brk, tp2_brk = breakout_targets(entry_brk)
                tp1_brk = round_to_tick(tp1_brk, t)
                tp2_brk = round_to_tick(tp2_brk, t)

                render_plan(
                    "🚀 Breakout 突破型",
                    entry_brk, stop_brk,
                    tp1_brk, tp2_brk,
                    rr_gate=2.0,
                    setup_ok=breakout_setup,
                    color_hex="#ff4b4b"
                )

            # Pullback
            with col_pb:
                entry_pb = round_to_tick(ma20_val + 0.2 * atr, t)
                stop_pb = round_to_tick(entry_pb - 1.2 * atr - slip, t)

                tp1_pb, tp2_pb = pullback_targets(entry_pb)
                tp1_pb = round_to_tick(tp1_pb, t)
                tp2_pb = round_to_tick(tp2_pb, t)

                render_plan(
                    "💎 Pullback 拉回型",
                    entry_pb, stop_pb,
                    tp1_pb, tp2_pb,
                    rr_gate=3.0,
                    setup_ok=pullback_setup,
                    color_hex="#00c853"
                )

            # ---- 9) 圖表 ----
            st.divider()
            chart_df = df.tail(120).copy()
            chart_df["date"] = pd.to_datetime(chart_df["date"])

            line = alt.Chart(chart_df).mark_line(color="#2962FF").encode(
                x=alt.X("date:T", title="日期"),
                y=alt.Y("close:Q", scale=alt.Scale(zero=False), title="價格")
            )
            ma = alt.Chart(chart_df).mark_line(color="orange", strokeDash=[5, 5]).encode(
                x="date:T",
                y="MA20:Q"
            )

            st.altair_chart((line + ma).interactive(), use_container_width=True)
            st.caption("提示：盤中請改接 TWSE 即時價與 bid/ask，才能做 Spread Gate（更接近實盤）。")

            # ---- 10) 參考資料表（可選） ----
            with st.expander("📋 近10日法人資料（若有）"):
                if df_inst is not None and not df_inst.empty:
                    st.dataframe(df_inst.tail(10))
                else:
                    st.caption("本次未取得法人資料。")

            with st.expander("📋 近6期月營收（若有）"):
                if df_rev is not None and not df_rev.empty:
                    st.dataframe(df_rev.tail(6))
                else:
                    st.caption("本次未取得月營收資料。")

        except Exception as e:
            st.error(f"錯誤: {e}")
