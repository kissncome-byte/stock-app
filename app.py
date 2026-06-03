import os
import time
import random
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
st.set_page_config(page_title="SOP v16 終極多因子秒級雷達系統", layout="wide")

# ============ 2. Global ============
TZ = pytz.timezone("Asia/Taipei")


# ============ 3. Helper ============
def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ["-", "", "None", "nan"]:
            return default
        return float(str(x).replace(",", ""))
    except Exception:
        return default


def tick_size(p: float) -> float:
    """符合台灣證券交易所現行法規之升降單位規則"""
    if p >= 1000:
        return 5.0
    if p >= 500:
        return 1.0
    if p >= 100:
        return 0.5
    if p >= 50:
        return 0.1
    if p >= 10:
        return 0.05
    return 0.01


def round_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t == 0:
        return 0.0
    return round(x / t) * t


def fmt_space(x) -> str:
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
            return "API_WAIT", f"連線受限，改用歷史價 | 歷史日期: {last_trade_date_str}", "orange"
        elif current_time < start_time:
            return "PRE_MARKET", f"盤前準備中 | 歷史日期: {last_trade_date_str}", "blue"
        else:
            if current_time > datetime.strptime("10:00", "%H:%M").time() and last_trade_date_str != now.strftime("%Y-%m-%d"):
                return "CLOSED_HOLIDAY", f"市場休市 (國定假日) | 數據日期: {last_trade_date_str}", "gray"
            return "POST_MARKET", f"今日已收盤 | 數據日期: {last_trade_date_str}", "green"


def next_resistance_above(price: float, levels):
    above = [lv for lv in levels if lv > price]
    return min(above) if above else float("inf")


def detect_style(result: dict) -> str:
    brk_score = 0
    pb_score = 0

    if result.get("breakout_setup"): brk_score += 3
    if result.get("pullback_setup"): pb_score += 3
    if result.get("space_ok_brk"): brk_score += 2
    if result.get("space_ok_pb"): pb_score += 2
    if result.get("rr1_brk", 0) >= 2.0: brk_score += 2
    if result.get("rr1_pb", 0) >= 3.0: pb_score += 2
    if result.get("brk_tradeable"): brk_score += 3
    if result.get("pb_tradeable"): pb_score += 3

    if brk_score > pb_score:
        return "突破型"
    if pb_score > brk_score:
        return "拉回型"

    if result.get("current_price", 0) >= result.get("pivot", 0):
        return "突破型"
    return "拉回型"


def judge_market_regime_from_df(df: pd.DataFrame) -> dict:
    if df is None or df.empty or len(df) < 30:
        return {
            "regime": "資料不足",
            "preferred_style": "拉回型",
            "reason": "資料不足，預設偏防守。"
        }

    x = df.copy()
    x["MA20"] = x["close"].rolling(20).mean()
    x = x.dropna(subset=["MA20"]).copy()
    if x.empty or len(x) < 6:
        return {
            "regime": "資料不足",
            "preferred_style": "拉回型",
            "reason": "資料不足，預設偏防守。"
        }

    price = float(x["close"].iloc[-1])
    ma20 = float(x["MA20"].iloc[-1])
    ma20_prev = float(x["MA20"].iloc[-6])
    slope_up = ma20 > ma20_prev

    high_60 = float(x.tail(60)["high"].max()) if len(x) >= 60 else float(x["high"].max())
    atr14 = float((x["high"] - x["low"]).rolling(14).mean().iloc[-1]) if len(x) >= 14 else 0.0

    near_high = price >= (high_60 - 0.5 * atr14) if atr14 > 0 else price >= high_60 * 0.98
    above_ma20 = price >= ma20

    if slope_up and above_ma20 and near_high:
        return {
            "regime": "強勢盤",
            "preferred_style": "突破型",
            "reason": "均線上彎、價格站上 MA20 且接近區間高點。"
        }

    if slope_up and above_ma20:
        return {
            "regime": "震盪偏強盤",
            "preferred_style": "拉回型",
            "reason": "均線仍上彎，但尚未明顯突破高點，偏向等回檔切入。"
        }

    return {
        "regime": "偏弱盤",
        "preferred_style": "拉回型",
        "reason": "價格/均線結構較弱，偏防守。"
    }


# ============ 4. Auth (已移除密碼驗證) ============
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")


# ============ 5. Cached API ============
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
    df = df.rename(columns={
        "Trading_Volume": "vol",
        "Trading_money": "amount",
        "max": "high",
        "min": "low",
    })

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


@st.cache_data(ttl=86400)
def get_financial_statement_df(stock_id: str, years: int = 2):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
    try:
        df_raw = api.taiwan_stock_financial_statement(stock_id=stock_id, start_date=start_date)
        if df_raw is None or df_raw.empty:
            return pd.DataFrame()
        df = df_raw.copy()
        targets = ["EPS", "OperatingIncomeGrossProfitRatio", "OperatingIncomeProfitRatio"]
        df = df[df["type"].isin(targets)]
        if df.empty:
            return pd.DataFrame()
        
        df_pivot = df.pivot_table(index="date", columns="type", values="value", aggfunc="last").reset_index()
        
        core_cols = ["EPS", "OperatingIncomeGrossProfitRatio", "OperatingIncomeProfitRatio"]
        for col in core_cols:
            if col not in df_pivot.columns:
                df_pivot[col] = 0.0
            else:
                df_pivot[col] = pd.to_numeric(df_pivot[col], errors="coerce").fillna(0.0)
                
        return df_pivot
    except Exception:
        return pd.DataFrame()


# ============ 6. Core ============
def prepare_indicator_df(df: pd.DataFrame):
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

    delta = x["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    avg_loss = np.where(avg_loss == 0, 0.00001, avg_loss)
    rs = avg_gain / avg_loss
    x["RSI14"] = 100 - (100 / (1 + rs))

    x["up_move"] = x["high"].diff()
    x["down_move"] = x["low"].shift(1) - x["low"]
    x["plus_dm"] = np.where((x["up_move"] > x["down_move"]) & (x["up_move"] > 0), x["up_move"], 0)
    x["minus_dm"] = np.where((x["down_move"] > x["up_move"]) & (x["down_move"] > 0), x["down_move"], 0)

    x["tr1"] = x["high"] - x["low"]
    x["tr2"] = (x["high"] - x["close"].shift(1)).abs()
    x["tr3"] = (x["low"] - x["close"].shift(1)).abs()
    x["TR"] = x[["tr1", "tr2", "tr3"]].max(axis=1)

    tr_14 = x["TR"].rolling(14).sum()
    tr_14 = np.where(tr_14 == 0, 0.00001, tr_14)
    plus_dm_14 = x["plus_dm"].rolling(14).sum()
    minus_dm_14 = x["minus_dm"].rolling(14).sum()

    x["PLUS_DI"] = (plus_dm_14 / tr_14) * 100
    x["MINUS_DI"] = (minus_dm_14 / tr_14) * 100

    di_sum = x["PLUS_DI"] + x["MINUS_DI"]
    di_sum = np.where(di_sum == 0, 0.00001, di_sum)
    x["DX"] = ((x["PLUS_DI"] - x["MINUS_DI"]).abs() / di_sum) * 100
    x["ADX14"] = x["DX"].rolling(14).mean()

    x = x.dropna(subset=["ATR14", "MA20", "MA20_Amount", "OBV_MA10", "RSI14", "ADX14"]).copy()
    if x.empty:
        return None
    return x


def compute_live_price(stock_id: str, hist_last_close: float, live_price_override: float = None):
    if live_price_override is not None and live_price_override > 0:
        return live_price_override, True, "雷達模擬串流", "realtime"

    rt_price = None
    rt_success = False
    rt_source = "歷史收盤"
    rt_type = "historical"

    try:
        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0"}
        session.get("https://mis.twse.com.tw/stock/index.jsp", headers=headers, timeout=2, verify=certifi.where())
        ts = int(time.time() * 1000)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
        r = session.get(url, headers=headers, timeout=2, verify=certifi.where())

        if r.status_code == 200:
            data = r.json()
            if "msgArray" in data and len(data["msgArray"]) > 0:
                info = data["msgArray"][0]
                z = safe_float(info.get("z"))
                tv = safe_float(info.get("tv"))
                if z > 0 and tv > 0:
                    rt_price = z
                    rt_success = True
                    rt_source = "TWSE 即時成交"
                    rt_type = "realtime"
    except Exception:
        pass

    if not rt_success:
        try:
            for suffix in [".TW", ".TWO"]:
                url = f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}"
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=2, verify=certifi.where())
                if r.status_code == 200:
                    meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                    p = safe_float(meta.get("regularMarketPrice"))
                    if p > 0:
                        rt_price = p
                        rt_success = True
                        rt_source = f"Yahoo 價格 {suffix}"
                        rt_type = "delayed"
                        break
        except Exception:
            pass

    final_price = rt_price if rt_success else hist_last_close
    if not rt_success:
        rt_source = "歷史收盤"
        rt_type = "historical"

    return final_price, rt_success, rt_source, rt_type


def evaluate_stock(
    stock_id: str,
    total_capital: float,
    risk_per_trade: float,
    liq_gate: float,
    slip_ticks: int,
    space_atr_mult: float,
    space_tick_buffer: int,
    live_price_override: float = None
):
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

    current_price, rt_success, rt_source, rt_type = compute_live_price(
        stock_id, float(hist_last["close"]), live_price_override=live_price_override
    )
    rt_y_price = float(hist_last["close"])
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

    inst_df = get_inst_df(stock_id, days=15)
    inst_3d_sum = 0.0
    if not inst_df.empty and "buy" in inst_df.columns and "sell" in inst_df.columns:
        inst_df["net_sheets"] = pd.to_numeric(inst_df["buy"], errors="coerce").fillna(0) - pd.to_numeric(inst_df["sell"], errors="coerce").fillna(0)
        daily_inst = inst_df.groupby("date")["net_sheets"].sum().reset_index()
        inst_3d_sum = float(daily_inst.tail(3)["net_sheets"].sum())

    rev_df = get_rev_df(stock_id, days=120)
    latest_yoy = 0.0
    if not rev_df.empty and "revenue_year_growth_rate" in rev_df.columns:
        rev_sorted = rev_df.sort_values("date")
        latest_yoy = safe_float(rev_sorted.iloc[-1]["revenue_year_growth_rate"])

    ma20_val = float(hist_last["MA20"])
    atr = float(hist_last["ATR14"]) if not np.isnan(hist_last["ATR14"]) else current_price * 0.03
    t = tick_size(current_price)
    slip = float(slip_ticks) * t
    risk_amt = float(total_capital) * 10000 * (float(risk_per_trade) / 100)

    rsi_now = float(hist_last["RSI14"])
    adx_now = float(hist_last["ADX14"])
    plus_di = float(hist_last["PLUS_DI"])
    minus_di = float(hist_last["MINUS_DI"])

    tech_conclusion_long = "⚖️ 擺動指標目前處於中性區，大資金尚未表態，短線缺乏爆發性動能。"
    tech_conclusion_short = "中性觀望"

    if adx_now < 20:
        if inst_3d_sum < 0 and latest_yoy < 0:
            tech_conclusion_long = "❌ **【死水無底洞，請直接忽略】** 技術面死氣沉沉完全沒有攻擊動能，而且法人像逃難一樣天天倒貨，月營收也慘不忍睹。這種股票哪怕跌再深，進去也只是浪費資金的潛在時間成本，請直接忽略它！"
            tech_conclusion_short = "❌ 死水忽略"
        else:
            tech_conclusion_long = "💤 **【盤整死水期】** 目前處於毫無波瀾的死水盤整期（ADX低於20）。多空沒有方向，此時任何『突破型策略』失敗率極高，容易買了就被洗盤，建議把資金抽回換去有量的地方。"
            tech_conclusion_short = "💤 盤整死水"
            
    elif rsi_now >= 75:
        if inst_3d_sum > 0:
            tech_conclusion_long = "🔥 **【極度過熱！主力硬幹妖股】** 短線技術指標已經高達 75 以上，追高被埋的風險巨大。但雷達警報發現，外資與投信完全不管指標死活，繼續瘋狂加碼硬幹！這屬於極高風險、高回報的妖股模式，如果想上車，千萬不能重倉，且手速要快、停損要設得極窄！"
            tech_conclusion_short = "🔥 妖股狂飆"
        else:
            tech_conclusion_long = "⚠️ **【短線極度過熱】** 股價買盤短線已經推升到極限（RSI超買）。此時衝動追高的性價比極低，回檔修正隨時會來，強烈建議高位克制雙手，耐心等待拉回均線再找機會。"
            tech_conclusion_short = "⚠️ 短線過熱"
            
    elif rsi_now <= 30:
        tech_conclusion_long = "📉 **【恐慌超賣區】** 股價極度超賣，市場出現恐慌性拋售，空頭宣洩中。雖然價格便宜，但目前尚未見到底部止跌訊號，暫不具備進場做多的攻擊條件，不可盲目伸手接刀。"
        tech_conclusion_short = "📉 恐慌超賣"
        
    elif plus_di > minus_di and adx_now >= 20:
        if inst_3d_sum > 0 and latest_yoy > 20:
            tech_conclusion_long = "🚀 **【黃金進攻訊號】** 這隻股票目前技術面強勢多頭，買盤動能飽滿。最漂亮的是法人在後面用真金白銀幫忙抬轎，基本面又有強勁營收撐腰！不論你想走『突破快市追擊』還是『拉回小波段』，這檔都是今天勝率極高的極品首選！"
            tech_conclusion_short = "🚀 完美多頭"
        elif inst_3d_sum < 0:
            tech_conclusion_long = "⚠️ **【小心假突破！主力在出貨】** 日K線雖然看起來很漂亮、好像要發動大攻擊，但雷達抓到三大法人這幾天一邊拉抬股價、一邊瘋狂倒貨給散戶！這高度懷疑是個美麗的假突破陷阱，盤中千萬別追高，進去極容易接盤！"
            tech_conclusion_short = "⚠️ 假突破嫌疑"
        else:
            tech_conclusion_long = "趨勢多頭成形，買盤動能延續性佳，屬於健康的攻擊型態，適合尋找突破點切入。"
            tech_conclusion_short = "🚀 多頭成形"
            
    elif minus_di > plus_di and adx_now >= 20:
        tech_conclusion_long = "📉 **【強勢空頭成形】** 技術面完全由空方主導（ADX上攻且空頭掌控）。市場賣壓極其沉重，此時盲目做多無異於螳臂擋車，極易逆勢受傷，強烈建議觀望，或尋找融券放空機會。"
        tech_conclusion_short = "📉 空頭成形"

    if tech_conclusion_short not in ["🚀 完美多頭", "⚠️ 假突破嫌疑", "🔥 股狂飆", "❌ 死水忽略"]:
        if current_price >= ma20_val and (current_price - ma20_val) / ma20_val <= 0.04:
            if inst_3d_sum > 0 and latest_yoy > 15:
                tech_conclusion_long = "🛡️ **【高手最愛！拉回安全防守點】** 股價經歷短線修正，目前精準跌到 MA20 均線防守區，過熱指標被洗乾淨了。最棒的是，下跌期間法人在偷偷吃貨，營收也很好！這就是最標準的拉回極品，下檔有肉墊，適合分批佈局。"
                tech_conclusion_short = "🛡️ 精準拉回"

    pivot = ma20_val
    brk_setup = (current_price >= pivot) and (rsi_now < 70)
    pb_setup = (current_price < pivot) and (current_price >= ma20_val * 0.97)

    levels = [ma20_val * 1.1, ma20_val * 1.2, ma20_val * 1.3]
    next_res = next_resistance_above(current_price, levels)

    target_brk = next_res if next_res != float("inf") else current_price * 1.15
    stop_brk = current_price - (2 * atr) - slip
    r_brk = target_brk - current_price
    s_brk = current_price - stop_brk
    rr1_brk = r_brk / s_brk if s_brk > 0 else 0

    target_pb = pivot
    stop_pb = current_price - atr - slip
    r_pb = target_pb - current_price
    s_pb = current_price - stop_pb
    rr1_pb = r_pb / s_pb if s_pb > 0 else 0

    result = {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "industry": industry,
        "current_price": current_price,
        "hist_last_close": rt_y_price,
        "market_desc": m_desc,
        "market_color": m_color,
        "pivot": pivot,
        "atr": atr,
        "tick_size": t,
        "inst_3d_sheets": inst_3d_sum,
        "latest_revenue_yoy": latest_yoy,
        "breakout_setup": brk_setup,
        "pullback_setup": pb_setup,
        "space_ok_brk": r_brk > (space_atr_mult * atr),
        "space_ok_pb": r_pb > (float(space_tick_buffer) * t),
        "rr1_brk": rr1_brk,
        "rr1_pb": rr1_pb,
        "brk_tradeable": brk_setup and rr1_brk >= 1.5,
        "pb_tradeable": pb_setup and rr1_pb >= 2.0,
        "tech_conclusion_long": tech_conclusion_long,
        "tech_conclusion_short": tech_conclusion_short
    }

    result["style"] = detect_style(result)
    return result


# ============ 7. Streamlit UI ============
st.title("SOP v16 終極多因子雷達決策系統")

st.sidebar.header("⚙️ 全局風控參數")
total_cap = st.sidebar.number_input("總本金 (萬元)", value=100.0)
risk_pct = st.sidebar.slider("單筆最大風險 (%)", 0.5, 3.0, 1.0)
target_stock = st.sidebar.text_input("輸入股票代碼", value="2330").strip()

if st.sidebar.button("開始秒級雷達掃描"):
    with st.spinner("多因子融合矩陣計算中..."):
        res = evaluate_stock(
            stock_id=target_stock,
            total_capital=total_cap,
            risk_per_trade=risk_pct,
            liq_gate=500,
            slip_ticks=1,
            space_atr_mult=1.5,
            space_tick_buffer=3
        )
        
        if res:
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("股價狀態", f"{res['current_price']} 元", f"狀態: {res['tech_conclusion_short']}")
            col2.metric("近3日法人買賣超", f"{res['inst_3d_sheets']:.0f} 張")
            col3.metric("最新營收年增率", f"{res['latest_revenue_yoy']:.2f} %")
            col4.metric("建議操盤風格", res["style"])
            
            st.subheader("💡 終極雷達白話文操盤建議")
            st.info(res["tech_conclusion_long"])
            
            st.write("### 🔍 因子診斷後台數據")
            st.json(res)
        else:
            st.error("找不到該股票資料，請檢查代碼是否輸入正確或 API 額度限制。")
