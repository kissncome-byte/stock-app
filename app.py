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
        {"stock_id": "8069", "stock_name": "元太", "type": "two", "industry_category": "光電業"},
        {"stock_id": "2409", "stock_name": "友達", "type": "twse", "industry_category": "光電業"},
        {"stock_id": "3481", "stock_name": "群創", "type": "twse", "industry_category": "光電業"},
        {"stock_id": "3008", "stock_name": "大立光", "type": "twse", "industry_category": "光電業"},
        {"stock_id": "3406", "stock_name": "玉晶光", "type": "twse", "industry_category": "光電業"},
        {"stock_id": "2393", "stock_name": "億光", "type": "twse", "industry_category": "光電業"},
        {"stock_id": "2382", "stock_name": "廣達", "type": "twse", "industry_category": "電腦及週邊設備業"}
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
            vol_col = 'Trading_money' if 'Trading_money' in df.columns else 'vol' if 'vol' in df.columns else df.columns[-1]
            df['vol_work'] = pd.to_numeric(df[vol_col], errors='coerce').fillna(0)
            df['MA20_Vol'] = df['vol_work'].rolling(20).mean()
            last, prev = df.iloc[-1], (df.iloc[-5] if len(df) >= 5 else df.iloc[0])
            ret = ((last['close'] - prev['close']) / prev['close']) * 100
            panic = (last['close'] < last['MA20']) and (ret <= -3.5)
            bias = ((last['close'] - last['MA60']) / last['MA60']) * 100
            market_vol_healthy = float(last['vol_work']) >= float(last['MA20_Vol'])
            market_vol_desc = "🟢 大盤資金大部隊在線（大盤實質總血量高於20日均量）" if market_vol_healthy else "🚨 大盤量能窒息流失（大盤缺血假突破率高）"
            if panic: return False, f"🚨 大盤瀑布重挫 ({last['close']:.1f})，近週跌 {ret:.1f}%【補跌危機】", True, False, market_vol_healthy, market_vol_desc
            if bias >= 8.5: return True, f"⚠️ 大盤過熱警告 ({last['close']:.1f})，季線正乖離 {bias:.1f}%【強制控量】", False, True, market_vol_healthy, market_vol_desc
            macro_bull = last['close'] >= last['MA20']
            macro_text = f"加權指數 ({last['close']:.1f}) 站穩 20MA 多頭常態" if macro_bull else f"加權指數 ({last['close']:.1f}) 跌破 20MA 空方警戒"
            return macro_bull, macro_text, False, False, market_vol_healthy, market_vol_desc
    except Exception: pass
    return True, "🟢 多頭常態 (數據獲取受限，開啟寬鬆保護)", False, False, True, "🟢 常態安全血量"

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
    try: return get_api().taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"))
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
    if df_hist is None or len(df_hist) < 90: return {"verdict": "📊 歷史數據不足，無法進行週期因果解耦", "early_win": 50, "mid_win": 50, "late_win": 50, "early_ret": 0, "mid_ret": 0, "late_ret": 0, "macro_season": "未知"}
    x = df_hist.copy().sort_values("date")
    x["return"] = x["close"].pct_change()
    x = x.dropna(subset=["return"])
    x["month"] = pd.to_numeric(pd.to_datetime(x["date"]).dt.month, errors="coerce")
    x["day"] = pd.to_numeric(pd.to_datetime(x["date"]).dt.day, errors="coerce")
    early_period, mid_period, late_period = x[x["day"] <= 10], x[(x["day"] > 10) & (x["day"] <= 20)], x[x["day"] > 20]
    
    def get_period_stats(p_df):
        if p_df.empty: return 0.0, 50.0
        return float(p_df["return"].mean() * 100), float((p_df["return"] > 0).mean() * 100)
    
    e_ret, e_win = get_period_stats(early_period)
    m_ret, m_win = get_period_stats(mid_period)
    l_ret, l_win = get_period_stats(late_period)
    current_month, current_day = datetime.now(TZ).month, datetime.now(TZ).day
    
    if current_month in [3, 6, 9, 12]:
        macro_season, macro_bias = ("🚨 季底法人清算結帳期 (極高風險)", "⚠️ 注意：當前正值季底最後結帳清算。若個股拉回，極可能是法人踩踏棄養，月循環的低吸信號在此處特許失效，嚴禁高倉位接飛刀！") if current_day >= 18 else ("🔥 季底法人績效作帳衝刺期", "💡 提示：正值季底法人作帳衝刺。資金會極端往 RS 強勢股報團，弱勢股會被當作提款機，強弱極度分化。")
    elif current_month in [1, 4, 7, 10]: macro_season, macro_bias = "🌱 新季度資金重新配置期 (作夢行情起跑)", "💡 提示：新季度剛開始，法人資金大洗牌、重新尋找新題材建倉。此時若配合『上旬營收利多公告』，很容易放量啟動波段新主升浪。"
    else: macro_season, macro_bias = "⚖️ 季度中繼常態換手期", "觀察提示：市場回歸常態產業基本面對位，沒有極端的作帳 or 清算壓力，日曆統計的慣性準確度最高。"

    if e_ret > 0.05 and l_ret < -0.05 and e_win >= 53.0 and l_win <= 47.0: base_verdict = "🦅 **典型月循環**：【月初吸金拉抬 ➔ 月底賣壓壓低】。"
    elif l_ret > 0.05 and e_ret < -0.05 and l_win >= 53.0 and e_win <= 47.0: base_verdict = "⚡ **逆向月循環**：【月底提前卡位 ➔ 月初開高出貨】。"
    elif e_win >= 55.0 and m_win >= 55.0 and l_win >= 55.0: base_verdict = "🔥 **全月多頭報團**：此股歷史上極易受大資金連續鎖碼，日曆天數雜訊低。"
    else: base_verdict = "⚖️ **隨機常態波動**：歷史日曆慣性不明顯，回歸常態量價防線指標。"
        
    return {"verdict": f"【宏觀季節】：{macro_season}\n\n{macro_bias}\n\n---\n\n【微觀日曆慣性】：{base_verdict}", "early_ret": e_ret, "early_win": e_win, "mid_ret": m_ret, "mid_win": m_win, "late_ret": l_ret, "late_win": l_win, "macro_season": macro_season}

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
    x["RSI14"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=13, adjust=False).mean() / delta.clip(upper=0).ewm(com=13, adjust=False).mean().replace(0, -0.00001).abs())))
    x["RSI5"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=4, adjust=False).mean() / delta.clip(upper=0).ewm(com=4, adjust=False).mean().replace(0, -0.00001).abs())))
    x["RSI10"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=9, adjust=False).mean() / delta.clip(upper=0).ewm(com=9, adjust=False).mean().replace(0,
