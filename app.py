import os
import time
import requests
import certifi
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timedelta
import pytz
import concurrent.futures
import xml.etree.ElementTree as ET
from FinMind.data import DataLoader
from streamlit.runtime.scriptrunner import get_script_run_ctx, add_script_run_ctx
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
        if is_trading_hours: return "OPEN", "市場交易中 (即時更新)", "red"
        elif current_time < start_time: return "PRE_MARKET", "盤前準備中 (即時連線正常)", "blue"
        else: return "POST_MARKET", "今日已收盤 (即時報價)", "green"
    else:
        if is_trading_hours: return "API_WAIT", f"連線受限，改用歷史價 | 歷史日期: {last_trade_date_str}", "orange"
        elif current_time < start_time: return "PRE_MARKET", f"盤前準備中 | 歷史日期: {last_trade_date_str}", "blue"
        else:
            if current_time > datetime.strptime("16:00", "%H:%M").time() and last_trade_date_str != now.strftime("%Y-%m-%d"):
                return "CLOSED_HOLIDAY", f"市場休市 (國定假日) | 數據日期: {last_trade_date_str}", "gray"
            return "POST_MARKET", f"今日已收盤 | 數據日期: {last_trade_date_str}", "green"

def detect_style(result: dict) -> str:
    if "黑馬" in result.get("invest_status", ""): return "大轉折潛力黑馬型"
    if result.get("tech_conclusion_short") == "🚀 準備起漲": return "爆發突破型"
    brk_score, pb_score = 0, 0
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
    if pos_score > neg_score: return "🟢 利多", "green"
    elif neg_score > pos_score: return "🔴 利空", "red"
    return "🟡 中性", "gray"

# ============ 4. Auth, API Initialization & Shared Connection ============
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

@st.cache_resource
def get_requests_session():
    """全域共用的大盤資料 Session，並加入 Retry 機制提升連線穩定度"""
    session = requests.Session()
    # 設定自動重試 3 次，遇到 5xx 錯誤時啟動退避策略
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    return session

def compute_live_data(stock_id: str, hist_last_close: float, hist_last_vol: float, live_price_override: float = None):
    if live_price_override is not None and live_price_override > 0:
        return live_price_override, hist_last_vol * 1.2, True, "模擬串流", "realtime"
    rt_price, rt_vol, rt_success = None, None, False
    rt_source, rt_type = "歷史收盤", "historical"
    session = get_requests_session()

    try:
        session.get("https://mis.twse.com.tw/stock/index.jsp", timeout=2, verify=certifi.where())
        ts = int(time.time() * 1000)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
        r = session.get(url, timeout=3, verify=certifi.where())
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
            yahoo_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
            for suffix in [".TW", ".TWO"]:
                url = f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}"
                r = session.get(url, headers=yahoo_headers, timeout=3, verify=certifi.where())
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
    if df is None or df.empty: return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category"])
    df = df.copy()
    if "stock_id" in df.columns: df["stock_id"] = df["stock_id"].astype(str).str.strip()
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
    return df[df["vol"] > 0].copy()

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
    except Exception: return pd.DataFrame()

@st.cache_data(ttl=300)
def get_realtime_news_df(stock_id: str, stock_name: str):
    news_list = []
    session = get_requests_session()
    try:
        query = f"{stock_name} {stock_id} when:1d"
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        r = session.get(url, timeout=5)
        
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ""
                link = item.find('link').text if item.find('link') is not None else ""
                pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ""
                source = item.find('source').text if item.find('source') is not None else "財經新聞"
                if " - " in title: title = title.rsplit(" - ", 1)[0]
                news_list.append({"date": pub_date, "title": title, "source": source, "link": link})
                
        if news_list:
            df = pd.DataFrame(news_list)
            df["parsed_date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_convert('Asia/Taipei')
            df["date"] = df["parsed_date"].dt.strftime('%Y-%m-%d %H:%M')
            df = df.sort_values(by="parsed_date", ascending=False).drop(columns=["parsed_date"])
            return df
            
    except Exception as e: 
        pass
    
    return pd.DataFrame(news_list)

# ============ 6. Math & Technical Processing Engine ============
def prepare_indicator_df(df: pd.DataFrame):
    if df is None or df.empty: return None
    x = df.copy().sort_values("date").reset_index(drop=True)
    
    close_prev = x["close"].shift(1)
    x["TR"] = np.maximum(x["high"] - x["low"], np.maximum((x["high"] - close_prev).abs(), (x["low"] - close_prev).abs()))
    x["ATR14"] = x["TR"].ewm(com=13, adjust=False).mean()
    x["MA20"] = x["close"].rolling(20).mean()
    x["MA20_Vol"] = x["vol"].rolling(20).mean()
    x["Res_20D"] = x["high"].rolling(20).max()

    x["std20"] = x["close"].rolling(20).std()
    x["BB_upper"] = x["MA20"] + (x["std20"] * 2)
    x["BB_lower"] = x["MA20"] - (x["std20"] * 2)
    x["BB_bandwidth"] = (x["BB_upper"] - x["BB_lower"]) / x["MA20"]

    delta = x["close"].diff()
    avg_gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    avg_loss = -delta.clip(upper=0).ewm(com=13, adjust=False).mean().replace(0, 0.00001)
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

    return x.dropna(subset=["ATR14", "MA20", "Res_20D", "BB_bandwidth", "RSI14", "ADX14", "MACD_HIST"]).copy()

def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int, fetch_news: bool = True):
    df_raw = get_daily_df(stock_id, days=365)
    if df_raw is None or df_raw.empty: return None

    df = prepare_indicator_df(df_raw)
    if df is None or df.empty: return None

    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    stock_name = match["stock_name"].values[0] if not match.empty else "指定標的"
    industry = match["industry_category"].values[0] if not match.empty else "未知板塊"

    hist_last = df.iloc[-1]
    last_trade_date_str = str(hist_last["date"])

    # 判定大象股門檻 (20億)
    recent_amount_ma = df["amount"].tail(20).mean()
    is_heavyweight = recent_amount_ma > 2000000000  
    if is_heavyweight: vol_multiplier, compress_quantile, target_atr_ratio = 1.25, 0.35, 2.0
    else: vol_multiplier, compress_quantile, target_atr_ratio = 2.2, 0.18, 3.5    

    current_price, current_vol, rt_success, rt_source, rt_type = compute_live_data(
        stock_id, float(hist_last["close"]), float(hist_last["vol"])
    )
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

    # ============ 1. 先計算量化技術指標物理量 ============
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

    # 籌碼與財報獲取
    inst_df = get_inst_df(stock_id, days=20)
    inst_3d_sum = 0.0
    if not inst_df.empty and "buy" in inst_df.columns:
        inst_df["net_sheets"] = pd.to_numeric(inst_df["buy"], errors="coerce").fillna(0) - pd.to_numeric(inst_df.get("sell",0), errors="coerce").fillna(0)
        inst_daily = inst_df.groupby("date")["net_sheets"].sum().reset_index().sort_values("date")
        if not inst_daily.empty: inst_3d_sum = float(inst_daily.tail(3)["net_sheets"].sum())

    # 營收安全獲取
    rev_df = get_rev_df(stock_id, days=365)
    latest_yoy, latest_rev_month, latest_rev_value, has_revenue_data = 0.0, "尚無公告", 0.0, False
    if not rev_df.empty and "revenue" in rev_df.columns:
        rev_clean = rev_df.copy()
        
        if "revenue_year_growth_rate" not in rev_clean.columns:
            rev_clean["revenue_year_growth_rate"] = 0.0
            
        rev_clean["revenue"] = pd.to_numeric(rev_clean["revenue"].astype(str).str.replace(",", ""), errors="coerce")
        rev_clean["revenue_year_growth_rate"] = pd.to_numeric(rev_clean["revenue_year_growth_rate"].astype(str).str.replace("%", ""), errors="coerce")
        
        rev_clean = rev_clean.dropna(subset=["revenue_year_growth_rate", "revenue"])
        rev_clean = rev_clean[rev_clean["revenue"] > 0].sort_values("date")
        
        if not rev_clean.empty:
            rev_last_row = rev_clean.iloc[-1]
            latest_yoy = float(rev_last_row["revenue_year_growth_rate"])
            latest_rev_month = str(rev_last_row.get("date", "未知月份"))
            latest_rev_value = float(rev_last_row["revenue"]) / 100000000.0
            has_revenue_data = True

    fin_df = get_financial_statement_df(stock_id, years=2)
    eps_now, eps_prev, gpm_now, gpm_prev, opm_now, opm_prev = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    fin_conclusion = "📋 該標的暫無足夠季度財報歷史數據（非個股或部分特殊標的）。"
    has_financial_data = False
    
    if not fin_df.empty and "Revenue" in fin_df.columns and "EPS" in fin_df.columns:
        fin_df = fin_df.sort_values("date").reset_index(drop=True)
        for idx in range(len(fin_df)):
            rev_amt = safe_float(fin_df.loc[idx, "Revenue"])
            fin_df.loc[idx, "gpm"] = (safe_float(fin_df.loc[idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df.loc[idx, "opm"] = (safe_float(fin_df.loc[idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        
        latest_fin = fin_df.iloc[-1]
        eps_now, gpm_now, opm_now = safe_float(latest_fin.get("EPS", 0.0)), safe_float(latest_fin.get("gpm", 0.0)), safe_float(latest_fin.get("opm", 0.0))
        has_financial_data = True
        if len(fin_df) >= 2:
            prev_fin = fin_df.iloc[-2]
            eps_prev, gpm_prev, opm_prev = safe_float(prev_fin.get("EPS", 0.0)), safe_float(prev_fin.get("gpm", 0.0)), safe_float(prev_fin.get("opm", 0.0))
            gpm_text = "進步" if gpm_now > gpm_prev else "退步" if gpm_now < gpm_prev else "持平"
            opm_text = "進步" if opm_now > opm_prev else "退步" if opm_now < opm_prev else "持平"
            eps_lbl = "多賺" if eps_now > eps_prev else "少賺" if eps_now < eps_prev else "持平"
            if gpm_now > gpm_prev and opm_now > opm_prev and eps_now > eps_prev:
                fin_conclusion = f"📈 **【財報結構升級】** 最新三大獲利指標全數超越上一季！本業獲利體質強健。"
            elif gpm_now < gpm_prev and opm_now < opm_prev and eps_now < eps_prev:
                fin_conclusion = f"📉 **【獲利能力全面退步】** 毛利、營益率、EPS 同步倒退，提防題材虛火。"
            else:
                fin_conclusion = f"⚖️ **【橫盤調整期】** 結構互有勝負：毛利『{gpm_text}』、營益率『{opm_text}』、EPS『{eps_lbl}』。"

    # ============ 2. 全新設計的消息面焦點分析引擎 ============
    news_summary, news_color = "🟡 中性消息", "gray"
    news_analysis_report = "⚪ 暫無最新重要輿情分析資訊。"
    news_raw_list = []
    
    if fetch_news:
        news_df = get_realtime_news_df(stock_id, stock_name)
        if not news_df.empty and "title" in news_df.columns:
            news_raw_list = news_df.head(5).to_dict('records') 
            pos_cnt, neg_cnt, neu_cnt = 0, 0, 0
            keywords_found = []
            core_tags = {'創新高': '🎯 歷史新高', '雙率雙升': '💰 獲利結構升級', '大賺': '🔥 暴利發酵', '利多': '📣 多頭題材', '衰退': '🚨 動能失速', '虧損': '❌ 營運赤字', '利空': '⚠️ 消息利空'}
            
            for title in news_df["title"].head(10).tolist():
                lbl, _ = analyze_news_sentiment(title)
                if "利多" in lbl: pos_cnt += 1
                elif "利空" in lbl: neg_cnt += 1
                else: neu_cnt += 1
                for k, v in core_tags.items():
                    if k in title and v not in keywords_found: keywords_found.append(v)
            
            total_scanned = pos_cnt + neg_cnt + neu_cnt
            if pos_cnt > neg_cnt and pos_cnt >= 2:
                news_summary, news_color = "🟢 即時輿情偏多", "green"
                news_analysis_report = f"🔥 **【輿情熱度上升：市場買單積極】** 掃描近期重要消息，**利多消息佔比達 {pos_cnt/total_scanned*100:.0f}%**。焦點圍繞在 {', '.join(keywords_found) if keywords_found else '波段營運成長題材'} 上，市場追價意願高。"
            elif neg_cnt > pos_cnt and neg_cnt >= 2:
                news_summary, news_color = "🔴 即時輿情偏空", "red"
                news_analysis_report = f"🚨 **【輿情警報：利空連環發酵】** 掃描近期重要消息，**利空消息佔比高達 {neg_cnt/total_scanned*100:.0f}%**。焦點透露出 {', '.join(keywords_found) if keywords_found else '營運基本面修正'} 的隱憂。切勿盲目進場接刀！"
            else:
                news_summary, news_color = "🟡 輿情結構中性", "gray"
                news_analysis_report = f"⚖️ **【消息平淡：缺乏新故事刺激】** 近期多空雜音交錯（利多 {pos_cnt} 則、利空 {neg_cnt} 則），盤面主要受大盤或是內資技術面籌碼拉鋸影響。"
            
            # 防禦系統
            if current_price >= real_resistance * 0.98 and neg_cnt >= 3:
                news_analysis_report += " ⚠️ **【主力高檔反向防禦警示】**：注意！現價面臨前高重壓但背後利空頻傳，可能為主力測試浮額或洗盤，嚴格控管停損。"
            elif current_price >= real_resistance * 0.98 and pos_cnt >= 4:
                news_analysis_report += " ⚠️ **【高檔過熱警示】**：注意！股價達相對高點且市場瘋狂看好（利多爆量），小心主力藉由利多誘多出貨，千萬不可再重倉追高！"
    else:
        news_summary, news_color = "⚪ 批次雷達略過新聞", "gray"

    # ============ 3. 技術動能定論判定 ============
    tech_conclusion_short = "中性觀望"
    tech_conclusion_long = "⚖️ 擺動指標目前處於中性橫盤區，大資金尚未表態，短線缺乏爆發性動能。"

    if current_price >= real_resistance * 0.995 and vol_spike and is_compressed:
        tech_conclusion_short = "🚀 準備起漲"
        tech_conclusion_long = f"🚀 **【完美風暴！爆發起漲點】** 盤中強勢挑戰前高壓力（{real_resistance:.2f} 元），且**爆發主力攻擊量**！通道歷經充分盤整洗盤。"
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

    # ============ 4. 雙軌精準診斷加權引擎 ============
    f_score, i_score, t_score = 0, 0, 0
    diag_fundamentals, diag_chips, diag_technicals = [], [], []
    is_turnaround_dark_horse = False
    turnaround_reason = ""

    if is_compressed and vol_spike and current_price >= real_resistance * 0.99:
        eps_turn_around = (eps_now > 0) and (eps_prev <= 0) if has_financial_data else False
        revenue_rocket = latest_yoy >= 40.0 if has_revenue_data else False
        if eps_turn_around or revenue_rocket:
            is_turnaround_dark_horse = True
            if eps_turn_around: turnaround_reason = f"🔮 **【大轉折：轉虧為盈】** 過去虧損，最新一季 EPS 成功扭虧為盈（{eps_prev:.
