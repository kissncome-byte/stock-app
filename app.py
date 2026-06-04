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
import concurrent.futures
from FinMind.data import DataLoader

# ============ 1. Page Config ============
st.set_page_config(page_title="SOP v17 高精準多因子個股深度診斷系統", layout="wide")

# ============ 2. Global Constants ============
TZ = pytz.timezone("Asia/Taipei")

# ============ 3. Helper Functions & Defensive Utilities ============
def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ["-", "", "None", "nan", "NaN"]:
            return default
        clean_str = str(x).replace(",", "").replace("%", "").strip()
        return float(clean_str)
    except Exception:
        return default


def tick_size(p: float) -> float:
    """符合台灣證券交易所現行法規之升降單位規則"""
    if p >= 1000: return 5.0
    if p >= 500:  return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.05
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
            if current_time > datetime.strptime("16:00", "%H:%M").time() and last_trade_date_str != now.strftime("%Y-%m-%d"):
                return "CLOSED_HOLIDAY", f"市場休市 (國定假日) | 數據日期: {last_trade_date_str}", "gray"
            return "POST_MARKET", f"今日已收盤 | 數據日期: {last_trade_date_str}", "green"


def detect_style(result: dict) -> str:
    if "黑馬" in result.get("invest_status", ""):
        return "大轉折潛力黑馬型"
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
    pos_words = ['創新高', '大賺', '暴增', '飆', '大成長', '利多', '優於預期', '加碼', '看旺', '強勢', '獲利', '突破', '轉盈', '充沛', '加持', '買超', '爆發', '新高', '雙率雙升', '三率三升', '扭虧為盈', '轉虧為盈', '急單']
    neg_words = ['衰退', '虧損', '重挫', '低於預期', '縮水', '跌破', '警告', '利空', '下滑', '疲弱', '裁員', '大跌', '慘', '賣壓', '修正', '賣超', '暴跌', '逆風', '雙率雙降']
    
    pos_score = sum(1 for w in pos_words if w in title)
    neg_score = sum(1 for w in neg_words if w in title)
    
    if pos_score > neg_score: return "🟢 即時利多", "green"
    elif neg_score > pos_score: return "🔴 即時利空", "red"
    return "🟡 中性消息", "gray"


def compute_live_data(stock_id: str, hist_last_close: float, hist_last_vol: float, live_price_override: float = None):
    if live_price_override is not None and live_price_override > 0:
        return live_price_override, hist_last_vol * 1.2, True, "模擬串流", "realtime"

    rt_price, rt_vol, rt_success = None, None, False
    rt_source, rt_type = "歷史收盤", "historical"

    try:
        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        session.get("https://mis.twse.com.tw/stock/index.jsp", headers=headers, timeout=2, verify=certifi.where())
        ts = int(time.time() * 1000)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
        r = session.get(url, headers=headers, timeout=2, verify=certifi.where())

        if r.status_code == 200:
            data = r.json()
            if "msgArray" in data and len(data["msgArray"]) > 0:
                info = data["msgArray"][0]
                z = safe_float(info.get("z"))
                v = safe_float(info.get("v")) 
                if z == 0: z = safe_float(info.get("o"))
                if z > 0:
                    rt_price = z
                    rt_vol = v if v > 0 else hist_last_vol
                    rt_success = True
                    rt_source, rt_type = "TWSE 即時", "realtime"
    except Exception: pass

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
                        rt_vol = (v / 1000) if v > 0 else hist_last_vol
                        rt_success = True
                        rt_source, rt_type = f"Yahoo {suffix}", "delayed"
                        break
        except Exception: pass

    return (rt_price if rt_success else hist_last_close), (rt_vol if (rt_success and rt_vol > 0) else hist_last_vol), rt_success, rt_source, rt_type


# ============ 4. Auth & API Initialization ============
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")


# ============ 5. Cached Data Layer ============
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
                source = item.find('source').text if item.find('source') is not None else "財經新聞"
                if " - " in title: title = title.rsplit(" - ", 1)[0]
                news_list.append({"date": pub_date, "title": title, "source": source, "link": link})
    except Exception: pass
    return pd.DataFrame(news_list)


# ============ 6. Math & Technical Processing Engine ============
def prepare_indicator_df(df: pd.DataFrame):
    if df is None or df.empty: return None
    x = df.copy().sort_values("date").reset_index(drop=True)
    
    # 標準威爾德平滑法技術指標計算
    close_prev = x["close"].shift(1)
    tr1 = x["high"] - x["low"]
    tr2 = (x["high"] - close_prev).abs()
    tr3 = (x["low"] - close_prev).abs()
    x["TR"] = np.maximum(tr1, np.maximum(tr2, tr3))
    
    x["ATR14"] = x["TR"].ewm(com=13, adjust=False).mean()
    x["MA20"] = x["close"].rolling(20).mean()
    x["MA20_Vol"] = x["vol"].rolling(20).mean()
    x["Res_20D"] = x["high"].rolling(20).max()

    x["std20"] = x["close"].rolling(20).std()
    x["BB_upper"] = x["MA20"] + (x["std20"] * 2)
    x["BB_lower"] = x["MA20"] - (x["std20"] * 2)
    x["BB_bandwidth"] = (x["BB_upper"] - x["BB_lower"]) / x["MA20"]

    delta = x["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean().replace(0, 0.00001)
    x["RSI14"] = 100 - (100 / (1 + (avg_gain / avg_loss)))

    x["up_move"] = x["high"].diff()
    x["down_move"] = x["low"].shift(1) - x["low"]
    x["plus_dm"] = np.where((x["up_move"] > x["down_move"]) & (x["up_move"] > 0), x["up_move"], 0)
    x["minus_dm"] = np.where((x["down_move"] > x["up_move"]) & (x["down_move"] > 0), x["down_move"], 0)
    
    tr_smooth = x["TR"].ewm(com=13, adjust=False).mean().replace(0, 0.00001)
    x["PLUS_DI"] = (x["plus_dm"].ewm(com=13, adjust=False).mean() / tr_smooth) * 100
    x["MINUS_DI"] = (x["minus_dm"].ewm(com=13, adjust=False).mean() / tr_smooth) * 100
    di_sum = (x["PLUS_DI"] + x["MINUS_DI"]).replace(0, 0.00001)
    x["ADX14"] = ((x["PLUS_DI"] - x["MINUS_DI"]).abs() / di_sum * 100).ewm(com=13, adjust=False).mean()

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
    stock_name = match["stock_name"].values[0] if not match.empty else "指定標的"
    industry = match["industry_category"].values[0] if not match.empty else "未知板塊"

    hist_last = df.iloc[-1]
    last_trade_date_str = str(hist_last["date"])

    # 根據歷史成交金額自動化判定是否為巨量權值龍頭
    recent_amount_ma = df["amount"].tail(20).mean()
    is_heavyweight = recent_amount_ma > 1500000000  # 日均成交額大於15億視為大象權值股
    
    if is_heavyweight:
        vol_multiplier = 1.25     
        compress_quantile = 0.35  
        target_atr_ratio = 2.0    
    else:
        vol_multiplier = 2.2      
        compress_quantile = 0.18  
        target_atr_ratio = 3.5    

    current_price, current_vol, rt_success, rt_source, rt_type = compute_live_data(
        stock_id, float(hist_last["close"]), float(hist_last["vol"])
    )
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

    # 籌碼防禦性縱深檢查
    inst_df = get_inst_df(stock_id, days=20)
    inst_3d_sum = 0.0
    if not inst_df.empty and "buy" in inst_df.columns and "sell" in inst_df.columns and "date" in inst_df.columns:
        inst_df = inst_df.copy()
        inst_df["net_sheets"] = pd.to_numeric(inst_df["buy"], errors="coerce").fillna(0) - pd.to_numeric(inst_df["sell"], errors="coerce").fillna(0)
        inst_daily = inst_df.groupby("date")["net_sheets"].sum().reset_index().sort_values("date")
        if not inst_daily.empty:
            inst_3d_sum = float(inst_daily.tail(3)["net_sheets"].sum())

    # 🚀 【關鍵修正】縱深縱向防護機制：徹底阻斷不存在欄位引起的 KeyError
    rev_df = get_rev_df(stock_id, days=365)
    latest_yoy = 0.0
    latest_rev_month = "尚無公告"
    latest_rev_value = 0.0
    has_revenue_data = False
    
    if not rev_df.empty and "revenue" in rev_df.columns and "revenue_year_growth_rate" in rev_df.columns and "date" in rev_df.columns:
        rev_df = rev_df.copy()
        rev_df["revenue"] = pd.to_numeric(rev_df["revenue"].astype(str).str.replace(",", ""), errors="coerce")
        rev_df["revenue_year_growth_rate"] = pd.to_numeric(rev_df["revenue_year_growth_rate"].astype(str).str.replace("%", ""), errors="coerce")
            
        rev_clean = rev_df.dropna(subset=["revenue_year_growth_rate", "revenue"])
        rev_clean = rev_clean[rev_clean["revenue"] > 0]
        
        if not rev_clean.empty:
            rev_sorted = rev_clean.sort_values("date")
            rev_last_row = rev_sorted.iloc[-1]
            latest_yoy = float(rev_last_row["revenue_year_growth_rate"])
            latest_rev_month = str(rev_last_row.get("date", "未知月份"))
            latest_rev_value = float(rev_last_row["revenue"]) / 100000000.0
            has_revenue_data = True

    # 財報季度防禦性體檢
    fin_df = get_financial_statement_df(stock_id, years=2)
    eps_now, eps_prev, gpm_now, gpm_prev, opm_now, opm_prev = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    fin_conclusion = "📋 該標的暫無足夠或不適用季度財報數據（如指數或部分特殊ETF）。"
    has_financial_data = False
    
    if not fin_df.empty and "Revenue" in fin_df.columns and "GrossProfit" in fin_df.columns and "OperatingIncome" in fin_df.columns and "EPS" in fin_df.columns:
        fin_df = fin_df.sort_values("date").reset_index(drop=True)
        for idx in range(len(fin_df)):
            rev_amt = safe_float(fin_df.loc[idx, "Revenue"])
            fin_df.loc[idx, "gpm"] = (safe_float(fin_df.loc[idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df.loc[idx, "opm"] = (safe_float(fin_df.loc[idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        
        latest_fin = fin_df.iloc[-1]
        eps_now = safe_float(latest_fin.get("EPS", 0.0))
        gpm_now = safe_float(latest_fin.get("gpm", 0.0))
        box_opm = latest_fin.get("opm", 0.0)
        opm_now = safe_float(box_opm)
        has_financial_data = True
        
        if len(fin_df) >= 2:
            prev_fin = fin_df.iloc[-2]
            eps_prev = safe_float(prev_fin.get("EPS", 0.0))
            gpm_prev = safe_float(prev_fin.get("gpm", 0.0))
            opm_prev = safe_float(prev_fin.get("opm", 0.0))
            gpm_text = "進步" if gpm_now > gpm_prev else "退步" if gpm_now < gpm_prev else "持平"
            opm_text = "進步" if opm_now > opm_prev else "退步" if opm_now < opm_prev else "持平"
            eps_lbl = "多賺" if eps_now > eps_prev else "少賺" if eps_now < eps_prev else "持平"
            if gpm_now > gpm_prev and opm_now > opm_prev and eps_now > eps_prev:
                fin_conclusion = f"📈 **【財報結構升級】** 最新三大獲利指標全數超越上一季！本業獲利體質強健。"
            elif gpm_now < gpm_prev and opm_now < opm_prev and eps_now < eps_prev:
                fin_conclusion = f"📉 **【獲利能力全面退步】** 毛利、營益率、EPS 同步倒退，提防題材虛火。"
            else:
                fin_conclusion = f"⚖️ **【橫盤調整期】** 結構互有勝負：毛利『{gpm_text}』、營益率『{opm_text}』、EPS『{eps_lbl}』。"

    # 新聞情緒分析
    news_df = get_realtime_news_df(stock_id, stock_name)
    news_summary, news_color = "門🟡 中性消息", "gray"
    if not news_df.empty and "title" in news_df.columns:
        pos_cnt, neg_cnt = 0, 0
        for title in news_df["title"].tail(6).tolist():
            lbl, _ = analyze_news_sentiment(title)
            if "利多" in lbl: pos_cnt += 1
            elif "利空" in lbl: neg_cnt += 1
        if pos_cnt > neg_cnt: news_summary, news_color = "🟢 即時輿情偏多", "green"
        elif neg_cnt > pos_cnt: news_summary, news_color = "🔴 即時輿情偏空", "red"

    # 量化技術指標物理量
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

    vol_spike = current_vol > (vol_ma20_val * vol_multiplier)
    bandwidth_60d = df["BB_bandwidth"].tail(60)
    is_compressed = current_bandwidth < bandwidth_60d.quantile(compress_quantile) if not bandwidth_60d.empty else False

    tech_conclusion_short = "中性觀望"
    tech_conclusion_long = "⚖️ 擺動指標目前處於中性橫盤區，大資金尚未表態，短線缺乏爆發性動能。"

    if current_price >= real_resistance * 0.995 and vol_spike and is_compressed:
        tech_conclusion_short = "🚀 準備起漲"
        tech_conclusion_long = f"🚀 **【完美風暴！爆發性起漲點】** 盤中強勢挑戰前高壓力（{real_resistance:.2f} 元），且**爆發主力攻擊量**！通道歷經充分盤整洗盤。"
    elif adx_now < 20:
        tech_conclusion_short = "💤 盤整死水"
        tech_conclusion_long = "💤 **【盤整死水期】** ADX趨向極低，多空沒有方向，容易橫盤磨人，突破策略失敗率高。"
    elif rsi_now >= 75:
        tech_conclusion_short = "⚠️ 短線過熱"
        tech_conclusion_long = "⚠️ **【短線極度過熱】** RSI進入嚴重超買區，追高性價比低，高機率回檔修正。"
    elif float(hist_last["PLUS_DI"]) > float(hist_last["MINUS_DI"]) and adx_now >= 20:
        if inst_3d_sum > 0 and latest_yoy > 20:
            tech_conclusion_short = "🚀 完美多頭"
            tech_conclusion_long = "🚀 **【黃金進攻波段】** 技術面強勢多頭，法人金流真槍實彈幫忙抬轎。"
        elif inst_3d_sum < 0:
            tech_conclusion_short = "⚠️ 假突破嫌疑"
            tech_conclusion_long = "⚠️ **【提防假突破】** K線強勢但法人這幾天趁拉高偷偷倒貨，極可能是誘多陷阱。"
        else:
            tech_conclusion_short = "🚀 多頭成形"
            tech_conclusion_long = "趨勢多頭成形，買盤動能延續性佳，適合尋找突破點切入。"
    elif float(hist_last["MINUS_DI"]) > float(hist_last["PLUS_DI"]) and adx_now >= 20:
        tech_conclusion_short = "📉 空頭成形"
        tech_conclusion_long = "📉 **【強勢空頭成形】** 技術面由空方完全主導，賣壓沉重，切勿盲目摸底。"

    if tech_conclusion_short not in ["🚀 準備起漲", "🚀 完美多頭", "⚠️ 假突破嫌疑"] and current_price >= ma20_val and (current_price - ma20_val) / ma20_val <= 0.03:
        if inst_3d_sum > 0 and latest_yoy > 15:
            tech_conclusion_short = "🛡️ 精準拉回"
            tech_conclusion_long = "🛡️ **【高手低吸點】** 股價修正至 MA20 均線防守區，過熱指標已洗淨，且下跌期間法人偷偷吃貨。"

    # ============ 核心成長與大轉折黑馬雙軌精準診斷引擎 ============
    f_score, i_score, t_score = 0, 0, 0
    diag_fundamentals, diag_chips, diag_technicals = [], [], []
    is_turnaround_dark_horse = False
    turnaround_reason = ""

    # 黑馬特戰特判邏輯（當量能與空間形成完美風暴，且基本面迎來暴增轉折）
    if is_compressed and vol_spike and current_price >= real_resistance * 0.99:
        eps_turn_around = (eps_now > 0) and (eps_prev <= 0) if has_financial_data else False
        revenue_rocket = latest_yoy >= 40.0 if has_revenue_data else False
        if eps_turn_around or revenue_rocket:
            is_turnaround_dark_horse = True
            if eps_turn_around:
                turnaround_reason = f"🔮 **【超級黑馬：單季轉虧為盈！】** 過去大虧損，最新一季 EPS 成功扭虧為盈（{eps_prev:.2f} ➔ {eps_now:.2f}），具備巨大想像空間！"
            else:
                turnaround_reason = f"🔮 **【轉型爆發：營收懸崖式暴增！】** 最新月營收 YoY 拔地而起高達 {latest_yoy:.1f}%，新產能或新訂單大灌頂！"

    # 基本面縱深體檢
    if has_revenue_data:
        if latest_yoy >= 25:
            f_score += 20
            diag_fundamentals.append(f"🟢 **營收極度強勁**：最新月營收年增達 {latest_yoy:.1f}%，產業天花板尚未到來。")
        elif latest_yoy >= 10:
            f_score += 10
            diag_fundamentals.append(f"🟡 **營收溫和成長**：最新月營收年增 {latest_yoy:.1f}%，表現平穩。")
        else:
            diag_fundamentals.append(f"🔴 **營收動能失速**：最新月營收年增僅 {latest_yoy:.1f}%，小心成長型題材鈍化。")
    else:
        diag_fundamentals.append("⚪ **營收數據不適用**：該標的類型未公告或不適用一般月營收常態分析。")

    if has_financial_data:
        if gpm_now > gpm_prev and opm_now > opm_prev:
            f_score += 20
            diag_fundamentals.append("🟢 **定價權極強（雙率雙升）**：毛利率與營益率皆超越上季，轉嫁成本能力一流。")
        elif gpm_now < gpm_prev and opm_now < opm_prev:
            diag_fundamentals.append("🔴 **本業獲利倒退（雙率雙降）**：毛利率與營益率雙雙衰退，陷入削價競爭，基本面亮紅燈！")
        else:
            f_score += 10
            diag_fundamentals.append("⚖️ **獲利結構調整**：毛利與營益率互有勝負，本業防守力一般。")
    else:
        diag_fundamentals.append("⚪ **季度財報不適用**：暫無季度獲利三率歷史對比數據。")

    # 籌碼面評分
    if inst_3d_sum > 800:
        i_score += 30
        diag_chips.append(f"🟢 **法人瘋狂抬轎**：近3日法人集體灌入 {inst_3d_sum:.0f} 張，籌碼朝大戶口袋鎖死。")
    elif inst_3d_sum < -800:
        diag_chips.append(f"🔴 **法人大舉出貨**：近3日法人瘋狂調節 {abs(inst_3d_sum):.0f} 張，大舉倒貨給散戶，切勿接刀！")
    else:
        i_score += 15
        diag_chips.append(f"🟡 **籌碼處於拉鋸**：法人短線無大幅度單邊動作（{inst_3d_sum:.0f} 張），由內資或散戶情緒主導。")

    # 技術面評分
    if rsi_now > 75:
        diag_technicals.append(f"🔴 **技術面極度超買**：RSI 達 {rsi_now:.1f}，短線引擎過熱，追高性價比極低，提防修正！")
    elif 50 <= rsi_now <= 68:
        t_score += 20
        diag_technicals.append(f"🟢 **多頭蓄勢區**：RSI {rsi_now:.1f} 處於健康發散區，多頭推進有序。")
    else:
        t_score += 10
        diag_technicals.append(f"🟡 **動能陷入膠著**：RSI 處於 {rsi_now:.1f} 擺動區，多空短線拉鋸。")

    if current_price <= ma20_val * 1.03 and current_price >= ma20_val * 0.98:
        t_score += 10
        diag_technicals.append("🟢 **貼近黃金防守區**：現價距離生命線(MA20)極近，下檔有支撐，風報比優異。")

    total_score = f_score + i_score + t_score

    if is_turnaround_dark_horse:
        invest_status, status_color, status_emoji = "🔮 特戰訊號：轉折爆發黑馬股", "purple", "🔮"
    elif total_score >= 75:
        invest_status, status_color, status_emoji = "🔥 砸大資金極品買點", "green", "🚀"
    elif total_score >= 45:
        if rsi_now > 75: invest_status, status_color, status_emoji = "⚠️ 暫勿投入：短線過熱", "orange", "⏳"
        else: invest_status, status_color, status_emoji = "🟢 分批布局強勢股", "blue", "⚖️"
    else:
        invest_status, status_color, status_emoji = "❌ 結構轉弱：不值投入", "red", "🚨"

    # 交易藍圖精算與動態風控部位計算
    brk_setup = (current_price >= real_resistance * 0.98) and (rsi_now < 73)
    pb_setup = (current_price < real_resistance) and (current_price >= ma20_val * 0.98)

    target_brk = round_to_tick(current_price + (target_atr_ratio * atr), t)
    stop_brk = round_to_tick(real_resistance - (1.5 * atr) - slip, t)
    if stop_brk >= current_price: stop_brk = round_to_tick(current_price - (1.0 * atr), t)
    rr1_brk = (target_brk - current_price) / (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0

    target_pb = round_to_tick(real_resistance, t)
    stop_pb = round_to_tick(current_price - atr - slip, t)
    rr1_pb = (target_pb - current_price) / (current_price - stop_pb) if (current_price - stop_pb) > 0 else 0

    # 核心(80%)與衛星(20%)動態資金配額
    pool_allocation = total_capital * 0.8 if is_heavyweight else total_capital * 0.2
    risk_money = pool_allocation * (risk_per_trade / 100) * 10000 
    
    loss_per_share_brk = (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0.01
    suggested_lots_brk = int((risk_money / loss_per_share_brk) / 1000) if loss_per_share_brk > 0 else 0
    if is_turnaround_dark_horse: suggested_lots_brk = int(suggested_lots_brk * 0.5) # 黑馬股預算自動砍半
    
    loss_per_share_pb = (current_price - stop_pb) if (current_price - stop_pb) > 0 else 0.01
    suggested_lots_pb = int((risk_money / loss_per_share_pb) / 1000) if loss_per_share_pb > 0 else 0

    return {
        "stock_id": stock_id, "stock_name": stock_name, "industry": industry,
        "current_price": current_price, "current_vol": current_vol, "vol_ma20": vol_ma20_val,
        "market_desc": m_desc, "market_color": m_color, "pivot": ma20_val, "real_resistance": real_resistance,
        "atr": atr, "tick_size": t, "inst_3d_sheets": inst_3d_sum,
        "latest_revenue_yoy": latest_yoy, "latest_rev_month": latest_rev_month, "latest_rev_value": latest_rev_value,
        "breakout_setup": brk_setup, "pullback_setup": pb_setup,
        "target_brk": target_brk, "stop_brk": stop_brk, "rr1_brk": rr1_brk, "suggested_lots_brk": suggested_lots_brk,
        "target_pb": target_pb, "stop_pb": stop_pb, "rr1_pb": rr1_pb, "suggested_lots_pb": suggested_lots_pb,
        "brk_tradeable": (brk_setup or tech_conclusion_short == "🚀 準備起漲") and rr1_brk >= 1.5,
        "pb_tradeable": pb_setup and rr1_pb >= 2.0,
        "tech_conclusion_long": tech_conclusion_long, "tech_conclusion_short": tech_conclusion_short,
        "eps_now": eps_now, "eps_prev": eps_prev, "gpm_now": gpm_now, "gpm_prev": gpm_prev, "opm_now": opm_now, "opm_prev": opm_prev,
        "fin_conclusion": fin_conclusion, "macd_hist": macd_hist, "rsi_now": rsi_now, "adx_now": adx_now,
        "is_compressed": is_compressed, "vol_spike": vol_spike, "style": "",
        "invest_status": invest_status, "status_color": status_color, "status_emoji": status_emoji,
        "diag_fundamentals": diag_fundamentals, "diag_chips": diag_chips, "diag_technicals": diag_technicals,
        "is_turnaround_dark_horse": is_turnaround_dark_horse, "turnaround_reason": turnaround_reason,
        "news_summary": news_summary, "news_color": news_color, "is_heavyweight": is_heavyweight
    }


# ============ 7. Streamlit UI Layer ============
st.sidebar.header("⚙️ 核心-衛星雙軌風控系統")
total_cap = st.sidebar.number_input("總資產配置本金 (萬元)", value=100.0)
risk_pct = st.sidebar.slider("單筆最大可承受風險 (%)", 0.5, 3.0, 1.0)

core_cap_pool = total_cap * 0.8
sat_cap_pool = total_cap * 0.2

st.sidebar.markdown(f"""
---
### 📊 資金池動態配額
* 🏛️ **核心權值大象池(80%)：** `{core_cap_pool:.1f}` 萬元
* 🚀 **衛星突擊飆股池(20%)：** `{sat_cap_pool:.1f}` 萬元
* 🛡️ **每筆最大損失金額：** `{total_cap * (risk_pct / 100) * 10000:.0f}` 元
""")

tab1, tab2 = st.tabs(["🔍 核心個股「值不值投入」深度診斷", "🛸 大盤全自動掃描雷達"])

with tab1:
    target_stock = st.text_input("請輸入要進行診斷的股票代碼（如 2330、2317）", value="2330").strip()
    if st.button("開始個股雷達全因子診斷"):
        with st.spinner("多因子數據縱深交叉分析中..."):
            res = evaluate_stock(target_stock, total_cap, risk_pct, 1)
            
            if res:
                res["style"] = detect_style(res)
                
                # 黑馬特戰專用大面板
                if res["invest_status"] == "🔮 特戰訊號：轉折爆發黑馬股":
                    st.balloons()
                    st.markdown(f"""
                    <div style="background-color:#F3E5F5; padding:22px; border-radius:12px; border-left:8px solid #8E24AA; margin-bottom:20px;">
                        <h2 style="color:#4A148C; margin:0; font-size:24px;">{res['status_emoji']} 核心決策：【{res['invest_status']}】</h2>
                        <p style="color:#4A148C; font-size:16px; margin-top:10px; line-height:1.6;">
                            {res['turnaround_reason']}<br>
                            <b>【操盤特戰鐵律】</b>：黑馬股炒作的是未來扭轉的想像空間，歷史財報極差是常態。目前系統已<b>自動將建議配置張數砍半</b>，請嚴格執行下方的鋼鐵停損。
                        </p>
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    status_colors_map = {"green": "#E8F5E9", "orange": "#FFF3CD", "blue": "#E3F2FD", "red": "#FFEBEE"}
                    text_colors_map = {"green": "#1B5E20", "orange": "#856404", "blue": "#0D47A1", "red": "#B71C1C"}
                    border_colors_map = {"green": "#4CAF50", "orange": "#FFC107", "blue": "#2196F3", "red": "#F44336"}
                    
                    st.markdown(f"""
                    <div style="background-color:{status_colors_map[res['status_color']]}; padding:22px; border-radius:12px; border-left:8px solid {border_colors_map[res['status_color']]}; margin-bottom:20px;">
                        <h2 style="color:{text_colors_map[res['status_color']]}; margin:0; font-size:24px;">{res['status_emoji']} 核心決策：【{res['invest_status']}】</h2>
                        <p style="color:{text_colors_map[res['status_color']]}; font-size:16px; margin-top:10px;">
                            該股目前綜合評估屬於 <b>{res['style']}</b> 戰略區。系統即時連線狀態為：<b>{res['tech_conclusion_short']}</b>。
                        </p>
                    </div>
                    """, unsafe_allow_html=True)
                
                st.markdown(f"## 🏢 {res['stock_name']} ({res['stock_id']}) · `{res['industry']}` · 資產分流：`{'🏛️ 核心權值大象' if res['is_heavyweight'] else '🚀 衛星特戰飆股'}`")
                
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("即時現價", f"{res['current_price']} 元", f"診斷狀態: {res['tech_conclusion_short']}")
                col2.metric("即時量 / 20日均量", f"{res['current_vol']:.0f} 張", f"均量: {res['vol_ma20']:.0f} 張")
                col3.metric(f"最新月營收 ({res['latest_rev_month']})", f"{res['latest_revenue_yoy']:.2f} %", f"單月營收: {res['latest_rev_value']:.2f} 億")
                col4.metric("即時新聞輿情狀態", res["news_summary"])
                
                # 白話文診斷大面板
                st.subheader("📊 核心全因子白話文深度診斷報告")
                with st.container():
                    c_f, c_i, c_t = st.columns(3)
                    with c_f:
                        st.markdown("#### 🏢 基本面體檢報告")
                        for item in res["diag_fundamentals"]: st.markdown(item)
                    with c_i:
                        st.markdown("#### 👥 籌碼面大戶追蹤")
                        for item in res["diag_chips"]: st.markdown(item)
                    with c_t:
                        st.markdown("#### 📈 技術面進場擇時")
                        for item in res["diag_technicals"]: st.markdown(item)

                st.markdown("---")
                st.subheader("🎯 交易藍圖與風控張數精算")
                box_brk, box_pb = st.columns(2)
                with box_brk:
                    st.markdown("### 🏃‍♂️ 【突破追擊策略】交易藍圖")
                    st.markdown(f"* **建議進場點：** `{res['current_price']}` 元")
                    st.markdown(f"* **預估停利價：** `{res['target_brk']}` 元")
                    st.markdown(f"* **防守停損點：** `{res['stop_brk']}` 元")
                    st.markdown(f"* **💡 系統建議購買張數：** <span style='color:red; font-size:18px; font-weight:bold;'>{res['suggested_lots_brk']}</span> 張", unsafe_allow_html=True)
                    if res['rr1_brk'] < 1.5: st.error(f"❌ 當前風報比: {res['rr1_brk']:.2f} (空間不足，不符勝率利潤比)")
                    else: st.success(f"🚀 當前風報比: {res['rr1_brk']:.2f} (優勢突破點點位)")
                with box_pb:
                    st.markdown("### 🛡️ 【拉回均線低吸策略】交易藍圖")
                    st.markdown(f"* **理想買入點：** `{res['current_price']}` 元")
                    st.markdown(f"* **短線停利價：** `{res['target_pb']}` 元")
                    st.markdown(f"* **破位停損點：** `{res['stop_pb']}` 元")
                    st.markdown(f"* **💡 系統建議購買張數：** <span style='color:green; font-size:18px; font-weight:bold;'>{res['suggested_lots_pb']}</span> 張", unsafe_allow_html=True)
                    if res['rr1_pb'] < 2.0: st.error(f"❌ 當前風報比: {res['rr1_pb']:.2f} (拉回利潤空間過窄)")
                    else: st.success(f"🚀 當前風報比: {res['rr1_pb']:.2f} (理想黃金低吸點)")

                st.subheader("📈 盤中五大核心量化指標")
                tech_col1, tech_col2, tech_col3, tech_col4, tech_col5 = st.columns(5)
                tech_col1.metric("MACD 柱狀體 (Hist)", f"{res['macd_hist']:.3f}", "🔺 多頭擴張" if res['macd_hist'] > 0 else "🔻 空頭修正")
                tech_col2.metric("RSI(14) 強弱度", f"{res['rsi_now']:.2f}", "超買 >70 | 超賣 <30")
                tech_col3.metric("20日均線支撐 (MA20)", f"{res['pivot']:.1f} 元", f"20D壓力: {res['real_resistance']:.1f}元")
                tech_col4.metric("DMI 趨勢動能 (ADX)", f"{res['adx_now']:.1f}", "壓縮緊繃" if res['is_compressed'] else "常態發散")
                tech_col5.metric("14日真實波動度 (ATR)", f"{res['atr']:.2f} 元", f"量能爆發: {'是' if res['vol_spike'] else '否'}")

                st.write("### 🔍 原始 JSON 因子診斷數據欄位")
                st.json(res)
            else:
                st.error("找不到該標的之歷史資料，請確認輸入代碼是否正確。")

with tab2:
    st.subheader("🛸 大盤多因子高速並發自動掃描雷達")
    
    scan_scope = st.radio(
        "🎯 請選擇雷達自動掃描範圍：",
        ["🔥 市值前 15 大核心熱門權值標的", "🏭 依特定產業類股全自動橫掃"],
        key="bulk_scan_scope"
    )
    
    selected_ind = None
    if scan_scope == "🏭 依特定產業類股全自動橫掃":
        selected_ind = st.selectbox("選擇要攻擊的產業板塊：", all_industries, index=all_industries.index("半導體業") if "半導體業" in all_industries else 0)

    if st.button("🚀 啟動選股雷達掃描"):
        if scan_scope == "🔥 市值前 15 大核心熱門權值標的":
            stock_list = ["2330", "2317", "2454", "2382", "3711", "2308", "2357", "2881", "2882", "2603", "2609", "3231", "2408", "2449", "3017"]
        else:
            stock_list = info_df[info_df["industry_category"] == selected_ind]["stock_id"].tolist()
            if len(stock_list) > 40: stock_list = stock_list[:40]
            
        all_results = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        def worker(sid):
            try: return evaluate_stock(sid, total_cap, risk_pct, 1)
            except Exception: return None

        status_text.text(f"🚀 高速並發雷達啟動，正在橫掃 {len(stock_list)} 檔標的...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_sid = {executor.submit(worker, sid): sid for sid in stock_list}
            for idx, future in enumerate(concurrent.futures.as_completed(future_to_sid)):
                res = future.result()
                if res:
                    all_results.append({
                        "代碼": res["stock_id"], "名稱": res["stock_name"], "現價": res["current_price"],
                        "核心決策": res["invest_status"], "技術狀態": res["tech_conclusion_short"],
                        "法人3日(張)": res["inst_3d_sheets"], "營收YoY(%)": res["latest_revenue_yoy"],
                        "單月營收(億)": round(res["latest_rev_value"], 2),
                        "突破風報比": round(res["rr1_brk"], 2), "建議張數(突破)": res["suggested_lots_brk"],
                        "拉回風報比": round(res["rr1_pb"], 2), "建議張數(拉回)": res["suggested_lots_pb"],
                        "突破型建議": "🟢 值得進攻" if res["brk_tradeable"] else "❌ 風報比不及格",
                        "拉回型建議": "🟢 值得低吸" if res["pb_tradeable"] else "❌ 空間不足"
                    })
                progress_bar.progress((idx + 1) / len(stock_list))
                
        status_text.empty()
        
        if all_results:
            scan_df = pd.DataFrame(all_results)
            st.success(f"🎉 掃描完成！已成功分析 {len(all_results)} 檔標的。")
            
            tradeable_only = st.checkbox("🎯 只顯示目前具備價格優勢（極品買點/轉折黑馬/符合風報比）的個股")
            if tradeable_only:
                filtered_df = scan_df[
                    (scan_df["核心決策"].str.contains("極品")) | 
                    (scan_df["核心決策"].str.contains("黑馬")) | 
                    (scan_df["突破型建議"] == "🟢 值得進攻") | 
                    (scan_df["拉回型建議"] == "🟢 值得低吸")
                ]
                if not filtered_df.empty:
                    st.dataframe(filtered_df.sort_values(by=["突破風報比"], ascending=False), use_container_width=True, hide_index=True)
                else:
                    st.warning("😭 當前全市場內暫時沒有完美的起漲或拉回黃金訊號標的。")
            else:
                st.dataframe(scan_df, use_container_width=True, hide_index=True)
        else:
            st.error("未能成功讀取任何標的數據。")
