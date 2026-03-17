import urllib3
import os
import time
import requests
import certifi
import pandas as pd
import numpy as np
import streamlit as st
import altair as alt
from datetime import datetime, timedelta
import pytz
from FinMind.data import DataLoader

# ============ 1. Page Config ============
st.set_page_config(page_title="SOP v11.5 旗艦完全體", layout="wide")

# ============ 2. 智慧市場狀態判斷 ============
def get_market_status_label(rt_success: bool, last_trade_date_str: str):
    tz = pytz.timezone("Asia/Taipei")
    now = datetime.now(tz)
    weekday = now.weekday()
    current_time = now.time()

    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time()

    if weekday >= 5:
        return "CLOSED_WEEKEND", f"市場休市 (週末) | 數據日期: {last_trade_date_str}", "gray"

    is_trading_hours = start_time <= current_time <= end_time

    if rt_success:
        if is_trading_hours:
            return "OPEN", "市場交易中 (即時更新)", "red"
        elif current_time < start_time:
            return "PRE_MARKET", "盤前準備中 (即時連線正常)", "blue"
        else:
            return "POST_MARKET", "今日已收盤 (即時報價)", "green"
    else:
        if is_trading_hours:
            return "API_WAIT", f"連線受限，改用昨收 | 歷史日期: {last_trade_date_str}", "orange"
        elif current_time < start_time:
            return "PRE_MARKET", f"盤前準備中 | 歷史日期: {last_trade_date_str}", "blue"
        else:
            if current_time > datetime.strptime("10:00", "%H:%M").time() and last_trade_date_str != now.strftime("%Y-%m-%d"):
                return "CLOSED_HOLIDAY", f"市場休市 (國定假日) | 數據日期: {last_trade_date_str}", "gray"
            return "POST_MARKET", f"今日已收盤 | 數據日期: {last_trade_date_str}", "green"

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
    if x is None or (isinstance(x, float) and np.isnan(x)) or t == 0:
        return 0.0
    return round(x / t) * t

def _rq_get(url: str, headers=None, timeout=5):
    headers = headers or {"User-Agent": "Mozilla/5.0"}

    # 先走你指定的 certifi
    try:
        return requests.get(url, headers=headers, timeout=timeout, verify=certifi.where())
    except requests.exceptions.SSLError as e:
        # 特例：Streamlit Cloud/OpenSSL 對 TWSE 憑證 SKI 檢核失敗
        if "CERTIFICATE_VERIFY_FAILED" in str(e) and "Subject Key Identifier" in str(e):
            if "allow_insecure_ssl_fallback" in globals() and allow_insecure_ssl_fallback:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                return requests.get(url, headers=headers, timeout=timeout, verify=False)
        raise

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
st.title("🦅 SOP v11.5 旗艦整合系統")
st.caption("首頁可選：個股分析 / 篩選建議（近5日平均成交金額）")

# ============ 5-1. Sidebar Controls ============
with st.sidebar:
    st.header("⚙️ 實戰風控設定")
    total_capital = st.number_input("操作本金 (萬)", value=100, step=10)
    risk_per_trade = st.slider("單筆最大風險 (%)", 1.0, 20.0, 2.0)
    st.divider()

    st.header("🛡️ 硬性門檻")
    liq_gate = st.number_input("流動性：MA20成交額(億) ≥", value=2.0, step=0.5)
    slip_ticks = st.number_input("滑價 Buffer (ticks)", value=3, step=1, min_value=0)
    st.info("💡 v11.5：Tradeable = 流動性 + Space Gate + RR1（硬門檻）。")

    st.header("🧠 v11.5 空間優勢濾網")
    space_atr_mult = st.number_input("Space Gate：到下一壓力至少 ≥ ATR ×", value=2.0, step=0.5, min_value=0.0)
    space_tick_buffer = st.number_input("壓力位 Tick Buffer", value=2, step=1, min_value=0)
    st.caption("v11.5：自動剔除『壓力太近』導致 RR 偏低的交易情境。")

    st.divider()
    st.header("🧾 v11.7 篩選（近 5 日平均成交金額）")
    screen_top_n = st.number_input("先抓今日成交金額 Top N（加速）", value=80, step=10, min_value=20, max_value=300)
    min_avg5_amt_yi = st.number_input("近5日平均成交金額(億) ≥", value=2.0, step=0.5, min_value=0.0)
    allow_insecure_ssl_fallback = st.checkbox(
        "允許 SSL 降級（僅 openapi.twse.com.tw 失敗時）",
        value=True,
        help="Streamlit Cloud 對 TWSE 憑證驗證失敗時，才會改用 verify=False 重試。"
    )
# ============ 5-2. Render Plan ============
def render_plan(
    container,
    name,
    entry,
    stop,
    tp1,
    tp2,
    rr_gate,
    setup_ok,
    accent,
    liq_ok,
    risk_amt,
    slip,
    space_ok,
    rr2_gate_bonus=1.0,
):
    R = entry - stop
    risk_per_share = abs(entry - stop) + slip

    rr1 = ((tp1 - entry) / R) if R > 0 else 0.0
    rr2 = ((tp2 - entry) / R) if R > 0 else 0.0

    rr1_ok = rr1 >= rr_gate
    rr2_ok = rr2 >= (rr_gate + rr2_gate_bonus)

    # v11.5：Tradeable = 流動性 + Space Gate + RR1（硬門檻）
    tradeable = liq_ok and space_ok and rr1_ok

    total_lots = int(risk_amt / (risk_per_share * 1000)) if (tradeable and risk_per_share > 0) else 0
    tp1_lots = total_lots // 2
    runner_lots = total_lots - tp1_lots

    with container:
        st.markdown(f"### {accent} {name}")
        st.write(
            f"Setup {'✅' if setup_ok else '❌'} | "
            f"Liquidity {'✅' if liq_ok else '❌'} | "
            f"Space {'✅' if space_ok else '❌'} | "
            f"RR1 {rr1:.2f} {'✅' if rr1_ok else '❌'} | "
            f"RR2 {rr2:.2f} {'✅' if rr2_ok else '❌'}"
        )
        st.write(f"**Tradeable {'✅YES' if tradeable else '❌NO'}**")
        st.write(f"🔹 進場 `{entry:.2f}`  |  🛑 停損 `{stop:.2f}`")
        st.write(f"🎯 目標1 `{tp1:.2f}` | 🚀 目標2 `{tp2:.2f}`")

        m1, m2, m3 = st.columns(3)
        m1.metric("建議總張數", f"{total_lots}")
        m2.metric("TP1 賣出", f"{tp1_lots}")
        m3.metric("Runner", f"{runner_lots}")

        if not tradeable:
            st.caption("⚠️ v11.5：未通過 Tradeable（流動性 / 空間 / RR1 任一不足）。")

# ============ v11.7 篩選暫存 ============
if "screen_df" not in st.session_state:
    st.session_state.screen_df = None
if "screen_ts" not in st.session_state:
    st.session_state.screen_ts = None

# ============ v11.7 篩選工具（只用 openapi.twse.com.tw + FinMind） ============
@st.cache_data(ttl=90)
@st.cache_data(ttl=90)
def fetch_twse_stock_day_all():
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    r = _rq_get(url, timeout=6)
    r.raise_for_status()
    js = r.json()
    df = pd.DataFrame(js)

    for col in ["TradeValue", "TradeVolume", "ClosingPrice"]:
        if col in df.columns:
            df[col] = df[col].apply(safe_float)

    if "Code" in df.columns:
        df["Code"] = df["Code"].astype(str).str.strip()
    if "Name" in df.columns:
        df["Name"] = df["Name"].astype(str).str.strip()

    return df

def finmind_avg5_amount_yi(api: DataLoader, sid: str):
    start_date = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
    df = api.taiwan_stock_daily(stock_id=sid, start_date=start_date)
    if df is None or df.empty:
        return np.nan, 0

    df = df.copy()
    df.columns = [c.strip() for c in df.columns]
    if "Trading_money" not in df.columns:
        return np.nan, 0

    df["Trading_money"] = pd.to_numeric(df["Trading_money"], errors="coerce")
    df = df.dropna(subset=["Trading_money"])
    if df.empty:
        return np.nan, 0

    last5 = df.tail(5)
    return float(last5["Trading_money"].mean() / 1e8), int(len(last5))

def _tag_row(r, min_avg5_amt_yi: float):
    tags = []
    if r.get("avg5_amount_yi", 0) >= float(min_avg5_amt_yi) * 1.5:
        tags.append("強流動性")
    if r.get("today_trade_value_yi", 0) >= float(min_avg5_amt_yi) * 2:
        tags.append("今日爆量")
    return " / ".join(tags) if tags else "達標"

# ============ 6. Home Choice: Tabs ============
tab_a, tab_b = st.tabs(["📌 個股分析", "🔎 篩選建議"])

# ===================== TAB B: Screening =====================
with tab_b:
    st.subheader("🔎 v11.7 篩選（近 5 日平均成交金額）")
    run_screen = st.button("🚦 自動篩選候選股", type="primary", key="btn_run_screen")

    if run_screen:
        with st.spinner("正在自動篩選（近5日平均成交金額）..."):
            try:
                api = DataLoader()
                if FINMIND_TOKEN:
                    api.login_by_token(FINMIND_TOKEN)

                today_all = fetch_twse_stock_day_all()
                if (
                    today_all is None
                    or today_all.empty
                    or "TradeValue" not in today_all.columns
                    or "Code" not in today_all.columns
                ):
                    st.error("❌ 無法取得今日全市場成交資料（openapi.twse.com.tw）")
                else:
                    topn = (
                        today_all.sort_values("TradeValue", ascending=False)
                        .head(int(screen_top_n))
                        .copy()
                    )

                    rows = []
                    for _, rr in topn.iterrows():
                        sid = str(rr.get("Code", "")).strip()
                        name = str(rr.get("Name", "")).strip()
                        avg5_yi, n_days = finmind_avg5_amount_yi(api, sid)

                        rows.append(
                            {
                                "stock_id": sid,
                                "name": name,
                                "avg5_amount_yi": avg5_yi,
                                "n_days": n_days,
                                "today_trade_value_yi": float(rr.get("TradeValue", 0.0)) / 1e8,
                                "close": safe_float(rr.get("ClosingPrice", 0.0)),
                            }
                        )

                    out = pd.DataFrame(rows)
                    out = out.dropna(subset=["avg5_amount_yi"])
                    out = out[out["avg5_amount_yi"] >= float(min_avg5_amt_yi)].copy()
                    out = out.sort_values(
                        ["avg5_amount_yi", "today_trade_value_yi"],
                        ascending=False
                    ).reset_index(drop=True)

                    st.session_state.screen_df = out.copy()
                    st.session_state.screen_ts = datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")

                    st.subheader("✅ 候選清單（符合近5日平均成交金額門檻）")
                    st.caption(f"更新時間：{st.session_state.screen_ts}")
                    st.dataframe(out, use_container_width=True)

                    st.subheader("⭐ 篩選建議（Top Picks）")
                    st.caption(
                        f"條件：近5日均額 ≥ {float(min_avg5_amt_yi):.2f} 億 ｜"
                        f"今日 TopN：{int(screen_top_n)}"
                    )

                    topk = out.head(10).copy()
                    if not topk.empty:
                        topk["tag"] = topk.apply(lambda r: _tag_row(r, float(min_avg5_amt_yi)), axis=1)
                        st.dataframe(
                            topk[["stock_id", "name", "avg5_amount_yi", "today_trade_value_yi", "close", "tag"]],
                            use_container_width=True
                        )
                        st.caption("用法：把 stock_id 複製到左側『股票代號』→ 切到「個股分析」→ 按「啟動旗艦診斷」。")
                    else:
                        st.write("本次沒有符合條件的候選股。")

            except Exception as e:
                st.error(f"自動篩選執行出錯: {e}")

    if st.session_state.screen_df is not None and not run_screen:
        st.subheader("✅ 上次篩選結果（常駐）")
        ts = st.session_state.screen_ts or ""
        st.caption(f"更新時間：{ts}")
        st.dataframe(st.session_state.screen_df, use_container_width=True)

        st.subheader("⭐ 上次篩選建議（Top Picks）")
        topk2 = st.session_state.screen_df.head(10).copy()
        if not topk2.empty:
            topk2["tag"] = topk2.apply(lambda r: _tag_row(r, float(min_avg5_amt_yi)), axis=1)
            show_cols = [c for c in ["stock_id", "name", "avg5_amount_yi", "today_trade_value_yi", "close", "tag"] if c in topk2.columns]
            st.dataframe(topk2[show_cols], use_container_width=True)

# ===================== TAB A: Single Stock Analysis =====================
with tab_a:
    st.subheader("📌 個股分析（v11.5）")
    with st.form("query_form"):
        col1, col2 = st.columns([3, 1])
        with col1:
            stock_id = st.text_input("股票代號", value="2330").strip()
        with col2:
            submitted = st.form_submit_button("啟動旗艦診斷", type="primary")

    if submitted:
        with st.spinner("正在執行旗艦級大數據掃描..."):
            try:
                api = DataLoader()
                if FINMIND_TOKEN:
                    api.login_by_token(FINMIND_TOKEN)

                start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
                df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
                df_inst = api.taiwan_stock_institutional_investors(
                    stock_id=stock_id, start_date=(datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
                )
                df_rev = api.taiwan_stock_month_revenue(
                    stock_id=stock_id, start_date=(datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
                )

                df_info = api.taiwan_stock_info()
                match = df_info[df_info["stock_id"] == stock_id]
                stock_name = match["stock_name"].values[0] if not match.empty else "未知"
                industry = match["industry_category"].values[0] if not match.empty else "未知產業"

                if df_raw is None or df_raw.empty:
                    st.error("❌ 無法取得歷史資料")
                    st.stop()

                df = df_raw.copy()
                df.columns = [c.strip() for c in df.columns]
                df = df.rename(columns={"Trading_Volume": "vol", "Trading_money": "amount", "max": "high", "min": "low"})
                for c in ["close", "high", "low", "vol", "amount"]:
                    if c in df.columns:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df[df["vol"] > 0].dropna(subset=["close"]).copy()

                df["ATR14"] = (df["high"] - df["low"]).rolling(14).mean()
                df["MA20"] = df["close"].rolling(20).mean()
                df["MA20_Amount"] = (df["amount"] / 1e8).rolling(20).mean() if "amount" in df.columns else (df["close"] * df["vol"] / 1e8).rolling(20).mean()
                df["OBV"] = (np.where(df["close"].diff() > 0, 1, np.where(df["close"].diff() < 0, -1, 0)) * df["vol"]).cumsum()
                df["OBV_MA10"] = df["OBV"].rolling(10).mean()

                df = df.dropna(subset=["ATR14", "MA20", "MA20_Amount", "OBV_MA10"]).copy()
                if df.empty:
                    st.error("❌ 指標不足（資料長度太短或缺漏）")
                    st.stop()

                hist_last = df.iloc[-1]
                last_trade_date_str = str(hist_last["date"])

                # ============ 7. 雙擎即時報價引擎 ============
                rt_price = None
                rt_success = False
                current_vol = float(hist_last["vol"])
                rt_y_price = 0.0

                # 引擎 A: TWSE MIS (帶 Cookie)
                try:
                    session = requests.Session()
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                    session.get("https://mis.twse.com.tw/stock/index.jsp", headers=headers, timeout=3, verify=certifi.where())
                    ts = int(time.time() * 1000)
                    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
                    r = session.get(url, headers=headers, timeout=3, verify=certifi.where())
                    if r.status_code == 200:
                        data = r.json()
                        if "msgArray" in data and len(data["msgArray"]) > 0:
                            info = data["msgArray"][0]
                            z = safe_float(info.get("z"))
                            y = safe_float(info.get("y"))
                            if z > 0:
                                rt_price = z
                                rt_success = True
                                current_vol = safe_float(info.get("v"))
                                rt_y_price = y
                            elif y > 0:
                                rt_price = y
                                rt_success = True
                                current_vol = safe_float(info.get("v"))
                                rt_y_price = y
                except:
                    pass

                # 引擎 B: Yahoo 備援
                if not rt_success:
                    try:
                        for suffix in [".TW", ".TWO"]:
                            yh_url = f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}"
                            yh_r = requests.get(yh_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3, verify=certifi.where())
                            if yh_r.status_code == 200:
                                meta = yh_r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                                p = safe_float(meta.get("regularMarketPrice"))
                                if p > 0:
                                    rt_price = p
                                    rt_success = True
                                    rt_y_price = safe_float(meta.get("previousClose"))
                                    break
                    except:
                        pass

                # ============ 8. 深度解析邏輯 ============
                m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)
                current_price = rt_price if rt_success else float(hist_last["close"])
                ma20_val = float(hist_last["MA20"])
                atr = float(hist_last["ATR14"]) if not np.isnan(hist_last["ATR14"]) else current_price * 0.03
                t = tick_size(current_price)
                slip = float(slip_ticks) * t
                risk_amt = float(total_capital) * 10000 * (float(risk_per_trade) / 100)

                pivot = float(df.tail(60)["high"].max())
                res_120 = float(df.tail(120)["high"].max()) if len(df) >= 120 else pivot
                res_252 = float(df.tail(252)["high"].max()) if len(df) >= 252 else res_120
                res_504 = float(df.tail(504)["high"].max()) if len(df) >= 504 else res_252
                levels = [pivot, res_120, res_252, res_504]

                ma20_prev = float(df["MA20"].iloc[-6]) if len(df) > 6 else ma20_val
                ma20_slope_up = ma20_val > ma20_prev
                obv_up = df["OBV"].iloc[-1] > df["OBV_MA10"].iloc[-1]

                price_10d_max = df["close"].tail(10).max()
                obv_10d_max = df["OBV"].tail(10).max()
                is_div = (current_price >= price_10d_max) and (df["OBV"].iloc[-1] < obv_10d_max)

                avg_vol_20 = float(df["vol"].rolling(20).mean().iloc[-1])
                vol_ratio = current_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0

                liq_ok = float(hist_last["MA20_Amount"]) >= float(liq_gate)
                breakout_setup = (current_price >= pivot) and obv_up
                pullback_setup = ma20_slope_up and (ma20_val <= current_price <= ma20_val + 1.2 * atr)

                # 先保底（避免 UI 先顯示造成 NameError）
                space_ok_brk = False
                space_ok_pb = False
                space_to_res_brk = float("nan")
                space_to_res_pb = float("nan")

                st.divider()
                top1, top2, top3 = st.columns([2.2, 1, 1.5])
                with top1:
                    st.header(f"{stock_name} {stock_id}")
                    st.caption(f"產業：{industry} | 資料來源：{'即時' if rt_success else '歷史'}")
                with top2:
                    diff = current_price - (rt_y_price if rt_y_price > 0 else float(hist_last["close"]))
                    st.metric("目前現價", f"{current_price:.2f}", delta=f"{diff:.2f}")
                with top3:
                    st.subheader(f":{m_color}[{m_desc}]")

                st.markdown("### 🧬 價量與型態深度解析")
                c1, c2 = st.columns(2)
                with c1:
                    st.success("📈 **均線趨勢**：MA20 向上" if ma20_slope_up else "📉 **均線趨勢**：MA20 向下或走平")
                    st.success("🟢 **量能配合**：OBV > OBV_MA10" if obv_up else "⚪ **量能配合**：OBV 低於均線")
                    if is_div:
                        st.error("⚠️ **型態警示**：量價背離（慎防假突破）")
                    elif vol_ratio > 1.5:
                        st.success(f"🔥 **攻擊量能**：今日成交量達均量 {vol_ratio:.1f} 倍！")
                with c2:
                    st.write(f"**突破 Setup**：{'✅成立' if breakout_setup else '❌不成立'}")
                    st.write(f"**拉回 Setup**：{'✅成立' if pullback_setup else '❌不成立'}")
                    st.write(f"**流動性**：{'✅合格' if liq_ok else '❌不足'} ({float(hist_last['MA20_Amount']):.2f}億)")

                st.divider()
                st.subheader("⚔️ 多階層交易計畫")
                col_brk, col_pb = st.columns(2)

                def calc_breakout_targets(entry, r120, r252, atr_val, t_val):
                    tp1 = r120 if r120 > entry else entry + 2.0 * atr_val
                    tp2 = r252 if r252 > tp1 else tp1 + 3.0 * atr_val
                    return round_to_tick(tp1, t_val), round_to_tick(tp2, t_val)

                def calc_pullback_targets(entry, pivot_val, r120, atr_val, t_val):
                    tp1 = pivot_val if pivot_val > entry else entry + 2.0 * atr_val
                    tp2 = r120 if r120 > tp1 else tp1 + 2.0 * atr_val
                    return round_to_tick(tp1, t_val), round_to_tick(tp2, t_val)

                entry_brk = round_to_tick(pivot + t, t)
                stop_brk = round_to_tick(entry_brk - 1.5 * atr - slip, t)
                tp1_brk, tp2_brk = calc_breakout_targets(entry_brk, res_120, res_252, atr, t)

                entry_pb = round_to_tick(current_price if pullback_setup else ma20_val + 0.3 * atr, t)
                stop_pb = round_to_tick(entry_pb - 1.2 * atr - slip, t)
                tp1_pb, tp2_pb = calc_pullback_targets(entry_pb, pivot, res_120, atr, t)

                # ============ v11.6-A Entry-Based Space Gate ============
                space_buf = float(space_tick_buffer) * t

                def next_resistance_above(price, lvls):
                    above = [lv for lv in lvls if lv > price]
                    return min(above) if above else float("inf")

                next_res_brk = next_resistance_above(entry_brk, levels)
                space_to_res_brk = (next_res_brk - entry_brk) if np.isfinite(next_res_brk) else float("inf")
                space_ok_brk = space_to_res_brk >= (float(space_atr_mult) * atr + space_buf)

                next_res_pb = next_resistance_above(entry_pb, levels)
                space_to_res_pb = (next_res_pb - entry_pb) if np.isfinite(next_res_pb) else float("inf")
                space_ok_pb = space_to_res_pb >= (float(space_atr_mult) * atr + space_buf)

                st.markdown("### 🧠 Space Gate（以 Entry 為基準）")

                def fmt_space(x):
                    if x is None:
                        return "無更高壓力位"
                    if isinstance(x, float) and (np.isinf(x) or np.isnan(x)):
                        return "無更高壓力位"
                    return f"{x:.2f}"

                st.write(f"**Breakout Space**：{'✅' if space_ok_brk else '❌'} ｜距離下一壓力 `{fmt_space(space_to_res_brk)}`")
                st.write(f"**Pullback Space**：{'✅' if space_ok_pb else '❌'} ｜距離下一壓力 `{fmt_space(space_to_res_pb)}`")

                with col_brk:
                    render_plan(
                        st.container(border=True),
                        "Breakout 突破方案",
                        entry_brk, stop_brk,
                        tp1_brk, tp2_brk,
                        2.0, breakout_setup, "🚀",
                        liq_ok, risk_amt, slip,
                        space_ok_brk,
                        rr2_gate_bonus=1.0
                    )

                with col_pb:
                    render_plan(
                        st.container(border=True),
                        "Pullback 拉回方案",
                        entry_pb, stop_pb,
                        tp1_pb, tp2_pb,
                        3.0, pullback_setup, "💎",
                        liq_ok, risk_amt, slip,
                        space_ok_pb,
                        rr2_gate_bonus=1.0
                    )

                st.divider()
                st.markdown("### 📈 趨勢觀測 (藍線:價 / 橘線:OBV)")
                chart_df = df.tail(100).copy()
                chart_df["date"] = pd.to_datetime(chart_df["date"])
                base = alt.Chart(chart_df).encode(x=alt.X("date:T", title="日期"))
                lp = base.mark_line(color="#2962FF").encode(y=alt.Y("close:Q", scale=alt.Scale(zero=False), title="價格 (藍)"))
                lma = base.mark_line(color="rgba(0,0,0,0.3)", strokeDash=[5, 5]).encode(y="MA20:Q")
                lo = base.mark_line(color="#FF6D00").encode(y=alt.Y("OBV:Q", scale=alt.Scale(zero=False), title="OBV (橘)"))
                st.altair_chart(alt.layer(lma, lp, lo).resolve_scale(y="independent").interactive(), use_container_width=True)

                with st.expander("📋 詳細數據"):
                    ti, trr = st.tabs(["法人動態", "月營收"])
                    with ti:
                        if df_inst is not None and not df_inst.empty:
                            st.dataframe(df_inst.tail(10))
                        else:
                            st.write("無資料")
                    with trr:
                        if df_rev is not None and not df_rev.empty:
                            st.dataframe(df_rev.tail(6))
                        else:
                            st.write("無資料")

            except Exception as e:
                st.error(f"系統執行出錯: {e}")
