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
st.set_page_config(page_title="SOP v17 終極多因子爆發動能雷達系統", layout="wide")

# ============ 2. Global ============
TZ = pytz.timezone("Asia/Taipei")


# ============ 3. Helper ============
def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ["-", "", "None", "nan", "NaN"]:
            return default
        # 增強過濾：移除逗號與百分比符號
        clean_str = str(x).replace(",", "").replace("%", "").strip()
        return float(clean_str)
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


def detect_style(result: dict) -> str:
    if result.get("tech_conclusion_short") == "🚀 準備起漲":
        return "爆發突破型"
    
    brk_score = 0
    pb_score = 0
    if result.get("breakout_setup"): brk_score += 3
    if result.get("pullback_setup"): pb_score += 3
    if result.get("rr1_brk", 0) >= 1.5: brk_score += 2
    if result.get("rr1_pb", 0) >= 2.0: pb_score += 2

    if brk_score > pb_score: return "突破型"
    if pb_score > brk_score: return "拉回型"
    return "突破型" if result.get("current_price", 0) >= result.get("pivot", 0) else "拉回型"


def analyze_news_sentiment(title: str) -> tuple:
    pos_words = ['創新高', '大賺', '暴增', '飆', '大成長', '利多', '優於預期', '加碼', '看旺', '強勢', '獲利', '突破', '轉盈', '充沛', '加持', '買超', '爆發', '新高']
    neg_words = ['衰退', '虧損', '重挫', '低於預期', '縮水', '跌破', '警告', '利空', '下滑', '疲弱', '裁員', '大跌', '慘', '賣壓', '修正', '賣超', '暴跌', '逆風']
    
    pos_score = sum(1 for w in pos_words if w in title)
    neg_score = sum(1 for w in neg_words if w in title)
    
    if pos_score > neg_score: return "🟢 即時利多", "green"
    elif neg_score > pos_score: return "🔴 即時利空", "red"
    return "🟡 中性消息", "gray"


def compute_live_data(stock_id: str, hist_last_close: float, hist_last_vol: float, live_price_override: float = None):
    """同步獲取即時價格與盤中累計成交量(張)"""
    if live_price_override is not None and live_price_override > 0:
        return live_price_override, hist_last_vol * 1.8, True, "雷達模擬串流", "realtime"

    rt_price = None
    rt_vol = None
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
                v = safe_float(info.get("v")) # 當日累計成交張數
                if z == 0: 
                    z = safe_float(info.get("o")) # 若尚未有成交價，取開盤價
                if z > 0:
                    rt_price = z
                    rt_vol = v if v > 0 else hist_last_vol
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
                    v = safe_float(meta.get("regularMarketVolume"))
                    if p > 0:
                        rt_price = p
                        rt_vol = (v / 1000) if v > 0 else hist_last_vol # Yahoo為股數，換算為張數
                        rt_success = True
                        rt_source = f"Yahoo 價格 {suffix}"
                        rt_type = "delayed"
                        break
        except Exception:
            pass

    final_price = rt_price if rt_success else hist_last_close
    final_vol = rt_vol if (rt_success and rt_vol > 0) else hist_last_vol

    return final_price, final_vol, rt_success, rt_source, rt_type


# ============ 4. Auth ============
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")


# ============ 5. Cached API ============
@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try: api.login_by_token(FINMIND_TOKEN)
        except Exception: pass
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
    if df_raw is None or df_raw.empty: return None

    df = df_raw.copy()
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"Trading_Volume": "vol", "Trading_money": "amount", "max": "high", "min": "low"})

    for c in ["close", "high", "low", "vol", "amount"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce")

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
def get_rev_df(stock_id: str, days: int = 365):
    """【關鍵修復】擴大天數到 365 天，防止因 API 延遲查無最新營收"""
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
        if df_raw is None or df_raw.empty: return pd.DataFrame()
        df = df_raw.copy()
        
        targets = ["EPS", "Revenue", "GrossProfit", "OperatingIncome"]
        df = df[df["type"].isin(targets)]
        if df.empty: return pd.DataFrame()
        
        df_pivot = df.pivot_table(index="date", columns="type", values="value", aggfunc="last").reset_index()
        for col in targets:
            if col not in df_pivot.columns: df_pivot[col] = 0.0
            else: df_pivot[col] = pd.to_numeric(df_pivot[col], errors="coerce").fillna(0.0)
        return df_pivot
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def get_realtime_news_df(stock_id: str, stock_name: str):
    news_list = []
    try:
        import xml.etree.ElementTree as ET
        query = f"{stock_id} {stock_name}"
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ""
                link = item.find('link').text if item.find('link') is not None else ""
                pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ""
                source = item.find('source').text if item.find('source') is not None else "即時財經快訊"
                if " - " in title: title = title.rsplit(" - ", 1)[0]
                news_list.append({"date": pub_date, "title": title, "source": source, "link": link})
    except Exception: pass
    return pd.DataFrame(news_list)


# ============ 6. Core ============
def prepare_indicator_df(df: pd.DataFrame):
    if df is None or df.empty: return None
    x = df.copy()
    
    x["ATR14"] = (x["high"] - x["low"]).rolling(14).mean()
    x["MA20"] = x["close"].rolling(20).mean()
    x["MA20_Vol"] = x["vol"].rolling(20).mean() # 20日均量線

    # 【新增】真實技術面 20日最高價壓力線 (Donchian Channel Upper)
    x["Res_20D"] = x["high"].rolling(20).max()

    # 【新增】布林通道與波動壓縮度 (Bandwidth)
    x["std20"] = x["close"].rolling(20).std()
    x["BB_upper"] = x["MA20"] + (x["std20"] * 2)
    x["BB_lower"] = x["MA20"] - (x["std20"] * 2)
    x["BB_bandwidth"] = (x["BB_upper"] - x["BB_lower"]) / x["MA20"]

    # 傳統技術指標計算
    direction = np.where(x["close"].diff() > 0, 1, np.where(x["close"].diff() < 0, -1, 0))
    x["OBV"] = (direction * x["vol"]).cumsum()
    x["OBV_MA10"] = x["OBV"].rolling(10).mean()

    delta = x["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean().replace(0, 0.00001)
    x["RSI14"] = 100 - (100 / (1 + (avg_gain / avg_loss)))

    x["up_move"] = x["high"].diff()
    x["down_move"] = x["low"].shift(1) - x["low"]
    x["plus_dm"] = np.where((x["up_move"] > x["down_move"]) & (x["up_move"] > 0), x["up_move"], 0)
    x["minus_dm"] = np.where((x["down_move"] > x["up_move"]) & (x["down_move"] > 0), x["down_move"], 0)
    x["TR"] = x[["high", "low", "close"]].max(axis=1) - x[["high", "low", "close"]].min(axis=1) # 簡化TR
    tr_14 = x["TR"].rolling(14).sum().replace(0, 0.00001)
    x["PLUS_DI"] = (x["plus_dm"].rolling(14).sum() / tr_14) * 100
    x["MINUS_DI"] = (x["minus_dm"].rolling(14).sum() / tr_14) * 100
    di_sum = (x["PLUS_DI"] + x["MINUS_DI"]).replace(0, 0.00001)
    x["ADX14"] = ((x["PLUS_DI"] - x["MINUS_DI"]).abs() / di_sum * 100).rolling(14).mean()

    x["EMA12"] = x["close"].ewm(span=12, adjust=False).mean()
    x["EMA26"] = x["close"].ewm(span=26, adjust=False).mean()
    x["MACD_DIF"] = x["EMA12"] - x["EMA26"]
    x["MACD_SIGNAL"] = x["MACD_DIF"].ewm(span=9, adjust=False).mean()
    x["MACD_HIST"] = x["MACD_DIF"] - x["MACD_SIGNAL"]

    x = x.dropna(subset=["ATR14", "MA20", "Res_20D", "BB_bandwidth", "RSI14", "ADX14", "MACD_HIST"]).copy()
    return x


def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int):
    df_raw = get_daily_df(stock_id, days=365)
    if df_raw is None or df_raw.empty: return None

    df = prepare_indicator_df(df_raw)
    if df is None or df.empty: return None

    info_df = get_stock_info_df()
    match = info_df[info_df["stock_id"] == stock_id]
    stock_name = match["stock_name"].values[0] if not match.empty else "未知"
    industry = match["industry_category"].values[0] if not match.empty else "未知產業"

    hist_last = df.iloc[-1]
    last_trade_date_str = str(hist_last["date"])

    # 即時量價對齊
    current_price, current_vol, rt_success, rt_source, rt_type = compute_live_data(
        stock_id, float(hist_last["close"]), float(hist_last["vol"])
    )
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

    # 籌碼面
    inst_df = get_inst_df(stock_id, days=15)
    inst_3d_sum = 0.0
    if not inst_df.empty and "buy" in inst_df.columns and "sell" in inst_df.columns:
        inst_df["net_sheets"] = pd.to_numeric(inst_df["buy"], errors="coerce").fillna(0) - pd.to_numeric(inst_df["sell"], errors="coerce").fillna(0)
        inst_3d_sum = float(inst_df.groupby("date")["net_sheets"].sum().reset_index().tail(3)["net_sheets"].sum())

    # 【營收核心修復與擴充】
    rev_df = get_rev_df(stock_id, days=365)
    latest_yoy = 0.0
    latest_rev_month = "無數據(請檢查API或代碼)"
    latest_rev_value = 0.0
    if not rev_df.empty:
        rev_sorted = rev_df.sort_values("date")
        rev_last_row = rev_sorted.iloc[-1]
        latest_yoy = safe_float(rev_last_row.get("revenue_year_growth_rate", 0.0))
        latest_rev_month = str(rev_last_row.get("date", "未知月份"))
        latest_rev_value = safe_float(rev_last_row.get("revenue", 0.0)) / 100000000.0 # 換算為億元

    # 財報季報數據
    fin_df = get_financial_statement_df(stock_id, years=2)
    eps_now, eps_prev, gpm_now, gpm_prev, opm_now, opm_prev = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    fin_conclusion = "📋 該標的暫無足夠的季度財報歷史數據可供比對。"
    if fin_df is not None and not fin_df.empty:
        fin_df = fin_df.sort_values("date").reset_index(drop=True)
        for idx in range(len(fin_df)):
            rev_amt = safe_float(fin_df.loc[idx, "Revenue"])
            fin_df.loc[idx, "gpm"] = (safe_float(fin_df.loc[idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df.loc[idx, "opm"] = (safe_float(fin_df.loc[idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        latest_fin = fin_df.iloc[-1]
        eps_now = safe_float(latest_fin.get("EPS", 0.0))
        gpm_now = safe_float(latest_fin.get("gpm", 0.0))
        opm_now = safe_float(latest_fin.get("opm", 0.0))
        if len(fin_df) >= 2:
            prev_fin = fin_df.iloc[-2]
            eps_prev = safe_float(prev_fin.get("EPS", 0.0))
            gpm_prev = safe_float(prev_fin.get("gpm", 0.0))
            opm_prev = safe_float(prev_fin.get("opm", 0.0))
            gpm_text = "進步" if gpm_now > gpm_prev else "退步" if gpm_now < gpm_prev else "持平"
            opm_text = "進步" if opm_now > opm_prev else "退步" if opm_now < opm_prev else "持平"
            eps_lbl = "多賺" if eps_now > eps_prev else "少賺" if eps_now < eps_prev else "持平"
            if gpm_now > gpm_prev and opm_now > opm_prev and eps_now > eps_prev:
                fin_conclusion = f"📈 **【財報全面升級！】** 最新三大賺錢指標全數超越上一季！本業獲利與體質極度強健。"
            elif gpm_now < gpm_prev and opm_now < opm_prev and eps_now < eps_prev:
                fin_conclusion = f"📉 **【獲利能力全面退步】** 毛利、營益率、EPS 同步倒退，提防題材虛火盤。"
            else:
                fin_conclusion = f"⚖️ **【橫盤調整期】** 結構互有勝負：毛利『{gpm_text}』、營益率『{opm_text}』、EPS『{eps_lbl}』。"

    # 技術面核心因子
    ma20_val = float(hist_last["MA20"])
    vol_ma20_val = float(hist_last["MA20_Vol"])
    real_resistance = float(hist_last["Res_20D"])
    current_bandwidth = float(hist_last["BB_bandwidth"])
    
    atr = float(hist_last["ATR14"])
    t = tick_size(current_price)
    slip = float(slip_ticks) * t

    rsi_now = float(hist_last["RSI14"])
    adx_now = float(hist_last["ADX14"])
    macd_hist = float(hist_last["MACD_HIST"])

    # 【量能爆發與壓縮起漲過濾核心邏輯】
    vol_spike = current_vol > (vol_ma20_val * 1.5) # 當前量大於20日均量1.5倍
    # 判斷近60日頻寬是否處於低壓榨乾狀態 (波動極致壓縮)
    bandwidth_60d = df["BB_bandwidth"].tail(60)
    is_compressed = current_bandwidth < bandwidth_60d.quantile(0.3) if not bandwidth_60d.empty else False

    tech_conclusion_short = "中性觀望"
    tech_conclusion_long = "⚖️ 擺動指標目前處於中性區，大資金尚未表態，短線缺乏爆發性動能。"

    # 【起漲點精準覆蓋判斷】
    if current_price >= real_resistance * 0.995 and vol_spike and is_compressed:
        tech_conclusion_short = "🚀 準備起漲"
        tech_conclusion_long = f"🚀 **【完美風暴！爆發性起漲點降臨】** 該股盤中強勢挑戰20日實質最高壓力位（{real_resistance:.2f}元），且**爆發突破量**（達20日均量的 {current_vol/vol_ma20_val:.1f} 倍）！在此之前，布林通道已歷經極致洗盤壓縮。基本面營收年增 {latest_yoy:.1f}%，這是高度完美的『入手起漲點』！"
    elif adx_now < 20:
        tech_conclusion_short = "💤 盤整死水"
        tech_conclusion_long = "💤 **【盤整死水期】** ADX低於20，多空沒有方向，此時任何突破策略失敗率極高，建議克制雙手。"
    elif rsi_now >= 75:
        tech_conclusion_short = "⚠️ 短線過熱"
        tech_conclusion_long = "⚠️ **【短線極度過熱】** RSI超買，追高性價比極低，回檔修正隨時會來，耐心等待拉回均線。"
    elif plus_di := float(hist_last["PLUS_DI"]) > (minus_di := float(hist_last["MINUS_DI"])) and adx_now >= 20:
        if inst_3d_sum > 0 and latest_yoy > 20:
            tech_conclusion_short = "🚀 完美多頭"
            tech_conclusion_long = "🚀 **【黃金進攻波段】** 技術面強勢多頭，法人真金白銀幫忙抬轎，基本面又有強勁營收撐腰！"
        elif inst_3d_sum < 0:
            tech_conclusion_short = "⚠️ 假突破嫌疑"
            tech_conclusion_long = "⚠️ **【小心假突破！主力在出貨】** 日K強勢，但法人這幾天一邊拉抬一邊倒貨給散戶！高度懷疑是陷阱。"
        else:
            tech_conclusion_short = "🚀 多頭成形"
            tech_conclusion_long = "趨勢多頭成形，買盤動能延續性佳，適合尋找突破點切入。"
    elif minus_di > plus_di and adx_now >= 20:
        tech_conclusion_short = "📉 空頭成形"
        tech_conclusion_long = "📉 **【強勢空頭成形】** 技術面由空方主導，賣壓沉重，盲目做多無異於螳臂擋車。"

    if tech_conclusion_short not in ["🚀 準備起漲", "🚀 完美多頭", "⚠️ 假突破嫌疑"] and current_price >= ma20_val and (current_price - ma20_val) / ma20_val <= 0.03:
        if inst_3d_sum > 0 and latest_yoy > 15:
            tech_conclusion_short = "🛡️ 精準拉回"
            tech_conclusion_long = "🛡️ **【高手低吸點】** 股價修正至 MA20 均線防守區，過熱指標已洗淨，且下跌期間法人偷偷吃貨，下檔防守強固。"

    # 交易藍圖精算（改採 20D高點 作為突破決策 Pivot）
    brk_setup = (current_price >= real_resistance * 0.98) and (rsi_now < 73)
    pb_setup = (current_price < real_resistance) and (current_price >= ma20_val * 0.98)

    target_brk = round_to_tick(current_price + (2.5 * atr), t)
    stop_brk = round_to_tick(real_resistance - (1.5 * atr) - slip, t)
    rr1_brk = (target_brk - current_price) / (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0

    target_pb = round_to_tick(real_resistance, t)
    stop_pb = round_to_tick(current_price - atr - slip, t)
    rr1_pb = (target_pb - current_price) / (current_price - stop_pb) if (current_price - stop_pb) > 0 else 0

    return {
        "stock_id": stock_id, "stock_name": stock_name, "industry": industry,
        "current_price": current_price, "current_vol": current_vol, "vol_ma20": vol_ma20_val,
        "market_desc": m_desc, "market_color": m_color, "pivot": ma20_val, "real_resistance": real_resistance,
        "atr": atr, "tick_size": t, "inst_3d_sheets": inst_3d_sum,
        "latest_revenue_yoy": latest_yoy, "latest_rev_month": latest_rev_month, "latest_rev_value": latest_rev_value,
        "breakout_setup": brk_setup, "pullback_setup": pb_setup,
        "target_brk": target_brk, "stop_brk": stop_brk, "rr1_brk": rr1_brk,
        "target_pb": target_pb, "stop_pb": stop_pb, "rr1_pb": rr1_pb,
        "brk_tradeable": (brk_setup or tech_conclusion_short == "🚀 準備起漲") and rr1_brk >= 1.5,
        "pb_tradeable": pb_setup and rr1_pb >= 2.0,
        "tech_conclusion_long": tech_conclusion_long, "tech_conclusion_short": tech_conclusion_short,
        "eps_now": eps_now, "eps_prev": eps_prev, "gpm_now": gpm_now, "gpm_prev": gpm_prev, "opm_now": opm_now, "opm_prev": opm_prev,
        "fin_conclusion": fin_conclusion, "macd_hist": macd_hist, "rsi_now": rsi_now, "adx_now": adx_now,
        "is_compressed": is_compressed, "vol_spike": vol_spike, "style": ""
    }


# ============ 7. Streamlit UI ============
st.title("SOP v17 終極多因子爆發動能雷達系統")

st.sidebar.header("⚙️ 全局風控參數")
total_cap = st.sidebar.number_input("總本金 (萬元)", value=100.0)
risk_pct = st.sidebar.slider("單筆最大風險 (%)", 0.5, 3.0, 1.0)

tab1, tab2 = st.tabs(["🔍 單股詳細資料深度診斷", "🚀 大盤多股批量雷達"])

with tab1:
    target_stock = st.text_input("輸入股票代碼進行多因子健檢", value="2330").strip()
    if st.button("開始單股雷達掃描"):
        with st.spinner("爆發動能交叉矩陣計算中..."):
            res = evaluate_stock(target_stock, total_cap, risk_pct, 1)
            
            if res:
                res["style"] = detect_style(res)
                st.markdown(f"## 🏢 {res['stock_name']} ({res['stock_id']}) · `{res['industry']}`")
                
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("即時股價", f"{res['current_price']} 元", f"診斷: {res['tech_conclusion_short']}")
                col2.metric("盤中即時量 / 20日均量", f"{res['current_vol']:.0f} 張", f"均量: {res['vol_ma20']:.0f} 張")
                col3.metric(f"月營收衰退/增長率 ({res['latest_rev_month']})", f"{res['latest_revenue_yoy']:.2f} %", f"單月營收: {res['latest_rev_value']:.2f} 億")
                col4.metric("系統推薦操盤風格", res["style"])
                
                st.subheader("💡 終極雷達白話文操盤建議")
                if res["tech_conclusion_short"] == "🚀 準備起漲":
                    st.success(res["tech_conclusion_long"])
                else:
                    st.info(res["tech_conclusion_long"])
                
                st.subheader("📈 盤中五大核心量化指標")
                tech_col1, tech_col2, tech_col3, tech_col4, tech_col5 = st.columns(5)
                macd_trend = "🔺 多頭擴張" if res['macd_hist'] > 0 else "🔻 空頭修正"
                tech_col1.metric("MACD 柱狀體 (Hist)", f"{res['macd_hist']:.3f}", macd_trend)
                tech_col2.metric("RSI(14) 強弱度", f"{res['rsi_now']:.2f}", "超買 >70 | 超賣 <30")
                tech_col3.metric("20日均線支撐 (MA20)", f"{res['pivot']:.1f} 元", f"實質20D壓力: {res['real_resistance']:.1f}元")
                tech_col4.metric("DMI 趨勢動能 (ADX)", f"{res['adx_now']:.1f}", "壓縮狀態" if res['is_compressed'] else "波動發散")
                tech_col5.metric("14日真實波動度 (ATR)", f"{res['atr']:.2f} 元", f"量能爆發: {'是' if res['vol_spike'] else '否'}")

                st.subheader("📦 月營收與法人籌碼動能盾牌")
                c_rev, c_inst = st.columns(2)
                with c_rev:
                    st.markdown(f"### 🏢 月營收動能面板 (最新公佈月份: `{res['latest_rev_month']}`)")
                    if res['latest_revenue_yoy'] > 20:
                        st.success(f"🟢 **營收年增率：{res['latest_revenue_yoy']:.2f}% (單月吸金 {res['latest_rev_value']:.2f} 億)**\n\n營收爆發成長！具備極強本業底氣，起漲突破成功率極高。")
                    elif res['latest_revenue_yoy'] >= 0:
                        st.info(f"🟡 **營收年增率：{res['latest_revenue_yoy']:.2f}% (單月吸金 {res['latest_rev_value']:.2f} 億)**\n\n表現平穩，無衰退危機。適合拉回型防守策略。")
                    else:
                        st.error(f"🔴 **營收年增率：{res['latest_revenue_yoy']:.2f}% (單月吸金 {res['latest_rev_value']:.2f} 億)**\n\n警報！本業動能失速，盤中若強行 breakthrough 高機率是假突破，切勿追高。")
                        
                with c_inst:
                    st.markdown("### 👥 三大法人籌碼追蹤 (3日合計)")
                    if res['inst_3d_sheets'] > 500:
                        st.success(f"🟢 **近3日法人合計買超：{res['inst_3d_sheets']:.0f} 張**\n\n主力部隊用真金白銀控盤幫忙抬轎！籌碼高度集中。")
                    elif res['inst_3d_sheets'] >= -500:
                        st.info(f"🟡 **近3日法人合計買賣超：{res['inst_3d_sheets']:.0f} 張**\n\n法人處於觀望拉鋸狀態，主要由內資與盤中散戶情緒主導。")
                    else:
                        st.error(f"🔴 **近3日法人合計賣超：{res['inst_3d_sheets']:.0f} 張**\n\n危險！法人正逢高集體出逃，提防『假突破、真埋人』的巨大風險。")
                
                st.subheader("📊 季度核心基本面體檢")
                st.success(res["fin_conclusion"])
                f_col1, f_col2, f_col3 = st.columns(3)
                def get_trend_tag(now, prev): return "🔺 進步" if now > prev else "🔻 退步" if now < prev else "➖ 持平"
                f_col1.metric("每股盈餘 (EPS)", f"{res['eps_now']:.2f} 元", f"前季: {res['eps_prev']:.2f} | {get_trend_tag(res['eps_now'], res['eps_prev'])}")
                f_col2.metric("營業毛利率", f"{res['gpm_now']:.2f} %", f"前季: {res['gpm_prev']:.2f} | {get_trend_tag(res['gpm_now'], res['gpm_prev'])}")
                f_col3.metric("營業利益率", f"{res['opm_now']:.2f} %", f"前季: {res['opm_prev']:.2f} | {get_trend_tag(res['opm_now'], res['opm_prev'])}")
                
                st.subheader("🎯 交易藍圖與精算風控價位 (突破參考20D高點压力)")
                box_brk, box_pb = st.columns(2)
                with box_brk:
                    st.markdown("### 跑 🏃‍♂️ 【突破/起漲追擊策略】藍圖")
                    st.markdown(f"* **現價/突破進場點：** `{res['current_price']}` 元")
                    st.markdown(f"* **預估停利目標價：** `{res['target_brk']}` 元")
                    st.markdown(f"* **防守停損點：** `{res['stop_brk']}` 元")
                    if res['rr1_brk'] < 1.5: st.error(f"❌ 當前風報比: **{res['rr1_brk']:.2f}** (空間不足/不符合交易期望值)")
                    else: st.success(f"🚀 當前風報比: **{res['rr1_brk']:.2f}** (🟢 優勢起漲突圍點位)")
                with box_pb:
                    st.markdown("### 🛡️ 【拉回均線低吸策略】藍圖")
                    st.markdown(f"* **理想買入點：** `{res['current_price']}` 元")
                    st.markdown(f"* **短線停利價：** `{res['target_pb']}` 元")
                    st.markdown(f"* **破位停損點：** `{res['stop_pb']}` 元")
                    if res['rr1_pb'] < 2.0: st.error(f"❌ 當前風報比: **{res['rr1_pb']:.2f}** (利潤空間過窄)")
                    else: st.success(f"🚀 當前風報比: **{res['rr1_pb']:.2f}** (🟢 理想低吸點)")

                st.write("### 🔍 因子診斷後台原始 JSON 數據")
                st.json(res)
            else:
                st.error("找不到該股票歷史資料，請確認代碼是否有誤。")

with tab2:
    st.subheader(" UFO 大盤多因子全自動選股雷達")
    info_df = get_stock_info_df()
    all_industries = sorted([str(x) for x in info_df["industry_category"].dropna().unique() if str(x).strip() != ""])
    
    scan_scope = st.radio(
        "🎯 請選擇雷達自動掃描範圍：",
        ["🔥 核心權值龍頭股 (自動精選市值前30大標的)", "🏭 依特定產業類股全自動橫掃"],
        key="bulk_scan_scope"
    )
    
    selected_ind = None
    if scan_scope == "🏭 依特定產業類股全自動橫掃":
        selected_ind = st.selectbox("選擇要轟炸的產業板塊：", all_industries, index=all_industries.index("半導體業") if "半導體業" in all_industries else 0)

    if st.button("🚀 啟動全自動雷達大盤掃描"):
        if scan_scope == "🔥 核心權值龍頭股 (自動精選市值前30大標的)":
            stock_list = [
                "2330", "2317", "2454", "2308", "2382", "2324", "2881", "2882", "2603", "2609", 
                "2618", "2357", "4938", "3231", "2301", "2886", "2891", "2884", "2892", "2379", 
                "3008", "3045", "2408", "1301", "1303", "2002", "2912", "9910", "2345", "6505"
            ]
        else:
            stock_list = info_df[info_df["industry_category"] == selected_ind]["stock_id"].tolist()
            if len(stock_list) > 40: stock_list = stock_list[:40] # 限制掃描數量防止超時
            
        all_results = []
        progress_bar = st.progress(0)
        
        for idx, sid in enumerate(stock_list):
            try:
                res = evaluate_stock(sid, total_cap, risk_pct, 1)
                if res:
                    res["style"] = detect_style(res)
                    all_results.append({
                        "代碼": res["stock_id"], "名稱": res["stock_name"], "即時現價": res["current_price"],
                        "操作風格": res["style"], "技術狀態": res["tech_conclusion_short"],
                        "法人3日(張)": res["inst_3d_sheets"], "營收YoY(%)": res["latest_revenue_yoy"],
                        "單月營收(億)": round(res["latest_rev_value"], 2), "營收月份": res["latest_rev_month"],
                        "突破型風報比": round(res["rr1_brk"], 2), "拉回型風報比": round(res["rr1_pb"], 2),
                        "突破型建議": "🟢 值得進攻" if res["brk_tradeable"] else "❌ 風報比不及格",
                        "拉回型建議": "🟢 值得低吸" if res["pb_tradeable"] else "❌ 空間不足"
                    })
            except Exception: pass
            progress_bar.progress((idx + 1) / len(stock_list))
            
        if all_results:
            scan_df = pd.DataFrame(all_results)
            st.success(f"🎉 大盤全自動掃描完成！成功分析 {len(all_results)} 檔標的。")
            tradeable_only = st.checkbox("🎯 只顯示『準備起漲』或符合黃金策略標準的精選標的")
            
            if tradeable_only:
                filtered_df = scan_df[(scan_df["技術狀態"] == "🚀 準備起漲") | (scan_df["突破型建議"] == "🟢 值得進攻") | (scan_df["拉回型建議"] == "🟢 值得低吸")]
                if not filtered_df.empty:
                    st.dataframe(filtered_df.sort_values(by=["突破型風報比"], ascending=False), use_container_width=True, hide_index=True)
                else:
                    st.warning("😭 因子掃描完畢，目前市場內暫時沒有極品起漲訊號標的。")
            else:
                st.dataframe(scan_df, use_container_width=True, hide_index=True)
        else:
            st.error("未能成功讀取任何標的數據，請確認 API 狀態。")
