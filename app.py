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
st.set_page_config(page_title="SOP v46 機構級現股決策系統 (自動路由版)", layout="wide")

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
                res = r.json()
                p_c = safe_float(res.get("closePrice")) or safe_float(res.get("referencePrice"))
                v_s = safe_float(res.get("total", {}).get("tradeVolume", 0))
                if p_c > 0: return safe_float(res.get("openPrice")) or p_c, safe_float(res.get("highPrice")) or p_c, safe_float(res.get("lowPrice")) or p_c, p_c, v_s/1000.0 if v_s > 0 else hist_lots, True, "Fugle 富果快流", "realtime"
        except Exception: pass
    for prefix in ["otc", "tse"] if is_otc else ["tse", "otc"]:
        try:
            r = session.get(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_id}.tw&json=1&delay=0&_={int(time.time()*1000)}", headers={"Referer": "https://mis.twse.com.tw/"}, timeout=2)
            if r.status_code == 200 and "msgArray" in r.json() and r.json()["msgArray"]:
                info = r.json()["msgArray"][0]
                p_c = safe_float(info.get("z")) or safe_float(info.get("b", "").split("_")[0]) or safe_float(info.get("o"))
                if p_c > 0: return safe_float(info.get("o")) or p_c, safe_float(info.get("h")) or p_c, safe_float(info.get("l")) or p_c, p_c, safe_float(info.get("v")) or hist_lots, True, f"TWSE {prefix.upper()} 官方流", "realtime"
        except Exception: pass
    return hist_last_close, hist_last_close, hist_last_close, hist_last_close, hist_lots, False, "歷史收盤備援", "historical"

# ============ 6. Data Fetching Layers ============
@st.cache_data(ttl=1800)
def get_overnight_radar():
    session = get_requests_session()
    targets = {"台指期近月 (WTX=F)": "WTX=F", "Nasdaq那指 (^IXIC)": "^IXIC", "費城半導體 (^SOX)": "^SOX", "台積電 ADR (TSM)": "TSM"}
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
                        if symbol == "WTX=F": wtx_change = pct
                        if symbol != "WTX=F" and pct <= -2.0: is_us_panic, panic_desc = True, f"昨晚美股重挫，{label} 慘跌 {pct:.1f}%"
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
        {"stock_id": "8069", "stock_name": "元太", "type": "two", "industry_category": "光電業"}
    ]
    return pd.DataFrame(fallback)

@st.cache_data(ttl=900)
def get_daily_df(stock_id: str, market_type: str = "TSE", days: int = 450):
    session = get_requests_session()
    suffix = ".TWO" if any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "上櫃"]) else ".TW"
    p1, p2 = int((datetime.now(TZ)-timedelta(days=days)).timestamp()), int(datetime.now(TZ).timestamp())
    for prefix in ["query2", "query1"]:
        try:
            r = session.get(f"https://{prefix}.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}?period1={p1}&period2={period2}&interval=1d", timeout=5)
            if r.status_code == 200 and r.json().get("chart", {}).get("result"):
                res = r.json()["chart"]["result"][0]
                raw = pd.DataFrame({
                    "date": [datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d") for ts in res.get("timestamp", [])],
                    "open": res["indicators"]["quote"][0].get("open", []), "high": res["indicators"]["quote"][0].get("high", []),
                    "low": res["indicators"]["quote"][0].get("low", []), "close": res["indicators"]["quote"][0].get("close", []),
                    "vol": res["indicators"]["quote"][0].get("volume", []), "adjclose": res["indicators"]["adjclose"][0].get("adjclose", [])
                }).dropna(subset=["close", "adjclose"])
                f = raw["adjclose"] / raw["close"].replace(0, 0.00001)
                raw["open"], raw["high"], raw["low"], raw["close"] = raw["open"]*f, raw["high"]*f, raw["low"]*f, raw["adjclose"]
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
    return get_api().taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d"))

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
    
    x["up"], x["down"] = x["high"].diff(), x["low"].shift(1) - x["low"]
    x["p_dm"] = np.where((x["up"] > x["down"]) & (x["up"] > 0), x["up"], 0)
    x["m_dm"] = np.where((x["down"] > x["up"]) & (x["down"] > 0), x["down"], 0)
    tr_s = x["TR"].ewm(com=13, adjust=False).mean().replace(0, 0.00001)
    x["P_DI"] = (x["p_dm"].ewm(com=13, adjust=False).mean() / tr_s) * 100
    x["M_DI"] = (x["m_dm"].ewm(com=13, adjust=False).mean() / tr_s) * 100
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
    
    if "open" in x.columns:
        x["u_shadow"] = x["high"] - np.maximum(x["open"], x["close"])
        x["is_long_upper_shadow"] = (x["u_shadow"] > (x["open"] - x["close"]).abs()) & (x["u_shadow"] / (x["high"] - x["low"]).replace(0, 0.00001) > 0.4)
    else: x["is_long_upper_shadow"] = False
    return x.dropna(subset=["ATR14", "MA5", "MA20", "MA60", "MA100", "Res_20D", "BB_bandwidth", "RSI14", "MACD_HIST", "K9", "D9", "ADX14"]).copy()

# ============ 8. 大腦決策層模型 ============
def auto_strategy_classifier(res_dict):
    p, r, m20, spring, phase = res_dict["current_price"], res_dict["real_resistance"], res_dict["ma20_val"], res_dict["spring_verdict"], res_dict["trend_phase"]
    if "買點一成立" in spring or "買點二成立" in spring or "醞釀中" in spring:
        if p < r * 0.98: return "LEFT_SPRING", "🛡️ 左側交易：破底翻結構"
    if p >= r * 0.97 or (p > m20 and phase == "🔥 波段多頭主升段"): return "RIGHT_BREAKOUT", "🚀 右側交易：強勢突破型態"
    return "NEUTRAL_ZONE", "⚖️ 混沌常態：無極端共振型態"

def unified_institutional_brain(res_dict, df_hist):
    st_type, st_name = auto_strategy_classifier(res_dict)
    p, r, m20, m100, s3d = res_dict["current_price"], res_dict["real_resistance"], res_dict["ma20_val"], res_dict["ma100_val"], res_dict["sitc_3d_sum"]
    m_safe = "安全" in res_dict["macro_desc"] or "站穩" in res_dict["macro_desc"] or "過熱" in res_dict["macro_desc"]
    panic, overextended = res_dict.get("is_market_panic", False), res_dict.get("is_market_overextended", False)
    u_panic, u_desc, wtx = res_dict.get("is_us_panic", False), res_dict.get("us_panic_desc", ""), res_dict.get("wtx_change", 0.0)
    w_panic = wtx <= -1.0
    final, atr = res_dict["final_decision"], res_dict["atr"]
    
    f_good = "【財報年增擴張】" in res_dict["fin_conclusion"] or res_dict["latest_yoy"] >= 20
    c_lock = "強力鎖碼" in res_dict["sitc_trend"] or "融資大退" in res_dict["margin_trend"]
    
    p20 = float(df_hist["close"].tail(20).max())
    t_stop = p20 - (2.5 * atr)
    r_low = float(df_hist["low"].tail(10).min())
    kd_dead = (df_hist["K9"].iloc[-1] < df_hist["D9"].iloc[-1]) and (df_hist["K9"].iloc[-2] >= df_hist["D9"].iloc[-2])
    
    if "長上影" in final or "金流陷阱" in final or (kd_dead and df_hist["K9"].iloc[-1] > 75):
        msg = "❌ 爆量長上影：大戶高檔瘋狂倒貨！" if "長上影" in final else "🚨 惡性金流陷阱：隔日沖主力開高出貨！" if "金流陷阱" in final else "⚠️ 超買區死亡交叉：短線動能高位衰退。"
        return {"strategy_name": st_name, "color": "#FF4B4B", "action_now": "🚨 🔴 【立即清倉 / 獲利了結】", "signal": "極端出貨與慣性改變訊號共振", "desc": msg, "blueprint": {"停損防守": "全面清倉離場", "移動停利": "無", "預期目標": "保全資金"}}

    if (w_panic or u_panic) and st_type == "RIGHT_BREAKOUT":
        return {"strategy_name": st_name, "color": "#F59E0B", "action_now": "⚠️ 🟡 【夜盤背離：沒收開火權觀望】", "signal": "🚨 跨市場金流斷層：期現貨背離", "desc": f"昨晚台指期夜盤重挫 {wtx:.2f}% 或美股大跌（{u_desc}）。早盤突破高機率為誘多走勢，大腦直接沒收追高開火權，強制觀望！", "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "避開早盤陷阱盤"}}

    if w_panic and st_type == "LEFT_SPRING":
        return {"strategy_name": st_name, "color": "#EF4444", "action_now": "🛑 🔴 【期現貨跳空引信：取消低吸掛單】", "signal": "📉 夜盤引力崩塌：均線支撐全面失效", "desc": f"夜盤暴跌 {wtx:.2f}% 預示今日將大跳空低開，強勢股必將發生末跌段補跌，嚴禁此時伸手接飛刀！", "blueprint": {"停損防守": "禁止進場", "移動停利": "無", "預期目標": "保留實力避開活埋"}}

    if panic and st_type != "RIGHT_BREAKOUT":
        return {"strategy_name": st_name, "color": "#EF4444", "action_now": "🛑 🔴 【強勢股補跌警戒：關閉低吸掛單】", "signal": "☠️ 總體流動性清算：多頭踩踏進行中", "desc": "加權指數5日重挫逾3.5%失守月線，觸發非自願性踩踏。強勢股支撐失效，硬性取消低吸試布局！", "blueprint": {"停損防守": "禁止開火", "移動停利": "無", "預期目標": "手握現金等待止穩"}}

    if not m_safe:
        return {"strategy_name": st_name, "color": "#FF4B4B", "action_now": "🚨 🔴 【強制空倉防禦 / 嚴禁開火】", "signal": "大盤空頭暴風雨警戒", "desc": "大盤失守 20MA 生命線，環境架構偏空。強勢股突破極易淪為陷阱，一票否決！", "blueprint": {"停損防守": "嚴禁進場", "移動停利": "觀望", "預期目標": "等待大盤重返安全區"}}

    if st_type == "RIGHT_BREAKOUT":
        if m_safe and p >= m100 and p >= r * 0.99 and res_dict["vol_spike"] and s3d > 300 and f_good and c_lock:
            if overextended:
                return {"strategy_name": st_name, "color": "#F59E0B", "action_now": "⚠️ 🟡 【大盤過熱：防守型控量輕倉開火】", "signal": "⚡ 瘋狗浪末段逆勢突破：慎防高檔流動性陷阱", "desc": "個股達成完美共振！但大盤與季線正乖離率突破 8.5% 過熱區。解鎖開火權但風控模組強制削減 60% 資金配置，嚴防高位重倉套牢！", "blueprint": {"停損防守": f"收盤跌破 {r:.2f} 元", "移動停利": f"即時價破 {t_stop:.2f} 元", "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"}}
            return {"strategy_name": st_name, "color": "#7D3CFF", "action_now": "🔮 🔮 【立即開火進場】", "signal": "🔮 頂級多頭共振：黃金主升飆股型態發動", "desc": "五維度因子完美黃金交集！基本面擴張、法人強力鎖碼、帶量越過前高，上方無怨魂，大膽切入推進利潤！", "blueprint": {"停損防守": f"收盤跌破前高牆 {r:.2f} 元", "移動停利": f"跌破波動率防線 {t_stop:.2f} 元", "預期目標": f"獲利擴張目標對位 {res_dict['target_brk']:.2f} 元"}}
        
        if p < t_stop:
            return {"strategy_name": st_name, "color": "#FF4B4B", "action_now": "⚠️ 🔴 【動態多頭防線破防：分批落袋】", "signal": "右側動能高位衰竭回撤", "desc": f"市價已回撤擊穿動態 ATR 安全防線 ({t_stop:.2f} 元)，微觀慣性已改，爆發力凍結。進入防守獲利程序！", "blueprint": {"停損防守": "無", "移動停利": "已觸發", "預期目標": "鎖住波段利潤資金退場"}}
        
        if res_dict["short_term_trend"] and f_good:
            return {"strategy_name": st_name, "color": "#1C86EE", "action_now": "🔥 🔵 【穩健波段主升：持股續抱】", "signal": "多方有序推進、基本面實實質支撐", "desc": "短長期趨勢健康排列，營收結構扎實，主力籌碼未見異常流失。手中有部位者現股續抱，讓利潤在主升浪中奔跑。", "blueprint": {"停損防守": f"技術硬風控底線 {res_dict['stop_brk']:.2f} 元", "移動停利": f"動態移動安全線為 {t_stop:.2f} 元", "預期目標": f"持續看好對位目標 {res_dict['target_brk']:.2f} 元"}}

    elif st_type == "LEFT_SPRING":
        if overextended:
            return {"strategy_name": st_name, "color": "#64748B", "action_now": "⚖️ 🔵 【大盤過熱：關閉左側抄底掛單】", "signal": "📥 總體估值亢奮：拒絕左側逆張交易", "desc": "大盤正乖離處於極端超買歷史高位。在指數面臨修正威脅下，中小型股左側抄底易被砸成真破底，大腦一票否決左側接單！", "blueprint": {"停損防守": "禁止進場", "移動停利": "無", "預期目標": "等待大盤乖離吐回健康水準"}}
        if "買點一成立" in res_dict["spring_verdict"] or "買點二成立" in res_dict["spring_verdict"]:
            return {"strategy_name": st_name, "color": "#10B981", "action_now": "🟢 🟢 【立即精密低吸進場】", "signal": "🛡️ 良性回檔：最完美破底翻結構確立", "desc": f"{res_dict['spring_verdict']} 估值踩回高度安全邊際，散戶浮額遭主力洗淨。此進場享有最極致風險報酬比，停損點極小！", "blueprint": {"停損防守": f"硬性死穴防線對位洗盤最低點 {r_low:.2f} 元", "移動停利": "左側不採用移動停利", "預期目標": f"定點停利直指前高壓力牆 {res_dict['target_pb']:.2f} 元"}}
        if p < r_low:
            return {"strategy_name": st_name, "color": "#FF4B4B", "action_now": "🛑 🔴 【立即現股砍單停損】", "signal": "左側結構崩毀、轉惡性真破底", "desc": f"現價已破近十日洗盤實質最低點 ({r_low:.2f} 元)，假跌破被無情證偽轉為惡性真破底！請立即砍單，保全實力！", "blueprint": {"停損防守": "已觸發死穴立刻執行", "移動停利": "無", "預期目標": "保全資金實力"}}
        return {"strategy_name": st_name, "color": "#1C86EE", "action_now": "⚖️ 🔵 【空倉保持耐心 / 靜待右腳確認】", "signal": "假破底洗盤完成、多頭翻轉訊號醞釀中", "desc": "體質符合高手低吸潛伏範疇，融資大退。但微觀翻轉訊號尚未放量確立，請空倉保持耐心，等待右腳出量點火訊號。", "blueprint": {"停損防守": f"預估防守硬線為 {r_low:.2f} 元", "移動停利": "無", "預期目標": f"反彈目標看 {res_dict['target_pb']:.2f} 元"}}
    return {"strategy_name": st_name, "color": "#1C86EE", "action_now": "⚖️ 🔵 【遵循量化紀律常規操作】", "signal": "後台因子互有勝負、未達極端背離", "desc": "財務與動能因子未觸發極端共振背離。個股處於箱型常態調整區，請嚴格遵循下方精密雙軌交易藍圖紀律操作。", "blueprint": {"停損防守": f"突破防守線 {res_dict['stop_brk']:.2f} 元 ｜ 低吸防守線 {res_dict['stop_pb']:.2f} 元", "移動停利": "常態整理區暫不啟動", "預期目標": f"突破目標看 {res_dict['target_brk']:.2f} 元 ｜ 低吸目標看 {res_dict['target_pb']:.2f} 元"}}

# ============ 9. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int):
    # 🌟 戰略最高防守補丁：在第一行直接強制初始化 fin_df 作用域，徹底斬草除根 NameError 隱患！
    fin_df = pd.DataFrame()
    trend_phase, short_term_trend, long_term_trend = "⚖️ 綜合平衡盤整期", "⚪ 技術因子調整中", "⚪ 波段底蘊定型中"
    
    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    if match.empty: return None
    
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

    volume_poc = current_price
    hist_180 = df.tail(180)
    if len(hist_180) >= 20:
        counts, bins = np.histogram(hist_180["close"], bins=15, weights=hist_180["vol"])
        volume_poc = float((bins[np.argmax(counts)] + bins[np.argmax(counts) + 1]) / 2)

    hist_last = df.iloc[-1]
    ma5_val, vol_ma5_val = float(hist_last["MA5"]), float(hist_last["MA5_Vol"])
    ma20_val, ma60_val, ma100_val = float(hist_last["MA20"]), float(hist_last["MA60"]), float(hist_last["MA100"])
    vol_ma20_val, real_resistance, current_bandwidth = float(hist_last["MA20_Vol"]), float(hist_last["Res_20D"]), float(hist_last["BB_bandwidth"])
    bb_upper, bb_lower = float(hist_last["BB_upper"]), float(hist_last["BB_lower"])
    rsi_now, adx_now, macd_hist, atr, k9_now, d9_now = safe_float(hist_last.get("RSI14", 50.0)), safe_float(hist_last.get("ADX14", 20.0)), safe_float(hist_last.get("MACD_HIST", 0.0)), safe_float(hist_last.get("ATR14", 1.0)), safe_float(hist_last.get("K9", 50.0)), safe_float(hist_last.get("D9", 50.0))

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

    bb_stage, kd_timing, volume_verdict = "⚖️ 常態軌道整理中", "⚪ 進入常態整理區間", "⚪ 常態量能交織"
    if is_compressed and (df["close"].tail(10) < df["MA20"].tail(10)).sum() >= 7: bb_stage = "💤 打底觀望期：布林帶收窄，股價中軌下方。主力低檔吸籌【只觀察、絕不進場】"
    elif current_price > ma20_val and df["close"].iloc[-2] <= df["MA20"].iloc[-2] and df["close"].iloc[-1] > df["open"].iloc[-1]: bb_stage = "🔥 啟漲共振點：實體強勢突破藍色 MA20 中軌，趨勢正式由空轉多！"
    elif current_price >= bb_upper or (current_price > ma20_val and df["close"].tail(5).mean() > bb_upper * 0.95): bb_stage = "🚀 主升維持階段：強勢多頭沿布林上軌推升"

    is_kd_had_low_cross_recently = False
    for i in range(-5, 0):
        if df["K9"].iloc[i] > df["D9"].iloc[i] and df["K9"].iloc[i-1] <= df["D9"].iloc[i-1] and df["K9"].iloc[i] < 35: is_kd_had_low_cross_recently = True; break
    if k9_now < 20 and d9_now < 20: kd_timing = "📥 打底階段：KD 指標落入超賣區，靜待共振反彈"
    elif "啟漲" in bb_stage and k9_now >= 45 and is_kd_had_low_cross_recently: kd_timing = "⚡ 共振啟漲點：KD 金叉順勢衝破 50 多空分水嶺共振發動！"
    elif k9_now > 70: kd_timing = "🚨 高檔死亡交叉：超買區反轉弱化訊號觸發" if (df["K9"].iloc[-1] < df["D9"].iloc[-1]) and (df["K9"].iloc[-2] >= df["D9"].iloc[-2]) else "🦅 高檔判定：強勢主升浪出現高位鈍化，靜待死叉"

    if "打底" in bb_stage or (df["vol"].tail(10) < vol_ma20_val).sum() >= 7: volume_verdict = "📉 底部震盪期：成交量長期萎縮，代表浮動籌碼已被主力高度鎖定"
    elif vol_spike and ("啟漲" in bb_stage or current_price >= real_resistance * 0.95): volume_verdict = "🐳 共振突破點：突破關鍵位階且紅量柱連續堆高，大資金進場！"
    elif (df["close"].iloc[-1] > df["close"].tail(15).max() * 0.98) and ((current_vol * 1000.0) < vol_ma20_val * 0.8):
        volume_verdict = "🦅 強力鎖碼縮量主升：法人高度控盤鎖死籌碼（浮額洗淨），無量空氣單主升，續抱！" if main_force_score >= 65 or "強力鎖碼" in sitc_trend else "🚨 散戶型量價背離風險：量能萎縮，短線多頭動能衰退"

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
        if current_price >= detected_prior_low and df["close"].iloc[-2] <= detected_prior_low and df["close"].iloc[-1] > df["open"].iloc[-1]: spring_verdict = f"🟢 【破底翻：買點一成立】主力砸盤誘空完成！重新站回前低 {detected_prior_low:.2f} 元，輕倉試布局！"
        elif current_price >= detected_neckline and vol_spike: spring_verdict = f"🔮 【破底翻：買點二成立】多頭翻轉爆發！強勢突破關鍵頸線 {detected_neckline:.2f} 元，追加倉位！"
        else: spring_verdict = f"🔍 【破底翻結構醞釀中】觸發經典假破底洗盤（前低：{detected_prior_low:.2f}，關鍵頸線：{detected_neckline:.2f}），正等待翻轉訊號。"

    kd_status = "黃金交叉" if k9_now > d9_now else "死亡交叉"
    if current_price >= ma5_val and ma5_val >= ma20_val: short_term_trend = f"🚀 五日線多頭噴發 (KD {kd_status})"
    elif current_price >= ma5_val and current_price < ma20_val: short_term_trend = f"📈 週線跌深反彈 (KD {kd_status})"
    elif current_price < ma5_val and current_price >= ma20_val: short_term_trend = f"⚠️ 短線跌破週線 (KD {kd_status})"
    else: short_term_trend = f"📉 均線全面蓋頭 (KD {kd_status})"
        
    if current_price >= ma60_val and (df["MA60"].iloc[-1] > df["MA60"].iloc[-5]): long_term_trend = "🔥 季線全面向上（主升段架構）"
    elif current_price < ma60_val and (df["MA60"].iloc[-1] < df["MA60"].iloc[-5]): long_term_trend = "📉 季線下彎蓋頭（空頭修正架構）"
    else: long_term_trend = "💤 季線橫向延伸（箱型潛伏築底）"

    if current_price >= ma20_val and ma20_val >= ma60_val and (df["MA20"].iloc[-1] > df["MA20"].iloc[-5]): trend_phase = "🔥 波段多頭主升段"
    elif current_price < ma20_val and ma20_val >= ma60_val: trend_phase = "🛡️ 多頭架獲拉回洗盤期"
    elif is_compressed: trend_phase = "💤 潛伏築底蓄勢期"
    else: trend_phase = "📉 空頭波段修正期"

    latest_yoy = 0.0
    rev_df = get_rev_df(stock_id, days=730)
    if rev_df is not None and not rev_df.empty and "revenue" in rev_df.columns:
        rev_clean = rev_df.copy()
        rev_clean["revenue"] = pd.to_numeric(rev_clean["revenue"].astype(str).str.replace(",", ""), errors="coerce")
        rev_clean["revenue_year_growth_rate"] = rev_clean["revenue"].pct_change(12) * 100
        if not rev_clean.dropna(subset=["revenue_year_growth_rate"]).empty: latest_yoy = float(rev_clean.dropna(subset=["revenue_year_growth_rate"]).sort_values("date").iloc[-1]["revenue_year_growth_rate"])

    # 🌟 修改點：將覆蓋複寫邏輯理順，優先執行賦值
    fin_df_raw = get_financial_statement_df(stock_id, years=2)
    fin_conclusion, pe_desc, pe_val, sum_eps_4q, gpm_now, opm_now = "📋 該標的暫無足夠季度財報數據。", "⚪ 數據不足無法計算估值", 0.0, 0.0, 0.0, 0.0
    if not fin_df_raw.empty and "Revenue" in fin_df_raw.columns and "EPS" in fin_df_raw.columns:
        fin_df = fin_df_raw.sort_values("date").reset_index(drop=True)
        for col_name in ["Revenue", "EPS", "GrossProfit", "OperatingIncome"]:
            if col_name not in fin_df.columns: fin_df[col_name] = 0.0
        for idx in range(len(fin_df)):
            rev_amt = safe_float(fin_df.loc[idx, "Revenue"])
            fin_df.loc[idx, "gpm"] = (safe_float(fin_df.loc[idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df.loc[idx, "opm"] = (safe_float(fin_df.loc[idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        
        last_fin = fin_df.iloc[-1]
        gpm_now, opm_now, sum_eps_4q = safe_float(last_fin.get("gpm", 0.0)), safe_float(last_fin.get("opm", 0.0)), pd.to_numeric(fin_df.tail(4)['EPS'], errors='coerce').sum()
        if sum_eps_4q > 0:
            pe_val = current_price / sum_eps_4q
            db_t = 55.0 if latest_yoy >= 30.0 else 45.0 if latest_yoy >= 15.0 else 35.0
            dc_t = 22.0 if latest_yoy >= 30.0 else 18.0 if latest_yoy >= 15.0 else 13.0
            pe_desc = "🚨 估值瘋狂（高檔吹泡泡）" if pe_val > db_t else "🟢 價值鐵板（安全邊際高）" if pe_val < dc_t else "⚖️ 估值合理區間"
        if len(fin_df) >= 5:
            prev_fin = fin_df.iloc[-5] 
            fin_conclusion = "📈 【財報年增擴張】 最新季度獲利指標全數超越去年同期！" if gpm_now > safe_float(prev_fin.get("gpm", 0.0)) and opm_now > safe_float(prev_fin.get("opm", 0.0)) else "📉 【本業結構退步】 獲利結構遜於去年同期，需提高警覺。"

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
    target_pb = round_to_tick(real_resistance, t)
    stop_pb = round_to_tick(ma20_val - atr - (float(slip_ticks) * t), t) if round_to_tick(ma20_val - atr - (float(slip_ticks) * t), t) < current_price else round_to_tick(current_price - (1.5 * atr), t)
    
    open_gap_pct = ((safe_float(df["open"].iloc[-1]) - safe_float(df["close"].iloc[-2])) / safe_float(df["close"].iloc[-2]) * 100) if len(df) > 1 else 0
    close_to_low_pct = ((current_price - rt_low) / (rt_high - rt_low)) if (rt_high - rt_low) > 0 else 1
    is_broker_dumping_risk = (open_gap_pct > 3.5) and (close_to_low_pct < 0.35) and ((current_vol * 1000.0) > (vol_ma20_val * 2.5))

    final_decision = "⚖️ 綜合評估"
    k_shadow_trap = bool(df.iloc[-1].get("is_long_upper_shadow", False)) and vol_spike
    if k_shadow_trap: final_decision = "❌ 爆量長上影"
    elif is_broker_dumping_risk: final_decision = "🚨 惡性金流陷阱"

    package = {
        "current_price": current_price, "current_vol": current_vol, "vol_ma20_val": vol_ma20_val, "real_resistance": real_resistance, "ma20_val": ma20_val, "ma100_val": ma100_val,
        "sitc_3d_sum": sitc_3d_sum, "margin_diff": margin_diff, "macro_desc": macro_desc, "is_market_panic": is_market_panic, "is_market_overextended": is_market_overextended,
        "is_us_panic": is_us_panic, "us_panic_desc": us_panic_desc, "wtx_change": wtx_change, "spring_verdict": spring_verdict, "final_decision": final_decision, "trend_phase": trend_phase,
        "vol_spike": vol_spike, "pe_desc": pe_desc, "margin_trend": margin_trend, "target_brk": target_brk, "stop_brk": stop_brk, "target_pb": target_pb, "stop_pb": stop_pb,
        "atr": atr, "fin_conclusion": fin_conclusion, "latest_yoy": latest_yoy, "sitc_trend": sitc_trend, "short_term_trend": short_term_trend, "volume_poc": volume_poc
    }
    
    tactical_blueprint = unified_institutional_brain(package, df.copy())
    expected_stop_price = package["stop_brk"] if "突破" in tactical_blueprint["strategy_name"] else package["stop_pb"]
    if "破底翻" in tactical_blueprint["strategy_name"] and ("買點一成立" in spring_verdict or "買點二成立" in spring_verdict):
        expected_stop_price = round_to_tick(spring_lowest_low - t, t) if round_to_tick(spring_lowest_low - t, t) < current_price else round_to_tick(current_price - (1.0 * atr), t)
        strategy_route = "🔮 破底翻底吸試 layout/加倉劇本"
    else: strategy_route = "🚀 強勢突破前高劇本" if "突破" in tactical_blueprint["strategy_name"] else "🛡️ 均線拉回低吸劇本"

    adjusted_risk = risk_per_trade
    if "立即" in tactical_blueprint["action_now"] and "清倉" in tactical_blueprint["action_now"]: adjusted_risk = 0.0
    elif "🛑" in tactical_blueprint["action_now"] or "暫緩追高" in tactical_blueprint["action_now"]: adjusted_risk = 0.0 
    elif "防守型控量" in tactical_blueprint["action_now"]: adjusted_risk *= 0.4 
    elif "🔮" in tactical_blueprint["action_now"]: adjusted_risk *= 1.5 
    
    rr1_brk = (target_brk - current_price) / (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0
    rr1_pb = (target_pb - current_price) / (current_price - stop_pb) if (current_price - stop_pb) > 0 else 0
    suggested_lots = min(int((total_capital * (adjusted_risk / 100) * 10000 / (current_price - expected_stop_price)) / 1000), int((total_capital * 10000) / (current_price * 1000))) if (current_price - expected_stop_price > 0 and adjusted_risk > 0) else 0

    return {
        "stock_id": stock_id, "stock_name": stock_name, "industry": industry, "current_price": current_price, "current_vol": current_vol,
        "ma5_val": ma5_val, "vol_ma5_val": vol_ma5_val, "ma20_val": ma20_val, "ma60_val": ma60_val, "ma100_val": ma100_val, "vol_ma20_val": vol_ma20_val, "real_resistance": real_resistance,
        "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_bandwidth": current_bandwidth, "rsi_now": rsi_now, "adx_now": adx_now, "macd_hist": macd_hist,
        "macro_desc": macro_desc, "sitc_trend": sitc_trend, "margin_trend": margin_trend, "sitc_3d_sum": sitc_3d_sum, "margin_diff": margin_diff,
        "latest_yoy": latest_yoy, "pe_val": pe_val, "pe_desc": pe_desc, "eps_4q": sum_eps_4q, "fin_conclusion": fin_conclusion,
        "gpm_now": gpm_now, "opm_now": opm_now, "is_compressed": is_compressed, "vol_spike": vol_spike,
        "news_analysis_report": news_analysis_report, "raw_news_list": raw_news_list, "trend_phase": trend_phase, "short_term_trend": short_term_trend, "long_term_trend": long_term_trend,
        "target_brk": target_brk, "stop_brk": stop_brk, "rr1_brk": rr1_brk, "target_pb": target_pb, "stop_pb": stop_pb, "rr1_pb": rr1_pb,
        "suggested_lots": suggested_lots, "expected_stop_price": expected_stop_price, "strategy_route": strategy_route, "expected_target_price": target_brk if "突破" in tactical_blueprint["strategy_name"] or "暫緩追高" in tactical_blueprint["action_now"] else target_pb, 
        "trailing_stop_line": round_to_tick(peak_price_20d - (2.5 * atr), t), "rt_source": rt_source, "m_desc": m_desc, "m_color": m_color, "volume_poc": volume_poc, "main_force_label": main_force_label, "recent_catalyst_summary": recent_catalyst_summary, "fin_df": fin_df, "k9_now": k9_now, "d9_now": d9_now, "spring_verdict": spring_verdict, "bb_stage": bb_stage, "kd_timing": kd_timing, "volume_verdict": volume_verdict, "tactical_blueprint": tactical_blueprint, "radar_results": radar_results 
    }

# ============ 10. UI Presentation Layer ============
with st.sidebar:
    st.header("🛡️ 全球資金池風控參數")
    capital = st.number_input("核心大資金池 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆最大核心風險承受 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守技術滑價 (Ticks)", 0, 5, 1)
    st.markdown("---")
    auto_refresh = st.checkbox("🔄 開啟盤中每 5 秒自動秒刷報價", value=False)

macro_bull, macro_label, is_market_panic, is_market_overextended = get_market_macro_status()
full_info_df = get_stock_info_df()
all_industries = sorted([str(i) for i in full_info_df["industry_category"].unique() if i != "nan" and i != ""])

st.markdown("## 📡 策略大腦主動式綜合看盤台")
st.markdown("### 🎛️ 戰術總指揮中心 (Command Center)")
top_col1, top_col2 = st.columns(2)

with top_col1:
    st.markdown("""<div style='background-color:#F0FDF4; padding:8px; border-radius:6px; border-left:4px solid #10B981; margin-bottom:8px;'><b style='color:#065F46; font-size:13.5px;'>流派 A：多策略/全板塊當下即時策略掃描選股池</b></div>""", unsafe_allow_html=True)
    scan_mode = st.selectbox("選擇當下你想全網掃描的篩選大環境：", ["🔥 大盤市值前15大權值股（自動網羅）"] + all_industries)
    if scan_mode == "🔥 大盤市值前15大權值股（自動網羅）":
        industry_stocks = ["2330", "2454", "2308", "2317", "3711", "2383", "3037", "2345", "2881", "2382", "2882", "3017", "2412", "2891", "2303", "8069"]
        scan_label = "大盤前15大特選"
    else:
        industry_stocks = full_info_df[full_info_df["industry_category"] == scan_mode]["stock_id"].tolist()[:10]
        scan_label = scan_mode
    scan_trigger = st.button(f"🔍 啟動 【{scan_label}】 當下全因子動態矩陣掃描排行榜", use_container_width=True)

with top_col2:
    st.markdown("""<div style='background-color:#EFF6FF; padding:8px; border-radius:6px; border-left:4px solid #3B82F6; margin-bottom:8px;'><b style='color:#1E40AF; font-size:13.5px;'>流派 B：個股五維度縱向因果深度診斷與策略開火</b></div>""", unsafe_allow_html=True)
    stock_input = st.text_input("輸入或由左方排行榜選定之目標個股代碼：", value="8069")
    diag_trigger = st.button(f"🔥 執行 【{stock_input}】 精密大腦全串聯深度診斷與利多監測", use_container_width=True)

st.markdown("---")

if scan_trigger:
    st.subheader(f"📊 【{scan_label}】即時動態連線篩選排行榜")
    with st.spinner(f"五維度大腦正在對 {scan_label} 股池進行籌碼洗滌與 K 線真實流解碼..."):
        scan_results = []
        for sid in industry_stocks:
            res = evaluate_stock(sid, capital, risk_pct, slip_input)
            if res:
                bp_data = res["tactical_blueprint"]
                scan_results.append({"代碼": res["stock_id"], "股名": res["stock_name"], "盤中市價": f"{res['current_price']:.2f} 元", "大腦路由分類": bp_data["strategy_name"].split("：")[-1], "當下即時動作": bp_data["action_now"], "短期動能": res["short_term_trend"], "波段底蘊": res["long_term_trend"], "color_code": bp_data["color"]})
        if scan_results:
            df_scan = pd.DataFrame(scan_results)
            st.dataframe(df_scan.style.apply(lambda r: [f'background-color: {r["color_code"]}15; font-weight: 600;'] * len(r), axis=1), column_order=["代碼", "股名", "盤中市價", "大腦路由分類", "當下即時動作", "短期動能", "波段底蘊"], use_container_width=True, height=360)

if diag_trigger or (not scan_trigger and stock_input):
    with st.spinner("五維度大腦深度因果解耦中..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input)
        if res is None: st.error("該個股代碼數據獲取失敗，請確認編號是否正確（數據歷史長度需大於100日）。")
        else:
            bp_data = res["tactical_blueprint"]
            bp = bp_data["blueprint"]
            
            st.html(f"""
            <div style="background-color: {bp_data['color']}10; border: 2px solid {bp_data['color']}; padding: 22px; border-radius: 8px; margin-bottom: 25px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.03);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <span style="color: {bp_data['color']}; font-size: 14px; font-weight: 900; letter-spacing: 0.05em;">📢 系統自動標籤分類：{bp_data['strategy_name']}</span>
                    <span style="background-color: {bp_data['color']}; color: white; padding: 4px 12px; border-radius: 4px; font-size: 13px; font-weight:800; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">{bp_data['action_now']}</span>
                </div>
                <h3 style="margin: 5px 0; color: {bp_data['color']}; font-size: 23px; font-weight: 900;">動態訊號：{bp_data['signal']}</h3>
                <p style="margin: 8px 0 15px 0; color: #1E293B; font-size: 14.5px; line-height: 1.6; text-align: justify;"><b>即時戰術研判：</b>{bp_data['desc']}</p>
                <div style="background-color: white; border: 1px solid #E2E8F0; padding: 15px; border-radius: 6px; margin-top: 10px;">
                    <span style="color: #475569; font-size: 13px; font-weight: 800; display: block; margin-bottom: 8px;">🎯 現股開火後・配套技術面出場計畫藍圖 (Exit Execution Blueprint)</span>
                    <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px;">
                        <div style="background-color: #FFF5F5; padding: 10px; border-radius: 4px; border-left: 3px solid #EF4444;">
                            <small style="color: #DC2626; font-weight: 800; font-size: 11px;">🛑 1. 技術硬性支撐停損</small>
                            <p style="margin: 3px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">{bp['停損防守']}</p>
                        </div>
                        <div style="background-color: #FFFBEB; padding: 10px; border-radius: 4px; border-left: 3px solid #F59E0B;">
                            <small style="color: #D97706; font-weight: 800; font-size: 11px;">⚠️ 2. 盤中最高價移動停利</small>
                            <p style="margin: 3px 0 0 0; font-size: 13px; font-weight: bold; color: #1E293B;">{bp['移動停利']}</p>
                        </div>
                        <div style="background-color: #F0FDF4; padding: 10px; border-radius: 4px; border-left: 3px solid #10B981;">
                            <small style="color: #16A34A; font-weight: 800; font-size: 11px;">🚀 3. 預期波段獲利目標</small>
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
            else: st.warning("⚠️ 跨市場夜盤雷達連線逾時或盤前伺服器維護中，暫無即時戰報數據。")

            st.markdown(f"""<div style="background-color: #1F2937; padding: 18px; border-radius: 8px; border: 2px solid #3B82F6; margin-bottom: 20px;"><div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;"><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600; letter-spacing: 0.05em;">DIAGNOSTIC TARGET</span><h1 style="margin: 4px 0 0 0; color: #FFFFFF; font-size: 28px; font-weight: 800;">{res['stock_name']} <span style="color: #3B82F6;">({res['stock_id']})</span></h1></div><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">大類板塊歸屬</span><h3 style="margin: 4px 0 0 0; color: #F3F4F6; font-size: 18px; font-weight: 700;">{res['industry']}</h3></div><div style="text-align: right; background-color: rgba(255,255,255,0.05); padding: 6px 12px; border-radius: 6px;"><span style="color: #9CA3AF; font-size: 11px; font-weight: 600; display:block;">即時流報價狀態</span><span style="color: #F9FAFB; font-weight: 600; font-size: 13px;">來源: {res['rt_source']} | 狀態: </span><span style="color: {res['m_color']}; font-weight: 700; font-size: 13px;">{res['m_desc']}</span></div></div></div>""", unsafe_allow_html=True)

            c1, c2, c3, c4 = st.columns(4)
            with c1: st.markdown(custom_hud_box("💡 當前即市價 (K線精密流解碼)", f"<span style='font-size:20px; color:#0F172A;'>{res['current_price']:.2f} 元</span><br><small style='color:#64748B; font-weight:500;'>今日成交: {res['current_vol']:.0f} 張</small>"), unsafe_allow_html=True)
            with c2: st.markdown(custom_hud_box("⏱️ 短期動能趨勢 (含MA5週線)", res["short_term_trend"], font_color="#10B981" if "多頭" in res["short_term_trend"] or "噴發" in res["short_term_trend"] else "#EF4444"), unsafe_allow_html=True)
            with c3: st.markdown(custom_hud_box("⏳ 長期波段底蘊", res["long_term_trend"], font_color="#7C3AED" if "主升段" in res["long_term_trend"] else "#64748B"), unsafe_allow_html=True)
            with c4: st.markdown(custom_hud_box("🎯 核心開火預期價", f"<span style='font-size:15px; color:#2563EB;'>{res['expected_target_price']:.2f} 元</span><br><small style='color:#64748B; font-weight:500;'>最佳對位: {res['strategy_route']}</small>"), unsafe_allow_html=True)

            st.markdown("### 🏛️ 四維度因子核心動態曝光面板")
            f1, f2, f3, f4 = st.columns(4)
            with f1: st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #10B981; min-height:185px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;"><h5 style="margin:0; color:#065F46; font-size:14px; font-weight:700;">💎 財務面基本結構</h5><ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;"><li>最新月營收YoY: <span style="color:#10B981; font-weight:700;">""" + f"{res['latest_yoy']:.1f}%" + """</span></li><li>單季毛利率: """ + f"{res['gpm_now']:.1f}%" + """</li><li>單季營益率: """ + f"{res['opm_now']:.1f}%" + """</li><li>體質定性: """ + res['fin_conclusion'].replace("📈", "").replace("📉", "").replace("⚖️", "").strip() + """</li></ul></div>""", unsafe_allow_html=True)
            with f2: st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #3B82F6; min-height:185px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;"><h5 style="margin:0; color:#1E40AF; font-size:14px; font-weight:700;">🦅 籌碼面核心金流</h5><ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;"><li><b>神祕主力控盤度</b>: <span style="color:#2563EB; font-weight:700;">""" + res["main_force_label"] + """</span></li><li>投信3日進出: """ + f"{res['sitc_3d_sum']:.0f} 張" + """</li><li>散戶融資5日增減: """ + f"{res['margin_diff']:.0f} 張" + """</li><li>浮額沉澱狀態: """ + res['margin_trend'].replace("🚨", "").replace("🟢", "").replace("🟡", "").strip() + """</li></ul></div>""", unsafe_allow_html=True)
            with f3: st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #F59E0B; min-height:175px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;"><h5 style="margin:0; color:#92400E; font-size:14px; font-weight:700;">📊 估值面歷史位階</h5><ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;"><li>滾動本益比: <span style="color:#D97706; font-weight:700;">""" + f"{res['pe_val']:.1f} 倍" + """</span></li><li>近四季總EPS: """ + f"{res['eps_4q']:.2f} 元" + """</li><li>位階判定: """ + res['pe_desc'].replace("🚨", "").replace("🟢", "").replace("⚖️", "").strip() + """</li><li>防禦邊際: """ + ("高鐵板 (便宜)" if res['pe_val']<13 else "常態區間" if res['pe_val']<=35 else "危險區 (泡沫)") + """</li></ul></div>""", unsafe_allow_html=True)
            with f4: st.markdown("""<div style="background-color:#FDF4FF; padding:12px; border-radius:6px; border-top:4px solid #7C3AED; min-height:185px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;"><h5 style="margin:0; color:#5B21B6; font-size:14px; font-weight:700;">⏱️ 微觀技術與消息面</h5><ul style="margin:6px 0 0 0; padding-left:16px; font-size:13px; color:#1E293B; line-height:1.45; font-weight:600;"><li>五日攻擊線(MA5): <span style="color:#7C3AED;">""" + f"{res['ma5_val']:.2f} 元" + """</span></li><li>分價量密集牆(POC): """ + f"{res['volume_poc']:.2f} 元" + """</li><li>強弱指標: RSI=""" + f"{res['rsi_now']:.1f}" + """ / <b>KD=""" + f"{res['k9_now']:.1f}/{res['d9_now']:.1f}</b>" + """</li></ul><hr style="margin:6px 0; border:0; border-top:1px solid #E2E8F0;"><p style="margin:0; padding:0; font-size:12px; color:#6B21A8; line-height:1.45; font-weight:600;">""" + res["recent_catalyst_summary"] + """</p></div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("### 🗺️ 精密雙軌量化交易藍圖對照區")
            bl1, bl2 = st.columns(2)
            with bl1: st.markdown(f"""<div style="background-color: #F8FAFC; padding: 16px; border-radius: 6px; border-left: 5px solid #2563EB; border-top: 1px solid #E2E8F0; border-right: 1px solid #E2E8F0; border-bottom: 1px solid #E2E8F0;"><h4 style="margin: 0 0 12px 0; color: #1E40AF; font-weight:800;">🚀 流派一：突破前高起漲劇本 (Breakout)</h4><p style="font-size: 14px; margin: 5px 0;"><b>精密建倉觸發點</b>：&le; {res['real_resistance']:.2f} 元</p><p style="font-size: 14px; margin: 5px 0;"><b>精密獲利目標</b>：<span style="color:#2563EB; font-weight:700;">{res['target_brk']:.2f} 元</span></p><p style="font-size: 14px; margin: 5px 0;"><b>技術防守停損</b>：{res['stop_brk']:.2f} 元</p><p style="font-size: 14px; margin: 5px 0;"><b>期望風險報酬比 (R:R)</b>：{res['rr1_brk']:.2f}</p></div>""", unsafe_allow_html=True)
            with bl2: st.markdown(f"""<div style="background-color: #F8FAFC; padding: 16px; border-radius: 6px; border-left: 5px solid #10B981; border-top: 1px solid #E2E8F0; border-right: 1px solid #E2E8F0; border-bottom: 1px solid #E2E8F0;"><h4 style="margin: 0 0 12px 0; color: #065F46; font-weight:800;">🛡️ 流派二：均線拉回低吸劇本 (Pullback)</h4><p style="font-size: 14px; margin: 5px 0;"><b>精密低吸買點</b>：貼近 {res['ma20_val']:.2f} 元</p><p style="font-size: 14px; margin: 5px 0;"><b>期望反彈目標</b>：<span style="color:#10B981; font-weight:700;">{res['target_pb']:.2f} 元</span></p><p style="font-size: 14px; margin: 5px 0;"><b>技術防守停損</b>：{res['stop_pb']:.2f} 元</p><p style="font-size: 14px; margin: 5px 0;"><b>期望風險報酬比 (R:R)</b>：{res['rr1_pb']:.2f}</p></div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("### 🛡️ 量化核心風控配額開火劇本")
            if res["suggested_lots"] == 0:
                if "#FF4B4B" in bp_data["color"] or "#EF4444" in bp_data["color"] or "#F59E0B" in bp_data["color"]: st.error("🚨 【核心風控最高警戒：大腦策略拒絕進場】 敞口強制關閉！大盤或期指夜盤觸發系統性恐慌/背離倒貨風險。")
                else: st.warning("⚠️ 【風控提示：資金配額不足 1 張】 當前趨勢健康，但因帳戶大資金池或核心曝險比率設定過於緊繃，系統自動阻斷高滑價下單。")

            b1, b2, b3, b4 = st.columns(4)
            with b1: st.metric("精算風控進場配置", f"{res['suggested_lots']} 張", "大腦依劇本自動加減碼")
            with b2: st.metric("當前劇本硬停損價", f"{res['expected_stop_price']:.2f} 元")
            with b3: st.metric("盤中動態移動停利線", f"{res['trailing_stop_line']:.2f} 元")
            with b4: st.metric("大盤加權指數防禦網", "多頭過熱" if is_market_overextended else "多頭安全" if macro_bull else "空頭高風險", res["macro_desc"])

            st.markdown("---")
            st.markdown("### 🔍 跨因子微觀底層驗證數據")
            with st.expander("🧱 破底翻特徵與布林通道骨架大腦解碼", expanded=True):
                st.write(f"**⚡ 破底翻結構驗證裁決**：{res['spring_verdict']}")
                st.write(f"**🟡 布林通道大趨勢骨架**：{res['bb_stage']}")
                st.write(f"**⏱️ KDJ 時機捕捉定位**：{res['kd_timing']}")
                st.write(f"**🐳 真假主力資金成交量辨識**：{res['volume_verdict']}")
                st.markdown("""---""")
                st.markdown("<small style='color:#64748B;'><b>💡 戰術執行官專家提示：</b><br>• <b>跨市場因果連動</b>：昨晚美股費半或台指期夜盤大殺時，大腦會強制沒收早盤右側追高劇本的開火權，嚴防隔日沖主力高檔誘多。<br>• <b>大盤過熱降阻</b>：當大盤與季線正乖離率拉得太緊（>8.5%）時，一票否決所有左側抄底，且右側開火規模強制削減 60% 曝險資金。</small>", unsafe_allow_html=True)

            with st.expander("📊 財務基本面完整財務矩陣大表"):
                if not res["fin_df"].empty:
                    clean_fin_show = res["fin_df"].copy().sort_values("date", ascending=False)
                    clean_fin_show.columns = ["季度日期", "單季 EPS", "營業收入", "營業毛利", "營業利益", "單季毛利率 (%)", "單季營益率 (%)"]
                    st.dataframe(clean_fin_show.style.format({"單季 EPS": "{:.2f}", "營業收入": "{:,.0f}", "營業毛利": "{:,.0f}", "營業利益": "{:,.0f}", "單季毛利率 (%)": "{:.2f}%", "單季營益率 (%)": "{:.2f}%"}), use_container_width=True)

            with st.expander("📈 技術面後台詳細物理量"):
                st.write(f"**分價量密集牆(POC)** = `{res['volume_poc']:.2f}` 元 ｜ **5日線 MA5** = `{res['ma5_val']:.2f}` 元 ｜ **月線 MA20** = `{res['ma20_val']:.2f}` 元 ｜ **20週線 MA100** = `{res['ma100_val']:.2f}` 元")
                st.write(f"**微觀指標物理量**: MACD 柱狀體 = `{res['macd_hist']:.3f}` ｜ 通道帶寬 = `{res['bb_bandwidth']:.4f}` ｜ ADX14 = `{res['adx_now']:.2f}`")

            with st.expander("📰 資訊面 24H 網路輿情即時新聞流水線"):
                st.markdown(f"> **24H 網路即時輿情綜合定論**：`{res['news_analysis_report']}`")
                if isinstance(res["raw_news_list"], list) and res["raw_news_list"]:
                    for n in res["raw_news_list"]: st.markdown(f"* **[{n['date']}]** 【{n['source']}】 [{n['sentiment']}] [{n['title']}]({n['link']})")

if auto_refresh:
    time.sleep(5)
    st.rerun()
