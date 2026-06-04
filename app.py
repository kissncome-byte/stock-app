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
from streamlit.runtime.scriptrunner import add_script_run_ctx

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
    
    if pos_score > neg_score: return "🟢 利多", "green"
    elif neg_score > pos_score: return "🔴 利空", "red"
    return "🟡 中性", "gray"


# ============ 4. Auth, API Initialization & Shared Connection ============
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

@st.cache_resource
def get_requests_session():
    """全域共用的 Session，避免頻繁重建連線提升併發效率"""
    session = requests.Session()
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

    # 1. 嘗試從台灣證交所 API 獲取盤中即時數據
    try:
        session.get("https://mis.twse.com.tw/stock/index.jsp", timeout=2, verify=certifi.where())
        ts = int(time.time() * 1000)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
        r = session.get(url, timeout=2, verify=certifi.where())

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

    # 2. 若證交所失敗，改用備份方案 Yahoo Finance
    if not rt_success:
        try:
            yahoo_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
            for suffix in [".TW", ".TWO"]:
                url = f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}"
                r = requests.get(url, headers=yahoo_headers, timeout=2, verify=certifi.where())
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
        # 獨立建立請求，不共用大盤 Session 防止特徵被阻擋
        query = f"{stock_id} {stock_name}"
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(url, headers=headers, timeout=5)
        
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

    # 動態大象股判定門檻 20 億
    recent_amount_ma = df["amount"].tail(20).mean()
    is_heavyweight = recent_amount_ma > 2000000000  
    
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

    # 籌碼防禦性檢查
    inst_df = get_inst_df(stock_id, days=20)
    inst_3d_sum = 0.0
    if not inst_df.empty and "buy" in inst_df.columns and "sell" in inst_df.columns and "date" in inst_df.columns:
        inst_df = inst_df.copy()
        inst_df["net_sheets"] = pd.to_numeric(inst_df["buy"], errors="coerce").fillna(0) - pd.to_numeric(inst_df["sell"], errors="coerce").fillna(0)
        inst_daily = inst_df.groupby("date")["net_sheets"].sum().reset_index().sort_values("date")
        if not inst_daily.empty:
            inst_3d_sum = float(inst_daily.tail(3)["net_sheets"].sum())

    # 營收安全獲取
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

    # 財報季度體檢
    fin_df = get_financial_statement_df(stock_id, years=2)
    eps_now, eps_prev, gpm_now, gpm_prev, opm_now, opm_prev = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    fin_conclusion = "📋 該標的暫無足夠季度財報歷史數據（非個股或部分特殊標的）。"
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
        opm_now = safe_float(latest_fin.get("opm", 0.0))
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

    # ============ 全新設計的消息面焦點分析引擎 ============
    news_summary, news_color = "🟡 中性消息", "gray"
    news_analysis_report = "⚪ 暫無最新重要輿情分析資訊。"
    news_raw_list = []
    
    if fetch_news:
        news_df = get_realtime_news_df(stock_id, stock_name)
        if not news_df.empty and "title" in news_df.columns:
            news_raw_list = news_df.tail(5)[::-1].to_dict('records') # 拿最新的 5 則並翻轉排序
            pos_cnt, neg_cnt, neu_cnt = 0, 0, 0
            keywords_found = []
            
            # 建立多空關鍵字庫，並淬煉出焦點標籤
            core_tags = {'創新高': '🎯 歷史新高', '雙率雙升': '💰 獲利結構升級', '大賺': '🔥 暴利發酵', '利多': '📣 多頭題材', '衰退': '🚨 動能失速', '虧損': '❌ 營運赤字', '利空': '⚠️ 消息利空'}
            
            for title in news_df["title"].tail(10).tolist():
                lbl, _ = analyze_news_sentiment(title)
                if "利多" in lbl: pos_cnt += 1
                elif "利空" in lbl: neg_cnt += 1
                else: neu_cnt += 1
                
                for k, v in core_tags.items():
                    if k in title and v not in keywords_found: keywords_found.append(v)
            
            # 決定輿情總定論
            total_scanned = pos_cnt + neg_cnt + neu_cnt
            if pos_cnt > neg_cnt and pos_cnt >= 2:
                news_summary, news_color = "🟢 即時輿情偏多", "green"
                news_analysis_report = f"🔥 **【輿情熱度上升：市場買單積極】** 掃描近期的重要消息面，**利多消息佔比達 {pos_cnt/total_scanned*100:.0f}%**。焦點主要圍繞在 {', '.join(keywords_found) if keywords_found else '波段營運成長題材'} 上，市場追價意願與認同度高。"
            elif neg_cnt > pos_cnt and neg_cnt >= 2:
                news_summary, news_color = "🔴 即時輿情偏空", "red"
                news_analysis_report = f"🚨 **【輿情警報：利空連環發酵】** 掃描近期的重要消息面，**利空消息佔比高達 {neg_cnt/total_scanned*100:.0f}%**。焦點透露出 {', '.join(keywords_found) if keywords_found else '營運基本面修正'} 的隱憂。此時切勿盲目進場接刀，提防消息面引發的主力連環多殺多！"
            else:
                news_summary, news_color = "🟡 輿情結構中性", "gray"
                news_analysis_report = f"⚖️ **【消息平淡：缺乏新故事刺激】** 掃描近期的重要消息面，多空雜音交錯（利多 {pos_cnt} 則、利空 {neg_cnt} 則），無明顯單邊趨勢，盤面主要受大盤或是內資技術面籌碼拉鋸影響。"
            
            # 矛盾分析防禦系統 (如果股價創20日新高但是輿情偏空，或者股價創新低但輿情偏多)
            if current_price >= real_resistance * 0.98 and neg_cnt >= 3:
                news_analysis_report += " ⚠️ **【主力高檔反向防禦警示】**：注意！當前股價正面臨前高重壓，但背後利空新聞頻傳，可能為主力在測試浮額，或利空不跌的洗盤跡象，請嚴格控管停損。"
            elif current_price >= real_resistance * 0.98 and pos_cnt >= 4:
                news_analysis_report += " ⚠️ **【高檔過熱警示】**：注意！股價已達相對高點，且市場新聞全面一致瘋狂看好（利多爆量），小心主力藉由利多誘多出貨給散戶，千萬不可再重倉追高！"
    else:
        news_summary, news_color = "⚪ 批次雷達略過新聞", "gray"

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

    # ============ 雙軌精準診斷加權引擎 ============
    f_score, i_score, t_score = 0, 0, 0
    diag_fundamentals, diag_chips, diag_technicals = [], [], []
    is_turnaround_dark_horse = False
    turnaround_reason = ""

    if is_compressed and vol_spike and current_price >= real_resistance * 0.99:
        eps_turn_around = (eps_now > 0) and (eps_prev <= 0) if has_financial_data else False
        revenue_rocket = latest_yoy >= 40.0 if has_revenue_data else False
        if eps_turn_around or revenue_rocket:
            is_turnaround_dark_horse = True
            if eps_turn_around:
                turnaround_reason = f"🔮 **【大轉折：單季轉虧為盈！】** 過去大虧損，最新一季 EPS 成功扭虧為盈（{eps_prev:.2f} ➔ {eps_now:.2f}），具備巨大想像空間！"
            else:
                turnaround_reason = f"🔮 **【大轉折：營收懸崖式暴增！】** 最新月營收 YoY 拔地而起高達 {latest_yoy:.1f}%，新產能或新訂單大灌頂！"

    # 基本面評分
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
        diag_technicals.append("🟢 **貼近黃金防守區**：現價距離生命線(MA20)極近，下檔有鐵板支撐。")

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

    # 交易藍圖精算
    brk_setup = (current_price >= real_resistance * 0.98) and (rsi_now < 73)
    pb_setup = (current_price < real_resistance) and (current_price >= ma20_val * 0.98)

    target_brk = round_to_tick(current_price + (target_atr_ratio * atr), t)
    stop_brk = round_to_tick(real_resistance - (1.5 * atr) - slip, t)
    if stop_brk >= current_price: stop_brk = round_to_tick(current_price - (1.0 * atr), t)
    rr1_brk = (target_brk - current_price) / (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0

    target_pb = round_to_tick(real_resistance, t)
    stop_pb = round_to_tick(current_price - atr - slip, t)
    rr1_pb = (target_pb - current_price) / (current_price - stop_pb) if (current_price - stop_pb) > 0 else 0

    pool_allocation = total_capital * 0.8 if is_heavyweight else total_capital * 0.2
    risk_money = pool_allocation * (risk_per_trade / 100) * 10000 
    
    loss_per_share_brk = (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0.01
    suggested_lots_brk = int((risk_money / loss_per_share_brk) / 1000) if loss_per_share_brk > 0 else 0
    if is_turnaround_dark_horse: suggested_lots_brk = int(suggested_lots_brk * 0.5) 
    
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
        "news_summary": news_summary, "news_color": news_color, "news_analysis_report": news_analysis_report, "news_raw_list": news_raw_list,
        "is_heavyweight": is_heavyweight, "has_financial_data": has_financial_data
    }


# ============ 7. 全域基礎資料初始化 ============
info_df = get_stock_info_df()
if not info_df.empty and "industry_category" in info_df.columns:
    all_industries = sorted(info_df["industry_category"].dropna().unique().tolist())
else:
    all_industries = ["半導體業", "電子零組件業", "光電業", "金融保險", "電腦及週邊設備業", "航運業"]


# ============ 8. Streamlit UI Layer ============
st.sidebar.header("⚙️ 核心-衛星雙軌風控系統")
total_cap = st.sidebar.number_input("總資產配置本金 (萬元)", value=100.0)
risk_pct = st.sidebar.slider("單筆最大可承受風險 (%)", 0.5, 3.0, 1.0)

core_cap_pool = total_cap * 0.8
sat_cap_pool = total_cap * 0.2

st.sidebar.markdown(f"""
---
### 📊 資金池動態配額
* 🏛️ **核心權值大象池(80%)：** `{core_cap_pool:.1f}` 萬元
  > 💡 **核心大象：** 日均成交額大於 20 億的超級龍頭股，流動性極佳、走勢穩健。適合佈局大資金，進出主看月線 (MA20) 防守與法人籌碼動向。
* 🚀 **衛星突擊飆股池(20%)：** `{sat_cap_pool:.1f}` 萬元
  > 💡 **衛星黑馬：** 中高波動的中小型股，籌碼易被主力鎖定。爆發力強但也伴隨高風險，適合快進快出，破線須嚴格停損不留戀。
* 🛡️ **每筆最大損失金額：** `{total_cap * (risk_pct / 100) * 10000:.0f}` 元
""")

if st.sidebar.button("🧹 清除系統快取數據"):
    st.cache_data.clear()
    st.toast("系統快取已全數清理，下次查詢將同步最新實時市場因子！")

tab1, tab2 = st.tabs(["🔍 核心個股「值不值投入」深度診斷", "🛸 大盤全自動多執行緒雷達"])

with tab1:
    target_stock = st.text_input("請輸入要進行分析的股票代碼（支援個股、ETF與大盤類別防禦診斷）", value="2330").strip()
    if st.button("開始個股雷達全因子診斷"):
        with st.spinner("多因子數據縱深交叉分析中..."):
            res = evaluate_stock(target_stock, total_cap, risk_pct, 1, fetch_news=True)
            
            if res:
                res["style"] = detect_style(res)
                
                # 1. 頂層：雙軌操盤性格卡
                if res['is_heavyweight']:
                    st.markdown(f"""
                    <div style="background-color:#E3F2FD; padding:18px; border-radius:12px; border-left:8px solid #1E88E5; margin-bottom:15px;">
                        <h3 style="color:#0D47A1; margin:0; font-size:20px;">🏛️ 【 核心大象劇本：航空母艦穩健戰 】</h3>
                        <p style="color:#1565C0; font-size:14px; margin-top:6px; margin-bottom:0;">
                            <b>現正監控：{res['stock_name']} ({res['stock_id']}) · {res['industry']}</b><br>
                            指標特性：此標的屬於市場超級巨獸（成交額大），流動性極大。進場莫著急，重點看月線(MA20)防守與法人金流臉色！
                        </p>
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    st.markdown(f"""
                    <div style="background-color:#F3E5F5; padding:18px; border-radius:12px; border-left:8px solid #8E24AA; margin-bottom:15px;">
                        <h3 style="color:#4A148C; margin:0; font-size:20px;">⚡ 【 衛星黑馬劇本：戰鬥機閃擊戰 】</h3>
                        <p style="color:#6A1B9A; font-size:14px; margin-top:6px; margin-bottom:0;">
                            <b>現正監控：{res['stock_name']} ({res['stock_id']}) · {res['industry']}</b><br>
                            指標特性：此標的屬於中高波動中小型股，主力與分點籌碼容易高度鎖碼。戰術是快進快出，不對就跑，跌破停損絕不留戀！
                        </p>
                    </div>
                    """, unsafe_allow_html=True)

                if res["invest_status"] == "🔮 特戰訊號：轉折爆發黑馬股":
                    st.balloons()
                
                # 2. 中層：三力多空量化紅綠燈矩陣
                st.markdown("#### 🚦 三力多空量化紅綠燈矩陣")
                matrix_col1, matrix_col2, matrix_col3, matrix_col4 = st.columns(4)

                f_light = res["diag_fundamentals"][0] if res["diag_fundamentals"] else "⚪ 數據不足"
                c_light = res["diag_chips"][0] if res["diag_chips"] else "⚪ 數據不足"
                t_light = res["diag_technicals"][0] if res["diag_technicals"] else "⚪ 數據不足"

                matrix_col1.metric("1. 戰術分流歸屬", "🏛️ 核心大象" if res['is_heavyweight'] else "🚀 衛星黑馬")
                matrix_col2.markdown(f"**2. 🏢 基本面燃料**\n\n{f_light}")
                matrix_col3.markdown(f"**3. 👥 籌碼面大人**\n\n{c_light}")
                matrix_col4.markdown(f"**4. 📈 技術面時機**\n\n{t_light}")

                # 3. 下層：一針見血矛盾定論
                if res['status_color'] == "red" and res['rr1_brk'] >= 1.5:
                    st.markdown(f"""
                    <div style="background-color:#FFEBEE; padding:15px; border-radius:8px; border-left:5px solid #F44336; margin-top:10px; margin-bottom:15px;">
                        <span style="color:#B71C1C; font-weight:bold; font-size:16px;">🚨 【操盤手一針見血定論：空間極美，但勝率極低！】</span><br>
                        <span style="color:#B71C1C; font-size:14px;">
                            雖然技術線型走到了突破前高點，計算出的<b>風報比高達 {res['rr1_brk']:.2f} 倍</b>，但是此時<b>基本面核心燃料或籌碼面大人正在大舉外逃</b>。這極有可能是誘多的<b>假突破陷阱</b>。請直接「放棄」。
                        </span>
                    </div>
                    """, unsafe_allow_html=True)
                elif res['status_color'] == "green":
                    st.markdown(f"""
                    <div style="background-color:#E8F5E9; padding:15px; border-radius:8px; border-left:5px solid #4CAF50; margin-top:10px; margin-bottom:15px;">
                        <span style="color:#1B5E20; font-weight:bold; font-size:16px;">🚀 【操盤手一針見血定論：黃金訊號，方向與空間完美共振！】</span><br>
                        <span style="color:#1B5E20; font-size:14px;">
                            核心體質強健，技術面同時具備極佳的風報比防守優勢。這是具備基本面護城河支持的<b>真金白銀波段發動點</b>，請堅決執行下方計劃。
                        </span>
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    st.markdown(f"""> ⚖️ **【操盤手一針見血定論】**：{res['tech_conclusion_long']}""")

                st.markdown("---")
                
                # 4. 個股基本交易資料面板
                st.subheader("🏢 盤中即時交易數據面板")
                col_m1, col_m2, col_m3, col_m4 = st.columns(4)
                col_m1.metric("即時現價", f"{res['current_price']} 元", f"核心決策: {res['invest_status']}")
                col_m2.metric("盤中即時量 / 20日均量", f"{res['current_vol']:.0f} 張", f"均量: {res['vol_ma20']:.0f} 張")
                col_m3.metric(f"最新公告月營收 ({res['latest_rev_month']})", f"{res['latest_revenue_yoy']:.2f} %", f"單月營收: {res['latest_rev_value']:.2f} 億")
                col_m4.metric("即時新聞輿情定論", res["news_summary"])
                
                # ============ 全新設計：AI 核心輿情重點診斷艙 ============
                st.markdown("### 📰 AI 核心輿情重點診斷艙")
                st.info(res["news_analysis_report"])
                
                if res["news_raw_list"]:
                    st.markdown("#### 🔍 焦點穿透式消息日誌")
                    for news in res["news_raw_list"]:
                        lbl_tag, lbl_color = analyze_news_sentiment(news["title"])
                        # 使用 HTML 呈現漂亮的新聞條目與連結
                        st.markdown(f"""
                        <div style="padding: 10px; margin-bottom: 8px; border-radius: 6px; background-color: #FAFAFA; border-left: 4px solid {lbl_color};">
                            <span style="font-size:12px; color:gray;">[{news['date'][:16]}] ({news['source']})</span> — <b>{lbl_tag}</b><br>
                            <a href="{news['link']}" target="_blank" style="text-decoration: none; color: #1E88E5; font-size:14px;">{news['title']} 🔗</a>
                        </div>
                        """, unsafe_allow_html=True)
                # =======================================================

                # 5. 實質籌碼面與季度財報數據面板
                st.subheader("👥 實質籌碼動能與季度財報體檢 (核心數值)")
                col_d1, col_d2, col_d3, col_d4 = st.columns(4)
                
                chip_trend = "🔺 法人吸籌" if res['inst_3d_sheets'] > 0 else "🔻 法人拋售" if res['inst_3d_sheets'] < 0 else "➖ 持平"
                col_d1.metric("近 3 日三大法人合計買賣超", f"{res['inst_3d_sheets']:.0f} 張", chip_trend)
                
                def get_trend_tag(now, prev): return "🔺 進步" if now > prev else "🔻 退步" if now < prev else "➖ 持平"
                
                if res['has_financial_data']:
                    col_d2.metric("最新每股盈餘 (EPS)", f"{res['eps_now']:.2f} 元", f"前季: {res['eps_prev']:.2f} | {get_trend_tag(res['eps_now'], res['eps_prev'])}")
                    col_d3.metric("最新營業毛利率 (GPM)", f"{res['gpm_now']:.2f} %", f"前季: {res['gpm_prev']:.2f} | {get_trend_tag(res['gpm_now'], res['gpm_prev'])}")
                    col_d4.metric("最新營業利益率 (OPM)", f"{res['opm_now']:.2f} %", f"前季: {res['opm_prev']:.2f} | {get_trend_tag(res['opm_now'], res['opm_prev'])}")
                else:
                    col_d2.metric("最新每股盈餘 (EPS)", "不適用", "非個股商品")
                    col_d3.metric("最新營業毛利率 (GPM)", "不適用", "非個股商品")
                    col_d4.metric("最新營業利益率 (OPM)", "不適用", "非個股商品")
                
                st.success(res["fin_conclusion"])

                with st.expander("🔍 點開查看全因子完整白話文深度診斷報告", expanded=False):
                    c_f, c_i, c_t = st.columns(3)
                    with c_f:
                        st.markdown("#### 🏢 基本面深度診斷")
                        for item in res["diag_fundamentals"]: st.markdown(item)
                    with c_i:
                        st.markdown("#### 👥 籌碼面大戶追蹤")
                        for item in res["diag_chips"]: st.markdown(item)
                    with c_t:
                        st.markdown("#### 📈 技術面時機精選")
                        for item in res["diag_technicals"]: st.markdown(item)

                st.markdown("---")
                st.subheader("🎯 精算交易藍圖與風控部位張數建議")
                box_brk, box_pb = st.columns(2)
                with box_brk:
                    st.markdown("### 🏃‍♂️ 【突破/起漲追擊策略】藍圖")
                    st.markdown(f"* **建議突破進場點：** `{res['current_price']}` 元")
                    st.markdown(f"* **預估停利目標價：** `{res['target_brk']}` 元")
                    st.markdown(f"* **防守撤退停損點：** `{res['stop_brk']}` 元")
                    if res['status_color'] == "red":
                        st.markdown(f"* **💡 系統建議購買張數：** <span style='color:gray; font-size:18px; font-weight:bold;'>0</span> 張 (因核心決策結構崩壞，策略強制關閉)", unsafe_allow_html=True)
                    else:
                        st.markdown(f"* **💡 系統建議購買張數：** <span style='color:red; font-size:18px; font-weight:bold;'>{res['suggested_lots_brk']}</span> 張", unsafe_allow_html=True)
                    
                    if res['rr1_brk'] < 1.5: st.error(f"❌ 當前風報比: {res['rr1_brk']:.2f} (利潤空間不足/不符期望值)")
                    else: st.success(f"🚀 當前風報比: {res['rr1_brk']:.2f} (🟢 符合進攻點幾何空間)")
                with box_pb:
                    st.markdown("### 🛡️ 【均線拉回低吸策略】藍圖")
                    st.markdown(f"* **理想買入點：** `{res['current_price']}` 元")
                    st.markdown(f"* **短線停利價：** `{res['target_pb']}` 元")
                    st.markdown(f"* **破位防守停損點：** `{res['stop_pb']}` 元")
                    if res['status_color'] == "red":
                        st.markdown(f"* **💡 系統建議購買張數：** <span style='color:gray; font-size:18px; font-weight:bold;'>0</span> 張 (因核心決策結構崩壞，策略強制關閉)", unsafe_allow_html=True)
                    else:
                        st.markdown(f"* **💡 系統建議購買張數：** <span style='color:green; font-size:18px; font-weight:bold;'>{res['suggested_lots_pb']}</span> 張", unsafe_allow_html=True)
                    
                    if res['rr1_pb'] < 2.0: st.error(f"❌ 當前風報比: {res['rr1_pb']:.2f} (拉回空間過窄)")
                    else: st.success(f"🚀 當前風報比: {res['rr1_pb']:.2f} (🟢 理想低吸性價比點位)")

                st.subheader("📊 盤中核心量化基礎指標")
                tech_col1, tech_col2, tech_col3, tech_col4, tech_col5 = st.columns(5)
                tech_col1.metric("MACD 柱狀體 (Hist)", f"{res['macd_hist']:.3f}", "🔺 多頭擴張" if res['macd_hist'] > 0 else "🔻 空頭修正")
                tech_col2.metric("RSI(14) 強弱度(標準平滑)", f"{res['rsi_now']:.2f}", "超買 >70 | 超賣 <30")
                tech_col3.metric("20日均線支撐 (MA20)", f"{res['pivot']:.1f} 元", f"20D前高壓力: {res['real_resistance']:.1f}元")
                tech_col4.metric("DMI 趨勢動能 (ADX)", f"{res['adx_now']:.1f}", "壓縮鎖碼" if res['is_compressed'] else "常態發散")
                tech_col5.metric("14日真實波動度 (ATR)", f"{res['atr']:.2f} 元", f"開火爆量: {'是' if res['vol_spike'] else '否'}")

            else:
                st.error("找不到該代碼的市場數據，請確認是否輸入錯誤（如為ETF或指數，部分欄位將相容性降維診斷）。")

with tab2:
    st.subheader("🛸 大盤核心權值板塊高速並發自動掃描雷達")
    
    scan_scope = st.radio(
        "🎯 請選擇雷達自動掃描範圍：",
        ["🔥 核心成交權值焦點股池（日成交金額前15大龍頭）", "🏭 依特定產業類股全自動橫橫掃"],
        key="bulk_scan_scope"
    )
    
    selected_ind = None
    if scan_scope == "🏭 依特定產業類股全自動橫橫掃":
        selected_ind = st.selectbox("選擇要攻擊的產業板塊：", all_industries, index=all_industries.index("半導體業") if "半導體業" in all_industries else 0)

    if st.button("🚀 啟動高速並發雷達大盤掃描"):
        if scan_scope == "🔥 核心成交權值焦點股池（日成交金額前15大龍頭）":
            stock_list = ["2330", "2317", "2454", "2382", "3711", "2308", "2357", "2881", "2882", "2603", "2609", "3231", "2449", "3017", "3037"]
        else:
            stock_list = info_df[info_df["industry_category"] == selected_ind]["stock_id"].tolist()
            if len(stock_list) > 40: stock_list = stock_list[:40]
            
        all_results = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        def worker(sid):
            try: return evaluate_stock(sid, total_cap, risk_pct, 1, fetch_news=False)
            except Exception: return None

        status_text.text(f"⚡ 高速並發線程池啟動，正在處理 {len(stock_list)} 檔標的量化矩陣...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_sid = {}
            for sid in stock_list:
                future = executor.submit(worker, sid)
                add_script_run_ctx(future)
                future_to_sid[future] = sid
                
            for idx, future in enumerate(concurrent.futures.as_completed(future_to_sid)):
                res = future.result()
                if res:
                    all_results.append({
                        "代碼": res["stock_id"], "名稱": res["stock_name"], "現價": res["current_price"],
                        "戰術池": "🏛️ 核心大象" if res["is_heavyweight"] else "🚀 衛星黑馬",
                        "核心決策定論": res["invest_status"], "技術動能狀態": res["tech_conclusion_short"],
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
            
            tradeable_only = st.checkbox("🎯 只篩選顯示目前具有實質安全邊際與優勢買點（極品/黑馬/風報比及格）的黃金個股")
            if tradeable_only:
                filtered_df = scan_df[
                    (scan_df["核心決策定論"].str.contains("極品")) | 
                    (scan_df["核心決策定論"].str.contains("黑馬")) | 
                    (scan_df["突破型建議"] == "🟢 值得進攻") | 
                    (scan_df["拉回型建議"] == "🟢 值得低吸")
                ]
                if not filtered_df.empty:
                    st.dataframe(filtered_df.sort_values(by=["突破風報比"], ascending=False), use_container_width=True, hide_index=True)
                else:
                    st.warning("😭 當前大盤環境因子掃描完畢，目前暫無任何符合標準的極品訊號標的。")
            else:
                st.dataframe(scan_df, use_container_width=True, hide_index=True)
        else:
            st.error("未能成功讀取 any 標的數據，請檢查 FinMind 連線狀態。")
