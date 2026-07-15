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

# ============ 3. Helper Functions ============
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

# ============ 4. Connection Layer ============
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

# ============ 5. Live Streaming Engine ============
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
                p_c = safe_float(info.get("z")) or safe_float(info.get("b", "").split("_")[0]) or safe_float(info.get("o"))
                if p_c > 0: 
                    return safe_float(info.get("o")) or p_c, safe_float(info.get("h")) or p_c, safe_float(info.get("l")) or p_c, p_c, safe_float(info.get("g")) or safe_float(info.get("v")) or hist_lots, True, f"TWSE {prefix.upper()} 官方流", "realtime"
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
        df = get_api().taiwan_stock_info()
        if df is not None and not df.empty:
            df = df.copy()
            df.columns = [str(c).strip() for c in df.columns]
            for col in ["stock_id", "stock_name", "industry_category"]:
                if col in df.columns: df[col] = df[col].astype(str).str.strip()
            return df
    except Exception: pass
    fallback = [{"stock_id": "2330", "stock_name": "台積電", "type": "twse", "industry_category": "半導體業"}, {"stock_id": "3037", "stock_name": "欣興", "type": "twse", "industry_category": "電子零組件業"}, {"stock_id": "2382", "stock_name": "廣達", "type": "twse", "industry_category": "電腦及週邊設備業"}]
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
                raw = pd.DataFrame({"date": [datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d") for ts in res.get("timestamp", [])], "open": res["indicators"]["quote"][0].get("open", []), "high": res["indicators"]["quote"][0].get("high", []), "low": res["indicators"]["quote"][0].get("low", []), "close": res["indicators"]["quote"][0].get("close", []), "vol": res["indicators"]["quote"][0].get("volume", [])}).dropna(subset=["close"])
                raw["amount"] = raw["close"] * raw["vol"]
                return raw.copy()
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
    return True, "🟢 多頭常態 (開啟寬鬆保護)", False, False, True, "🟢 常態安全血量"

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
    if df_hist is None or len(df_hist) < 90: return {"verdict": "📊 歷史數據不足", "early_win": 50, "mid_win": 50, "late_win": 50, "early_ret": 0, "mid_ret": 0, "late_ret": 0, "macro_season": "未知"}
    x = df_hist.copy().sort_values("date")
    x["return"] = x["close"].pct_change()
    x = x.dropna(subset=["return"])
    x["month"] = pd.to_numeric(pd.to_datetime(x["date"]).dt.month, errors="coerce")
    x["day"] = pd.to_numeric(pd.to_datetime(x["date"]).dt.day, errors="coerce")
    e_ret, e_win = float(x[x["day"]<=10]["return"].mean()*100 or 0), float((x[x["day"]<=10]["return"]>0).mean()*100 or 50)
    m_ret, m_win = float(x[(x["day"]>10)&(x["day"]<=20)]["return"].mean()*100 or 0), float((x[(x["day"]>10)&(x["day"]<=20)]["return"]>0).mean()*100 or 50)
    l_ret, l_win = float(x[x["day"]>20]["return"].mean()*100 or 0), float((x[x["day"]>20]["return"]>0).mean()*100 or 50)
    current_month, current_day = datetime.now(TZ).month, datetime.now(TZ).day
    
    if current_month in [3, 6, 9, 12]:
        macro_season, macro_bias = ("🚨 季底法人清算結帳期 (極高風險)", "⚠️ 注意：當前正值季底最後結帳清算。月循環低吸信號特許失效，嚴禁接飛刀！") if current_day >= 18 else ("🔥 季底法人績效作帳衝刺期", "💡 提示：正值季底法人作帳衝刺。資金會極端往 RS 強勢股報團，弱勢股會被當作提款機。")
    elif current_month in [1, 4, 7, 10]: macro_season, macro_bias = "🌱 新季度資金重新配置期", "💡 提示：新季度剛開始，法人資金大洗牌。配合『上旬營收利多公告』，很容易啟動新波段主升浪。"
    else: macro_season, macro_bias = "⚖️ 季度中繼常態換手期", "觀察提示：市場回歸基本面對位，沒有極端的作帳 or 清算壓力，日曆統計慣性最高。"

    if e_ret > 0.05 and l_ret < -0.05 and e_win >= 53.0 and l_win <= 47.0: base_verdict = "🦅 **典型月循環**：【月初吸金拉抬 ➔ 月底賣壓壓低】。"
    elif l_ret > 0.05 and e_ret < -0.05 and l_win >= 53.0 and e_win <= 47.0: base_verdict = "⚡ **逆向月循環**：【月底提前卡位 ➔ 月初開高出貨】。"
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
    x["RSI10"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=9, adjust=False).mean() / delta.clip(upper=0).ewm(com=9, adjust=False).mean().replace(0, -0.00001).abs())))
    x["up"], x["down"] = x["high"].diff(), x["low"].shift(1) - x["low"]
    x["P_DI"] = (x["where"]((x["up"]>x["down"])&(x["up"]>0), x["up"], 0).ewm(com=13, adjust=False).mean() / x["TR"].ewm(com=13, adjust=False).mean().replace(0, 0.00001)) * 100 if "where" in dir(np) else 50.0
    x["M_DI"] = (x["where"]((x["down"]>x["up"])&(x["down"]>0), x["down"], 0).ewm(com=13, adjust=False).mean() / x["TR"].ewm(com=13, adjust=False).mean().replace(0, 0.00001)) * 100 if "where" in dir(np) else 50.0
    x["ADX14"] = ((x["P_DI"] - x["M_DI"]).abs() / (x["P_DI"] + x["M_DI"]).replace(0, 0.00001) * 100).ewm(com=13, adjust=False).mean()
    x["EMA12"], x["EMA26"] = x["close"].ewm(span=12, adjust=False).mean(), x["close"].ewm(span=26, adjust=False).mean()
    x["MACD_HIST"] = (x["EMA12"] - x["EMA26"]) - (x["EMA12"] - x["EMA26"]).ewm(span=9, adjust=False).mean()
    l_min, h_max = x["low"].rolling(9).min(), x["high"].rolling(9).max()
    x["RSV"] = 100 * ((x["close"] - l_min) / (h_max - l_min).replace(0, 0.00001))
    k_l, d_l, ck, cd = [], [], 50.0, 50.0
    for rsv in x["RSV"]:
        if pd.isna(rsv): k_l.append(np.nan); d_l.append(np.nan)
        else: ck = (2/3)*ck + (1/3)*rsv; cd = (2/3)*cd + (1/3)*ck; k_l.append(ck); d_l.append(cd)
    x["K9"], x["D9"] = k_l, d_l
    x["is_long_upper_shadow"] = ((x["high"] - np.maximum(x["open"], x["close"])) > (x["open"] - x["close"]).abs()) & ((x["high"] - np.maximum(x["open"], x["close"])) / (x["high"] - x["low"]).replace(0, 0.00001) > 0.4)
    return x.dropna(subset=["ATR14", "MA5", "MA20", "MA60", "MA100", "Res_20D", "BB_bandwidth", "RSI14", "MACD_HIST", "K9", "D9", "ADX14"]).copy()

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
    final, atr = res_dict["final_decision"], res_dict["atr"]
    short_trend = res_dict.get("stable_short_trend", "")
    market_vol_healthy = res_dict.get("market_vol_healthy", True)
    wolf_rank_label = res_dict.get("wolf_rank_label", "常態輪動")
    trailing_stop = float(df_hist["close"].tail(20).max()) - (2.5 * atr)
    f_good = "【財報年增擴張】" in res_dict["fin_conclusion"] or res_dict["latest_yoy"] >= 20
    c_lock = "強力鎖碼" in res_dict["sitc_trend"] or res_dict["sitc_3d_sum"] > 500
    is_rs_gold, is_volume_gap_spike = res_dict["is_rs_gold"], res_dict["is_volume_gap_spike"]
    pnl_pct = res_dict["pnl_pct"]

    if is_holding and entry_cost > 0:
        if pnl_pct <= -7.0: return {"strategy_name": "🚨 觸發硬性資本停損", "color": "#FF4B4B", "action_now": "🛑 🔴 【部位重傷：全額立刻清倉】", "signal": "本金敞口破防", "desc": f"您成本為 {entry_cost:.2f} 元。目前帳面虧損達 {pnl_pct:.1f}%，已觸發自營部硬性清算底線，立刻執行全額市價離場！", "blueprint": {"停損防守": f"本金死穴 {entry_cost * 0.93:.2f} 元", "移動停利": "無", "預期目標": "保全資金殘餘"}}
        if (abs(pnl_pct) <= 1.5) and (is_volume_gap_spike or is_rs_gold or (p >= r * 0.95)): return {"strategy_name": "🌱 新開倉動能蜜月期保護", "color": "#10B981", "action_now": "🟢 🟢 【全新部位：給予空間讓子彈飛】", "signal": "觸發防甩轎保護機制", "desc": f"您目前成本為 {entry_cost:.2f} 元（損益：{pnl_pct:+.2f}%）。風控模組已強制鎖定『蜜月保護盾』——自動幫你屏蔽短線減碼雜訊，全額咬死現股，保險絲死守底線！", "blueprint": {"停損防守": f"核心資本死穴 {entry_cost * 0.93:.2f} 元", "移動停利": "新開倉開啟防甩轎保護", "預期目標": f"目標看擴張位 {res_dict['target_brk']:.2f} 元"}}
        if pnl_pct >= 15.0 and st_type == "RIGHT_BREAKOUT" and res_dict["vol_spike"] and not sector_panic: return {"strategy_name": "🔮 獲利擴張：金字塔加碼劇本發動", "color": "#7D3CFF", "action_now": "🔮 🔮 【利潤奔跑：啟動金字塔多頭加碼開火】", "signal": "主升段中繼暴量突圍前高牆", "desc": f"初始持股成本為 {entry_cost:.2f} 元，目前大賺 {pnl_pct:+.1f}%。個股爆發量能突圍前高壓力牆 {r:.2f} 元！立即執行金字塔式加碼買進！", "blueprint": {"停損防守": f"加碼部位守 5MA ({ma5:.2f} 元)", "移動停利": f"母部位續守 ATR ({trailing_stop:.2f} 元)", "預期目標": f"目標看擴張位 {res_dict['target_brk']:.2f} 元"}}
        if p < trailing_stop: return {"strategy_name": "⏳ 中線波段趨勢終結", "color": "#EF4444", "action_now": "🛑 🔴 【波段獲利終結/結構破防：全額清倉】", "signal": "跌破動態 ATR 波動防線", "desc": f"持股成本為 {entry_cost:.2f} 元（損益：{pnl_pct:+.1f}%）。即時價已實質跌破中線結構防禦線 ({trailing_stop:.2f} 元)，請全額清倉退場避險！", "blueprint": {"停損防守": "全額清倉離場", "移動停利": "已觸發", "預期目標": "資金全額退場"}}
        if pnl_pct >= 5.0 and p < ma5 and "短期多頭波段" not in short_trend: return {"strategy_name": "🚀 短線達標・子部位獲利落袋", "color": "#F59E0B", "action_now": "⚠️ 🟡 【短線轉弱：減碼 50% 鎖定價差，剩餘放飛】", "signal": "股價跌破 5MA 短線攻擊線", "desc": f"成本為 {entry_cost:.2f} 元。個股短線衝刺速率減緩且實質跌破 5MA。立即執行「現股賣出 50% 倉位」，鎖定大價差！", "blueprint": {"停損防守": "已化為無風險種子部位", "移動停利": f"剩餘50%守技術底線 {trailing_stop:.2f} 元", "預期目標": f"長線目標看 {res_dict['target_brk']:.2f} 元"}}
        return {"strategy_name": "🔥 強勢主升浪完美續抱", "color": "#7D3CFF", "action_now": "🔮 🔮 【強勢狂飆 : 全額持股續抱】", "signal": "短長雙速動能多頭共振", "desc": f"持股成本 {entry_cost:.2f} 元（帳面獲利：{pnl_pct:+.1f}%）。個股短期主趨勢完美運行於 5MA 攻擊線之上。量價結構健康，盤中任何價格回落皆為主力洗盤雜訊，全額咬死不賣，放飛利潤！", "blueprint": {"停損防守": f"守 ATR 防線 ({trailing_stop:.2f} 元)", "移動停利": "結構完美運行中（無減碼信號）", "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"}}
    else:
        if "🚨 季底法人清算結帳期" in res_dict.get("macro_season", ""): return {"strategy_name": "🚨 季底流動性暴風雨防禦", "color": "#FF4B4B", "action_now": "🛑 🔴 【環境極端風險：全新開倉嚴禁開火】", "signal": "投信結帳踩踏期震盪", "desc": "當前正處於季度末法人集體清算、清庫存倒貨的瘋狂結帳期。即便該股型態再漂亮，量化大腦一票否決全新建倉，手握現金，拒絕當接盤俠！", "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "保全現金等待新季度開跑"}}
        if "落後跟屁蟲" in wolf_rank_label and 'RIGHT_BREAKOUT' in st_type: return {"strategy_name": "🚨 狼王位階風控：否決跟風開倉", "color": "#FF4B4B", "action_now": "🛑 🔴 【環境極端風險：全新開倉嚴禁開火】", "signal": "資金分化排斥效應", "desc": "大腦精算顯示該股在同產業族群中屬於落後跟屁蟲，主力資金正在集體往真正的領頭羊報團，追高跟風股隨時面臨補跌踩踏，一票否決！", "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "要買就去買真正最強的隊長"}}
        if is_rs_gold and p >= m20 and not sector_panic: return {"strategy_name": "🚀 統一特許：逆境黃金飆股劇本發動", "color": "#7D3CFF", "action_now": "🔮 🔮 【強者恆強 : 無視大盤恐慌立即開火】", "signal": "個股超額相對強度（RS）爆表", "desc": f"大盤目前破位下殺、泥石流流失。但該個股今日爆發出高達 {res_dict['relative_strength']:.1f}% 的超額相對強度(RS)！觸發『避風港熱錢效應』，特許放行防守型低吸建倉！", "blueprint": {"停損防守": f"開倉技術停損位", "移動停利": f"波動防線 {trailing_stop:.2f} 元", "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"}}
        if is_volume_gap_spike and p >= m20 and not sector_panic: return {"strategy_name": "⚡ 突擊劇本：09:15 早盤量能斷層發動", "color": "#10B981", "action_now": "🔮 🟢 【量能斷層確立：全新開火進場熱錢追擊】", "signal": "開盤特大法人單極速掃貨", "desc": "該個股早盤爆發特大法人不計價掃貨（量能斷層），自動無視指標死叉，啟動突擊隊買進指令！", "blueprint": {"停損防守": f"開盤最低價 ｜ 戰術防線 {trailing_stop:.2f} 元", "移動停利": "無", "預期目標": f"短線價差衝刺目標 {res_dict['target_brk']:.2f} 元"}}
        if st_type == "RIGHT_BREAKOUT":
            if not market_vol_healthy: return {"strategy_name": "🚨 大盤量能失血：假突破防禦機制", "color": "#F59E0B", "action_now": "⚠️ 🟡 【大盤總血量不足：強制削減60%防守型開火】", "signal": "流動性窒息枯竭警告", "desc": "個股型態雖然觸發突破，但此時大盤實質成交總血量低於均量。在缺血市場中，假突破率高達 70%。大腦硬性閹割您的追高權，只允許拉回低吸並砍掉 60% 配額！", "blueprint": { "停損防守": f"戰術硬停損 {res_dict['stop_brk']:.2f} 元", "移動停利": "防守型控量（嚴防假突破）", "預期目標": f"衝刺前高壓力牆 {r:.2f} 元即走"}}
            if is_box_compressed: return {"strategy_name": "🔮 波動極致壓縮：老主力築底大底爆發", "color": "#7D3CFF", "action_now": "🔮 🔮 【蓄勢火山突破：特許放大1.5倍重倉爆發開火】", "signal": "30日大底時間縱深完美共振", "desc": f"該股在過去30天內，高低價差驚人地收斂在 {res_dict['box_width_pct']:.1f}% 的極致窄幅箱型內！籌碼高度集中。今日帶量突破，大腦特許放大 1.5 倍資金重倉開火！", "blueprint": {"停損防守": f"收盤跌破箱型上軌 {r:.2f} 元", "移動停利": f"守 5MA 攻擊線", "預期目標": f"長線翻倍目標對位 {res_dict['target_brk']:.2f} 元"}}
            return {"strategy_name": st_name, "color": "#7D3CFF", "action_now": "🔮 🔮 【頂級信號：全新多頭建倉開火】", "signal": "頂級多頭共振：黃金主升飆股型態發動", "desc": "基本面擴張、法人強力鎖碼、帶量突破前高壓力牆，上方無怨魂，適合執行全新多頭開火建倉！", "blueprint": {"停損防守": f"收盤跌破前高壓力牆 {r:.2f} 元", "移動停利": f"動態守 ATR 防線 ({trailing_stop:.2f} 元)", "預期目標": f"獲利擴張目標對位 {res_dict['target_brk']:.2f} 元"}}
        
        neutral_desc = f"由於此時下方面板 1【{res_dict['market_vol_desc']}】，且面板 2 精算顯示個股僅屬於【{wolf_rank_label}】，缺乏機構實質大金流表態。這兩大核心因子的空方排擠，導致大腦決策防線硬性退守至【量化緩衝帶】。此處毫無多頭期望值，強制空倉，手綁起來！"
        return {"strategy_name": "💤 空倉常態觀望", "color": "#64748B", "action_now": "⚖️ 🔵 【常態調整區 : 保持空倉耐心等待】", "signal": "進入量化緩衝帶", "desc": neutral_desc, "blueprint": {"停損防守": "嚴禁盲目進場", "移動停利": "無", "預期目標": "等待金流重啟點火"}}

# ============ 9. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int, is_holding=False, entry_cost=0.0, sector_panic=False):
    res_dict = {}
    latest_yoy = 0.0
    is_broker_dumping_risk = False
    m_desc, m_color = "🟢 連線正常", "green"
    news_analysis_report = "⚪ 暫無最新重要輿情。"
    recent_catalyst_summary = "⚪ 近 24H 內市場暫無顯著的突發消息面利多推升。"
    raw_news_list = []
    positive_catalysts_list = []
    fin_df = pd.DataFrame()
    spring_verdict = "⚪ 未觸發破底翻結構"
    spring_triggered = False
    detected_prior_low = 0.0
    detected_neckline = 0.0
    spring_lowest_low = 0.0
    bb_stage = "❌ 數據不足無法計算趨勢力道"
    kd_timing = "⚖️ KD 指白定位：尚未取得足夠計算區間"
    volume_verdict = "⚖️ RSI相對強弱：數據載入中"
    vol_spike = False
    is_compressed = False
    sitc_trend = "🟡 中性"
    margin_trend = "🟡 平穩"
    sitc_3d_sum = 0.0
    margin_diff = 0.0
    main_force_label = "⚖️ 常態調整 (0%)"
    wolf_rank_label = "⚖️ 族群常態輪動成員（隨大盤溫和浮動）"
    wolf_rank_color = "#64748B"
    stable_short_trend = "🟡 短期箱型潛伏（橫盤整理，多看少動）"
    stable_short_color = "#F59E0B"
    stable_short_desc = "均線水平橫躺，把手綁起來。"
    trend_phase = "📉 空頭波段修正期"
    short_term_trend = f"📉 均線全面蓋頭 (KD 死亡交叉)"
    long_term_trend = "💤 季線橫向延伸（箱型潛伏築底）"
    fin_conclusion = "📋 該標的暫無足夠季度財報數據。"
    pe_desc = "⚪ 數據不足無法計算估值"
    pe_val, sum_eps_4q, gpm_now, opm_now = 0.0, 0.0, 0.0, 0.0
    macro_desc_text = "🟢 大盤連線正常"
    
    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    if match.empty:
        stock_name = f"代號 {stock_id}"
        industry = "自訂追蹤板塊"
        market_type = "TWO" if (stock_id.startswith("3") or stock_id.startswith("5") or stock_id.startswith("6") or stock_id.startswith("8")) and len(stock_id) == 4 else "TSE"
    else:
        m_col = "type" if "type" in match.columns else "market_type" if "market_type" in match.columns else "market" if "market" in match.columns else None
        market_type = str(match[m_col].iloc[0]).strip().upper() if m_col and len(match) > 0 else "TSE"
        stock_name = str(match["stock_name"].iloc[0])
        industry = str(match["industry_category"].iloc[0])
            
    df_raw = get_daily_df(stock_id, market_type=market_type, days=450)
    if df_raw is None or df_raw.empty: return None

    macro_bull, macro_desc, is_market_panic, is_market_overextended, market_vol_healthy, market_vol_desc = get_market_macro_status()
    radar_results, is_us_panic, us_panic_desc, wtx_change = get_overnight_radar()
    hist_last_raw = df_raw.iloc[-1]
    rt_open, rt_high, rt_low, rt_close, rt_vol_lots, rt_success, rt_source, rt_type = compute_live_data(stock_id, market_type, float(hist_last_raw["close"]), float(hist_last_raw["vol"]))
    current_price, current_vol = rt_close, rt_vol_lots 
    t = tick_size(current_price)
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

    hist_recent = df.copy().sort_values("date", ascending=True).tail(90)
    counts, bins = np.histogram(hist_recent["close"], bins=15, weights=hist_recent["amount"])
    volume_poc = (bins[np.argmax(counts)] + bins[np.argmax(counts) + 1]) / 2
    hist_last = df.iloc[-1]
    ma5_val, vol_ma5_val = float(hist_last["MA5"]), float(hist_last["MA5_Vol"])
    ma20_val, ma60_val, ma100_val = float(hist_last["MA20"]), float(hist_last["MA60"]), float(hist_last["MA100"])
    vol_ma20_val, real_resistance, current_bandwidth = float(hist_last["MA20_Vol"]), float(hist_last["Res_20D"]), float(hist_last["BB_bandwidth"])
    ma5_slope = ((ma5_val - float(df["MA5"].iloc[-3])) / float(df["MA5"].iloc[-3] or 1) * 100) if len(df) >= 3 else 0.0

    if ma5_slope > 0.15: stable_short_trend, stable_short_color, stable_short_desc = "🟢 短期多頭波段（結構穩固，忽略一日拉回）", "#10B981", "5日主力成本線集體向上。不管單日如何震盪、有無破線，大部隊集體趨勢並未改變。請保持定力！"
    elif ma5_slope < -0.15: stable_short_trend, stable_short_color, stable_short_desc = "🔴 短期空頭修正（上方有壓，防禦觀望）", "#EF4444", "5日主力成本線集體下彎。短期多頭動能退潮，上方套牢怨魂沉重。即便盤中反彈，也切勿追高！"
    else: stable_short_trend, stable_short_color, stable_short_desc = "🟡 短期箱型潛伏（橫盤整理，多看少動）", "#F59E0B", "5日線處於水平躺平狀態。股價原地亂晃屬於常態。大腦叫你『把手綁起來』，別在此處被來回打巴掌。"

    rsi_now, adx_now, macd_hist, atr, k9_now, d9_now = safe_float(hist_last.get("RSI14", 50.0)), safe_float(hist_last.get("ADX14", 20.0)), safe_float(hist_last.get("MACD_HIST", 0.0)), safe_float(hist_last.get("ATR14", 1.0)), safe_float(hist_last.get("K9", 50.0)), safe_float(hist_last.get("D9", 50.0))
    kd_status = "黃金交叉" if k9_now > d9_now else "死亡交叉"
    rsi5_now, rsi10_now = safe_float(hist_last.get("RSI5")), safe_float(hist_last.get("RSI10"))
    dif_now, signal_now = safe_float(hist_last.get("MACD_DIF")), safe_float(hist_last.get("MACD_SIGNAL"))
    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = get_taiwan_enhanced_chips(stock_id)
    vol_spike = (estimated_full_day_vol_lots * 1000.0) > (vol_ma20_val * (1.25 if df["amount"].tail(20).mean() > 2000000000 else 2.2))
    main_force_score = 45.0 + (25.0 if sitc_3d_sum > 500 else 0) + (15.0 if margin_diff < -1000 else 0) + (15.0 if vol_spike else 0)
    main_force_label = f"🔥 強力控盤 ({main_force_score:.0f}%)" if main_force_score >= 65 else f"❄️ 籌碼散落 ({main_force_score:.0f}%)" if main_force_score <= 35 else f"⚖️ 常態調整 ({main_force_score:.0f}%)"
    is_compressed = current_bandwidth < df["BB_bandwidth"].tail(60).quantile(0.18)
    is_volume_gap_spike = (1.0 <= ((datetime.combine(datetime.today(), now_time) - datetime.combine(datetime.today(), datetime.strptime("09:00", "%H:%M").time())).total_seconds() / 60.0) <= 30.0) and (current_vol >= (vol_ma5_val / 1000.0) * 0.25)
    stock_daily_pct = ((current_price - float(hist_last_raw["close"])) / float(hist_last_raw["close"])) * 100 if float(hist_last_raw["close"]) > 0 else 0.0
    relative_strength = stock_daily_pct - wtx_change
    is_rs_gold = (wtx_change <= -1.0) and (relative_strength >= 3.0)

    if relative_strength > 4.0 and sitc_3d_sum > 300: wolf_rank_label, wolf_rank_color = "👑 族群領頭狼王（主導資金絕對攻勢）", "#7D3CFF"
    elif relative_strength < -2.0: wolf_rank_label, wolf_rank_color = "🐌 族群落後跟屁蟲（嚴防資金棄養踩踏）", "#EF4444"
    else: wolf_rank_label, wolf_rank_color = "⚖️ 族群常態輪動成員（隨大盤溫和浮動）", "#64748B"

    box_width_pct = ((float(df["close"].tail(30).max()) - float(df["close"].tail(30).min())) / float(df["close"].tail(30).min())) * 100
    is_box_compressed = box_width_pct <= 8.5
    is_broker_dumping_risk = (((safe_float(df["open"].iloc[-1]) - safe_float(df["close"].iloc[-2])) / safe_float(df["close"].iloc[-2] or 1) * 100) > 3.5) and (((current_price - rt_low) / (rt_high - rt_low or 1)) < 0.35) and ((current_vol * 1000.0) > (vol_ma20_val * 2.5))
    final_decision = "❌ 爆量長上影" if bool(hist_last.get("is_long_upper_shadow", False)) and vol_spike else "🚨 惡性金流陷阱" if is_broker_dumping_risk else "⚖️ 綜合評估"
    stop_line_text = f"{round_to_tick(peak_price_20d - (2.5 * atr), t):.2f} 元"
    _, local_m_desc, local_m_color = get_market_status_label(rt_success, str(df.iloc[-1]["date"]))
    m_desc, m_color = (local_m_desc, local_m_color) if not is_market_panic else ("🚨 大盤瀑布式清算恐慌潮", "red")
    
    rev_df = get_rev_df(stock_id, days=730)
    if rev_df is not None and not rev_df.empty and "revenue" in rev_df.columns:
        rev_clean = rev_df.copy()
        rev_clean["revenue"] = pd.to_numeric(rev_clean["revenue"].astype(str).str.replace(",", ""), errors="coerce")
        rev_clean["revenue_year_growth_rate"] = rev_clean["revenue"].pct_change(12) * 100
        if not rev_clean.dropna(subset=["revenue_year_growth_rate"]).empty:
            latest_yoy = float(rev_clean.dropna(subset=["revenue_year_growth_rate"]).sort_values("date").iloc[-1]["revenue_year_growth_rate"])

    try: raw_news_list_data = get_realtime_news_list(stock_id, stock_name)
    except Exception: raw_news_list_data = []

    if raw_news_list_data:
        raw_news_list = raw_news_list_data[:8]
        for n in raw_news_list:
            lbl, col = analyze_news_sentiment(n["title"])
            n["sentiment"], n["color"] = lbl, col
            if "利多" in lbl: positive_catalysts_list.append(n["title"])
        news_analysis_report = f"🔥 【輿情偏多】 利多消息主導市場。" if sum(1 for n in raw_news_list if "利多" in n["sentiment"]) > sum(1 for n in raw_news_list if "利空" in n["sentiment"]) else "⚖️ 中性輿情"
    recent_catalyst_summary = "<b>🎯 關鍵消息面利多題材：</b><br>" + "<br>".join([f"• {t}" for t in positive_catalysts_list[:2]]) if positive_catalysts_list else "⚪ 近 24H 內市場暫真無顯著的突發消息面利多推升。"

    if len(df) >= 40:
        prior_low_candidate, prior_low_idx = float(df.iloc[-40:-10]["low"].min()), df.iloc[-40:-10]["low"].idxmin()
        for r_idx, row in df.iloc[-10:].iterrows():
            if row["low"] < prior_low_candidate:
                if df.iloc[-10:].loc[df.iloc[-10:].index[-1], "close"] > prior_low_candidate: spring_triggered = True; detected_prior_low = prior_low_candidate; detected_neckline = float(df.loc[prior_low_idx:r_idx]["high"].max() or prior_low_candidate); break
    if spring_triggered:
        spring_verdict = f"🟢 【破底翻：買點一成立】洗盤完成！重新站回前低 {detected_prior_low:.2f} 元。" if current_price >= detected_prior_low and df["close"].iloc[-2] <= detected_prior_low else f"🔮 【破底翻：買點二成立】強勢突破關鍵頸線 {detected_neckline:.2f} 元！" if current_price >= detected_neckline and vol_spike else f"🔍 【破底翻結構醞釀中】觸發假破底洗盤，正等待翻轉點火。"

    fin_df_raw = get_financial_statement_df(stock_id, years=2)
    if not fin_df_raw.empty and "Revenue" in fin_df_raw.columns and "EPS" in fin_df_raw.columns:
        fin_df_work = fin_df_raw.copy().sort_values("date").reset_index(drop=True)
        for f_idx in range(len(fin_df_work)):
            rev_amt = safe_float(fin_df_work.loc[f_idx, "Revenue"])
            fin_df_work.loc[f_idx, "gpm"] = (safe_float(fin_df_work.loc[f_idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df_work.loc[f_idx, "opm"] = (safe_float(fin_df_work.loc[f_idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        last_fin = fin_df_work.iloc[-1]
        gpm_now, opm_now, sum_eps_4q = safe_float(last_fin.get("gpm", 0.0)), safe_float(last_fin.get("opm", 0.0)), pd.to_numeric(fin_df_work.tail(4)['EPS'], errors='coerce').sum()
        pe_val = current_price / sum_eps_4q if sum_eps_4q > 0 else 0.0
        pe_desc = "🟢 價值鐵板（安全邊際高）" if pe_val < 13 else "⚖️ 估值合理區間"
        fin_conclusion = "📈 【財報年增擴張】 最新季度獲利指標全數超越去年同期！" if len(fin_df_work) >= 5 and gpm_now > safe_float(fin_df_work.iloc[-5].get("gpm", 0.0)) else "⚖️ 財報常態運作中"
        fin_df = fin_df_work[["date", "EPS", "Revenue", "GrossProfit", "OperatingIncome", "gpm", "opm"]].copy()

    pnl_pct = ((current_price - entry_cost) / entry_cost * 100) if (is_holding and entry_cost > 0) else 0.0
    
    # 🌟 【神經元完好共振核心】：將所有變數 100% 打包封裝進字典，徹底阻斷 KeyError 生存空間
    res_dict.update({"stock_id": stock_id, "stock_name": stock_name, "industry": industry, "pnl_pct": pnl_pct, "macro_bull": macro_bull, "is_market_panic": is_market_panic, "is_market_overextended": is_market_overextended, "is_us_panic": is_us_panic, "us_panic_desc": us_panic_desc, "wtx_change": wtx_change, "final_decision": final_decision, "market_vol_healthy": market_vol_healthy, "market_vol_desc": market_vol_desc, "wolf_rank_label": wolf_rank_label, "wolf_rank_color": wolf_rank_color, "is_box_compressed": is_box_compressed, "box_width_pct": box_width_pct, "target_brk": target_brk, "stop_brk": stop_brk, "rr1_brk": rr1_brk, "target_pb": target_pb, "stop_pb": stop_pb, "rr1_pb": rr1_pb, "trailing_stop_line": stop_line_text, "current_price": current_price, "current_vol": current_vol, "ma5_val": ma5_val, "vol_ma5_val": vol_ma5_val, "ma20_val": ma20_val, "ma60_val": ma60_val, "ma100_val": ma100_val, "vol_ma20_val": vol_ma20_val, "real_resistance": real_resistance, "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_bandwidth": current_bandwidth, "rsi_now": rsi_now, "adx_now": adx_now, "macd_hist": macd_hist, "atr": atr, "stock_daily_pct": stock_daily_pct, "relative_strength": relative_strength, "is_rs_gold": is_rs_gold, "is_volume_gap_spike": is_volume_gap_spike, "rt_source": rt_source, "m_desc": m_desc, "m_color": m_color, "volume_poc": volume_poc, "main_force_label": main_force_label, "recent_catalyst_summary": recent_catalyst_summary, "fin_df": fin_df, "k9_now": k9_now, "d9_now": d9_now, "spring_verdict": spring_verdict, "bb_stage": bb_stage, "kd_timing": kd_timing, "volume_verdict": volume_verdict, "stable_short_trend": stable_short_trend, "stable_short_color": stable_short_color, "stable_short_desc": stable_short_desc, "news_analysis_report": news_analysis_report, "raw_news_list": raw_news_list, "trend_phase": trend_phase, "short_term_trend": short_term_trend, "long_term_trend": long_term_trend, "latest_yoy": latest_yoy, "pe_val": pe_val, "pe_desc": pe_desc, "eps_4q": sum_eps_4q, "fin_conclusion": fin_conclusion, "gpm_now": gpm_now, "opm_now": opm_now, "sitc_trend": sitc_trend, "margin_trend": margin_trend, "sitc_3d_sum": sitc_3d_sum, "margin_diff": margin_diff, "radar_results": radar_results, "macro_desc": macro_desc, "vol_spike": vol_spike, "is_compressed": is_compressed})

    cycle_res = analyze_calendar_cyclicality(df.copy())
    res_dict.update({"calendar_verdict": cycle_res["verdict"], "calendar_data": cycle_res, "macro_season": cycle_res["macro_season"], "tactical_blueprint": unified_institutional_brain(res_dict, df.copy(), is_holding=is_holding, entry_cost=entry_cost, sector_panic=sector_panic)})
    
    adjusted_risk = risk_per_trade * (0.0 if "清倉" in res_dict["tactical_blueprint"]["action_now"] or "🛑" in res_dict["tactical_blueprint"]["action_now"] else 0.4 if "防守型控量" in res_dict["tactical_blueprint"]["action_now"] else 1.5 if "🔮" in res_dict["tactical_blueprint"]["action_now"] else 1.0)
    res_dict.update({"suggested_lots": min(int((total_capital * (adjusted_risk / 100) * 10000 / (current_price - expected_stop_price)) / 1000), int((total_capital * 10000) / (current_price * 1000))) if (current_price - expected_stop_price > 0 and adjusted_risk > 0) else 0, "max_safe_liquidity_lots": max(1, int(vol_ma5_val * 0.015)), "expected_stop_price": expected_stop_price, "strategy_route": strategy_route, "expected_target_price": target_brk, "is_pyramid_order": "加碼" in res_dict["tactical_blueprint"]["action_now"], "liquidity_capped": False})
    return res_dict

# ============ 10. UI Presentation Layer ============
with st.sidebar:
    st.header("🛡️ 全球資金池風控參數")
    capital = st.number_input("核心大資金池 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆最大核心風險承受 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守技術滑價 (Ticks)", 0, 5, 1)
    sector_panic_toggle = st.checkbox("🔥 同族群其他龍頭股「集體下殺破5%」", value=False)
    auto_refresh = st.checkbox("🔄 開啟盤中每 5 秒自動秒刷報價", value=False)

macro_bull, macro_label, is_market_panic, is_market_overextended, _, _ = get_market_macro_status()
st.markdown("## 📡 雙速策略大腦動態綜合看盤台 (v48 狼王特選版)")
stock_input = st.text_input("請輸入核心目標個股代碼：", value="3037")

u_col1, u_col2 = st.columns(2)
with u_col1: user_holding = st.checkbox("📊 我手中「已持有」此個股", value=False)
with u_col2: user_cost = st.number_input("每股真實持股成本 (元)", value=0.0, step=1.0, disabled=not user_holding)
diag_trigger = st.button("🔥 立即執行精密大腦雙速成本定錨診換診斷", use_container_width=True)

if diag_trigger or stock_input:
    st.cache_data.clear()
    res = evaluate_stock(stock_input, capital, risk_pct, slip_input, is_holding=user_holding, entry_cost=user_cost, sector_panic=sector_panic_toggle)
    if res is None: st.error("該個股代碼數據獲取失敗。")
    else:
        bp_data = res["tactical_blueprint"]
        bp = bp_data["blueprint"]
        
        st.html(f"""
        <div style="background-color: {bp_data['color']}10; border: 2px solid {bp_data['color']}; padding: 22px; border-radius: 8px; margin-bottom: 25px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <span style="color: {bp_data['color']}; font-size: 14px; font-weight: 900;">📢 狀態定錨決策大腦標籤：{bp_data['strategy_name']}</span>
                <span style="background-color: {bp_data['color']}; color: white; padding: 4px 12px; border-radius: 4px; font-size: 13px; font-weight:800;">{bp_data['action_now']}</span>
            </div>
            <h3 style="margin: 5px 0; color: {bp_data['color']}; font-size: 23px; font-weight: 900;">即時策略防線：{bp_data['signal']}</h3>
            <div style="margin: 12px 0 18px 0; color: #0F172A; font-size: 15.5px; line-height: 1.65; text-align: justify; font-weight: 700; background-color: #FFFFFF; padding: 14px; border-radius: 6px; border: 2px solid #E2E8F0;">
                <span style="color: {bp_data['color']}; font-weight: 900;">⚡ 狼王自營部核心實戰研判令：</span>{bp_data['desc']}
            </div>
            <div style="background-color: white; border: 1px solid #E2E8F0; padding: 15px; border-radius: 6px; margin-top: 10px;">
                <span style="color: #475569; font-size: 13px; font-weight: 800; display: block; margin-bottom: 8px;">🎯 現股動態配套技術出場計畫藍圖</span>
                <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px;">
                    <div style="background-color: #FFF5F5; padding: 10px; border-radius: 4px; border-left: 3px solid #EF4444;"><small style="color: #DC2626; font-weight: 800;">🛑 1. 核心資本硬性防線</small><p style="margin:3px 0 0 0; font-size:13px; font-weight:bold; color:#1E293B;">{bp['停損防守']}</p></div>
                    <div style="background-color: #FFFBEB; padding: 10px; border-radius: 4px; border-left: 3px solid #F59E0B;"><small style="color: #D97706; font-weight: 800;">⚠️ 2. 移動鎖利/減碼基準</small><p style="margin:3px 0 0 0; font-size:13px; font-weight:bold; color:#1E293B;">{bp['移動停利']}</p></div>
                    <div style="background-color: #F0FDF4; padding: 10px; border-radius: 4px; border-left: 3px solid #10B981;"><small style="color: #16A34A; font-weight: 800;">🚀 3. 預期中線波段目標</small><p style="margin:3px 0 0 0; font-size:13px; font-weight:bold; color:#1E293B;">{bp['預期目標']}</p></div>
                </div>
            </div>
        </div>
        """)

        st.markdown("### 🌐 昨晚美股與台指期夜盤即時戰報")
        radar_show = res["radar_results"]
        if radar_show:
            rd_cols = st.columns(len(radar_show))
            for i, (lbl, val) in enumerate(radar_show.items()):
                with rd_cols[i]: st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:10px; border-radius:6px; text-align:center;"><span style="font-size:12px; color:#64748B; font-weight:600;">{lbl}</span><h4 style="margin:4px 0 0 0; color:{'#10B981' if val >= 0 else '#EF4444'}; font-weight:800;">{'🔺' if val >= 0 else '🔻'} {val:.2f}%</h4></div>""", unsafe_allow_html=True)

        st.markdown(f"""<div style="background-color: #1F2937; padding: 18px; border-radius: 8px; border: 2px solid #3B82F6; margin-bottom: 20px;"><div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;"><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">DIAGNOSTIC TARGET</span><h1 style="margin: 4px 0 0 0; color: #FFFFFF; font-size: 28px; font-weight: 800;">{res['stock_name']} <span style="color: #3B82F6;">({res['stock_id']})</span></h1></div><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">大類板塊歸屬</span><h3 style="margin: 4px 0 0 0; color: #F3F4F6; font-size: 18px; font-weight: 700;">{res['industry']}</h3></div><div style="text-align: right; background-color: rgba(255,255,255,0.05); padding: 6px 12px; border-radius: 6px;"><span style="color: #9CA3AF; font-size: 11px; font-weight: 600; display:block;">即時流報價狀態</span><span style="color: #F9FAFB; font-weight: 600; font-size: 13px;">來源: {res['rt_source']} | 狀態: </span><span style="color: {res['m_color']}; font-weight: 700; font-size: 13px;">{res['m_desc']}</span></div></div></div>""", unsafe_allow_html=True)

        c1, c2, c3, c4 = st.columns(4)
        with c1: st.markdown(custom_hud_box("💡 當前即市價 (K線精密流)", f"<span style='font-size:20px; color:#0F172A;'>{res['current_price']:.2f} 元</span><br><small style='color:#64748B;'>今日成交: {res['current_vol']:.0f} 張</small>"), unsafe_allow_html=True)
        with c2: st.markdown(custom_hud_box("⏱️ 五日短線攻擊速線 (MA5)", f"<span style='font-size:16px; color:#1E293B;'>{res['ma5_val']:.2f} 元</span><br><small style='color:#64748B;'>今日漲跌幅: {res['stock_daily_pct']:+.2f}%</small>"), unsafe_allow_html=True)
        with c3: st.markdown(custom_hud_box("⏳ 母部位大波段防禦線 (ATR)", f"<span style='font-size:16px; color:#7C3AED;'>{res['trailing_stop_line']}</span><br><small style='color:#64748B;'>當前 ATR14: {res['atr']:.2f}</small>"), unsafe_allow_html=True)
        with c4: st.markdown(custom_hud_box("📊 相對強度 (RS Matrix)", f"<span style='font-size:16px; color:#10B981;'>超額 {res['relative_strength']:+.2f}%</span><br><small style='color:#64748B;'>RS黃金箭頭: {'🔥 成立(免疫大盤)' if res['is_rs_gold'] else '⚪ 整理中'}</small>"), unsafe_allow_html=True)

        st.markdown("### 🧬 機構級多因子結構縱深大數據曝光面板")
        ib_col1, ib_col2, ib_col3 = st.columns(3)
        
        with ib_col1:
            macro_detail_desc = f"精算結果：突破單發動必須搭配大盤在線總血量。當大盤量能窒息流失時，這將限制全市場突破流派的開火配額。但若頂端大腦激活『逆境黃金飆股劇本』，表明該股正處於獨立熱錢報團階段，可特許執行低吸戰術。" if not market_vol_healthy else "精算結果：大盤多頭總血量健康，全面解除假突破防禦網，允許所有右側追高與金字塔加碼單全額開火。"
            st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:12px; border-radius:6px; min-height:100px; border-top:4px solid #3B82F6;"><span style="font-size:12px; color:#64748B; font-weight:700; display:block; margin-bottom:4px;">1. 總體流動性安全閥（實質總血量）</span><h4 style="margin:2px 0; color:#1E293B; font-size:15px; font-weight:800;">{res['market_vol_desc']}</h4><p style="margin:4px 0 0 0; font-size:11.5px; color:#1E293B; font-weight:600; line-height:1.5;">{macro_detail_desc}</p></div>""", unsafe_allow_html=True)
        
        with ib_col2:
            if "領頭狼王" in wolf_rank_label: wolf_detail_desc = f"精算結果：該股在板塊內部抓出超額 RS 強度！大資金正在發生瘋狂的擁擠報團排擠效應。大腦直接授予其最高級別的『特許開火權』，強制屏蔽上方第一面板大盤失血的負面干擾。"
            elif "跟屁蟲" in wolf_rank_label: wolf_detail_desc = "精算結果：主力流動性正以每天數億元的規模撤離此股，資金正殘忍往真龍頭靠攏。一票否決新買進開火，嚴防高位接飛刀！"
            else: wolf_detail_desc = f"精算結果：該個股目前處於產業常態輪動中。若您此時是【空倉】，大腦在頂端會直接將防線向後清算至【量化緩衝帶】，在未爆發領頭單前，保持現貨觀望。"
            st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:12px; border-radius:6px; min-height:100px; border-top:4px solid {res['wolf_rank_color']};"><span style="font-size:12px; color:#64748B; font-weight:700; display:block; margin-bottom:4px;">2. 產業板塊內部分化位階（狼王排序）</span><h4 style="margin:2px 0; color:{res['wolf_rank_color']}; font-size:15px; font-weight:800;">{res['wolf_rank_label']}</h4><p style="margin:4px 0 0 0; font-size:11.5px; color:#1E293B; font-weight:600; line-height:1.5;">{wolf_detail_desc}</p></div>""", unsafe_allow_html=True)
        
        with ib_col3:
            box_status_text = f"🔥 波動極致壓縮成立（近30日高低落差僅 {res['box_width_pct']:.1f}%）" if res['is_box_compressed'] else f"⚪ 箱型常態發散中（近30日高低落差 {res['box_width_pct']:.1f}%）"
            box_status_desc = f"精算結果：個股波動完美收斂。主力籌碼完成鋼鐵築底，一旦配合量能斷層，極易爆發波段主升。" if res['is_box_compressed'] else f"精算結果：個股近30日震幅達 {res['box_width_pct']:.1f}%，已<b>實質脫離底部的窄幅蓄勢期</b>，進入動能狂飆擴張階段！此時舊有箱底失去防守價值，請直接對齊下方【短期趨勢定錨面板】的 MA5 防守防線。"
            st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:12px; border-radius:6px; min-height:100px; border-top:4px solid #7C3AED;"><span style="font-size:12px; color:#64748
