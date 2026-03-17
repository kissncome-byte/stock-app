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
st.set_page_config(page_title="SOP v13 最終版", layout="wide")

# ============ 2. Global ============
TZ = pytz.timezone("Asia/Taipei")


# ============ 3. Helper ============
def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ["-", "", "None", "nan"]:
            return default
        return float(str(x).replace(",", ""))
    except:
        return default


def tick_size(p: float) -> float:
    if p >= 1000:
        return 5.0
    if p >= 500:
        return 1.0
    if p >= 100:
        return 0.5
    if p >= 50:
        return 0.1
    if p >= 10:
        return 0.01
    return 0.001


def round_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t == 0:
        return 0.0
    return round(x / t) * t


def fmt_space(x: float) -> str:
    if x is None or pd.isna(x) or np.isinf(x):
        return "無更高壓力位"
    return f"{x:.2f}"


def get_market_status_label(rt_success: bool, last_trade_date_str: str):
    now = datetime.now(TZ)
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


def next_resistance_above(price: float, levels: list[float]) -> float:
    above = [lv for lv in levels if lv > price]
    return min(above) if above else float("inf")


def _rq_get(url: str, headers=None, timeout=5):
    return requests.get(
        url,
        headers=headers or {"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
        verify=certifi.where(),
    )


# ============ 4. Auth ============
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


# ============ 5. Cached Data Access ============
@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try:
            api.login_by_token(FINMIND_TOKEN)
        except Exception:
            pass
    return api


@st.cache_data(ttl=3600)
def get_stock_info_df():
    api = get_api()
    df = api.taiwan_stock_info()
    if df is None or df.empty:
        return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category"])
    df = df.copy()
    if "stock_id" in df.columns:
        df["stock_id"] = df["stock_id"].astype(str).str.strip()
    return df


@st.cache_data(ttl=900)
def get_daily_df(stock_id: str, days: int = 365):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
    if df_raw is None or df_raw.empty:
        return None

    df = df_raw.copy()
    df.columns = [c.strip() for c in df.columns]
    rename_map = {
        "Trading_Volume": "vol",
        "Trading_money": "amount",
        "max": "high",
        "min": "low",
    }
    df = df.rename(columns=rename_map)

    for c in ["close", "high", "low", "vol", "amount"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["close", "high", "low", "vol"]).copy()
    df = df[df["vol"] > 0].copy()
    return df


@st.cache_data(ttl=900)
def get_inst_df(stock_id: str, days: int = 60):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
    return df if df is not None else pd.DataFrame()


@st.cache_data(ttl=900)
def get_rev_df(stock_id: str, days: int = 220):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=start_date)
    return df if df is not None else pd.DataFrame()


# ============ 6. Indicator / Plan Core ============
def prepare_indicator_df(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None

    x = df.copy()
    x["ATR14"] = (x["high"] - x["low"]).rolling(14).mean()
    x["MA20"] = x["close"].rolling(20).mean()

    if "amount" in x.columns:
        x["MA20_Amount"] = (x["amount"] / 1e8).rolling(20).mean()
    else:
        x["MA20_Amount"] = (x["close"] * x["vol"] / 1e8).rolling(20).mean()

    direction = np.where(x["close"].diff() > 0, 1, np.where(x["close"].diff() < 0, -1, 0))
    x["OBV"] = (direction * x["vol"]).cumsum()
    x["OBV_MA10"] = x["OBV"].rolling(10).mean()

    x = x.dropna(subset=["ATR14", "MA20", "MA20_Amount", "OBV_MA10"]).copy()
    if x.empty:
        return None
    return x


def compute_live_price(stock_id: str, hist_last_close: float):
    rt_price = None
    rt_success = False
    rt_y_price = 0.0

    # TWSE MIS
    try:
        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        session.get(
            "https://mis.twse.com.tw/stock/index.jsp",
            headers=headers,
            timeout=3,
            verify=certifi.where(),
        )
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
                    rt_y_price = y
                elif y > 0:
                    rt_price = y
                    rt_success = True
                    rt_y_price = y
    except Exception:
        pass

    # Yahoo fallback
    if not rt_success:
        try:
            for suffix in [".TW", ".TWO"]:
                yh_url = f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}"
                yh_r = requests.get(
                    yh_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=3,
                    verify=certifi.where(),
                )
                if yh_r.status_code == 200:
                    meta = yh_r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                    p = safe_float(meta.get("regularMarketPrice"))
                    if p > 0:
                        rt_price = p
                        rt_success = True
                        rt_y_price = safe_float(meta.get("previousClose"))
                        break
        except Exception:
            pass

    return (rt_price if rt_success else hist_last_close), rt_success, rt_y_price


def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, liq_gate: float, slip_ticks: int, space_atr_mult: float, space_tick_buffer: int):
    df_raw = get_daily_df(stock_id, days=365)
    if df_raw is None or df_raw.empty:
        return None

    df = prepare_indicator_df(df_raw)
    if df is None or df.empty:
        return None

    info_df = get_stock_info_df()
    match = info_df[info_df["stock_id"] == stock_id]
    stock_name = match["stock_name"].values[0] if ("stock_name" in match.columns and not match.empty) else "未知"
    industry = match["industry_category"].values[0] if ("industry_category" in match.columns and not match.empty) else "未知產業"

    hist_last = df.iloc[-1]
    last_trade_date_str = str(hist_last["date"])

    current_price, rt_success, rt_y_price = compute_live_price(stock_id, float(hist_last["close"]))
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

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
    current_vol = float(hist_last["vol"])
    vol_ratio = current_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0

    liq_ok = float(hist_last["MA20_Amount"]) >= float(liq_gate)
    breakout_setup = (current_price >= pivot) and obv_up
    pullback_setup = ma20_slope_up and (ma20_val <= current_price <= ma20_val + 1.2 * atr)

    # Targets
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

    space_buf = float(space_tick_buffer) * t

    next_res_brk = next_resistance_above(entry_brk, levels)
    space_to_res_brk = (next_res_brk - entry_brk) if np.isfinite(next_res_brk) else float("inf")
    space_ok_brk = space_to_res_brk >= (float(space_atr_mult) * atr + space_buf)

    next_res_pb = next_resistance_above(entry_pb, levels)
    space_to_res_pb = (next_res_pb - entry_pb) if np.isfinite(next_res_pb) else float("inf")
    space_ok_pb = space_to_res_pb >= (float(space_atr_mult) * atr + space_buf)

    R_brk = entry_brk - stop_brk
    rr1_brk = ((tp1_brk - entry_brk) / R_brk) if R_brk > 0 else 0.0
    rr2_brk = ((tp2_brk - entry_brk) / R_brk) if R_brk > 0 else 0.0
    brk_tradeable = liq_ok and space_ok_brk and (rr1_brk >= 2.0)

    R_pb = entry_pb - stop_pb
    rr1_pb = ((tp1_pb - entry_pb) / R_pb) if R_pb > 0 else 0.0
    rr2_pb = ((tp2_pb - entry_pb) / R_pb) if R_pb > 0 else 0.0
    pb_tradeable = liq_ok and space_ok_pb and (rr1_pb >= 3.0)

    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "industry": industry,
        "df": df,
        "last_trade_date_str": last_trade_date_str,
        "current_price": current_price,
        "rt_success": rt_success,
        "rt_y_price": rt_y_price,
        "m_code": m_code,
        "m_desc": m_desc,
        "m_color": m_color,
        "ma20_val": ma20_val,
        "atr": atr,
        "t": t,
        "slip": slip,
        "risk_amt": risk_amt,
        "pivot": pivot,
        "res_120": res_120,
        "res_252": res_252,
        "res_504": res_504,
        "levels": levels,
        "ma20_slope_up": ma20_slope_up,
        "obv_up": obv_up,
        "is_div": is_div,
        "vol_ratio": vol_ratio,
        "liq_ok": liq_ok,
        "ma20_amount": float(hist_last["MA20_Amount"]),
        "breakout_setup": breakout_setup,
        "pullback_setup": pullback_setup,
        "entry_brk": entry_brk,
        "stop_brk": stop_brk,
        "tp1_brk": tp1_brk,
        "tp2_brk": tp2_brk,
        "space_to_res_brk": space_to_res_brk,
        "space_ok_brk": space_ok_brk,
        "rr1_brk": rr1_brk,
        "rr2_brk": rr2_brk,
        "brk_tradeable": brk_tradeable,
        "entry_pb": entry_pb,
        "stop_pb": stop_pb,
        "tp1_pb": tp1_pb,
        "tp2_pb": tp2_pb,
        "space_to_res_pb": space_to_res_pb,
        "space_ok_pb": space_ok_pb,
        "rr1_pb": rr1_pb,
        "rr2_pb": rr2_pb,
        "pb_tradeable": pb_tradeable,
    }


# ============ 7. Scanner ============
@st.cache_data(ttl=3600)
def get_finmind_universe():
    info = get_stock_info_df().copy()
    if info.empty:
        return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category"])

    info["stock_id"] = info["stock_id"].astype(str).str.strip()
    info = info[info["stock_id"].str.fullmatch(r"\d{4}", na=False)].copy()

    keep_cols = ["stock_id"]
    if "stock_name" in info.columns:
        keep_cols.append("stock_name")
    if "industry_category" in info.columns:
        keep_cols.append("industry_category")

    return info[keep_cols].drop_duplicates("stock_id").reset_index(drop=True)


def render_single_stock_result(result: dict):
    st.divider()
    top1, top2, top3 = st.columns([2.2, 1, 1.5])
    with top1:
        st.header(f"{result['stock_name']} {result['stock_id']}")
        st.caption(f"產業：{result['industry']} | 資料來源：{'即時' if result['rt_success'] else '歷史'}")
    with top2:
        diff = result["current_price"] - (result["rt_y_price"] if result["rt_y_price"] > 0 else result["df"].iloc[-1]["close"])
        st.metric("目前現價", f"{result['current_price']:.2f}", delta=f"{diff:.2f}")
    with top3:
        st.subheader(f":{result['m_color']}[{result['m_desc']}]")

    st.markdown("### 🧬 價量與型態深度解析")
    c1, c2 = st.columns(2)
    with c1:
        if result["ma20_slope_up"]:
            st.success("📈 **均線趨勢**：MA20 向上，具備多頭保護力道")
        else:
            st.warning("📉 **均線趨勢**：MA20 向下或走平，動能偏弱")

        if result["obv_up"]:
            st.success("🟢 **量能配合**：OBV 位於均線之上，買盤穩定")
        else:
            st.warning("⚪ **量能配合**：OBV 低於均線，資金退潮中")

        if result["is_div"]:
            st.error("⚠️ **型態警示**：出現量價背離！慎防假突破。")
        elif result["vol_ratio"] > 1.5:
            st.success(f"🔥 **攻擊量能**：今日成交量達均量 {result['vol_ratio']:.1f} 倍！")

    with c2:
        st.write(f"**突破 Setup**：{'✅成立' if result['breakout_setup'] else '❌不成立'}")
        st.write(f"**拉回 Setup**：{'✅成立' if result['pullback_setup'] else '❌不成立'}")
        st.write(f"**流動性**：{'✅合格' if result['liq_ok'] else '❌不足'} ({result['ma20_amount']:.2f}億)")

    st.markdown("### 🧠 Space Gate（以 Entry 為基準）")
    st.write(f"**Breakout Space**：{'✅' if result['space_ok_brk'] else '❌'} ｜距離下一壓力 `{fmt_space(result['space_to_res_brk'])}`")
    st.write(f"**Pullback Space**：{'✅' if result['space_ok_pb'] else '❌'} ｜距離下一壓力 `{fmt_space(result['space_to_res_pb'])}`")

    st.divider()
    st.subheader("⚔️ 多階層交易計畫")
    col_brk, col_pb = st.columns(2)

    with col_brk:
        render_plan(
            st.container(border=True),
            "Breakout 突破方案",
            result["entry_brk"],
            result["stop_brk"],
            result["tp1_brk"],
            result["tp2_brk"],
            2.0,
            result["breakout_setup"],
            "🚀",
            result["liq_ok"],
            result["risk_amt"],
            result["slip"],
            result["space_ok_brk"],
            rr2_gate_bonus=1.0,
        )

    with col_pb:
        render_plan(
            st.container(border=True),
            "Pullback 拉回方案",
            result["entry_pb"],
            result["stop_pb"],
            result["tp1_pb"],
            result["tp2_pb"],
            3.0,
            result["pullback_setup"],
            "💎",
            result["liq_ok"],
            result["risk_amt"],
            result["slip"],
            result["space_ok_pb"],
            rr2_gate_bonus=1.0,
        )

    st.divider()
    st.markdown("### 📈 趨勢觀測 (藍線:價 / 橘線:OBV)")
    chart_df = result["df"].tail(100).copy()
    chart_df["date"] = pd.to_datetime(chart_df["date"])
    base = alt.Chart(chart_df).encode(x=alt.X("date:T", title="日期"))
    lp = base.mark_line(color="#2962FF").encode(y=alt.Y("close:Q", scale=alt.Scale(zero=False), title="價格 (藍)"))
    lma = base.mark_line(color="rgba(0,0,0,0.3)", strokeDash=[5, 5]).encode(y="MA20:Q")
    lo = base.mark_line(color="#FF6D00").encode(y=alt.Y("OBV:Q", scale=alt.Scale(zero=False), title="OBV (橘)"))
    st.altair_chart(alt.layer(lma, lp, lo).resolve_scale(y="independent").interactive(), use_container_width=True)

    df_inst = get_inst_df(result["stock_id"], days=60)
    df_rev = get_rev_df(result["stock_id"], days=220)
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

tab_a, tab_b = st.tabs(["📌 個股分析", "🔎 市場掃描"])
with tab_b:
    st.subheader("市場掃描（Quant Scanner）")
    scan_limit = st.number_input("最大掃描股票數", value=200, step=50, min_value=50, max_value=1000, key="scan_limit")
    top_show = st.number_input("輸出候選數", value=30, step=10, min_value=10, max_value=200, key="top_show")
    trend_filter = st.checkbox("只顯示 MA20 上升股票", value=True, key="trend_filter")
    only_tradeable = st.checkbox("只顯示 Tradeable", value=True, key="only_tradeable")

    run_scan = st.button("🚦 開始市場掃描", type="primary", key="run_scan")

    if run_scan:
        with st.spinner("正在掃描市場..."):
            try:
                uni = get_finmind_universe()
                if uni is None or uni.empty:
                    st.error("❌ 無法取得股票清單")
                else:
                    rows = []
                    total = min(len(uni), int(scan_limit))
                    prog = st.progress(0)

                    for i, (_, row) in enumerate(uni.head(total).iterrows(), start=1):
                        sid = str(row["stock_id"]).strip()

                        try:
                            result = evaluate_stock(
                                stock_id=sid,
                                total_capital=total_capital,
                                risk_per_trade=risk_per_trade,
                                liq_gate=liq_gate,
                                slip_ticks=slip_ticks,
                                space_atr_mult=space_atr_mult,
                                space_tick_buffer=space_tick_buffer,
                            )
                        except Exception:
                            prog.progress(i / total)
                            continue

                        if result is None:
                            prog.progress(i / total)
                            continue

                        if trend_filter and not result["ma20_slope_up"]:
                            prog.progress(i / total)
                            continue

                        if only_tradeable and not (result["brk_tradeable"] or result["pb_tradeable"]):
                            prog.progress(i / total)
                            continue

                        rows.append(
                            {
                                "stock_id": result["stock_id"],
                                "stock_name": result["stock_name"],
                                "industry": result["industry"],
                                "price": result["current_price"],
                                "liq20E": result["ma20_amount"],
                                "brk_setup": result["breakout_setup"],
                                "brk_space": result["space_ok_brk"],
                                "brk_rr1": result["rr1_brk"],
                                "brk_rr2": result["rr2_brk"],
                                "brk_tradeable": result["brk_tradeable"],
                                "pb_setup": result["pullback_setup"],
                                "pb_space": result["space_ok_pb"],
                                "pb_rr1": result["rr1_pb"],
                                "pb_rr2": result["rr2_pb"],
                                "pb_tradeable": result["pb_tradeable"],
                            }
                        )
                        prog.progress(i / total)

                    out = pd.DataFrame(rows)
                    if out.empty:
                        st.warning("本次掃描沒有符合條件的候選。")
                    else:
                        out = out.sort_values(
                            by=["brk_tradeable", "pb_tradeable", "brk_rr2", "pb_rr2", "liq20E"],
                            ascending=False,
                        ).reset_index(drop=True)

                        st.session_state.screen_df = out.copy()
                        st.session_state.screen_ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

                        st.subheader("✅ 掃描結果")
                        st.caption(f"更新時間：{st.session_state.screen_ts}")
                        st.dataframe(out.head(int(top_show)), use_container_width=True)

                        st.subheader("⭐ 今日最佳候選（Top Picks）")
                        topk = out.head(10).copy()
                        topk["tag"] = np.where(
                            topk["brk_tradeable"] | topk["pb_tradeable"],
                            "🔥 Tradeable",
                            "觀察"
                        )
                        st.dataframe(
                            topk[
                                [
                                    "stock_id",
                                    "stock_name",
                                    "industry",
                                    "price",
                                    "liq20E",
                                    "brk_rr1",
                                    "pb_rr1",
                                    "tag",
                                ]
                            ],
                            use_container_width=True,
                        )

                        pick_list = out["stock_id"].head(int(top_show)).tolist()
                        if pick_list:
                            picked = st.selectbox("帶入個股分析", pick_list, key="picked_from_scan")
                            if st.button("帶入左側個股分析", key="btn_use_pick"):
                                st.session_state["picked_stock"] = picked
                                st.rerun()

            except Exception as e:
                st.error(f"市場掃描執行出錯: {type(e).__name__}: {e}")

    if st.session_state.screen_df is not None and not run_scan:
        st.subheader("✅ 上次掃描結果（常駐）")
        st.caption(f"更新時間：{st.session_state.screen_ts or ''}")
        st.dataframe(st.session_state.screen_df.head(int(top_show)), use_container_width=True)

with tab_a:
    st.subheader("個股分析")
    default_stock = st.session_state.get("picked_stock", "2330")
    with st.form("single_stock_form"):
        col1, col2 = st.columns([3, 1])
        with col1:
            stock_id = st.text_input("股票代號", value=default_stock).strip()
        with col2:
            submitted = st.form_submit_button("啟動旗艦診斷", type="primary")

    if submitted:
        with st.spinner("正在執行旗艦級大數據掃描..."):
            try:
                result = evaluate_stock(
                    stock_id=stock_id,
                    total_capital=total_capital,
                    risk_per_trade=risk_per_trade,
                    liq_gate=liq_gate,
                    slip_ticks=slip_ticks,
                    space_atr_mult=space_atr_mult,
                    space_tick_buffer=space_tick_buffer,
                )
                if result is None:
                    st.error("❌ 無法取得資料或指標不足")
                else:
                    render_single_stock_result(result)
            except Exception as e:
                st.error(f"系統執行出錯: {type(e).__name__}: {e}")
