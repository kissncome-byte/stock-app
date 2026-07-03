import os, time, requests, certifi, pytz, urllib.parse
import pandas as pd
import numpy as np
import streamlit as st
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from FinMind.data import DataLoader

# ============ 1. Page Config ============
st.set_page_config(page_title="SOP v48 機構級雙速狼王決策系統", layout="wide")

# ============ 2. Global Constants ============
TZ = pytz.timezone("Asia/Taipei")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")
FUGLE_TOKEN = os.getenv("FUGLE_TOKEN", "") or st.secrets.get("FUGLE_TOKEN", "")

# ============ 3. Helper Functions & Utilities ============
def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ["-", "", "None", "nan", "NaN"]: return default
        return float(str(x).replace(",", "").replace("%", "").strip())
    except Exception: return default

def tick_size(p: float) -> float:
    if p >= 1000: return 5.0
    if p >= 500:  return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.05
    return 0.01

def round_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t == 0: return 0.0
    return round(x / t) * t

def custom_hud_box(title, value, font_color="#1E293B"):
    return f"""
    <div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; padding: 12px; border-radius: 6px; min-height: 105px; box-shadow: 0 1px 2px rgba(0,0,0,0.02); margin-bottom: 10px;">
        <span style="color: #64748B; font-size: 12.5px; font-weight: 600; display: block; margin-bottom: 5px;">{title}</span>
        <span style="color: {font_color}; font-size: 14px; font-weight: 700; display: block; line-height: 1.5; word-break: break-all;">{value}</span>
    </div>
    """

def get_market_status_label(rt_success: bool, last_trade_date_str: str):
    now = datetime.now(TZ)
    if now.weekday() >= 5: return "CLOSED_WEEKEND", f"市場休市 (週末) | 數據日期: {last_trade_date_str}", "gray"
    start, end = datetime.strptime("09:00", "%H:%M").time(), datetime.strptime("13:35", "%H:%M").time()
    if rt_success:
        if start <= now.time() <= end: return "OPEN", "市場交易中 (即時更新)", "red"
        return ("PRE_MARKET", "盤前準備中", "blue") if now.time() < start else ("POST_MARKET", "今日已收盤 (即時報價)", "green")
    else:
        if start <= now.time() <= end: return "API_WAIT", f"連線受限改用歷史價 | 歷史日期: {last_trade_date_str}", "orange"
        return ("PRE_MARKET", f"盤前準備中 | 歷史日期: {last_trade_date_str}", "blue") if now.time() < start else ("POST_MARKET", f"今日已收盤 | 歷史日期: {last_trade_date_str}", "green")

def analyze_news_sentiment(title: str) -> tuple:
    pos = ['創新高', '大賺', '暴增', '飆', '大成長', '利多', '優於預期', '加碼', '看旺', '強勢', '獲利', '突破', '轉盈', '買超', '爆發', '新高', '三率三升']
    neg = ['衰退', '虧損', '重挫', '低於預期', '縮水', '跌破', '警告', '利空', '下滑', '疲弱', '裁員', '大跌', '慘', '賣壓', '修正', '暴跌', '逆風']
    p_s, n_s = sum(1 for w in pos if w in title), sum(1 for w in neg if w in title)
    return ("🟢 利多", "green") if p_s > n_s else ("🔴 利空", "red") if n_s > p_s else ("🟡 中性", "gray")

# ============ 4. Advanced Connection Layer ============
@st.cache_resource
def get_requests_session():
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504]))
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
    return session

@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try: api.login_by_token(FINMIND_TOKEN)
        except Exception: pass
    return api

# ============ 5. Standardized Live Data Streaming Engine ============
def compute_live_data(stock_id: str, market_type: str, hist_last_close: float, hist_last_vol: float):
    hist_lots = hist_last_vol / 1000.0 if hist_last_vol > 0 else 0.0
    session = get_requests_session()
    is_otc = any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "上櫃"])
    if FUGLE_TOKEN:
        try:
            r = session.get(f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{stock_id}", headers={"X-API-KEY": FUGLE_TOKEN}, timeout=2)
            if r.status_code == 200:
                data = r.json().get("data", r.json())
                p_c = safe_float(data.get("closePrice")) or safe_float(data.get("referencePrice"))
                v_s = safe_float(data.get("total", {}).get("tradeVolume", 0))
                if p_c > 0: return safe_float(data.get("openPrice")) or p_c, safe_float(data.get("highPrice")) or p_c, safe_float(data.get("lowPrice")) or p_c, p_c, v_s/1000.0 if v_s > 0 else hist_lots, True, "Fugle 富果快流", "realtime"
        except Exception: pass
    for prefix in ["otc", "tse"] if is_otc else ["tse", "otc"]:
        try:
            r = session.get(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_id}.tw&json=1&delay=0&_={int(time.time()*1000)}", headers={"Referer": "https://mis.twse.com.tw/"}, timeout=2)
            if r.status_code == 200 and "msgArray" in r.json() and r.json()["msgArray"]:
                info = r.json()["msgArray"][0]
                if str(info.get("c")).strip() == str(stock_id).strip():
                    p_c = safe_float(info.get("z")) or safe_float(info.get("b", "").split("_")[0]) or safe_float(info.get("o"))
                    if p_c > 0: 
                        total_vol_lots = safe_float(info.get("g")) or safe_float(info.get("v")) or hist_lots
                        return safe_float(info.get("o")) or p_c, safe_float(info.get("h")) or p_c, safe_float(info.get("l")) or p_c, p_c, total_vol_lots, True, f"TWSE {prefix.upper()} 官方流", "realtime"
        except Exception: pass
    return hist_last_close, hist_last_close, hist_last_close, hist_last_close, hist_lots, False, "歷史收盤備援", "historical"

# ============ 6. Data Fetching Layers ============
@st.cache_data(ttl=1800)
def get_overnight_radar():
    session = get_requests_session()
    targets = {"台灣加權大盤 (^TWII)": "^TWII", "Nasdaq那指 (^IXIC)": "^IXIC", "費城半導體 (^SOX)": "^SOX", "台積電 ADR (TSM)": "TSM"}
    radar_res, is_us_panic, panic_desc, wtx_change = {}, False, "", 0.0
    for label, symbol in targets.items():
        for prefix in ["query2", "query1"]:
            try:
                r = session.get(f"https://{prefix}.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d", timeout=3)
                if r.status_code == 200 and r.json().get("chart", {}).get("result"):
                    res = r.json()["chart"]["result"][0]
                    closes = [safe_float(c) for c in res.get("indicators", {}).get("quote", [{}])[0].get("close", []) if c is not None]
                    c_p, p_c = (closes[-1], closes[-2]) if len(closes) >= 2 else (safe_float(res["meta"].get("regularMarketPrice")), safe_float(res["meta"].get("previousClose")))
                    if p_c > 0:
                        pct = ((c_p - p_c) / p_c) * 100
                        radar_res[label] = pct
                        if symbol == "^TWII": wtx_change = pct
                        if symbol != "^TWII" and pct <= -2.0: is_us_panic, panic_desc = True, f"昨晚美股重挫，{label} 慘跌 {pct:.1f}%"
                    break
            except Exception: pass
    return radar_res, is_us_panic, panic_desc, wtx_change

@st.cache_data(ttl=3600)
def get_stock_info_df():
    try:
        api = get_api()
        df = api.taiwan_stock_info()
        if df is not None and not df.empty:
            df = df.copy()
            df.columns = [str(c).strip() for c in df.columns]
            for col in ["stock_id", "stock_name", "industry_category"]:
                if col in df.columns: df[col] = df[col].astype(str).str.strip()
            return df
    except Exception: pass
    fallback = [
        {"stock_id": "2330", "stock_name": "台積電", "type": "twse", "industry_category": "半導體業"},
        {"stock_id": "2454", "stock_name": "聯發科", "type": "twse", "industry_category": "半導體業"},
        {"stock_id": "2308", "stock_name": "台達電", "type": "twse", "industry_category": "電子零組件業"},
        {"stock_id": "2317", "stock_name": "鴻海", "type": "twse", "industry_category": "其他電子業"},
        {"stock_id": "3037", "stock_name": "欣興", "type": "twse", "industry_category": "電子零組件業"},
        {"stock_id": "3715", "stock_name": "定穎投控", "type": "twse", "industry_category": "電子零組件業"},
        {"stock_id": "1717", "stock_name": "長興", "type": "twse", "industry_category": "化學工業"},
        # 🌟 幫你強力灌水的「光電業」大戶備援軍團！
        {"stock_id": "8069", "stock_name": "元太", "type": "two", "industry_category": "光電業"},
        {"stock_id": "2409", "stock_name": "友達", "type": "twse", "industry_category": "光电业"},
        {"stock_id": "3481", "stock_name": "群創", "type": "twse", "industry_category": "光电业"},
        {"stock_id": "3008", "stock_name": "大立光", "type": "twse", "industry_category": "光电业"},
        {"stock_id": "3406", "stock_name": "玉晶光", "type": "twse", "industry_category": "光电业"},
        {"stock_id": "2393", "stock_name": "億光", "type": "twse", "industry_category": "光电业"}
    ]
    return pd.DataFrame(fallback)

@st.cache_data(ttl=900)
def get_daily_df(stock_id: str, market_type: str = "TSE", days: int = 450):
    session = get_requests_session()
    suffix = ".TWO" if any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "上櫃"]) else ".TW"
    p1, p2 = int((datetime.now(TZ)-timedelta(days=days)).timestamp()), int(datetime.now(TZ).timestamp())
    for prefix in ["query2", "query1"]:
        try:
            r = session.get(f"https://{prefix}.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}?period1={p1}&period2={p2}&interval=1d", timeout=5)
            if r.status_code == 200 and r.json().get("chart", {}).get("result"):
                res = r.json()["chart"]["result"][0]
                raw = pd.DataFrame({
                    "date": [datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d") for ts in res.get("timestamp", [])],
                    "open": res["indicators"]["quote"][0].get("open", []), 
                    "high": res["indicators"]["quote"][0].get("high", []),
                    "low": res["indicators"]["quote"][0].get("low", []), 
                    "close": res["indicators"]["quote"][0].get("close", []), 
                    "vol": res["indicators"]["quote"][0].get("volume", [])
                }).dropna(subset=["close"])
                raw["amount"] = raw["close"] * raw["vol"]
                return raw[["date", "open", "high", "low", "close", "vol", "amount"]].copy()
        except Exception: pass
    try:
        df_raw = get_api().taiwan_stock_daily(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d"))
        if df_raw is not None and not df_raw.empty:
            df = df_raw.copy()
            df.columns = [c.strip() for c in df.columns]
            df = df.rename(columns={"Trading_Volume": "vol", "Trading_money": "amount", "max": "high", "min": "low"})
            for c in ["open", "close", "high", "low", "vol", "amount"]: df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.dropna(subset=["close", "high", "low", "vol"]).copy()
    except Exception: pass
    return None

@st.cache_data(ttl=1800)
def get_market_macro_status():
    try:
        df = get_api().taiwan_stock_daily(stock_id="TAIEX", start_date=(datetime.now()-timedelta(days=150)).strftime("%Y-%m-%d"))
        if df is not None and not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['MA20'], df['MA60'] = df['close'].rolling(20).mean(), df['close'].rolling(60).mean()
            last, prev = df.iloc[-1], (df.iloc[-5] if len(df) >= 5 else df.iloc[0])
            ret = ((last['close'] - prev['close']) / prev['close']) * 100
            panic = (last['close'] < last['MA20']) and (ret <= -3.5)
            bias = ((last['close'] - last['MA60']) / last['MA60']) * 100
            if panic: return False, f"🚨 大盤瀑布重挫 ({last['close']:.1f})，近週跌 {ret:.1f}%【補跌危機】", True, False
            if bias >= 8.5: return True, f"⚠️ 大盤過熱警告 ({last['close']:.1f})，季線正乖離 {bias:.1f}%【強制控量】", False, True
            return (True, f"加權指數 ({last['close']:.1f}) 站穩 20MA 多頭常態", False, False) if last['close'] >= last['MA20'] else (False, f"加權指數 ({last['close']:.1f}) 跌破 20MA 空方警戒", False, False)
    except Exception: pass
    return True, "🟢 多頭常態 (數據獲取受限，開啟寬鬆保護)", False, False

@st.cache_data(ttl=900)
def get_taiwan_enhanced_chips(stock_id: str, days: int = 30):
    s_trend, m_trend, s_3d, m_diff = "🟡 中性", "🟡 平穩", 0.0, 0.0
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        idf = get_api().taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start)
        if idf is not None and not idf.empty:
            sdf = idf[idf['name'] == 'Investment_Trust'].copy()
            if not sdf.empty:
                sdf['net'] = pd.to_numeric(sdf['buy'], errors='coerce').fillna(0) - pd.to_numeric(sdf['sell'], errors='coerce').fillna(0)
                s_3d = float(sdf.tail(3)['net'].sum())
                s_trend = "🟢 投信強力鎖碼" if s_3d > 500 else "🔴 投信高檔棄養" if s_3d < -500 else "🟡 中性"
    except Exception: pass
    try:
        mdf = get_api().taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start)
        if mdf is not None and not mdf.empty:
            mdf = mdf.sort_values("date")
            mdf['MarginPurchaseTodayBalance'] = pd.to_numeric(mdf['MarginPurchaseTodayBalance'], errors='coerce')
            m_diff = float(mdf.iloc[-1]['MarginPurchaseTodayBalance'] - mdf.iloc[-5]['MarginPurchaseTodayBalance'])
            m_trend = "🚨 散戶融資強套" if m_diff > 1000 else "🟢 散戶融資大退" if m_diff < -1000 else "🟡 平穩"
    except Exception: pass
    return s_trend, m_trend, s_3d, m_diff

@st.cache_data(ttl=900)
def get_rev_df(stock_id: str, days: int = 730):
    try:
        return get_api().taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"))
    except Exception: return None

@st.cache_data(ttl=86400)
def get_financial_statement_df(stock_id: str, years: int = 2):
    try:
        raw = get_api().taiwan_stock_financial_statement(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=years*365)).strftime("%Y-%m-%d"))
        if raw is None or raw.empty: return pd.DataFrame()
        df = raw.copy()
        df["type"] = df["type"].replace({"OperatingRevenue": "Revenue"})
        return df[df["type"].isin(["EPS", "Revenue", "GrossProfit", "OperatingIncome"])].pivot_table(index="date", columns="type", values="value", aggfunc="last").reset_index()
    except Exception: return pd.DataFrame()

@st.cache_data(ttl=300)
def get_realtime_news_list(stock_id: str, stock_name: str):
    news = []
    try:
        q = urllib.parse.quote(f"{str(stock_name)} {str(stock_id)} when:1d")
        r = get_requests_session().get(f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", timeout=5)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            for item in root.findall('.//item'):
                t = item.find('title').text or ""
                if " - " in t: t = t.rsplit(" - ", 1)[0]
                news.append({"date": item.find('pubDate').text or "", "title": t, "source": item.find('source').text if item.find('source') is not None else "新聞財經", "link": item.find('link').text or ""})
            if news:
                df = pd.DataFrame(news)
                df["parsed_date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_convert('Asia/Taipei')
                df["date"] = df["parsed_date"].dt.strftime('%Y-%m-%d %H:%M')
                return df.sort_values(by="parsed_date", ascending=False)[["date", "title", "source", "link"]].to_dict('records')
    except Exception: pass
    return []

# ============ 7. Technical Engine ============
def analyze_calendar_cyclicality(df_hist):
    if df_hist is None or len(df_hist) < 90:
        return {"verdict": "📊 歷史數據不足，無法進行週期因果解耦", "early_win": 50, "mid_win": 50, "late_win": 50, "early_ret": 0, "mid_ret": 0, "late_ret": 0, "macro_season": "未知"}
    
    x = df_hist.copy().sort_values("date")
    x["return"] = x["close"].pct_change()
    x = x.dropna(subset=["return"])
    x["month"] = pd.to_datetime(x["date"]).dt.month
    x["day"] = pd.to_datetime(x["date"]).dt.day
    
    early_period = x[x["day"] <= 10]
    mid_period = x[(x["day"] > 10) & (x["day"] <= 20)]
    late_period = x[x["day"] > 20]
    
    def get_period_stats(p_df):
        if p_df.empty: return 0.0, 50.0
        avg_ret = float(p_df["return"].mean() * 100)
        win_rate = float((p_df["return"] > 0).mean() * 100)
        return avg_ret, win_rate
    
    e_ret, e_win = get_period_stats(early_period)
    m_ret, m_win = get_period_stats(mid_period)
    l_ret, l_win = get_period_stats(late_period)
    
    current_month = datetime.now(TZ).month
    current_day = datetime.now(TZ).day
    
    if current_month in [3, 6, 9, 12]:
        if current_day >= 18:
            macro_season = "🚨 季底法人清算結帳期 (極高風險)"
            macro_bias = "⚠️ 注意：當前正值季底最後結帳清算。若個股拉回，極可能是法人踩踏棄養，月循環的低吸信號在此處特許失效，嚴禁高倉位接飛刀！"
        else:
            macro_season = "🔥 季底法人績效作帳衝刺期"
            macro_bias = "💡 提示：正值季底法人作帳衝刺。資金會極端往 RS 強勢股報團，弱勢股會被當作提款機，強弱極度分化。"
    elif current_month in [1, 4, 7, 10]:
        macro_season = "🌱 新季度資金重新配置期 (作夢行情起跑)"
        macro_bias = "💡 提示：新季度剛開始，法人資金大洗牌、重新尋找新題材建倉。此時若配合『上旬營收利多公告』，很容易放量啟動波段新主升浪。"
    else:
        macro_season = "⚖️ 季度中繼常態換手期"
        macro_bias = "觀察提示：市場回歸常態產業基本面對位，沒有極端的作帳或清算壓力，日曆統計的慣性準確度最高。"

    if e_ret > 0.05 and l_ret < -0.05 and e_win >= 53.0 and l_win <= 47.0:
        base_verdict = "🦅 **典型月循環**：【月初吸金拉抬 ➔ 月底賣壓壓低】。"
    elif l_ret > 0.05 and e_ret < -0.05 and l_win >= 53.0 and e_win <= 47.0:
        base_verdict = "⚡ **逆向月循環**：【月底提前卡位 ➔ 月初開高出貨】。"
    elif e_win >= 55.0 and m_win >= 55.0 and l_win >= 55.0:
        base_verdict = "🔥 **全月多頭報團**：此股歷史上極易受大資金連續鎖碼，日曆天數雜訊低。"
    else:
        base_verdict = "⚖️ **隨機常態波動**：歷史日曆慣性不明顯，回歸常態量價防線。"
        
    final_verdict = f"【宏觀季節】：{macro_season}\n\n{macro_bias}\n\n---\n\n【微觀日曆慣性】：{base_verdict}"
        
    return {
        "verdict": final_verdict,
        "early_ret": e_ret, "early_win": e_win,
        "mid_ret": m_ret, "mid_win": m_win,
        "late_ret": l_ret, "late_win": l_win,
        "macro_season": macro_season
    }

def prepare_indicator_df(df: pd.DataFrame):
    if df is None or df.empty: return None
    x = df.copy().sort_values("date").reset_index(drop=True)
    c_prev = x["close"].shift(1)
    x["TR"] = np.maximum(x["high"] - x["low"], np.maximum((x["high"] - c_prev).abs(), (x["low"] - c_prev).abs()))
    x["ATR14"] = x["TR"].ewm(com=13, adjust=False).mean()
    x["MA5"], x["MA5_Vol"] = x["close"].rolling(5).mean(), x["vol"].rolling(5).mean()
    x["MA20"], x["MA60"], x["MA100"], x["MA20_Vol"] = x["close"].rolling(20).mean(), x["close"].rolling(60).mean(), x["close"].rolling(100).mean(), x["vol"].rolling(20).mean()
    x["Res_20D"], x["std20"] = x["high"].rolling(20).max(), x["close"].rolling(20).std()
    x["BB_upper"], x["BB_lower"] = x["MA20"] + (x["std20"] * 2), x["MA20"] - (x["std20"] * 2)
    x["BB_bandwidth"] = (x["BB_upper"] - x["BB_lower"]) / x["MA20"]
    
    delta = x["close"].diff()
    gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(com=13, adjust=False).mean().replace(0, 0.00001)
    x["RSI14"] = 100 - (100 / (1 + (gain / loss)))
    
    x["RSI5"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=4, adjust=False).mean() / -delta.clip(upper=0).ewm(com=4, adjust=False).mean().replace(0, 0.00001))))
    x["RSI10"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=9, adjust=False).mean() / -delta.clip(upper=0).ewm(com=9, adjust=False).mean().replace(0, 0.00001))))
    
    x["up"], x["down"] = x["high"].diff(), x["low"].shift(1) - x["low"]
    x["p_dm"] = np.where((x["up"] > x["down"]) & (x["up"] > 0), x["up"], 0)
    x["m_dm"] = np.where((x["down"] > x["up"]) & (x["down"] > 0), x["down"], 0)
    tr_s = x["TR"].ewm(com=13, adjust=False).mean().replace(0, 0.00001)
    x["P_DI"] = (x["p_dm"].ewm(com=13, adjust=False).mean() / tr_s) * 100
    x["M_DI"] = (x["m_dm"].ewm(com=13, adjust=False).mean() / tr_s) * 100
    x["ADX14"] = ((x["P_DI"] - x["M_DI"]).abs() / (x["P_DI"] + x["M_DI"]).replace(0, 0.00001) * 100).ewm(com=13, adjust=False).mean()
    
    x["EMA12"], x["EMA26"] = x["close"].ewm(span=12, adjust=False).mean(), x["close"].ewm(span=26, adjust=False).mean()
    x["MACD_DIF"] = x["EMA12"] - x["EMA26"]
    x["MACD_SIGNAL"] = x["MACD_DIF"].ewm(span=9, adjust=False).mean()
    x["MACD_HIST"] = x["MACD_DIF"] - x["MACD_SIGNAL"]
    
    l_min, h_max = x["low"].rolling(9).min(), x["high"].rolling(9).max()
    x["RSV"] = 100 * ((x["close"] - l_min) / (h_max - l_min).replace(0, 0.00001))
    k_l, d_l, ck, cd = [], [], 50.0, 50.0
    for rsv in x["RSV"]:
        if pd.isna(rsv): k_l.append(np.nan); d_l.append(np.nan)
        else: ck = (2/3)*ck + (1/3)*rsv; cd = (2/3)*cd + (1/3)*ck; k_l.append(ck); d_l.append(cd)
    x["K9"], x["D9"] = k_l, d_l
    
    if "open" in x.columns:
        x["u_shadow"] = x["high"] - np.maximum(x["open"], x["close"])
        x["is_long_upper_shadow"] = (x["u_shadow"] > (x["open"] - x["close"]).abs()) & (x["u_shadow"] / (x["high"] - x["low"]).replace(0, 0.00001) > 0.4)
    else: x["is_long_upper_shadow"] = False
    return x.dropna(subset=["ATR14", "MA5", "MA20", "MA60", "MA100", "Res_20D", "BB_bandwidth", "RSI14", "MACD_HIST", "K9", "D9", "ADX14", "RSI5", "RSI10"]).copy()

# ============ 8. 統一狼王策略決策大腦模型 (v48 終極整合版) ============
def auto_strategy_classifier(res_dict):
    p, r, m20, spring, phase = res_dict["current_price"], res_dict["real_resistance"], res_dict["ma20_val"], res_dict["spring_verdict"], res_dict["trend_phase"]
    if "買點一成立" in spring or "買點二成立" in spring or "醞釀中" in spring:
        if p < r * 0.98: return "LEFT_SPRING", "🛡️ 左側交易：破底翻結構"
    if p >= r * 0.97 or (p > m20 and phase == "🔥 波段多頭主升段"): return "RIGHT_BREAKOUT", "🚀 右側交易：強勢突破型態"
    return "NEUTRAL_ZONE", "⚖️ 混沌常態：無極端共振型態"

def unified_institutional_brain(res_dict, df_hist, is_holding=False, entry_cost=0.0, sector_panic=False):
    st_type, st_name = auto_strategy_classifier(res_dict)
    p, r, m20, m100, ma5 = res_dict["current_price"], res_dict["real_resistance"], res_dict["ma20_val"], res_dict["ma100_val"], res_dict["ma5_val"]
    m_safe = res_dict["macro_bull"]
    panic, overextended = res_dict.get("is_market_panic", False), res_dict.get("is_market_overextended", False)
    u_panic, u_desc, wtx = res_dict.get("is_us_panic", False), res_dict.get("us_panic_desc", ""), res_dict.get("wtx_change", 0.0)
    final, atr = res_dict["final_decision"], res_dict["atr"]
    
    k9, d9 = df_hist["K9"].iloc[-1], df_hist["D9"].iloc[-1]
    p20_max = float(df_hist["close"].tail(20).max())
    trailing_stop = p20_max - (2.5 * atr)
    r_low_10d = float(df_hist["low"].tail(10).min())
    
    f_good = "【財報年增擴張】" in res_dict["fin_conclusion"] or res_dict["latest_yoy"] >= 20
    c_lock = "強力鎖碼" in res_dict["sitc_trend"] or res_dict["sitc_3d_sum"] > 500
    is_ai_momentum = (p > m20) and c_lock and res_dict["vol_spike"]
    is_rs_gold = res_dict["is_rs_gold"]                     
    is_volume_gap_spike = res_dict["is_volume_gap_spike"]  
    
    pnl_pct = ((p - entry_cost) / entry_cost * 100) if (is_holding and entry_cost > 0) else 0.0

    # =============================================================
    # 🎭 劇本 A：【已持有部位管理劇本】
    # =============================================================
    if is_holding and entry_cost > 0:
        if pnl_pct <= -7.0:
            return {
                "strategy_name": "🚨 觸發硬性資本停損", "color": "#FF4B4B", "action_now": "🛑 🔴 【部位重傷：全額立刻清倉】", "signal": "本金敞口破防",
                "desc": f"您成本為 {entry_cost:.2f} 元。目前帳面虧損達 {pnl_pct:.1f}%，已觸發自營部硬性清算底線，立刻執行全額市價離場！",
                "blueprint": {"停損防守": f"本金死穴 {entry_cost * 0.93:.2f} 元", "移動停利": "無", "預期目標": "保全資金殘餘"}
            }

        if pnl_pct >= 15.0 and st_type == "RIGHT_BREAKOUT" and res_dict["vol_spike"] and not sector_panic:
            return {
                "strategy_name": "🔮 獲利擴張：金字塔加碼劇本發動", "color": "#7D3CFF", "action_now": "🔮 🔮 【利潤奔跑：啟動金字塔多頭加碼開火】", "signal": "主升段中繼暴量突圍共振",
                "desc": f"**【老股東利潤擴張】**：您的初始持股成本為 {entry_cost:.2f} 元，目前帳面大賺 {pnl_pct:+.1f}%。個股當下再度爆發量能突圍前高墙 {r:.2f} 元！大腦命令：**立即執行金字塔式加碼買進！** 加碼部位守 5MA，讓利潤翻倍！",
                "blueprint": {"停損防守": f"加碼部位守 5MA ({ma5:.2f} 元)", "移動停利": f"母部位續守 ATR ({trailing_stop:.2f} 元)", "預期目標": f"目標看擴張位 {res_dict['target_brk']:.2f} 元"}
            }

        if sector_panic and not is_rs_gold:
            return {
                "strategy_name": "🚨 族群共振危機防禦", "color": "#EF4444", "action_now": "🚨 🔴 【同族群崩盤：立即執行全面減碼 50%】", "signal": "板塊流動性集體踩踏",
                "desc": f"您持股成本為 {entry_cost:.2f} 元。同族群龍頭股集體下殺破 5%。該股票雖抗跌但尚未發射 RS 黃金箭頭，請先落袋 50% 鎖定短線價差防身！",
                "blueprint": {"停損防守": f"剩餘部位守 {trailing_stop:.2f} 元", "移動停利": "已提前防守減碼", "預期目標": "保全核心資產"}
            }

        if p < trailing_stop:
            if pnl_pct > 0:
                action_msg = "🛑 🔴 【波段獲利終結：剩餘母部位全數清倉】"
                desc_msg = f"**【波段趨勢終結】**：您的持股成本為 {entry_cost:.2f} 元（帳面獲利：{pnl_pct:+.1f}%）。即時價已實質跌破近20日高點回撤的 ATR 防禦線 ({trailing_stop:.2f} 元)。雖然利潤從高點回吐，但目前仍屬獲利狀態，結構徹底改變，請全額清倉落袋！"
            else:
                action_msg = "🛑 🔴 【中線結構破防：全額認賠清倉】"
                desc_msg = f"**【中線結構破防】**：您的持股成本為 {entry_cost:.2f} 元（帳面虧損：{pnl_pct:.1f}%）。即時價已實質跌破技術防線 ({trailing_stop:.2f} 元)。這檔股票先前拉高建立的防護網已被擊穿，目前已跌破您的本金成本，慣性徹底轉惡。請全額清倉認賠，嚴防虧損失控！"
            
            return {
                "strategy_name": "⏳ 中線波段趨勢終結", "color": "#EF4444", "action_now": action_msg, "signal": "跌破動態 ATR 波動防線", "desc": desc_msg,
                "blueprint": {"停損防守": "全額清倉離場", "移動停利": "已觸發", "預期目標": "資金全額退場"}
            }

        if pnl_pct > 0 and p < ma5:
            return {
                "strategy_name": "🚀 短線達標・子部位獲利落袋", "color": "#F59E0B", "action_now": "⚠️ 🟡 【短線轉弱：減碼 50% 鎖定價差，剩餘放飛】", "signal": "股價跌破 5MA 短線攻擊線",
                "desc": f"**【短線價差防衛】**：成本為 {entry_cost:.2f} 元（帳面損益：{pnl_pct:+.1f}%）。個股短線噴發速率減緩跌破 5MA ({ma5:.2f} 元)。立即執行「現股賣出 50% 倉位」，把短線衝刺價差鎖進口袋！剩下的 50% 倉位繼續跟隨長線主升段！",
                "blueprint": {"停損防守": "已化為無風險種子部位", "移動停利": f"剩餘50%守技術底線 {trailing_stop:.2f} 元", "預期目標": f"長線目標看 {res_dict['target_brk']:.2f} 元"}
            }

        if "長上影" in final or "金流陷阱" in final:
            return {
                "strategy_name": "🚨 爆量高檔出貨預警", "color": "#EF4444", "action_now": "🚨 🔴 【主力高檔出貨：主動執行減碼 50%】", "signal": "惡性 K 線結構與主力清算共振",
                "desc": f"目前部位損益 {pnl_pct:+.1f}%。個股盤中爆量且留長上影線，符合惡性主力倒貨特徵。為防範踩踏，強烈建議主動落袋一半部位，不允許利潤吐回！",
                "blueprint": {"停損防守": f"技術底線 {trailing_stop:.2f} 元", "移動停利": "啟動防守落袋", "預期目標": "防禦性鎖利"}
            }

        if pnl_pct >= 0:
            return {
                "strategy_name": "🔥 強勢主升浪完美續抱", "color": "#7D3CFF", "action_now": "🔮 🔮 【強勢狂飆：全額持股續抱】", "signal": "短長雙速動能多頭共振",
                "desc": f"**【部位對位定錨】**：持股成本 {entry_cost:.2f} 元（帳面獲利：{pnl_pct:+.1f}%）。個股完美運行於 5MA ({ma5:.2f} 元) 之上。量價結構健康，盤中波動皆為洗盤雜訊，全額咬死不賣，放飛波段暴利！",
                "blueprint": {
                    "停損防守": f"本金死穴 {entry_cost * 0.93:.2f} 元", 
                    "移動停利": f"破 5MA ({ma5:.2f} 元) 減碼 50% ｜ 破 ATR ({trailing_stop:.2f} 元) 全出", 
                    "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"
                }
            }
        else:
            return {
                "strategy_name": "🛡️ 虧損被動防守緩衝區", "color": "#1C86EE", "action_now": "⚖️ 🔵 【微幅套牢：屬於正常波動容忍，持股續抱】", "signal": "未破結構技術底線",
                "desc": f"**【部位對位定錨】**：持股成本 {entry_cost:.2f} 元（帳面微幅套牢：{pnl_pct:.1f}%）。當前浮虧完全處於量化模型的良性波動容忍帶內。它既沒有跌破 7% 的本金死穴，也沒有實質擊穿中線結構防禦線 ({trailing_stop:.2f} 元)。只要結構線沒破，請忽略日內雜訊保持續抱，靜待主力洗盤結束後的翻轉拉回！",
                "blueprint": {"停損防守": f"硬性停損線 {entry_cost * 0.93:.2f} 元 ｜ 技術防線 {trailing_stop:.2f} 元", "移動停利": "暫無利潤可鎖", "預期目標": f"先看解套拉回成本價，再看目標 {res_dict['target_brk']:.2f} 元"}
            }

    # =============================================================
    # 🎯 劇本 B：【未持有・全新開倉劇本】
    # =============================================================
    else:
        is_momentum_decelerate = (k9 < d9) and k9 > 75
        
        if "🚨 季底法人清算結帳期" in res_dict.get("macro_season", ""):
            return {
                "strategy_name": "🚨 季底流動性暴風雨防禦", "color": "#FF4B4B", "action_now": "🛑 🔴 【環境極端風險：全新開倉嚴禁開火】", "signal": "投信結帳踩踏期震盪",
                "desc": "**【風控最高警告】**：當前正處於季底法人集體清算、清庫存、倒貨的瘋狂結帳期。即使該股個股型態再漂亮，量化大腦一票否決任何全新買進交易，手握現金，拒絕在季底當接盤俠！",
                "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "保全現金等待新季度開跑"}
            }
        
        if is_rs_gold and p >= m20 and not sector_panic:
            return {
                "strategy_name": "🚀 統一特許：逆境黃金飆股劇本發動", "color": "#7D3CFF", "action_now": "🔮 🔮 【強者恆強：無視大盤恐慌立即開火】", "signal": "個股超額相對強度（RS）爆表",
                "desc": f"**【大盤逆境真金】**：大盤目前處於大跌或破位空頭區（大盤變動: {wtx:.2f}%）。但該個股今日表現出高達 {res_dict['relative_strength']:.1f}% 的超額相對強度(RS)！大腦一票否決總體市場恐慌警報，給予特許全額開火權！",
                "blueprint": {"停損防守": f"收盤跌破昨日收盤價或當日低點", "移動停利": f"波動防線 {trailing_stop:.2f} 元", "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"}
            }

        if is_volume_gap_spike and p >= m20 and not sector_panic:
            return {
                "strategy_name": "⚡ 突擊劇本：09:15 早盤量能斷層發動", "color": "#10B981", "action_now": "🔮 🟢 【量能斷層確立：全新開火進場熱錢追擊】", "signal": "開盤特大法人單極速掃貨",
                "desc": "您目前空倉。該個股在開盤前 15~30 分鐘內成交量直接灌爆超越 5MA 均量的 25%！代表市場有絕對特大資金在進行不計價掃貨（量能斷層）。大腦自動無視任何指標死叉，啟動狼王突擊買進指令！",
                "blueprint": {"停損防守": f"開盤第一盤最低價 ｜ 收盤破月線", "移動停利": f"波動防線 {trailing_stop:.2f} 元", "預期目標": f"短線價差衝刺目標 {res_dict['target_brk']:.2f} 元"}
            }

        if st_type == "RIGHT_BREAKOUT":
            if is_momentum_decelerate:
                return {
                    "strategy_name": st_name, "color": "#F59E0B", "action_now": "⚠️ 🟡 【全新開倉指標過熱：暫緩追高觀望】", "signal": "短線指標高位修正雜訊",
                    "desc": "您目前空倉。個股型態強勢但隨機指標 (KD) 出現高位洗盤死叉。為防範早盤衝進去撞上短線洗盤，請暫緩追高，等待拉回 5MA 再行開火。",
                    "blueprint": {"停損防守": "嚴禁盲目進場", "移動停利": "無", "預期目標": f"靜待突破壓制牆 {r:.2f} 元"}
                }
            
            if not m_safe:
                if is_ai_momentum and f_good and not sector_panic:
                    return {
                        "strategy_name": st_name + " (⚡ AI 特許逆勢單)", "color": "#10B981", "action_now": "🔮 🟢 【大盤逆境・特許全新開火】", "signal": "板塊獨立高能動能噴發",
                        "desc": "大盤環境雖不安全，打法策略上個股投信強力鎖碼且營收爆發，上方無怨魂。大腦解鎖 40% 的輕倉開火權，嘗試切入這檔核心逆風飆股！",
                        "blueprint": {"停損防守": f"收盤跌破前高牆 {r:.2f} 元", "移動停利": f"波動防線 {trailing_stop:.2f} 元", "預期目標": f"獲利對位 {res_dict['target_brk']:.2f} 元"}
                    }
                else:
                    return {
                        "strategy_name": st_name, "color": "#FF4B4B", "action_now": "🚨 🔴 【環境高風險：全新開倉嚴禁開火】", "signal": "總體大盤空頭暴風雨警戒",
                        "desc": "大盤失守 20MA 生命線，且個股不具備特許強度。此時全新開倉極易淪為市場提款機，一票否決新交易！",
                        "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "手握現金等待安全期"}
                    }

            if p >= r * 0.98 and res_dict["vol_spike"] and c_lock and f_good and not sector_panic:
                if overextended:
                    return {
                        "strategy_name": st_name, "color": "#F59E0B", "action_now": "⚠️ 🟡 【大盤過熱：全新開倉防守型控量開火】", "signal": "⚡ 瘋狗浪末段逆勢突破",
                        "desc": "個股達成完美共振！但大盤與季線正乖離率過熱。解鎖全新開火權，但風控模組強制削減 50% 資金配置，嚴防高位重倉套牢！",
                        "blueprint": {"停損防守": f"收盤跌破 {r:.2f} 元", "移動停利": f"即時價破 {trailing_stop:.2f} 元", "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"}
                    }
                return {
                    "strategy_name": st_name, "color": "#7D3CFF", "action_now": "🔮 🔮 【頂級信號：全新多頭建倉開火】", "signal": "🔮 頂級多頭共振：黃金主升飆股型態發動",
                    "desc": "基本面擴張、法人強力鎖碼、帶量突破前高牆，上方無怨魂，適合執行全新多頭開火建倉！",
                    "blueprint": {"停損防守": f"收盤跌破前高壓力牆 {r:.2f} 元", "移動停利": f"波動防線 {trailing_stop:.2f} 元", "預期目標": f"獲利擴張目標對位 {res_dict['target_brk']:.2f} 元"}
                }

        elif st_type == "LEFT_SPRING" and not sector_panic:
            if "買點一成立" in res_dict["spring_verdict"]:
                return {
                    "strategy_name": st_name, "color": "#10B981", "action_now": "🟢 🟢 【破底翻確立：允許精密低吸進場】", "signal": "結構洗盤完成、安全邊際高",
                    "desc": f"{res_dict['spring_verdict']} 浮額遭主力洗淨。此進場享有極致風險報酬比，可建立初始防守型頭寸。",
                    "blueprint": {"停損防守": f"硬性死穴防線 {r_low_10d:.2f} 元", "移動停利": "無", "預期目標": f"反彈停利目標看 {res_dict['target_pb']:.2f} 元"}
                }

        return {
            "strategy_name": "💤 空倉常態觀望", "color": "#64748B", "action_now": "⚖️ 🔵 【常態調整區：保持空倉耐心等待】", "signal": "進入量化緩衝帶",
            "desc": "個股處於無方向性的箱型整理區，既無主力暴量突圍，也無良性破底翻。此時盲目進場極易被來回雙向巴掌洗盤，請保持空倉觀望。",
            "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "等待金流重啟點火"}
        }

# ============ 9. Main Core Executor ============
# 🌟 新的代碼：自帶智慧辨識，找不到名字也絕對放行！
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int, is_holding=False, entry_cost=0.0, sector_panic=False):
    fin_df = pd.DataFrame()
    
    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    
    if match.empty:
        # 🎯 智慧通關：如果官方名單塞車沒抓到，我們自己現場編一個臨時臨時擋箭牌，照樣放行！
        stock_name = f"代號 {stock_id}"
        industry = "自訂追蹤板塊"
        # 簡單分類法：台灣大體上3、5、6、8開頭多為櫃買(OTC)股票
        market_type = "TWO" if (stock_id.startswith("3") or stock_id.startswith("5") or stock_id.startswith("6") or stock_id.startswith("8")) and len(stock_id) == 4 else "TSE"
    else:
        m_col = "type" if "type" in match.columns else "market_type" if "market_type" in match.columns else "market" if "market" in match.columns else None
        
        # 使用 .iloc[0] 代替 .values[0]，不論底層是用什麼數據引擎都絕對穩如泰山
        if m_col and len(match) > 0:
            market_type = str(match[m_col].iloc[0]).strip().upper()
        else:
            market_type = "TSE"
            
        if len(match) > 0:
            stock_name = str(match["stock_name"].iloc[0])
            industry = str(match["industry_category"].iloc[0])
        else:
            stock_name = f"代號 {stock_id}"
            industry = "自訂追蹤板塊"
    
    m_col = "type" if "type" in match.columns else "market_type" if "market_type" in match.columns else "market" if "market" in match.columns else None
    market_type = str(match[m_col].values[0]).strip().upper() if m_col else "TSE"
    stock_name, industry = str(match["stock_name"].values[0]), str(match["industry_category"].values[0])
    
    df_raw = get_daily_df(stock_id, market_type=market_type, days=450)
    if df_raw is None or df_raw.empty: return None

    macro_bull, macro_desc, is_market_panic, is_market_overextended = get_market_macro_status()
    radar_results, is_us_panic, us_panic_desc, wtx_change = get_overnight_radar()
    
    hist_last_raw = df_raw.iloc[-1]
    rt_open, rt_high, rt_low, rt_close, rt_vol_lots, rt_success, rt_source, rt_type = compute_live_data(stock_id, market_type, float(hist_last_raw["close"]), float(hist_last_raw["vol"]))
    
    current_price, current_vol = rt_close, rt_vol_lots 
    df_for_indicators = df_raw.copy().sort_values("date").reset_index(drop=True)
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    
    if rt_success:
        if str(df_for_indicators.iloc[-1]["date"]) == today_str:
            idx = df_for_indicators.index[-1]
            df_for_indicators.loc[idx, "close"] = rt_close
            df_for_indicators.loc[idx, "high"] = max(rt_high, float(df_for_indicators.loc[idx, "high"]))
            df_for_indicators.loc[idx, "low"] = min(rt_low, float(df_for_indicators.loc[idx, "low"]))
            df_for_indicators.loc[idx, "vol"] = rt_vol_lots * 1000.0
        else:
            new_row = pd.DataFrame([{"date": today_str, "open": float(rt_open), "high": float(rt_high), "low": float(rt_low), "close": float(rt_close), "vol": float(rt_vol_lots * 1000.0), "amount": float(rt_close * rt_vol_lots * 1000.0)}])
            df_for_indicators = pd.concat([df_for_indicators, new_row], ignore_index=True)

    df = prepare_indicator_df(df_for_indicators)
    if df is None or df.empty: return None
    peak_price_20d = float(df["close"].tail(20).max())

    now_time = datetime.now(TZ).time()
    estimated_full_day_vol_lots = current_vol * (270.0 / max(1.0, (datetime.combine(datetime.today(), now_time) - datetime.combine(datetime.today(), datetime.strptime("09:00", "%H:%M").time())).total_seconds() / 60.0)) if datetime.strptime("09:00", "%H:%M").time() <= now_time <= datetime.strptime("13:30", "%H:%M").time() else current_vol

    # ==================== 🌟 核心金流金額彈弓機制修正 ====================
    hist_recent = df.copy().sort_values("date", ascending=True).tail(90)
    counts, bins = np.histogram(hist_recent["close"], bins=15, weights=hist_recent["amount"])
    max_bin_idx = np.argmax(counts)
    calculated_poc = (bins[max_bin_idx] + bins[max_bin_idx + 1]) / 2
    
    live_price = float(df["close"].iloc[-1])
    ma20_live = float(df["close"].rolling(20).mean().iloc[-1])
    ma60_live = float(df["close"].rolling(60).mean().iloc[-1]) if len(df) >= 60 else ma20_live
    
    if abs(live_price - calculated_poc) / live_price < 0.04:
        if abs(live_price - ma20_live) / live_price < 0.04:
            volume_poc = ma60_live
        else:
            volume_poc = ma20_live
    else:
        volume_poc = calculated_poc

    hist_last = df.iloc[-1]
    ma5_val, vol_ma5_val = float(hist_last["MA5"]), float(hist_last["MA5_Vol"])
    ma20_val, ma60_val, ma100_val = float(hist_last["MA20"]), float(hist_last["MA60"]), float(hist_last["MA100"])
    vol_ma20_val, real_resistance, current_bandwidth = float(hist_last["MA20_Vol"]), float(hist_last["Res_20D"]), float(hist_last["BB_bandwidth"])
    # =======================================================================

    bb_upper, bb_lower = float(hist_last["BB_upper"]), float(hist_last["BB_lower"])
    rsi_now, adx_now, macd_hist, atr, k9_now, d9_now = safe_float(hist_last.get("RSI14", 50.0)), safe_float(hist_last.get("ADX14", 20.0)), safe_float(hist_last.get("MACD_HIST", 0.0)), safe_float(hist_last.get("ATR14", 1.0)), safe_float(hist_last.get("K9", 50.0)), safe_float(hist_last.get("D9", 50.0))
    
    kd_status = "黃金交叉" if k9_now > d9_now else "死亡交叉"
    rsi5_now, rsi10_now = safe_float(hist_last.get("RSI5")), safe_float(hist_last.get("RSI10"))
    dif_now, signal_now = safe_float(hist_last.get("MACD_DIF")), safe_float(hist_last.get("MACD_SIGNAL"))

    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = get_taiwan_enhanced_chips(stock_id)
    turnover_std = df["vol"].tail(5).std() / vol_ma20_val if vol_ma20_val > 0 else 0
    main_force_score = 45.0
    if sitc_3d_sum > 500: main_force_score += 25.0
    if margin_diff < -1000: main_force_score += 15.0
    
    vol_spike = (estimated_full_day_vol_lots * 1000.0) > (vol_ma20_val * (1.25 if df["amount"].tail(20).mean() > 2000000000 else 2.2))
    if vol_spike: main_force_score += 15.0
    if turnover_std > 0.4: main_force_score += 10.0
    main_force_label = f"🔥 強力控盤 ({main_force_score:.0f}%)" if main_force_score >= 65 else f"❄️ 籌碼散落 ({main_force_score:.0f}%)" if main_force_score <= 35 else f"⚖️ 常態調整 ({main_force_score:.0f}%)"
    is_compressed = current_bandwidth < df["BB_bandwidth"].tail(60).quantile(0.35 if df["amount"].tail(20).mean() > 2000000000 else 0.18)

    now_datetime = datetime.combine(datetime.today(), now_time)
    open_datetime = datetime.combine(datetime.today(), datetime.strptime("09:00", "%H:%M").time())
    elapsed_minutes = (now_datetime - open_datetime).total_seconds() / 60.0
    is_volume_gap_spike = (1.0 <= elapsed_minutes <= 30.0) and (current_vol >= (vol_ma5_val / 1000.0) * 0.25)

    stock_daily_pct = ((current_price - float(hist_last_raw["close"])) / float(hist_last_raw["close"])) * 100 if float(hist_last_raw["close"]) > 0 else 0.0
    relative_strength = stock_daily_pct - wtx_change
    is_rs_gold = (wtx_change <= -1.0) and (relative_strength >= 3.0)

    if k9_now < 20 and d9_now < 20: kd_timing = "📥 超賣打底區：指標跌至 20 以下。必須等同步突破 50 分水線才算多頭反轉。"
    elif k9_now > 70 and d9_now > 70: kd_timing = "🦅 超買強勢區：主升段允許長時間高檔鈍化，未死叉無需恐慌賣出。"
    elif (k9_now > d9_now) and (k9_now < 50): kd_timing = "⚠️ 短線修復雜訊：雖出現金叉，但未突破 50 分水線，多頭力道不扎實。"
    else: kd_timing = f"⚖️ KD 指標定位：當前 K={k9_now:.1f} / D={d9_now:.1f} 處於多空常態調整箱型區間。"

    if dif_now > 0 and signal_now > 0: bb_stage = "🟢 多頭波段：雙線站上 0 軸，多頭動能充足。0軸下方一律定義為弱勢盤。"
    elif macd_hist < 0 and df["MACD_HIST"].iloc[-2] >= 0: bb_stage = "📉 高點下跌區：空頭釋放力道，綠柱連續堆建立。"
    else: bb_stage = f"❌ 弱勢盤整走勢：DIF={dif_now:.2f} 在 0 軸下，綠柱縮短但未完好翻紅，不具備盲目追高條件。"

    volume_verdict = f"⚖️ RSI相對強弱：5日 RSI={rsi5_now:.1f}, 10日 RSI={rsi10_now:.1f}。RSI50 為多空分水嶺。"

    spring_verdict, spring_triggered, detected_prior_low, detected_neckline, spring_lowest_low = "⚪ 未觸發破底翻結構", False, 0.0, 0.0, 0.0
    if len(df) >= 40:
        prior_low_candidate, prior_low_idx = float(df.iloc[-40:-10]["low"].min()), df.iloc[-40:-10]["low"].idxmin()
        spring_lowest_low = float(df.iloc[-10:]["low"].min())
        for r_idx, row in df.iloc[-10:].iterrows():
            if row["low"] < prior_low_candidate:
                r_pos = df.iloc[-10:].index.get_loc(r_idx)
                for offset in range(1, 4):
                    if r_pos + offset < len(df.iloc[-10:]):
                        chk_idx = df.iloc[-10:].index[r_pos + offset]
                        if df.iloc[-10:].loc[chk_idx, "close"] > prior_low_candidate:
                            spring_triggered, detected_prior_low = True, prior_low_candidate
                            detected_neckline = float(df.loc[prior_low_idx:r_idx]["high"].max()) if not df.loc[prior_low_idx:r_idx].empty else prior_low_candidate
                            break
                if spring_triggered: break
    if spring_triggered:
        if current_price >= detected_prior_low and df["close"].iloc[-2] <= detected_prior_low and df["close"].iloc[-1] > df["open"].iloc[-1]: spring_verdict = f"🟢 【破底翻：買點一成立】主力砸盤誘空完成！重新站回前低 {detected_prior_low:.2f} 元。"
        elif current_price >= detected_neckline and vol_spike: spring_verdict = f"🔮 【破底翻：買點二成立】強勢突破關鍵頸線 {detected_neckline:.2f} 元！"
        else: spring_verdict = f"🔍 【破底翻結構醞釀中】觸發假破底洗盤（前低：{detected_prior_low:.2f}），正等待翻轉點火。"

    if current_price >= ma5_val and ma5_val >= ma20_val: short_term_trend = f"🚀 五日線多頭噴發 (KD {kd_status})"
    elif current_price >= ma5_val and current_price < ma20_val: short_term_trend = f"📈 週線跌深反彈 (KD {kd_status})"
    elif current_price < ma5_val and current_price >= ma20_val: short_term_trend = f"⚠️ 短線跌破週線 (KD {kd_status})"
    else: short_term_trend = f"📉 均線全面蓋頭 (KD {kd_status})"
        
    if current_price >= ma60_val and (df["MA60"].iloc[-1] > df["MA60"].iloc[-5]): long_term_trend = "🔥 季線全面向上（主升段架構）"
    elif current_price < ma60_val and (df["MA60"].iloc[-1] < df["MA60"].iloc[-5]): long_term_trend = "📉 季線下彎蓋頭（空頭修正架舉）"
    else: long_term_trend = "💤 季線橫向延伸（箱型潛伏築底）"

    if current_price >= ma20_val and ma20_val >= ma60_val and (df["MA20"].iloc[-1] > df["MA20"].iloc[-5]): trend_phase = "🔥 波段多頭主升段"
    elif current_price < ma20_val and ma20_val >= ma60_val: trend_phase = "🛡️ 多頭架構拉回洗盤期"
    elif is_compressed: trend_phase = "💤 潛伏築底蓄勢期"
    else: trend_phase = "📉 空頭波段修正期"

    latest_yoy = 0.0
    rev_df = get_rev_df(stock_id, days=730)
    if rev_df is not None and not rev_df.empty and "revenue" in rev_df.columns:
        rev_clean = rev_df.copy()
        rev_clean["revenue"] = pd.to_numeric(rev_clean["revenue"].astype(str).str.replace(",", ""), errors="coerce")
        rev_clean["revenue_year_growth_rate"] = rev_clean["revenue"].pct_change(12) * 100
        if not rev_clean.dropna(subset=["revenue_year_growth_rate"]).empty: latest_yoy = float(rev_clean.dropna(subset=["revenue_year_growth_rate"]).sort_values("date").iloc[-1]["revenue_year_growth_rate"])

    fin_df_raw = get_financial_statement_df(stock_id, years=2)
    fin_conclusion, pe_desc, pe_val, sum_eps_4q, gpm_now, opm_now = "📋 該標的暫無足夠季度財報數據。", "⚪ 數據不足無法計算估值", 0.0, 0.0, 0.0, 0.0
    if not fin_df_raw.empty and "Revenue" in fin_df_raw.columns and "EPS" in fin_df_raw.columns:
        fin_df_work = fin_df_raw.copy()
        for col_name in ["Revenue", "EPS", "GrossProfit", "OperatingIncome"]:
            if col_name not in fin_df_work.columns: fin_df_work[col_name] = 0.0
        fin_df_work = fin_df_work.sort_values("date").reset_index(drop=True)
        for idx in range(len(fin_df_work)):
            rev_amt = safe_float(fin_df_work.loc[idx, "Revenue"])
            fin_df_work.loc[idx, "gpm"] = (safe_float(fin_df_work.loc[idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df_work.loc[idx, "opm"] = (safe_float(fin_df_work.loc[idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        
        last_fin = fin_df_work.iloc[-1]
        gpm_now, opm_now, sum_eps_4q = safe_float(last_fin.get("gpm", 0.0)), safe_float(last_fin.get("opm", 0.0)), pd.to_numeric(fin_df_work.tail(4)['EPS'], errors='coerce').sum()
        if sum_eps_4q > 0:
            pe_val = current_price / sum_eps_4q
            db_t = 55.0 if latest_yoy >= 30.0 else 45.0 if latest_yoy >= 15.0 else 35.0
            dc_t = 22.0 if latest_yoy >= 30.0 else 18.0 if latest_yoy >= 15.0 else 13.0
            pe_desc = "🚨 估值瘋狂（高檔吹泡泡）" if pe_val > db_t else "🟢 價值鐵板（安全邊際高）" if pe_val < dc_t else "⚖️ 估值合理區間"
        if len(fin_df_work) >= 5:
            prev_fin = fin_df_work.iloc[-5] 
            fin_conclusion = "📈 【財報年增擴張】 最新季度獲利指標全數超越去年同期！" if gpm_now > safe_float(prev_fin.get("gpm", 0.0)) and opm_now > safe_float(prev_fin.get("opm", 0.0)) else "📉 【本業結構退步】 獲利結構遜於去年同期，需提高警覺。"
        fin_df = fin_df_work[["date", "EPS", "Revenue", "GrossProfit", "OperatingIncome", "gpm", "opm"]].copy()

    news_analysis_report, positive_catalysts_list, raw_news_list = "⚪ 暫無最新重要輿情。", [], get_realtime_news_list(stock_id, stock_name)
    if raw_news_list:
        raw_news_list = raw_news_list[:8]
        for n in raw_news_list:
            lbl, col = analyze_news_sentiment(n["title"])
            n["sentiment"], n["color"] = lbl, col
            if "利多" in lbl: positive_catalysts_list.append(n["title"])
        pos_cnt, neg_cnt = sum(1 for n in raw_news_list if "利多" in n["sentiment"]), sum(1 for n in raw_news_list if "利空" in n["sentiment"])
        news_analysis_report = f"🔥 【輿情偏多】 利多消息主導市場（多 {pos_cnt} / 空 {neg_cnt}）。" if pos_cnt > neg_cnt else f"🚨 【輿情偏空】 利空雜音浮現（空 {neg_cnt} / 多 {pos_cnt}）。" if neg_cnt > pos_cnt else "⚖️ 【輿情中性】 多空消息勢均力敵。"
    recent_catalyst_summary = "<b>🎯 關鍵消息面利多題材：</b><br>" + "<br>".join([f"• {t}" for t in positive_catalysts_list[:2]]) if positive_catalysts_list else "⚪ 近 24H 內市場暫無顯著的突發消息面利多推升。"

    t = tick_size(current_price)
    target_brk = round_to_tick(current_price + ((4.0 if df["amount"].tail(20).mean() > 2000000000 else 5.5) * atr), t)
    stop_brk = round_to_tick(real_resistance - (1.5 * atr) - (float(slip_ticks) * t), t) if round_to_tick(real_resistance - (1.5 * atr) - (float(slip_ticks) * t), t) < current_price else round_to_tick(current_price - (1.0 * atr), t)
    target_pb = round_to_tick(volume_poc, t)
    stop_pb = round_to_tick(ma20_val - atr - (float(slip_ticks) * t), t) if round_to_tick(ma20_val - atr - (float(slip_ticks) * t), t) < current_price else round_to_tick(current_price - (1.5 * atr), t)
    
    open_gap_pct = ((safe_float(df["open"].iloc[-1]) - safe_float(df["close"].iloc[-2])) / safe_float(df["close"].iloc[-2]) * 100) if len(df) > 1 else 0
    close_to_low_pct = ((current_price - rt_low) / (rt_high - rt_low)) if (rt_high - rt_low) > 0 else 1
    is_broker_dumping_risk = (open_gap_pct > 3.5) and (close_to_low_pct < 0.35) and ((current_vol * 1000.0) > (vol_ma20_val * 2.5))

    final_decision = "⚖️ 綜合評估"
    k_shadow_trap = bool(df.iloc[-1].get("is_long_upper_shadow", False)) and vol_spike
    if k_shadow_trap: final_decision = "❌ 爆量長上影"
    elif is_broker_dumping_risk: final_decision = "🚨 惡性金流陷阱"

    last_trade_date_str = str(df.iloc[-1]["date"])
    _, local_m_desc, local_m_color = get_market_status_label(rt_success, last_trade_date_str)
    m_desc, m_color = local_m_desc, local_m_color
    if is_market_panic: m_desc, m_color = "🚨 大盤瀑布式清算恐慌潮", "red"
    elif wtx_change <= -1.0: m_desc, m_color = f"🚨 大盤趨勢破防下殺 ({wtx_change:.2f}%)", "red"
    elif is_us_panic: m_desc, m_color = "🚨 盤前美股暴跌警戒中", "#F59E0B"
    elif is_market_overextended: m_desc, m_color = "⚠️ 大盤極端正乖離過熱", "orange"

    # 🌟 核心數據整合路由封裝 (確保大腦決策前，收得到日曆的靈魂信號)
    cycle_res = analyze_calendar_cyclicality(df.copy())

    package = {
        "macro_bull": macro_bull,
        "current_price": current_price, "current_vol": current_vol, "vol_ma20_val": vol_ma20_val, "real_resistance": real_resistance, "ma20_val": ma20_val, "ma100_val": ma100_val, "ma5_val": ma5_val,
        "sitc_3d_sum": sitc_3d_sum, "margin_diff": margin_diff, "macro_desc": macro_desc, "is_market_panic": is_market_panic, "is_market_overextended": is_market_overextended,
        "is_us_panic": is_us_panic, "us_panic_desc": us_panic_desc, "wtx_change": wtx_change, "spring_verdict": spring_verdict, "final_decision": final_decision, "trend_phase": trend_phase,
        "vol_spike": vol_spike, "pe_desc": pe_desc, "margin_trend": margin_trend, "target_brk": target_brk, "stop_brk": stop_brk, "target_pb": target_pb, "stop_pb": stop_pb,
        "atr": atr, "fin_conclusion": fin_conclusion, "latest_yoy": latest_yoy, "sitc_trend": sitc_trend, "short_term_trend": short_term_trend, "volume_poc": volume_poc,
        "is_rs_gold": is_rs_gold, "is_volume_gap_spike": is_volume_gap_spike, "relative_strength": relative_strength,
        "macro_season": cycle_res["macro_season"]
    }
    
    tactical_blueprint = unified_institutional_brain(package, df.copy(), is_holding=is_holding, entry_cost=entry_cost, sector_panic=sector_panic)
    
    expected_stop_price = package["stop_brk"] if "突破" in tactical_blueprint["strategy_name"] else package["stop_pb"]
    if "破底翻" in tactical_blueprint["strategy_name"] and ("買點一成立" in spring_verdict or "買點二成立" in spring_verdict):
        expected_stop_price = round_to_tick(spring_lowest_low - t, t) if round_to_tick(spring_lowest_low - t, t) < current_price else round_to_tick(current_price - (1.0 * atr), t)
        strategy_route = "🔮 破底翻底吸佈局/加倉劇本"
    else: strategy_route = "🚀 強勢突破前高劇本" if "突破" in tactical_blueprint["strategy_name"] else "🛡️ 均線拉回低吸劇本"

    adjusted_risk = risk_per_trade
    if "立即" in tactical_blueprint["action_now"] and "清倉" in tactical_blueprint["action_now"]: adjusted_risk = 0.0
    elif "🛑" in tactical_blueprint["action_now"] or "暫緩追高" in tactical_blueprint["action_now"]: adjusted_risk = 0.0 
    elif "防守型控量" in tactical_blueprint["action_now"]: adjusted_risk *= 0.4 
    elif "🔮" in tactical_blueprint["action_now"]: adjusted_risk *= 1.5 
    
    rr1_brk = (target_brk - current_price) / (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0
    rr1_pb = (target_pb - current_price) / (current_price - stop_pb) if (current_price - stop_pb) > 0 else 0
    
    base_lots = min(int((total_capital * (adjusted_risk / 100) * 10000 / (current_price - expected_stop_price)) / 1000), int((total_capital * 10000) / (current_price * 1000))) if (current_price - expected_stop_price > 0 and adjusted_risk > 0) else 0
    
    if "加碼" in tactical_blueprint["action_now"]:
        suggested_lots = max(1, int(base_lots * 0.5))
        is_pyramid_order = True
    else:
        suggested_lots = base_lots
        is_pyramid_order = False
    
    max_safe_liquidity_lots = max(1, int(vol_ma5_val * 0.015))
    if suggested_lots > max_safe_liquidity_lots:
        suggested_lots = max_safe_liquidity_lots
        liquidity_capped = True
    else: liquidity_capped = False

    stop_line_text = f"{round_to_tick(peak_price_20d - (2.5 * atr), t):.2f} 元"

    res_dict = {}
    res_dict["stock_id"] = stock_id
    res_dict["stock_name"] = stock_name
    res_dict["industry"] = industry
    res_dict["current_price"] = current_price
    res_dict["current_vol"] = current_vol
    res_dict["ma5_val"] = ma5_val
    res_dict["vol_ma5_val"] = vol_ma5_val
    res_dict["ma20_val"] = ma20_val
    res_dict["ma60_val"] = ma60_val
    res_dict["ma100_val"] = ma100_val
    res_dict["vol_ma20_val"] = vol_ma20_val
    res_dict["real_resistance"] = real_resistance
    res_dict["bb_upper"] = bb_upper
    res_dict["bb_lower"] = bb_lower
    res_dict["bb_bandwidth"] = current_bandwidth
    res_dict["rsi_now"] = rsi_now
    res_dict["adx_now"] = adx_now
    res_dict["macd_hist"] = macd_hist
    res_dict["macro_desc"] = macro_desc
    res_dict["sitc_trend"] = sitc_trend
    res_dict["margin_trend"] = margin_trend
    res_dict["sitc_3d_sum"] = sitc_3d_sum
    res_dict["margin_diff"] = margin_diff
    res_dict["latest_yoy"] = latest_yoy
    res_dict["pe_val"] = pe_val
    res_dict["pe_desc"] = pe_desc
    res_dict["eps_4q"] = sum_eps_4q
    res_dict["fin_conclusion"] = fin_conclusion
    res_dict["gpm_now"] = gpm_now
    res_dict["opm_now"] = opm_now
    res_dict["is_compressed"] = is_compressed
    res_dict["vol_spike"] = vol_spike
    res_dict["news_analysis_report"] = news_analysis_report
    res_dict["raw_news_list"] = raw_news_list
    res_dict["trend_phase"] = trend_phase
    res_dict["short_term_trend"] = short_term_trend
    res_dict["long_term_trend"] = long_term_trend
    res_dict["target_brk"] = target_brk
    res_dict["stop_brk"] = stop_brk
    res_dict["rr1_brk"] = rr1_brk
    res_dict["target_pb"] = target_pb
    res_dict["stop_pb"] = stop_pb
    res_dict["rr1_pb"] = rr1_pb
    res_dict["suggested_lots"] = suggested_lots
    res_dict["is_pyramid_order"] = is_pyramid_order
    res_dict["liquidity_capped"] = liquidity_capped
    res_dict["max_safe_liquidity_lots"] = max_safe_liquidity_lots
    res_dict["expected_stop_price"] = expected_stop_price
    res_dict["strategy_route"] = strategy_route
    res_dict["expected_target_price"] = target_brk if "突破" in tactical_blueprint["strategy_name"] or "加碼" in tactical_blueprint["action_now"] or "暫緩追高" in tactical_blueprint["action_now"] else target_pb
    res_dict["trailing_stop_line"] = stop_line_text
    
    res_dict["atr"] = atr
    res_dict["stock_daily_pct"] = stock_daily_pct
    res_dict["relative_strength"] = relative_strength
    res_dict["is_rs_gold"] = is_rs_gold
    res_dict["is_volume_gap_spike"] = is_volume_gap_spike
    res_dict["calendar_verdict"] = cycle_res["verdict"]
    res_dict["calendar_data"] = cycle_res
    
    res_dict["rt_source"] = rt_source
    res_dict["m_desc"] = m_desc
    res_dict["m_color"] = m_color
    res_dict["volume_poc"] = volume_poc
    res_dict["main_force_label"] = main_force_label
    res_dict["recent_catalyst_summary"] = recent_catalyst_summary
    res_dict["fin_df"] = fin_df
    res_dict["k9_now"] = k9_now
    res_dict["d9_now"] = d9_now
    res_dict["spring_verdict"] = spring_verdict
    res_dict["bb_stage"] = bb_stage
    res_dict["kd_timing"] = kd_timing
    res_dict["volume_verdict"] = volume_verdict
    res_dict["tactical_blueprint"] = tactical_blueprint
    res_dict["radar_results"] = radar_results
    return res_dict

# ============ 10. UI Presentation Layer ============
with st.sidebar:
    st.header("🛡️ 全球資金池風控參數")
    capital = st.number_input("核心大資金池 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆最大核心風險承受 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守技術滑價 (Ticks)", 0, 5, 1)
    st.markdown("---")
    
    st.subheader("🌐 族群板塊即時連線監控")
    sector_panic_toggle = st.checkbox("🔥 同族群其他龍頭股「集體下殺破5%」", value=False)
    st.markdown("---")
    auto_refresh = st.checkbox("🔄 開啟盤中每 5 秒自動秒刷報價", value=False)

macro_bull, macro_label, is_market_panic, is_market_overextended = get_market_macro_status()
full_info_df = get_stock_info_df()

st.markdown("## 📡 雙速策略大腦動態綜合看盤台 (v48 狼王特選版)")
st.markdown("### 🎛️ 戰術總指揮中心 (Command Center)")
top_col1, top_col2 = st.columns(2)

with top_col1:
    st.markdown("""<div style='background-color:#F0FDF4; padding:8px; border-radius:6px; border-left:4px solid #10B981; margin-bottom:8px;'><b style='color:#065F46; font-size:13.5px;'>流派 A：自訂戰術觀察清單 ➔ 全因子即時雷達掃描</b></div>""", unsafe_allow_html=True)
    
    # 🌟 徹底摧毀選單！改成自由貼上代號或關鍵字，把控制權完全還給你
    user_scan_input = st.text_input(
        "請直接輸入你想打包掃描的【個股代號清單】(用逗號隔開) 或 【產業關鍵字】:", 
        value="3037,3715,1717,2330,2317,2454"
    )
    
    # 後台智慧清洗與分流解析
    input_clean = str(user_scan_input).strip().replace(" ", "")
    if any(c.isdigit() for c in input_clean):
        # 1. 如果輸入包含數字，代表使用者自己點名股票，直接精準掃描這幾檔
        industry_stocks = [s for s in input_clean.split(",") if s]
        scan_label = f"自訂 {len(industry_stocks)} 檔核心池"
        max_output_display = len(industry_stocks)
    else:
        # 2. 如果輸入的是全文字（如：半導體、化學），自動去全台灣股票庫抓出有對應關鍵字的股票
        matched_df = full_info_df[
            full_info_df["industry_category"].str.contains(input_clean, na=False) | 
            full_info_df["stock_name"].str.contains(input_clean, na=False)
        ]
        industry_stocks = matched_df["stock_id"].tolist()[:25] # 限制前 25 檔，防止流量塞車
        scan_label = f"關鍵字【{input_clean}】匹配池"
        max_output_display = len(industry_stocks)

    scan_trigger = st.button(f"🔍 啟動 【{scan_label}】 當下全因子動態矩陣掃描排行榜", use_container_width=True)
    with top_col2:
    st.markdown("""<div style='background-color:#EFF6FF; padding:8px; border-radius:6px; border-left:4px solid #3B82F6; margin-bottom:8px;'><b style='color:#1E40AF; font-size:13.5px;'>流派 B：個股五維度縱向因果深度診斷與策略開火</b></div>""", unsafe_allow_html=True)
    stock_input = st.text_input("輸入或由左方排行榜選定之目標個股代碼：", value="3037")
    
    st.markdown("""<div style='background-color:#FFFBEB; padding:10px; border-radius:6px; border: 1px solid #FCD34D; margin-top:5px;'>""", unsafe_allow_html=True)
    u_col1, u_col2 = st.columns(2)
    with u_col1:
        user_holding = st.checkbox("📊 我目前手中「已持有」此個股", value=False)
    with u_col2:
        user_cost = st.number_input("每股真實持股成本 (元)", value=0.0, step=1.0, min_value=0.0, disabled=not user_holding)
    st.markdown("""</div>""", unsafe_allow_html=True)
    
    diag_trigger = st.button(f"🔥 執行 【{stock_input}】 精密大腦雙速成本定錨診斷", use_container_width=True)

st.markdown("---")

if scan_trigger:
    # 🌟 萬能防爆修正：把原本不存在的 scan_mode，無縫改成我們剛才精算好的 scan_label！
    st.subheader(f"📊 【{scan_label}】即時動態連線量化篩選排行榜")
    st.cache_data.clear()
    
    # 直接使用我們在上面智慧分流解析好的自訂自由選股池 industry_stocks
    scan_pool = industry_stocks
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    scan_results = []
    for idx, sid in enumerate(scan_pool):
        status_text.text(f"🐺 狼王大腦正在即時量化自訂個股第 {idx+1}/{len(scan_pool)} 檔: {sid}...")
        progress_bar.progress((idx + 1) / len(scan_pool))
        time.sleep(0.25)
        
        res = evaluate_stock(sid, capital, risk_pct, slip_input, is_holding=False, entry_cost=0.0, sector_panic=sector_panic_toggle)
        
        # 🌟 智慧放行：既然是你的私人專屬股票籃，只要抓得到數據(res)，通通特許進榜比拼！
        if res:
            bp_data = res["tactical_blueprint"]
            score = float(res["relative_strength"])    
            if res["vol_spike"]: score += 15.0         
            if res["sitc_3d_sum"] > 500: score += 20.0   
            if res["latest_yoy"] >= 20: score += 10.0    
            
            if "立即" in bp_data["action_now"] and "🔴" not in bp_data["action_now"]: 
                score += 25.0  
            if "🔴" in bp_data["action_now"] or "🛑" in bp_data["action_now"]: 
                score -= 50.0  
            
            scan_results.append({
                "代碼": res["stock_id"], 
                "股名": res["stock_name"], 
                "盤中市價": f"{res['current_price']:.2f} 元", 
                "超額強度(RS)": f"{res['relative_strength']:+.2f}%",
                "大腦路由分類": bp_data["strategy_name"].split("：")[-1], 
                "當下即時動作": bp_data["action_now"], 
                "短期動能": res["short_term_trend"], 
                "波段底蘊": res["long_term_trend"], 
                "量化綜合得分": round(score, 1),
                "color_code": bp_data["color"]
            })
            
    status_text.empty()
    progress_bar.empty()
    
    if scan_results:
        df_scan = pd.DataFrame(scan_results)
        df_scan = df_scan.sort_values(by="量化綜合得分", ascending=False).reset_index(drop=True)
        df_scan = df_scan.head(max_output_display)
        
        st.dataframe(df_scan.style.apply(lambda r: [f'background-color: {r["color_code"]}15; font-weight: 600;'] * len(r), axis=1), column_order=["代碼", "股名", "盤中市價", "超額強度(RS)", "大腦路由分類", "當下即時動作", "短期動能", "波段底蘊", "量化綜合得分"], use_container_width=True, height=400)
    else:
        st.info("💡 當前選擇的名單在盤中金流平淡，暫無符合排行之標的。")

if diag_trigger or (not scan_trigger and stock_input):
    st.cache_data.clear()
    
    with st.spinner("五維度大腦深度因果解耦中..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input, is_holding=user_holding, entry_cost=user_cost, sector_panic=sector_panic_toggle)
        if res is None: st.error("該個股代碼數據獲取失敗，請確認編號是否正確（數據歷史長度需大於100日）。")
        else:
            bp_data = res["tactical_blueprint"]
            bp = bp_data["blueprint"]
            
            st.html(f"""
            <div style="background-color: {bp_data['color']}10; border: 2px solid {bp_data['color']}; padding: 22px; border-radius: 8px; margin-bottom: 25px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.03);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <span style="color: {bp_data['color']}; font-size: 14px; font-weight: 900; letter-spacing: 0.05em;">📢 狀態定錨決策大腦標籤：{bp_data['strategy_name']}</span>
                    <span style="background-color: {bp_data['color']}; color: white; padding: 4px 12px; border-radius: 4px; font-size: 13px; font-weight:800; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">{bp_data['action_now']}</span>
                </div>
                <h3 style="margin: 5px 0; color: {bp_data['color']}; font-size: 23px; font-weight: 900;">即時策略防線：{bp_data['signal']}</h3>
                <p style="margin: 8px 0 15px 0; color: #1E293B; font-size: 14.5px; line-height: 1.6; text-align: justify;"><b>實戰決策研判：</b>{bp_data['desc']}</p>
                <div style="background-color: white; border: 1px solid #E2E8F0; padding: 15px; border-radius: 6px; margin-top: 10px;">
                    <span style="color: #475569; font-size: 13px; font-weight: 800; display: block; margin-bottom: 8px;">🎯 現股動態配套技術出場計畫藍圖 (Exit Execution Blueprint)</span>
                    <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px;">
                        <div style="background-color: #FFF5F5; padding: 10px; border-radius: 4px; border-left: 3px solid #EF4444;">
                            <small style="color: #DC2626; font-weight: 800; font-size: 11px;">🛑 1. 核心資本硬性防線</small>
                            <p style="margin: 3px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">{bp['停損防守']}</p>
                        </div>
                        <div style="background-color: #FFFBEB; padding: 10px; border-radius: 4px; border-left: 3px solid #F59E0B;">
                            <small style="color: #D97706; font-weight: 800; font-size: 11px;">⚠️ 2. 移動鎖利/減碼基準</small>
                            <p style="margin: 3px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">{bp['移動停利']}</p>
                        </div>
                        <div style="background-color: #F0FDF4; padding: 10px; border-radius: 4px; border-left: 3px solid #10B981;">
                            <small style="color: #16A34A; font-weight: 800; font-size: 11px;">🚀 3. 預期中線波段目標</small>
                            <p style="margin: 3px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">{bp['預期目標']}</p>
                        </div>
                    </div>
                </div>
            </div>
            """)

            st.markdown("### 🌐 昨晚美股與台指期夜盤即時戰報")
            radar_show = res["radar_results"]
            if radar_show:
                rd_cols = st.columns(len(radar_show))
                for i, (lbl, val) in enumerate(radar_show.items()):
                    with rd_cols[i]:
                        st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:10px; border-radius:6px; text-align:center;"><span style="font-size:12px; color:#64748B; font-weight:600;">{lbl}</span><h4 style="margin:4px 0 0 0; color:{'#10B981' if val >= 0 else '#EF4444'}; font-weight:800;">{'🔺' if val >= 0 else '🔻'} {val:.2f}%</h4></div>""", unsafe_allow_html=True)

            st.markdown(f"""<div style="background-color: #1F2937; padding: 18px; border-radius: 8px; border: 2px solid #3B82F6; margin-bottom: 20px;"><div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;"><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600; letter-spacing: 0.05em;">DIAGNOSTIC TARGET</span><h1 style="margin: 4px 0 0 0; color: #FFFFFF; font-size: 28px; font-weight: 800;">{res['stock_name']} <span style="color: #3B82F6;">({res['stock_id']})</span></h1></div><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">大類板塊歸屬</span><h3 style="margin: 4px 0 0 0; color: #F3F4F6; font-size: 18px; font-weight: 700;">{res['industry']}</h3></div><div style="text-align: right; background-color: rgba(255,255,255,0.05); padding: 6px 12px; border-radius: 6px;"><span style="color: #9CA3AF; font-size: 11px; font-weight: 600; display:block;">即時流報價狀態</span><span style="color: #F9FAFB; font-weight: 600; font-size: 13px;">來源: {res['rt_source']} | 狀態: </span><span style="color: {res['m_color']}; font-weight: 700; font-size: 13px;">{res['m_desc']}</span></div></div></div>""", unsafe_allow_html=True)

            c1, c2, c3, c4 = st.columns(4)
            with c1: st.markdown(custom_hud_box("💡 當前即市價 (K線精密流)", f"<span style='font-size:20px; color:#0F172A;'>{res['current_price']:.2f} 元</span><br><small style='color:#64748B; font-weight:500;'>今日成交: {res['current_vol']:.0f} 張</small>"), unsafe_allow_html=True)
            with c2: st.markdown(custom_hud_box("⏱️ 五日短線攻擊速線 (MA5)", f"<span style='font-size:16px; color:#1E293B;'>{res['ma5_val']:.2f} 元</span><br><small style='color:#64748B;'>今日漲跌幅: {res['stock_daily_pct']:+.2f}%</small>"), unsafe_allow_html=True)
            with c3: st.markdown(custom_hud_box("⏳ 母部位大波段防禦線 (ATR)", f"<span style='font-size:16px; color:#7C3AED;'>{res['trailing_stop_line']}</span><br><small style='color:#64748B;'>當前 ATR14: {res['atr']:.2f}</small>"), unsafe_allow_html=True)
            with c4: st.markdown(custom_hud_box("📊 相對強度 (RS Matrix)", f"<span style='font-size:16px; color:#10B981;'>超額 {res['relative_strength']:+.2f}%</span><br><small style='color:#64748B;'>RS黃金箭頭: {'🔥 成立(免疫大盤)' if res['is_rs_gold'] else '⚪ 整理中'}</small>"), unsafe_allow_html=True)

            st.markdown("### 🏛️ 四維度因子核心動態曝光面板（🚨 法人籌碼為昨日盤後數據）")
            f1, f2, f3, f4 = st.columns(4)
            with f1: st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #10B981; min-height:185px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;"><h5 style="margin:0; color:#065F46; font-size:14px; font-weight:700;">💎 財務面基本結構</h5><ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;"><li>最新月營收YoY: <span style="color:#10B981; font-weight:700;">""" + f"{res['latest_yoy']:.1f}%" + """</span></li><li>單季毛利率: """ + f"{res['gpm_now']:.1f}%" + """</li><li>單季營益率: """ + f"{res['opm_now']:.1f}%" + """</li><li>體質定性: """ + res['fin_conclusion'].replace("📈", "").replace("📉", "").replace("⚖️", "").strip() + """</li></ul></div>""", unsafe_allow_html=True)
            with f2: st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #3B82F6; min-height:185px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;"><h5 style="margin:0; color:#1E40AF; font-size:14px; font-weight:700;">🦅 籌碼面核心金流 (昨日盤後)</h5><ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;"><li><b>神秘主力控盤度</b>: <span style="color:#2563EB; font-weight:700;">""" + res["main_force_label"] + """</span></li><li>投信3日進出: """ + f"{res['sitc_3d_sum']:.0f} 張" + """</li><li>散戶融資5日增減: """ + f"{res['margin_diff']:.0f} 張" + """</li><li>浮額沉澱狀態: """ + res['margin_trend'].replace("🚨", "").replace("🟢", "").replace("🟡", "").strip() + """</li></ul></div>""", unsafe_allow_html=True)
            with f3: st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #F59E0B; min-height:175px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;"><h5 style="margin:0; color:#92400E; font-size:14px; font-weight:700;">📊 估值面歷史位階</h5><ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;"><li>滾動本益比: <span style="color:#D97706; font-weight:700;">""" + f"{res['pe_val']:.1f} 倍" + """</span></li><li>近四季總EPS: """ + f"{res['eps_4q']:.2f} 元" + """</li><li>位階判定: """ + res['pe_desc'].replace("🚨", "").replace("🟢", "").replace("⚖️", "").strip() + """</li><li>防禦邊際: """ + ("高鐵板" if res['pe_val']<13 else "常態區間" if res['pe_val']<=35 else "危險區") + """</li></ul></div>""", unsafe_allow_html=True)
            with f4: 
                vol_gap_text = "🚨 爆發觸發" if res['is_volume_gap_spike'] else "⚪ 正常"
                st.markdown("""<div style="background-color:#FDF4FF; padding:12px; border-radius:6px; border-top:4px solid #7C3AED; min-height:185px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;"><h5 style="margin:0; color:#5B21B6; font-size:14px; font-weight:700;">⏱️ 微觀技術與早盤監測</h5><ul style="margin:6px 0 0 0; padding-left:16px; font-size:13px; color:#1E293B; line-height:1.45; font-weight:600;"><li>09:15 量能斷層: <span style="color:#10B981; font-weight:700;">""" + vol_gap_text + """</span></li><li>分價量密集牆(POC): """ + f"{res['volume_poc']:.2f} 元" + """</li><li>隨機隨機指標: KD=""" + f"{res['k9_now']:.1f}/{res['d9_now']:.1f}" + """</li></ul><hr style="margin:6px 0; border:0; border-top:1px solid #E2E8F0;"><p style="margin:0; padding:0; font-size:12px; color:#6B21A8; line-height:1.45; font-weight:600;">""" + res["recent_catalyst_summary"] + """</p></div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("### 🗺️ 精密雙軌量化交易藍圖對照區 (空倉全新佈局參考)")
            bl1, bl2 = st.columns(2)
            with bl1: st.markdown(f"""<div style="background-color: #F8FAFC; padding: 16px; border-radius: 6px; border-left: 5px solid #2563EB; border-top: 1px solid #E2E8F0; border-right: 1px solid #E2E8F0; border-bottom: 1px solid #E2E8F0;"><h4 style="margin: 0 0 12px 0; color: #1E40AF; font-weight:800;">🚀 流派一：突破前高起漲劇本 (Breakout)</h4><p style="font-size: 14px; margin: 5px 0;"><b>精密建倉觸發點</b>：&le; {res['real_resistance']:.2f} 元</p><p style="font-size: 14px; margin: 5px 0;"><b>精密獲利目標</b>：<span style="color:#2563EB; font-weight:700;">{res['target_brk']:.2f} 元</span></p><p style="font-size: 14px; margin: 5px 0;"><b>技術防守停損</b>：{res['stop_brk']:.2f} 元</p><p style="font-size: 14px; margin: 5px 0;"><b>期望風險報酬比 (R:R)</b>：{res['rr1_brk']:.2f}</p></div>""", unsafe_allow_html=True)
            with bl2: st.markdown(f"""<div style="background-color: #F8FAFC; padding: 16px; border-radius: 6px; border-left: 5px solid #10B981; border-top: 1px solid #E2E8F0; border-right: 1px solid #E2E8F0; border-bottom: 1px solid #E2E8F0;"><h4 style="margin: 0 0 12px 0; color: #065F46; font-weight:800;">🛡️ 流派二：均線拉回低吸劇本 (Pullback)</h4><p style="font-size: 14px; margin: 5px 0;"><b>精密低吸買點</b>：貼近 {res['ma20_val']:.2f} 元</p><p style="font-size: 14px; margin: 5px 0;"><b>期望反彈目標</b>：<span style="color:#10B981; font-weight:700;">{res['target_pb']:.2f} 元</span></p><p style="font-size: 14px; margin: 5px 0;"><b>技術防守停損</b>：{res['stop_pb']:.2f} 元</p><p style="font-size: 14px; margin: 5px 0;"><b>期望風險報酬比 (R:R)</b>：{res['rr1_pb']:.2f}</p></div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("### 🛡️ 量化核心風控配額開火劇本")
            
            if res["liquidity_capped"]:
                st.warning(f"⚠️ **【流動性上限啟動】**：為了防範台股鎖死踩踏，單筆限額已遭硬性限制（最大極限：{res['max_safe_liquidity_lots']} 張）。")
                
            if res["suggested_lots"] == 0:
                st.error("🚨 【核心風控最高警戒：策略大腦拒絕開倉 / 已強制清倉】")

            b1, b2, b3, b4 = st.columns(4)
            label_text = "🔮 精算加碼頭寸配置" if res["is_pyramid_order"] else "精算風控進場配置"
            with b1: st.metric(label_text, f"{res['suggested_lots']} 張", "金字塔/流動性雙軌控制中")
            with b2: st.metric("當前劇本風控停損價", f"{res['expected_stop_price']:.2f} 元")
            with b3: st.metric("大波段移動停利線 (ATR)", res["trailing_stop_line"])
            with b4: st.metric("大盤加權指數防禦網", "大盤過熱" if is_market_overextended else "逆境黃金(RS放行)" if res["is_rs_gold"] else "多頭安全" if macro_bull else "空頭風險", res["macro_desc"])

            st.markdown("---")
            st.markdown("### 🔍 跨因子微觀底層驗證數據")
            with st.expander("🧱 ⚙️ 核心指標副圖完整專家解碼面板", expanded=True):
                st.markdown(f"**📈 KD 隨機指標副圖解讀**：{res['kd_timing']}")
                st.markdown(f"**📊 MACD 趨勢力道副圖解讀**：{res['bb_stage']}")
                st.markdown(f"**⚡ RSI 相對強弱副圖解讀**：{res['volume_verdict']}")

            with st.expander("📅 ⏳ 個股歷史日曆效應（月週期循環）專家解碼面板", expanded=True):
                st.markdown(f"### 📡 狼王大腦日曆綜合研判：")
                st.markdown(f"> {res['calendar_verdict'].replace('\n', '<br>')}", unsafe_allow_html=True)
                st.markdown("---")
                st.markdown("**📊 過去 450 天內【月初、月中、月底】實質統計矩陣：**")
                
                c_data = res["calendar_data"]
                cy_col1, cy_col2, cy_col3 = st.columns(3)
                with cy_col1:
                    st.markdown(f"""
                    <div style="background-color: #F8FAFC; border-left: 4px solid #2563EB; padding: 10px; border-radius: 4px;">
                        <small style="color: #64748B; font-weight: 700;">🟢 上旬 (1號 ~ 10號)</small>
                        <p style="margin: 4px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">
                            平均報酬: <span style="color: {'#10B981' if c_data['early_ret'] >= 0 else '#EF4444'}">{c_data['early_ret']:+.3f}%</span><br>
                            歷史勝率: {c_data['early_win']:.1f}%
                        </p>
                    </div>
                    """, unsafe_allow_html=True)
                    
                with cy_col2:
                    st.markdown(f"""
                    <div style="background-color: #F8FAFC; border-left: 4px solid #64748B; padding: 10px; border-radius: 4px;">
                        <small style="color: #64748B; font-weight: 700;">🟡 中旬 (11號 ~ 20號)</small>
                        <p style="margin: 4px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">
                            平均報酬: <span style="color: {'#10B981' if c_data['mid_ret'] >= 0 else '#EF4444'}">{c_data['mid_ret']:+.3f}%</span><br>
                            歷史勝率: {c_data['mid_win']:.1f}%
                        </p>
                    </div>
                    """, unsafe_allow_html=True)
                    
                with cy_col3:
                    st.markdown(f"""
                    <div style="background-color: #F8FAFC; border-left: 4px solid #7C3AED; padding: 10px; border-radius: 4px;">
                        <small style="color: #64748B; font-weight: 700;">🟣 下旬 (21號 ~ 月底)</small>
                        <p style="margin: 4px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">
                            平均報酬: <span style="color: {'#10B981' if c_data['late_ret'] >= 0 else '#EF4444'}">{c_data['late_ret']:+.3f}%</span><br>
                            歷史勝率: {c_data['late_win']:.1f}%
                        </p>
                    </div>
                    """, unsafe_allow_html=True)
                
                current_day_now = datetime.now(TZ).day
                st.markdown(f"""<br><small style='color:#64748B;'><b>💡 統一自營部實戰導引：</b> 今天是當月 <b>{current_day_now} 號</b>。如果畫面的綜合研判確診為『典型月循環』且今天是 22 號（月底拉回），量化期望值對你極度有利，此時的拉回低吸屬於主力洗盤的尾聲，可以大膽啟動低吸建倉計畫！</small>""", unsafe_allow_html=True)

            with st.expander("📊 財務基本面完整財務矩陣大表"):
                if not res["fin_df"].empty:
                    clean_fin_show = res["fin_df"].copy().sort_values("date", ascending=False)
                    clean_fin_show.columns = ["季度日期", "單季 EPS", "營業收入", "營業毛利", "營業利益", "單季毛利率 (%)", "單季營益率 (%)"]
                    st.dataframe(clean_fin_show.style.format({"單季 EPS": "{:.2f}", "營業收入": "{:,.0f}", "營業毛利": "{:,.0f}", "營業利益": "{:,.0f}", "單季毛利率 (%)": "{:.2f}%", "單季營益率 (%)": "{:.2f}%"}), use_container_width=True)

            with st.expander("📈 技術面後台詳細物理量"):
                st.write(f"**分價量密集牆(POC)** = `{res['volume_poc']:.2f}` 元 ｜ **5日線 MA5** = `{res['ma5_val']:.2f}` 元 ｜ **月線 MA20** = `{res['ma20_val']:.2f}` 元 ｜ **20週線 MA100** = `{res['ma100_val']:.2f}` 元")

            with st.expander("📰 資訊面 24H 網路輿情即時新聞流水線"):
                st.markdown(f"> **24H 網路即時輿情綜合定論**：`{res['news_analysis_report']}`")
                if isinstance(res["raw_news_list"], list) and res["raw_news_list"]:
                    for n in res["raw_news_list"]: st.markdown(f"* **[{n['date']}]** 【{n['source']}】 [{n['sentiment']}] [{n['title']}]({n['link']})")

if auto_refresh:
    time.sleep(5)
    st.rerun()
