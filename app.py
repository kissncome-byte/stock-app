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
            macro_bias = "⚠️ 注意：當前正值季底最後結帳清算。若個股拉回，極局可能是法人踩踏棄養，月循環的低吸信號在此處特許失效，嚴禁高倉位接飛刀！"
        else:
            macro_season = "🔥 季底法人績效作帳衝刺期"
            macro_bias = "💡 提示：正值季底法人作帳衝刺。資金會極端往 RS 強勢股報團，弱勢股會被當作提款機，強弱極度分化。"
    elif current_month in [1, 4, 7, 10]:
        macro_season = "🌱 新季度資金重新配置期 (作夢行情起跑)"
        macro_bias = "💡 提示：新季度剛開始，法人資金大洗牌、重新尋找新題材建倉。此時若配合『上旬營收利多公告』，很容易放量啟動波段新主升浪。"
    else:
        macro_season = "⚖️ 季度中繼常態換手期"
        macro_bias = "觀察提示：市場回歸常態產業基本面對位，沒有極端的作帳 or 清算壓力，日曆統計的慣性準確度最高。"

    if e_ret > 0.05 and l_ret < -0.05 and e_win >= 53.0 and l_win <= 47.0:
        base_verdict = "🦅 **典型月循環**：【月初吸金拉抬 ➔ 月底賣壓壓低】。"
    elif l_ret > 0.05 and e_ret < -0.05 and l_win >= 53.0 and e_win <= 47.0:
        base_verdict = "⚡ **逆向月循環**：【月底提前卡位 ➔ 月初開高出貨】。"
    elif e_win >= 55.0 and m_win >= 55.0 and l_win >= 55.0:
        base_verdict = "🔥 **全月多頭報團**：此股歷史上極易受大資金連續鎖碼，日曆天數雜訊低。"
    else:
        base_verdict = "⚖️ **隨機常態波動**：歷史日曆慣性不明顯，回歸常態量價防線指標。"
        
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

# ============ 8. 統一狼王策略決策大腦模型 ============
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
    
    short_trend = res_dict.get("stable_short_trend", "")
    market_vol_healthy = res_dict.get("market_vol_healthy", True)
    is_box_compressed = res_dict.get("is_box_compressed", False)
    wolf_rank_label = res_dict.get("wolf_rank_label", "常態輪動")
    
    k9, d9 = df_hist["K9"].iloc[-1], df_hist["D9"].iloc[-1]
    p20_max = float(df_hist["close"].tail(20).max())
    trailing_stop = p20_max - (2.5 * atr)
    r_low_10d = float(df_hist["low"].tail(10).min())
    
    f_good = "【財報年增擴張】" in res_dict["fin_conclusion"] or res_dict["latest_yoy"] >= 20
    c_lock = "強力鎖碼" in res_dict["sitc_trend"] or res_dict["sitc_3d_sum"] > 500
    is_ai_momentum = (p > m20) and c_lock and res_dict["vol_spike"]
    is_rs_gold = res_dict["is_rs_gold"]                     
    is_volume_gap_spike = res_dict["is_volume_gap_spike"]  
    
    # 🌟 鋼鐵修復地雷 1：將原本誤寫的 JS 語法 && 徹底替換成 Python 標準 logic運算子 'and'
    pnl_pct = ((p - entry_cost) / entry_cost * 100) if (is_holding and entry_cost > 0) else 0.0

    if is_holding and entry_cost > 0:
        if pnl_pct <= -7.0:
            return {
                "strategy_name": "🚨 觸發硬性資本停損", "color": "#FF4B4B", "action_now": "🛑 🔴 【部位重傷：全額立刻清倉】", "signal": "本金敞口破防",
                "desc": f"您成本為 {entry_cost:.2f} 元。目前帳面虧損達 {pnl_pct:.1f}%，已觸發自營部硬性清算底線，立刻執行全額市價離場！",
                "blueprint": {"停損防守": f"本金死穴 {entry_cost * 0.93:.2f} 元", "移動停利": "無", "預期目標": "保全資金殘餘"}
            }

        is_fresh_trade = (abs(pnl_pct) <= 1.5) and (is_volume_gap_spike or is_rs_gold or (p >= r * 0.95))
        if is_fresh_trade:
            return {
                "strategy_name": "🌱 新開倉動能蜜月期保護", "color": "#10B981", 
                "action_now": "🟢 🟢 【全新部位：給予空間讓子彈飛】", "signal": "觸發防甩轎保護機制",
                "desc": f"**【新兵蜜月保護】**：您目前成本為 {entry_cost:.2f} 元（目前損益：{pnl_pct:+.2f}%）。風控模組已強制鎖定『蜜月保護盾』——**自動幫你屏蔽 5MA 等短線減碼雜訊，嚴禁一買進就被洗下車！** 雙手綁起來，全額現股咬死，保險絲死守核心底線！",
                "blueprint": {
                    "停損防守": f"核心資本死穴 {entry_cost * 0.93:.2f} 元 ｜ 戰術防線 {trailing_stop:.2f} 元", 
                    "移動停利": "剛進場拒絕盲目減碼（保護盾開啟中）", 
                    "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"
                }
            }

        if pnl_pct >= 15.0 and st_type == "RIGHT_BREAKOUT" and res_dict["vol_spike"] and not sector_panic:
            return {
                "strategy_name": "🔮 獲利擴張：金字塔加碼劇本發動", "color": "#7D3CFF", "action_now": "🔮 🔮 【利潤奔跑：啟動金字塔多頭加碼開火】", "signal": "主升段中繼暴量突圍前高牆",
                "desc": f"**【老股東利潤擴張】**：初始持股成本為 {entry_cost:.2f} 元，目前大賺 {pnl_pct:+.1f}%。個股爆發量能突圍前高牆 {r:.2f} 元！立即執行金字塔式加碼買進！",
                "blueprint": {"停損防守": f"加碼部位守 5MA ({ma5:.2f} 元)", "移動停利": f"母部位續守 ATR ({trailing_stop:.2f} 元)", "預期目標": f"目標看擴張位 {res_dict['target_brk']:.2f} 元"}
            }

        if sector_panic and not is_rs_gold:
            return {
                "strategy_name": "🚨 族群共振危機防禦", "color": "#EF4444", "action_now": "🚨 🔴 【同族群崩盤：立即執行全面減碼 50%】", "signal": "板塊流動性集體踩踏",
                "desc": f"您持股成本為 {entry_cost:.2f} 元。同族群龍頭股集體下殺破 5%。請先落袋 50% 鎖定短線價差防身！",
                "blueprint": {"停損防守": f"剩餘部位守 {trailing_stop:.2f} 元", "移動停利": "已提前防守減碼", "預期目標": "保全核心資產"}
            }

        if p < trailing_stop:
            return {
                "strategy_name": "⏳ 中線波段趨勢終結", "color": "#EF4444", "action_now": "🛑 🔴 【波段獲利終結/結構破防：全額清倉】", "signal": "跌破動態 ATR 波動防線",
                "desc": f"持股成本為 {entry_cost:.2f} 元（帳面損益：{pnl_pct:+.1f}%）。即時價已實質跌破中線結構防禦線 ({trailing_stop:.2f} 元)。防線已被擊穿，請全額清倉退場避險！",
                "blueprint": {"停損防守": "全額清倉離場", "移動停利": "已觸發", "預期目標": "資金全額退場"}
            }

        if pnl_pct >= 5.0 and p < ma5 and "短期多頭波段" not in short_trend:
            return {
                "strategy_name": "🚀 短線達標・子部位獲利落袋", "color": "#F59E0B", "action_now": "⚠️ 🟡 【短線轉弱：減碼 50% 鎖定價差，剩餘放飛】", "signal": "股價跌破 5MA 短線攻擊線",
                "desc": f"成本為 {entry_cost:.2f} 元。個股短線衝刺速率減緩且主趨勢線有放緩跡象，實質跌破 5MA。立即執行「現股賣出 50% 倉位」，鎖定大價差！",
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
                "strategy_name": "🔥 強勢主升浪完美續抱", "color": "#7D3CFF", "action_now": "🔮 🔮 【強勢狂飆 : 全額持股續抱】", "signal": "短長雙速動能多頭共振",
                "desc": f"**【部位對位定錨】**：持股成本 {entry_cost:.2f} 元（帳面獲利：{pnl_pct:+.1f}%）。個股短期主趨勢線健康且完美運行於 5MA 之上。量價結構健康，盤中任何價格回落皆為洗盤雜訊，全額咬死不賣，放飛波段暴利！",
                "blueprint": {"停損防守": f"本金死穴 {entry_cost * 0.93:.2f} 元", "移動停利": f"守 ATR 防線 ({trailing_stop:.2f} 元)", "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"}
            }
        else:
            return {
                "strategy_name": "🛡️ 虧損被動防守緩衝區", "color": "#1C86EE", "action_now": "⚖️ 🔵 【微幅套牢：屬於正常波動容忍，持股續抱】", "signal": "未破結構技術底線",
                "desc": f"持股成本 {entry_cost:.2f} 元（浮虧：{pnl_pct:.1f}%）。當前浮虧完全處於量化模型的良性波動容忍帶內。主趨勢並未轉惡，請保持續抱！",
                "blueprint": {"停損防守": f"技術防線 {trailing_stop:.2f} 元", "移動停利": "暫無利潤可鎖", "預期目標": f"目標看前高牆 {r:.2f} 元"}
            }

    else:
        is_momentum_decelerate = (k9 < d9) and k9 > 75
        
        if "🚨 季底法人清算結帳期" in res_dict.get("macro_season", ""):
            return {
                "strategy_name": "🚨 季底流度性暴風雨防禦", "color": "#FF4B4B", 
                "action_now": "🛑 🔴 【環境極端風險：全新開倉嚴禁開火】", "signal": "投信結帳踩踏期震盪",
                "desc": "**【風控最高警告】**：當前正處於季底法人集體清算、清庫存、倒貨的瘋狂結帳期。即使該股個股型態再漂亮，量化大腦一票否決 any 全新買進交易，手握現金，拒絕在季底當接盤俠！",
                "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "保全現金等待新季度開跑"}
            }
        
        if "落後跟屁蟲" in wolf_rank_label and 'RIGHT_BREAKOUT' in st_type:
            return {
                "strategy_name": "🚨 狼王位階風控：否決跟風開倉", "color": "#FF4B4B", "action_now": "🛑 🔴 【嚴禁開火：該標的為族群落後跟屁蟲】", "signal": "資金分化排斥效應",
                "desc": "大腦精算顯示該股在同產業族群中屬於**落後跟屁蟲**。主力資金正在瘋狂往真正的領頭羊抱團，買進跟風股隨時面臨補跌拉回風險。大腦一票否決！",
                "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "手握現金，要買就去買真正最強的隊長"}
            }
        
        if is_rs_gold and p >= m20 and not sector_panic:
            return {
                "strategy_name": "🚀 統一特許：逆境黃金飆股劇本發動", "color": "#7D3CFF", "action_now": "🔮 🔮 【強者恆強：無視大盤恐慌立即開火】", "signal": "個股超額相對強度（RS）爆表",
                "desc": f"大盤目前破位下殺。但該個股今日發射高達 {res_dict['relative_strength']:.1f}% 的超額相對強度(RS)！特許全額開火權！",
                "blueprint": {"停損防守": f"昨日收盤價 or 當日低點", "移動停利": f"波動防線 {trailing_stop:.2f} 元", "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"}
            }

        if is_volume_gap_spike and p >= m20 and not sector_panic:
            return {
                "strategy_name": "⚡ 突擊劇本：09:15 早盤量能斷層發動", "color": "#10B981", "action_now": "🔮 🟢 【量能斷層確立：全新開火進場熱錢追擊】", "signal": "開盤特大法人單極速掃貨",
                "desc": "該個股早盤爆發特大法人單不計價掃貨（量能斷層）。自動無視指標死叉，啟動狼王突擊買進指令！",
                "blueprint": {"停損防守": f"開盤第一盤最低價 ｜ 戰術防線 {trailing_stop:.2f} 元", "移動停利": "無", "預期目標": f"短線價差衝刺目標 {res_dict['target_brk']:.2f} 元"}
            }

        if st_type == "RIGHT_BREAKOUT":
            if is_momentum_decelerate:
                return {
                    "strategy_name": st_name, "color": "#F59E0B", "action_now": "⚠️ 🟡 【全新開倉指標過熱：暫緩追高觀望】", "signal": "短線指標高位修正雜訊",
                    "desc": "您目前空倉。個股型態強勢但隨機指標出現高位死叉。請暫緩追高，等待拉回 5MA 再行開火。",
                    "blueprint": {"停損防守": "嚴禁盲目進場", "移動停利": "無", "預期目標": f"靜待突破壓制牆 {r:.2f} 元"}
                }
            
            if not market_vol_healthy:
                return {
                    "strategy_name": "🚨 大盤量能失血：假突破防禦機制", "color": "#F59E0B", "action_now": "⚠️ 🟡 【大盤總血量不足：強制削減60%防守型開火】", "signal": "流動性窒息枯竭警告",
                    "desc": f"個股型態雖然觸發右側突破，但此時大盤實質成交總血量低於20日均量線。在缺血市場中，主力的突破有高達 70% 的機率是騙線。風控模組強制將你的資金配額砍掉 60%！",
                    "blueprint": {"停損防守": f"收盤破前高牆 {r:.2f} 元立刻認賠", "移動停利": "防守型控量", "預期目標": "賺取日內小價差即走"}
                }
            
            if is_box_compressed:
                return {
                    "strategy_name": "🔮 波動極致壓縮：老主力築底大底爆發", "color": "#7D3CFF", "action_now": "🔮 🔮 【蓄勢火山突破：特許放大1.5倍重倉爆發開火】", "signal": "30日大底時間縱深完美共振",
                    "desc": f"這檔股票在過去 30 天內，高低價差驚人地收斂在 {res_dict['box_width_pct']:.1f}% 的極致地獄狹幅箱型內！籌碼高度集中於法人手中。今日帶量斜率突破，大腦特許你放大 1.5 倍資金配置！",
                    "blueprint": {"停損防守": f"收盤跌破箱型上軌 {r:.2f} 元", "移動停利": f"守 5MA 攻擊線", "預期目標": f"長線翻倍目標對位 {res_dict['target_brk']:.2f} 元"}
                }

            if not m_safe:
                if "短期多頭波段" in short_trend or (is_ai_momentum and f_good and not sector_panic):
                    return {
                        "strategy_name": st_name + " (🛡️ 趨勢線多頭放行單)", "color": "#10B981", 
                        "action_now": "🔮 🟢 【短期趨勢多頭向上：允許全新開火建倉】", "signal": "主趨勢線斜率向上共振",
                        "desc": "大盤環境雖不安全，但量化雷達解碼顯示，該股的 5日短期主趨勢線正集體強勢向上翹起。大腦解除警報，特許釋放全新開倉權！",
                        "blueprint": {"停損防守": f"收盤跌破關鍵支撐牆 {m20:.2f} 元", "移動停利": f"波動防線 {trailing_stop:.2f} 元", "預期目標": f"獲利對位 {res_dict['target_brk']:.2f} 元"}
                    }
                else:
                    return {
                        "strategy_name": st_name, "color": "#FF4B4B", "action_now": "🚨 🔴 【趨勢下彎且環境高風險：嚴禁開火】", "signal": "總體大盤與個股短期趨勢雙重破防",
                        "desc": "大盤失守生命線，且該股短期主趨勢線已實質躺平或下彎，上方怨魂開始向下清算。一票否決新交易！",
                        "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "手握現金等待安全期"}
                    }

            if p >= r * 0.98 and res_dict["vol_spike"] and c_lock and f_good and not sector_panic:
                if overextended:
                    return {
                        "strategy_name": st_name, "color": "#F59E0B", "action_now": "⚠️ 🟡 【大盤過熱：全新開倉防守型控量開火】", "signal": "⚡ 瘋狗浪末段逆勢突破",
                        "desc": "個股達成完美共振！但大盤正乖離率過熱。解鎖全新開火權，但強制削減 50% 資金配置！",
                        "blueprint": {"停損防守": f"收盤跌破 {r:.2f} 元", "移動停利": f"即時價破 {trailing_stop:.2f} 元", "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"}
                    }
                return {
                    "strategy_name": st_name, "color": "#7D3CFF", "action_now": "🔮 🔮 【頂級信號：全新多頭建倉開火】", "signal": "🔮 頂級多頭共振：黃金主升飆股型態發動",
                    "desc": "基本面擴張、法人強力鎖碼、帶量突破前高牆，適合執行全新多頭開火建倉！",
                    "blueprint": {"停損防守": f"收盤跌破前高壓力牆 {r:.2f} 元", "移動停利": f"波動防線 {trailing_stop:.2f} 元", "預期目標": f"獲利擴張目標對位 {res_dict['target_brk']:.2f} 元"}
                }

        elif st_type == "LEFT_SPRING" and not sector_panic:
            if "短期空頭修正" in short_trend:
                return {
                    "strategy_name": "🛡️ 左側接飛刀遭無情否決", "color": "#FF4B4B", "action_now": "🚨 🔴 【主趨勢蓋頭下彎：嚴禁盲目左側低吸】", "signal": "拒絕逆勢接刀",
                    "desc": "該股雖然在分價量密集牆附近，但當下個股 5日短期主趨勢線正集體向下修正。大腦一票否決任何低吸嘗試，空倉觀望！",
                    "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "手握現金防止抄底重傷"}
                }
            
            if "買點一成立" in res_dict["spring_verdict"]:
                return {
                    "strategy_name": st_name, "color": "#10B981", "action_now": "🟢 🟢 【破底翻確立：允許精密低吸進場】", "signal": "結構洗盤完成、安全邊際高",
                    "desc": f"{res_dict['spring_verdict']} 浮額遭主力洗淨。此進場建立初始防守型頭寸。",
                    "blueprint": {"停損防守": f"硬性死穴防線 {r_low_10d:.2f} 元", "移動停利": "無", "預期目標": f"反彈停利目標看 {res_dict['target_pb']:.2f} 元"}
                }

        return {
            "strategy_name": "💤 空倉常態觀望", "color": "#64748B", "action_now": "⚖️ 🔵 【常態調整區 : 保持空倉耐心等待】", "signal": "進入量化緩衝帶",
            "desc": "個股處於無方向性的箱型整理區，請保持空倉觀望。",
            "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "等待金流重啟點火"}
        }

# ============ 9. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int, is_holding=False, entry_cost=0.0, sector_panic=False):
    res_dict = {}
    latest_yoy = 0.0
    
    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    
    if match.empty:
        stock_name = f"代號 {stock_id}"
        industry = "自訂追蹤板塊"
        market_type = "TWO" if (stock_id.startswith("3") or stock_id.startswith("5") or stock_id.startswith("6") or stock_id.startswith("8")) and len(stock_id) == 4 else "TSE"
    else:
        m_col = "type" if "type" in match.columns else "market_type" if "market_type" in match.columns else "market" if "market" in match.columns else None
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
            
    res_dict["stock_id"] = stock_id
    res_dict["stock_name"] = stock_name
    res_dict["industry"] = industry
    
    df_raw = get_daily_df(stock_id, market_type=market_type, days=450)
    if df_raw is None or df_raw.empty: return None

    macro_bull, macro_desc, is_market_panic, is_market_overextended, market_vol_healthy, market_vol_desc = get_market_macro_status()
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

    # ==================== 5日線動態斜率防震核心 ====================
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

    ma5_today = float(df["MA5"].iloc[-1])
    ma5_3days_ago = float(df["MA5"].iloc[-3]) if len(df) >= 3 else ma5_today
    ma5_slope = ((ma5_today - ma5_3days_ago) / ma5_3days_ago * 100) if ma5_3days_ago > 0 else 0.0

    if ma5_slope > 0.15:
        stable_short_trend = "🟢 短期多頭波段（結構穩固，忽略一日拉回）"
        stable_short_color = "#10B981"
        stable_short_desc = "5日主力成本線集體向上。不管單日如何震盪、有無破線，大部隊集體趨勢並未改變。請保持定力！"
    elif ma5_slope < -0.15:
        stable_short_trend = "🔴 短期空頭修正（上方有壓，防禦觀望）"
        stable_short_color = "#EF4444"
        stable_short_desc = "5日主力成本線集體下彎。短期多頭動能退潮，上方套牢怨魂沉重。即便盤中反彈，也切勿追高！"
    else:
        stable_short_trend = "🟡 短期箱型潛伏（橫盤整理，多看少動）"
        stable_short_color = "#F59E0B"
        stable_short_desc = "5日線處於水平躺平狀態。股價原地亂晃屬於常態。大腦叫你『把手綁起來』，別在此處被來回打巴掌。"

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

    if relative_strength > 4.0 and sitc_3d_sum > 300:
        wolf_rank_label = "👑 族群領頭狼王（主導資金絕對攻勢）"
        wolf_rank_color = "#7D3CFF"
    elif relative_strength < -2.0:
        wolf_rank_label = "🐌 族群落後跟屁蟲（嚴防資金棄養踩踏）"
        wolf_rank_color = "#EF4444"
    else:
        wolf_rank_label = "⚖️ 族群常態輪動成員（隨大盤溫和浮動）"
        wolf_rank_color = "#64748B"

    close_tail30 = df["close"].tail(30)
    max_30d = float(close_tail30.max())
    min_30d = float(close_tail30.min())
    box_width_pct = ((max_30d - min_30d) / min_30d) * 100
    is_box_compressed = box_width_pct <= 8.5

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
            
            # 🌟 強制轉型防禦網：全面根除巢狀單行條件式，完美收網
            try: latest_yoy_val = float(latest_yoy)
            except Exception: latest_yoy_val = 0.0

            if latest_yoy_val >= 30.0: db_t, dc_t = 55.0, 22.0
            elif latest_yoy_val >= 15.0: db_t, dc_t = 45.0, 18.0
            else: db_t, dc_t = 35.0, 13.0
                
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

    # 🌟 鋼鐵修復地雷 2：將在先前版本中被洗掉的 stop_line_text 核心計算式完美焊回前線！
    stop_line_text = f"{round_to_tick(peak_price_20d - (2.5 * atr), t):.2f} 元"

    # 🌟 鋼鐵修復地雷 3：將大腦呼叫前必須用到的所有環境變數，第一時間、毫無保留提早灌進 res_dict
    res_dict["macro_bull"] = macro_bull
    res_dict["is_market_panic"] = is_market_panic
    res_dict["is_market_overextended"] = is_market_overextended
    res_dict["is_us_panic"] = is_us_panic
    res_dict["us_panic_desc"] = us_panic_desc
    res_dict["wtx_change"] = wtx_change
    res_dict["final_decision"] = final_decision
    res_dict["market_vol_healthy"] = market_vol_healthy
    res_dict["market_vol_desc"] = market_vol_desc
    res_dict["wolf_rank_label"] = wolf_rank_label
    res_dict["wolf_rank_color"] = wolf_rank_color
    res_dict["is_box_compressed"] = is_box_compressed
    res_dict["box_width_pct"] = box_width_pct
    
    cycle_res = analyze_calendar_cyclicality(df.copy())
    res_dict["calendar_verdict"] = cycle_res["verdict"]
    res_dict["calendar_data"] = cycle_res
    res_dict["macro_season"] = cycle_res["macro_season"]

    res_dict["target_brk"] = target_brk
    res_dict["stop_brk"] = stop_brk
    res_dict["rr1_brk"] = rr1_brk
    res_dict["target_pb"] = target_pb
    res_dict["stop_pb"] = stop_pb
    res_dict["rr1_pb"] = rr1_pb
    res_dict["trailing_stop_line"] = stop_line_text

    # 疏通後台，引導策略大腦放行
    tactical_blueprint = unified_institutional_brain(res_dict, df.copy(), is_holding=is_holding, entry_cost=entry_cost, sector_panic=sector_panic)
    res_dict["tactical_blueprint"] = tactical_blueprint
    
    expected_stop_price = target_brk - (1.5 * atr) if "突破" in tactical_blueprint["strategy_name"] else stop_pb
    if "破底翻" in tactical_blueprint["strategy_name"] and ("買點一成立" in spring_verdict or "買點二成立" in spring_verdict):
        expected_stop_price = round_to_tick(spring_lowest_low - t, t) if round_to_tick(spring_lowest_low - t, t) < current_price else round_to_tick(current_price - (1.0 * atr), t)
        strategy_route = "🔮 破底翻底吸佈局/加倉劇本"
    else: strategy_route = "🚀 強勢突破前高劇本" if "突破" in tactical_blueprint["strategy_name"] else "🛡️ 均線拉回低吸劇本"

    adjusted_risk = risk_per_trade
    if "立即" in tactical_blueprint["action_now"] and "清倉" in tactical_blueprint["action_now"]: adjusted_risk = 0.0
    elif "🛑" in tactical_blueprint["action_now"] or "暫緩追高" in tactical_blueprint["action_now"]: adjusted_risk = 0.0 
    elif "防守型控量" in tactical_blueprint["action_now"]: adjusted_risk *= 0.4 
    elif "🔮" in tactical_blueprint["action_now"]: adjusted_risk *= 1.5 
    
    base_lots = min(int((total_capital * (adjusted_risk / 100) * 10000 / (current_price - expected_stop_price)) / 1000), int((total_capital * 10000) / (current_price * 1000))) if (current_price - expected_stop_price > 0 and adjusted_risk > 0) else 0
    
    if "加碼" in tactical_blueprint["action_now"]:
        suggested_lots = max(1, int(base_lots * 0.5))
        res_dict["is_pyramid_order"] = True
    else:
        suggested_lots = base_lots
        res_dict["is_pyramid_order"] = False
    
    max_safe_liquidity_lots = max(1, int(vol_ma5_val * 0.015))
    
    if suggested_lots > max_safe_liquidity_lots:
        suggested_lots = max_safe_liquidity_lots
        res_dict["liquidity_capped"] = True
    else:
        res_dict["liquidity_capped"] = False
    
    # 其餘物理變數完好補正裝箱
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
    res_dict["suggested_lots"] = suggested_lots
    res_dict["max_safe_liquidity_lots"] = max_safe_liquidity_lots
    res_dict["expected_stop_price"] = expected_stop_price
    res_dict["strategy_route"] = strategy_route
    res_dict["expected_target_price"] = target_brk if "突破" in tactical_blueprint["strategy_name"] or "加碼" in tactical_blueprint["action_now"] or "暫緩追高" in tactical_blueprint["action_now"] else target_pb
    res_dict["atr"] = atr
    res_dict["stock_daily_pct"] = stock_daily_pct
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
    res_dict["stable_short_trend"] = stable_short_trend
    res_dict["stable_short_color"] = stable_short_color
    res_dict["stable_short_desc"] = stable_short_desc

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

macro_bull, macro_label, is_market_panic, is_market_overextended, _, _ = get_market_macro_status()
full_info_df = get_stock_info_df()

st.markdown("## 📡 雙速策略大腦動態綜合看盤台 (v48 狼王特選版)")
st.markdown("### 🎛️ 戰術總指揮中心 (Command Center)")

st.markdown("""<div style='background-color:#EFF6FF; padding:10px; border-radius:6px; border-left:4px solid #3B82F6; margin-bottom:12px;'><b style='color:#1E40AF; font-size:14px;'>🎯 個股五維度縱向因果深度診斷與策略開火</b></div>""", unsafe_allow_html=True)

stock_input = st.text_input("請輸入你想診斷的核心目標個股代碼（例如廣達 2382、欣興 3037）：", value="3037")

st.markdown("""<div style='background-color:#FFFBEB; padding:12px; border-radius:6px; border: 1px solid #FCD34D; margin-bottom:12px;'>""", unsafe_allow_html=True)
u_col1, u_col2 = st.columns(2)
with u_col1:
    user_holding = st.checkbox("📊 我目前手中「已持有」此個股", value=False)
with u_col2:
    user_cost = st.number_input("每股真實持股成本 (元)", value=0.0, step=1.0, min_value=0.0, disabled=not user_holding)
st.markdown("""</div>""", unsafe_allow_html=True)

diag_trigger = st.button("🔥 立即執行精密大腦雙速成本定錨診斷", use_container_width=True)
st.markdown("---")

if diag_trigger or stock_input:
    st.cache_data.clear()
    
    with st.spinner("五維度大腦深度因果解耦中..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input, is_holding=user_holding, entry_cost=user_cost, sector_panic=sector_panic_toggle)
        if res is None: 
            st.error("該個股代碼數據獲取失敗，請確認編號是否正確（數據歷史長度需大於100日）。")
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

            st.markdown("### 🧬 機構級多因子結構縱深大數據曝光面板")
            ib_col1, ib_col2, ib_col3 = st.columns(3)
            with ib_col1:
                st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:12px; border-radius:6px; min-height:100px; border-top:4px solid #3B82F6;"><span style="font-size:12px; color:#64748B; font-weight:700; display:block; margin-bottom:4px;">1. 總體流動性安全閥（實質總血量）</span><h4 style="margin:2px 0; color:#1E293B; font-size:15px; font-weight:800;">{res['market_vol_desc']}</h4><p style="margin:4px 0 0 0; font-size:11.5px; color:#475569; font-weight:500;">精算結果：突破單發動必須搭配大盤在線總血量，量能萎縮時將強制啟動假突破防禦網。</p></div>""", unsafe_allow_html=True)
            with ib_col2:
                st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:12px; border-radius:6px; min-height:100px; border-top:4px solid {res['wolf_rank_color']};"><span style="font-size:12px; color:#64748B; font-weight:700; display:block; margin-bottom:4px;">2. 產業板塊內部分化位階（狼王排序）</span><h4 style="margin:2px 0; color:{res['wolf_rank_color']}; font-size:15px; font-weight:800;">{res['wolf_rank_label']}</h4><p style="margin:4px 0 0 0; font-size:11.5px; color:#475569; font-weight:500;">精算結果：資金具有極端排擠效應。大腦特許領頭狼王暴量突進，並無情否決任何落後跟屁蟲的開倉。</p></div>""", unsafe_allow_html=True)
            with ib_col3:
                box_status_text = f"🔥 波動極致壓縮成立（近30日高低落差僅 {res['box_width_pct']:.1f}%）" if res['is_box_compressed'] else f"⚪ 箱型常態發散中（近30日高低落差 {res['box_width_pct']:.1f}%）"
                st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:12px; border-radius:6px; min-height:100px; border-top:4px solid #7C3AED;"><span style="font-size:12px; color:#64748B; font-weight:700; display:block; margin-bottom:4px;">3. 箱型籌碼時間縱深（橫有多長）</span><h4 style="margin:2px 0; color:#7C3AED; font-size:15px; font-weight:800;">{box_status_text}</h4><p style="margin:4px 0 0 0; font-size:11.5px; color:#475569; font-weight:500;">精算結果：橫向窄幅整理超過一個月即定義為主力鋼鐵大底，一旦帶量斜率突破，爆發期望值極高。</p></div>""", unsafe_allow_html=True)

            st.markdown(f"""<div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; border-left: 6px solid {res['stable_short_color']}; padding: 16px; border-radius: 6px; margin-top: 15px; margin-bottom: 15px;"><div style="display: flex; justify-content: space-between; align-items: center;"><span style="font-size: 13px; color: #64748B; font-weight: 800; letter-spacing: 0.05em;">⏱️ 週級別・短期波段主趨勢定錨面板</span><span style="background-color: {res['stable_short_color']}20; color: {res['stable_short_color']}; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 700;">防震過濾器開啟中</span></div><h4 style="margin: 8px 0; color: {res['stable_short_color']}; font-weight: 800; font-size: 18px;">當前定錨狀態：{res['stable_short_trend']}</h4><p style="margin: 0; color: #334155; font-size: 13.5px; line-height: 1.5;"><b>操盤手實戰導引：</b>{res['stable_short_desc']}</p></div>""", unsafe_allow_html=True)

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
            with b1: st.metric(label_text, f"{res['suggested_lots']} 張", "流動性與多因子縱深控制中")
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
                    st.markdown(f"""<div style="background-color: #F8FAFC; border-left: 4px solid #2563EB; padding: 10px; border-radius: 4px;"><small style="color: #64748B; font-weight: 700;">🟢 上旬 (1號 ~ 10號)</small><p style="margin: 4px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">平均報酬: <span style="color: {'#10B981' if c_data['early_ret'] >= 0 else '#EF4444'}">{c_data['early_ret']:+.3f}%</span><br>歷史勝率: {c_data['early_win']:.1f}%</p></div>""", unsafe_allow_html=True)
                with cy_col2:
                    st.markdown(f"""<div style="background-color: #F8FAFC; border-left: 4px solid #64748B; padding: 10px; border-radius: 4px;"><small style="color: #64748B; font-weight: 700;">🟡 中旬 (11號 ~ 20號)</small><p style="margin: 4px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">平均報酬: <span style="color: {'#10B981' if c_data['mid_ret'] >= 0 else '#EF4444'}">{c_data['mid_ret']:+.3f}%</span><br>歷史勝率: {c_data['mid_win']:.1f}%</p></div>""", unsafe_allow_html=True)
                with cy_col3:
                    st.markdown(f"""<div style="background-color: #F8FAFC; border-left: 4px solid #7C3AED; padding: 10px; border-radius: 4px;"><small style="color: #64748B; font-weight: 700;">🟣 下旬 (21號 ~ 月底)</small><p style="margin: 4px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">平均報酬: <span style="color: {'#10B981' if c_data['late_ret'] >= 0 else '#EF4444'}">{c_data['late_ret']:+.3f}%</span><br>歷史勝率: {c_data['late_win']:.1f}%</p></div>""", unsafe_allow_html=True)
                
                current_day_now = datetime.now(TZ).day
                st.markdown(f"""<br><small style='color:#64748B;'><b>💡 統一自營部實戰導引：</b> 今天是當月 <b>{current_day_now} 號</b>。如果綜合研判為『典型月循環』且適逢月底拉回，量化期望值對多頭極有利。</small>""", unsafe_allow_html=True)

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
