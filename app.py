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
st.set_page_config(page_title="SOP v11.3.4 連線突破版", layout="wide")

# ============ 2. 智慧市場狀態判斷 (全新重寫) ============
def get_market_status_label(is_market_open_today: bool, rt_success: bool, last_trade_date_str: str):
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    current_time = now.time()
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    if not rt_success:
        # 如果 API 連線失敗，用時間粗略判斷並誠實顯示
        weekday = now.weekday()
        if weekday >= 5:
            return "CLOSED_WEEKEND", f"市場休市 (週末) | 歷史日期 {last_trade_date_str}"
        elif current_time > end_time:
            return "POST_MARKET", f"今日已收盤 | 歷史日期 {last_trade_date_str}"
        elif start_time <= current_time <= end_time:
            return "API_ERROR", f"即時連線異常 (請稍後再試) | 歷史日期 {last_trade_date_str}"
        else:
            return "PRE_MARKET", f"盤前準備中 | 歷史日期 {last_trade_date_str}"

    # API 連線成功，根據證交所回傳的日期精準判斷
    if is_market_open_today:
        if current_time < start_time:
            return "PRE_MARKET", "盤前準備中 (即時連線正常)"
        elif start_time <= current_time <= end_time:
            return "OPEN", "市場交易中 (即時報價)"
        else:
            return "POST_MARKET", "今日已收盤 (即時報價)"
    else:
        weekday = now.weekday()
        if weekday >= 5:
            return "CLOSED_WEEKEND", f"市場休市 (週末) | 歷史日期 {last_trade_date_str}"
        else:
            return "CLOSED_HOLIDAY", f"市場休市 (假日) | 歷史日期 {last_trade_date_str}"

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
        else:
            st.error("密碼錯誤")
    st.stop()

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

# ============ 5. 主介面 ============
st.title("🦅 SOP v11.3.4 全方位策略整合引擎")

with st.sidebar:
    st.header("⚙️ 實戰風控設定")
    total_capital = st.number_input("操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆最大風險 (%)", 1.0, 20.0, 2.0)
    st.divider()

    st.header("🛡️ 硬性門檻")
    liq_gate = st.number_input("流動性：MA20成交額(億) ≥", value=2.0, step=0.5)
    slip_ticks = st.number_input("滑價 Buffer (ticks)", value=3, step=1, min_value=0)

    st.info("💡 v11.3.4：突破證交所反爬蟲機制，精準判斷開盤與休市狀態。")

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

        st.write(
            f"Setup {'✅' if setup_ok else '❌'} | "
            f"Liquidity {'✅' if liq_ok else '❌'} | "
            f"RR {rr:.2f} {'✅' if rr_ok else '❌'} | "
            f"Tradeable {'✅YES' if tradeable else '❌NO'}"
        )

        st.write(f"🔹 進場 `{entry:.2f}`  |  🛑 停損 `{stop:.2f}`")
        st.write(f"🎯 目標1 `{tp1:.2f}`")
        st.write(f"🚀 目標2 `{tp2:.2f}`")

        m1, m2, m3 = st.columns(3)
        m1.metric("建議張數", f"{total_lots}")
        m2.metric("TP1 賣出", f"{tp1_lots}")
        m3.metric("Runner", f"{runner_lots}")

        if not tradeable:
            st.caption("⚠️ 目前為預案，未通過 Tradeable。")

# ============ 6. 核心處理 ============
if submitted:
    with st.spinner("正在執行工業級數據校準與連線..."):
        try:
            api = DataLoader()
            if FINMIND_TOKEN:
                api.login_by_token(FINMIND_TOKEN)

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

            df = df_raw.copy()
            df.columns = [c.strip() for c in df.columns]

            mapping = {
                "Trading_Volume": "vol",
                "Trading_money": "amount",
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

            for c in ["close", "high", "low", "vol"] + (["amount"] if "amount" in df.columns else []):
                df[c] = pd.to_numeric(df[c], errors='coerce')

            df = df.dropna(subset=["close", "high", "low", "vol"]).copy()
            df = df[df["vol"] > 0].copy()

            prev_close = df["close"].shift(1)
            tr = pd.concat([
                (df["high"] - df["low"]),
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs()
            ], axis=1).max(axis=1).fillna(df["high"] - df["low"])

            df["ATR14"] = tr.ewm(alpha=1/14, adjust=False).mean()
            df["MA20"] = df["close"].rolling(20).mean()

            if "amount" in df.columns:
                df["MA20_Amount"] = (df["amount"] / 1e8).rolling(20).mean()
            else:
                df["MA20_Amount"] = (df["close"] * df["vol"] / 1e8).rolling(20).mean()

            direction = np.where(df["close"].diff() > 0, 1, np.where(df["close"].diff() < 0, -1, 0))
            df["OBV"] = (direction * df["vol"]).cumsum()
            df["OBV_MA10"] = df["OBV"].rolling(10).mean()

            df = df.dropna(subset=["ATR14", "MA20", "MA20_Amount", "OBV_MA10"]).copy()
            if df.empty:
                st.error("❌ 指標不足（資料長度太短或缺漏）")
                st.stop()

            hist_last = df.iloc[-1]
            last_trade_date_str = str(hist_last["date"])

            # ============ 7. TWSE MIS 即時報價 (加入突破阻擋的 Headers) ============
            rt_price = None
            rt_y_price = None
            rt_success = False
            is_market_open_today = False

            try:
                ts = int(time.time() * 1000)
                url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
                # 加入 Headers 偽裝成正常瀏覽器，避免被證交所阻擋 (解決一直顯示休市的主因)
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest"
                }
                r = requests.get(url, headers=headers, timeout=5)
                
                if r.status_code == 200:
                    data = r.json()
                    if "msgArray" in data and len(data["msgArray"]) > 0:
                        info = data["msgArray"][0]
                        z = safe_float(info.get("z"))
                        y = safe_float(info.get("y"))
                        rt_y_price = y
                        
                        # 檢查證交所回傳的日期是否為今天
                        trade_date_str_twse = info.get("d", "") # e.g., "20240223"
                        tz = pytz.timezone('Asia/Taipei')
                        today_twse_format = datetime.now(tz).strftime('%Y%m%d')
                        
                        if trade_date_str_twse == today_twse_format:
                            is_market_open_today = True

                        if z > 0:
                            rt_price = z
                            rt_success = True
                        elif y > 0:
                            rt_price = y
                            rt_success = True
            except Exception as e:
                pass # 連線失敗則 rt_success 保持 False

            # ============ 8. 狀態與策略判定 ============
            m_code, m_desc = get_market_status_label(is_market_open_today, rt_success, last_trade_date_str)

            current_price = rt_price if rt_success else float(hist_last["close"])
            ma20_val = float(hist_last["MA20"])
            atr = float(hist_last["ATR14"])
            t = tick_size(current_price)
            slip = float(slip_ticks) * t
            risk_amt = float(total_capital) * 10000 * (float(risk_per_trade) / 100)

            pivot = float(df.tail(60)["high"].max())
            res_120 = float(df.tail(120)["high"].max()) if len(df) >= 120 else pivot
            res_252 = float(df.tail(252)["high"].max()) if len(df) >= 252 else res_120

            is_div = (df["close"].iloc[-1] >= df["close"].tail(10).max()) and (df["OBV"].iloc[-1] < df["OBV"].tail(10).max())

            ma20_prev = float(df["MA20"].iloc[-6]) if len(df) > 6 else ma20_val
            trend_up = ma20_val > ma20_prev

            breakout_setup = (current_price >= pivot + t) and (current_price > ma20_val) and (df["OBV"].iloc[-1] > df["OBV_MA10"].iloc[-1])
            pullback_setup = trend_up and (current_price >= ma20_val) and (current_price <= ma20_val + 1.0 * atr)

            liq_ok = float(hist_last["MA20_Amount"]) >= float(liq_gate)

            # UI 呈現
            st.divider()
            top1, top2, top3 = st.columns([2.2, 1, 1])
            with top1:
                st.header(f"{stock_name} {stock_id}")
                st.caption(f"產業：{industry}")
            with top2:
                # 計算漲跌幅
                if rt_success and rt_y_price > 0:
                    diff = current_price - rt_y_price
                    st.metric("目前現價", f"{current_price:.2f}", delta=f"{diff:.2f}")
                else:
                    st.metric("目前現價", f"{current_price:.2f}")
            with top3:
                # 若為連線異常，顯示橘色警告；正常開盤顯示紅色
                if "ERROR" in m_code:
                    st.subheader(f":orange[{m_desc}]")
                else:
                    st.subheader(f":red[{m_desc}]" if "OPEN" in m_code else f":gray[{m_desc}]")

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("#### 📋 趨勢與量能提示")
                st.write(f"{'📈' if trend_up else '📉'} MA20 趨勢")
                st.write(f"{'🟢' if df['OBV'].iloc[-1] > df['OBV_MA10'].iloc[-1] else '⚪'} OBV 相對均線")
                st.write(f"{'⚠️ 量價背離提示' if is_div else '✅ 無明顯背離'}")
                st.write(f"突破 Setup：{'✅成立' if breakout_setup else '❌不成立'}")
                st.write(f"拉回 Setup：{'✅成立' if pullback_setup else '❌不成立'}")

            with c2:
                st.markdown("#### 🛡️ 風控硬門檻")
                st.write(f"{'✅' if liq_ok else '❌'} 流動性 MA20成交額 {float(hist_last['MA20_Amount']):.2f} 億")
                st.write(f"Tick {t:g}｜Slip buffer {slip:g}")
                st.write(f"單筆風險金額 {risk_amt:,.0f} 元")

            st.divider()
            st.subheader("⚔️ 多階層交易計畫")
            col_brk, col_pb = st.columns(2)

            def breakout_targets(entry: float):
                tp1 = res_120 if res_120 > entry else res_252
                tp2 = res_252
                return tp1, tp2

            def pullback_targets(entry: float):
                tp1 = pivot
                tp2 = res_120 if res_120 > tp1 else res_252
                return tp1, tp2

            # 先算兩套方案的 entry/stop/targets
            entry_brk = round_to_tick(pivot + t, t)
            stop_brk  = round_to_tick(entry_brk - 1.5 * atr - slip, t)
            tp1_brk, tp2_brk = breakout_targets(entry_brk)
            tp1_brk = round_to_tick(tp1_brk, t)
            tp2_brk = round_to_tick(tp2_brk, t)

            entry_pb = round_to_tick(ma20_val + 0.2 * atr, t)
            stop_pb  = round_to_tick(entry_pb - 1.2 * atr - slip, t)
            tp1_pb, tp2_pb = pullback_targets(entry_pb)
            tp1_pb = round_to_tick(tp1_pb, t)
            tp2_pb = round_to_tick(tp2_pb, t)

            with col_brk:
                box1 = st.container(border=True)
                render_plan(box1, "突破方案", entry_brk, stop_brk, tp1_brk, tp2_brk, 2.0, breakout_setup, "🚀", liq_ok, risk_amt, slip)

            with col_pb:
                box2 = st.container(border=True)
                render_plan(box2, "拉回方案", entry_pb, stop_pb, tp1_pb, tp2_pb, 3.0, pullback_setup, "💎", liq_ok, risk_amt, slip)

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

            with st.expander("📋 近10日法人資料"):
                if df_inst is not None and not df_inst.empty:
                    st.dataframe(df_inst.tail(10))
                else:
                    st.caption("本次未取得法人資料。")

            with st.expander("📋 近6期月營收"):
                if df_rev is not None and not df_rev.empty:
                    st.dataframe(df_rev.tail(6))
                else:
                    st.caption("本次未取得月營收資料。")

        except Exception as e:
            st.error(f"錯誤: {e}")
