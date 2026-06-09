import os
import time
import requests
import certifi
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timedelta
import pytz
import xml.etree.ElementTree as ET
from FinMind.data import DataLoader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============ 1. Page Config ============
st.set_page_config(page_title="SOP v21 旗艦動態量化交易儀表板", layout="wide")

# ============ 2. Global Constants ============
TZ = pytz.timezone("Asia/Taipei")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

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
    if p >= 1000: return 5.0
    if p >= 500:  return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.05
    return 0.01

def round_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t == 0: return 0.0
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

def analyze_news_sentiment(title: str) -> tuple:
    pos_words = ['創新高', '大賺', '暴增', '飆', '大成長', '利多', '優於預期', '加碼', '看旺', '強勢', '獲利', '突破', '轉盈', '充沛', '加持', '買超', '爆發', '新高', '雙率雙升', '三率三升', '扭虧為盈', '轉虧為盈', '急單']
    neg_words = ['衰退', '虧損', '重挫', '低於預期', '縮水', '跌破', '警告', '利空', '下滑', '疲弱', '裁員', '大跌', '慘', '賣壓', '修正', '賣超', '暴跌', '逆風', '雙率雙降']
    pos_score = sum(1 for w in pos_words if w in title)
    neg_score = sum(1 for w in neg_words if w in title)
    if pos_score > neg_score: return "🟢 利多", "green"
    elif neg_score > pos_score: return "🔴 利空", "red"
    return "🟡 中性", "gray"

# ============ 4. Advanced Connection & Data Layers ============
@st.cache_resource
def get_requests_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    return session

@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try: api.login_by_token(FINMIND_TOKEN)
        except Exception: pass
    return api

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
            yahoo_headers = {"User-Agent": "Mozilla/5.0"}
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

@st.cache_data(ttl=3600)
def get_stock_info_df():
    api = get_api()
    df = api.taiwan_stock_info()
    if df is None or df.empty: return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category"])
    df = df.copy()
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
    return df.dropna(subset=["close", "high", "low", "vol"]).copy()

@st.cache_data(ttl=1800)
def get_market_macro_status():
    api = get_api()
    start_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    try:
        df = api.taiwan_stock_daily(stock_id="TAIEX", start_date=start_date)
        if df is not None and not df.empty:
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['MA20'] = df['close'].rolling(20).mean()
            last_row = df.iloc[-1]
            return (last_row['close'] >= last_row['MA20']), f"加權指數 ({last_row['close']:.1f}) 站穩 20MA 多頭防線" if last_row['close'] >= last_row['MA20'] else f"加權指數 ({last_row['close']:.1f}) 跌破 20MA 空頭防禦"
    except Exception: pass
    return True, "🟢 多頭常態 (未取得大盤數據，預設寬鬆保護)"

@st.cache_data(ttl=900)
def get_taiwan_enhanced_chips(stock_id: str, days: int = 30):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    sitc_trend, margin_trend, sitc_3d_sum = "🟡 投信無顯著動作", "🟡 融資平穩", 0.0
    try:
        inst_df = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
        if inst_df is not None and not inst_df.empty:
            sitc_df = inst_df[inst_df['name'] == 'Investment_Trust'].copy()
            if not sitc_df.empty:
                sitc_df['net'] = pd.to_numeric(sitc_df['buy'], errors='coerce').fillna(0) - pd.to_numeric(sitc_df['sell'], errors='coerce').fillna(0)
                sitc_3d_sum = float(sitc_df.tail(3)['net'].sum())
                if sitc_3d_sum > 500: sitc_trend = "🟢 投信強力鎖碼"
                elif sitc_3d_sum < -500: sitc_trend = "🔴 投信高檔棄養"
    except Exception: pass
    try:
        margin_df = api.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start_date)
        if margin_df is not None and not margin_df.empty:
            margin_df['MarginPurchaseTodayBalance'] = pd.to_numeric(margin_df['MarginPurchaseTodayBalance'], errors='coerce')
            margin_diff = margin_df.iloc[-1]['MarginPurchaseTodayBalance'] - margin_df.iloc[-5]['MarginPurchaseTodayBalance']
            if margin_diff > 1000: margin_trend = "🚨 散戶融資進場 (浮額凌亂)"
            elif margin_diff < -1000: margin_trend = "🟢 散戶融資大退 (籌碼洗淨)"
    except Exception: pass
    return sitc_trend, margin_trend, sitc_3d_sum

@st.cache_data(ttl=900)
def get_rev_df(stock_id: str, days: int = 365):
    api = get_api()
    return api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"))

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
                title = item.find('title').text or ""
                link = item.find('link').text or ""
                pub_date = item.find('pubDate').text or ""
                source = item.find('source').text if item.find('source') is not None else "新聞財經"
                if " - " in title: title = title.rsplit(" - ", 1)[0]
                news_list.append({"date": pub_date, "title": title, "source": source, "link": link})
        if news_list:
            df = pd.DataFrame(news_list)
            df["parsed_date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_convert('Asia/Taipei')
            df["date"] = df["parsed_date"].dt.strftime('%Y-%m-%d %H:%M')
            return df.sort_values(by="parsed_date", ascending=False).drop(columns=["parsed_date"])
    except Exception: pass
    return pd.DataFrame(news_list)

# ============ 5. Technical Engine ============
def prepare_indicator_df(df: pd.DataFrame):
    if df is None or df.empty: return None
    x = df.copy().sort_values("date").reset_index(drop=True)
    
    close_prev = x["close"].shift(1)
    x["TR"] = np.maximum(x["high"] - x["low"], np.maximum((x["high"] - close_prev).abs(), (x["low"] - close_prev).abs()))
    x["ATR14"] = x["TR"].ewm(com=13, adjust=False).mean()
    x["MA20"] = x["close"].rolling(20).mean()
    x["MA60"] = x["close"].rolling(60).mean()
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
    x["ADX14"] = ((x["PLUS_DI"] - x["MINUS_DI"]).abs() / (x["PLUS_DI"] + x["MINUS_DI"]).replace(0, 0.00001) * 100).ewm(com=13, adjust=False).mean()

    x["EMA12"] = x["close"].ewm(span=12, adjust=False).mean()
    x["EMA26"] = x["close"].ewm(span=26, adjust=False).mean()
    x["MACD_DIF"] = x["EMA12"] - x["EMA26"]
    x["MACD_SIGNAL"] = x["MACD_DIF"].ewm(span=9, adjust=False).mean()
    x["MACD_HIST"] = x["MACD_DIF"] - x["MACD_SIGNAL"]

    return x.dropna(subset=["ATR14", "MA20", "MA60", "Res_20D", "BB_bandwidth", "RSI14", "MACD_HIST"]).copy()

# ============ 6. 頂層動態儀表板矩陣串聯大腦 ============
def cross_factor_decoupling_engine(macro_bull, trend_phase, fin_conclusion, sitc_trend, margin_trend, tech_short, latest_yoy, pe_desc):
    if not macro_bull and tech_short in ["🚀 準備起漲", "🚀 完美多頭"]:
        return "🚨 大盤空頭陷阱：提防系統性獵殺", "red", "技術面拉出強勢突破，但台股加權指數處於月線下方。此時強勢股假突破、淪為割肉陷阱的機率高達 80%！強烈建議保持觀望。"
    if "主升段" in trend_phase and "本業結構退步" in fin_conclusion and "散戶進場攤平" in margin_trend:
        return "💥 主力強弩之末：基本面衰退+散戶接盤", "red", "股價雖位於主升段頂部，但最新季度財報已出現本業結構惡化（雙率雙降），且籌碼連續被散戶融資承接。此為極度危險的拉高出貨盤！"
    if macro_bull and tech_short == "🚀 準備起漲" and "投信強力鎖碼" in sitc_trend and latest_yoy >= 25:
        return "🔮 完美風暴：投信/集團季底作帳飆股", "purple", "布林長期壓縮後爆量向上突破 20 日實質前高！伴隨月營收強勁年增（>25%）與投信主力真槍實彈建倉。大盤環境支持，具備高度強勢飆股特徵。"
    if "拉回洗盤期" in trend_phase and pe_desc == "🟢 價值鐵板（安全邊際高）" and "散戶融資大退" in margin_trend:
        return "🛡️ 黃金右腳：高安全邊際良性換手點", "green", "中長期多頭排列，短線股價回踩月線進行良性浮額清洗。滾動 PE 處於歷史極低位，散戶融資認賠退場，籌碼極度沉澱，為優質低吸點。"
    if "空頭" in trend_phase:
        return "❌ 空頭波段結構破壞：嚴格避開", "red", "長短期均線已全面蓋頭壓制，空方完全控盤。任何單日暴漲多為誘多捕獸夾，操作上保持絕對觀望。"
    return "⚖️ 標準波段：回歸常規技術藍圖操作", "blue", "多空因子處於動態拉鋸平衡，未見極端的宏觀與籌碼共振。請嚴格依據量化風控之藍圖價位操作。"

# ============ 7. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int):
    df_raw = get_daily_df(stock_id, days=365)
    if df_raw is None or df_raw.empty: return None

    hist_last_raw = df_raw.iloc[-1]
    current_price, current_vol, rt_success, rt_source, rt_type = compute_live_data(
        stock_id, float(hist_last_raw["close"]), float(hist_last_raw["vol"])
    )
    
    df_for_indicators = df_raw.copy()
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    if rt_success and (rt_type in ["realtime", "delayed"]):
        if str(df_for_indicators.iloc[-1]["date"]) == today_str:
            df_for_indicators.iloc[-1, df_for_indicators.columns.get_loc("close")] = current_price
            df_for_indicators.iloc[-1, df_for_indicators.columns.get_loc("vol")] = current_vol
        else:
            new_row = pd.DataFrame([{"date": today_str, "close": float(current_price), "high": float(current_price), "low": float(current_price), "vol": float(current_vol), "amount": float(current_price * current_vol * 1000)}])
            df_for_indicators = pd.concat([df_for_indicators, new_row], ignore_index=True)

    df = prepare_indicator_df(df_for_indicators)
    if df is None or df.empty: return None

    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    stock_name = match["stock_name"].values[0] if not match.empty else "指定標的"
    industry = match["industry_category"].values[0] if not match.empty else "未知板塊"

    hist_last = df.iloc[-1]
    last_trade_date_str = str(hist_last["date"])
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

    # 物理量提取
    ma20_val, ma60_val = float(hist_last["MA20"]), float(hist_last["MA60"])
    vol_ma20_val, real_resistance = float(hist_last["MA20_Vol"]), float(hist_last["Res_20D"])
    bb_upper, bb_lower, current_bandwidth = float(hist_last["BB_upper"]), float(hist_last["BB_lower"]), float(hist_last["BB_bandwidth"])
    rsi_now, adx_now, macd_hist = float(hist_last["RSI14"]), float(hist_last["ADX14"]), float(hist_last["MACD_HIST"])
    macd_dif, macd_signal = float(hist_last["MACD_DIF"]), float(hist_last["MACD_SIGNAL"])
    atr = float(hist_last["ATR14"])

    is_heavyweight = df["amount"].tail(20).mean() > 2000000000
    vol_multiplier, compress_quantile = (1.25, 0.35) if is_heavyweight else (2.2, 0.18)
    vol_spike = current_vol > (vol_ma20_val * vol_multiplier)
    is_compressed = current_bandwidth < df["BB_bandwidth"].tail(60).quantile(compress_quantile)

    # 均線斜率波段架構定性
    ma20_trend_5d = "上升" if df["MA20"].iloc[-1] > df["MA20"].iloc[-5] else "平盤"
    ma60_trend_5d = "上升" if df["MA60"].iloc[-1] > df["MA60"].iloc[-5] else "平盤"
    
    # === 補足需求：明確拆解長、短期趨勢標籤 ===
    # 短期趨勢：看現價與月線相對位階與擺動動能
    if current_price >= ma20_val:
        short_term_trend = "🚀 多頭強勢發散" if rsi_now > 60 else "📈 多頭成形"
    else:
        short_term_trend = "⚠️ 短線回檔洗盤" if ma60_trend_5d == "上升" else "📉 空方動能控盤"
        
    # 長期趨勢：看現價與季線（生命線）架構
    if current_price >= ma60_val and ma60_trend_5d == "上升":
        long_term_trend = "🔥 季線向上（大波段主升段）"
    elif current_price < ma60_val and ma60_trend_5d == "下彎":
        long_term_trend = "📉 季線下彎（空頭修正波段）"
    else:
        long_term_trend = "💤 季線橫向（箱型築底蓄勢期）"

    if current_price >= ma20_val and ma20_val >= ma60_val and ma20_trend_5d == "上升": trend_phase = "🔥 波段多頭主升段"
    elif current_price < ma20_val and ma20_val >= ma60_val: trend_phase = "🛡️ 多頭架構拉回洗盤期"
    elif is_compressed: trend_phase = "💤 潛伏築底蓄勢期"
    else: trend_phase = "📉 空頭波段修正期"

    # 籌碼、大盤、基本營收獲取
    sitc_trend, margin_trend, sitc_3d_sum = get_taiwan_enhanced_chips(stock_id)
    macro_bull, macro_desc = get_market_macro_status()
    
    latest_yoy = 0.0
    rev_df = get_rev_df(stock_id)
    if rev_df is not None and not rev_df.empty:
        latest_yoy = safe_float(rev_df.iloc[-1].get("revenue_year_growth_rate", 0.0))

    # 財報解耦年增與 PE 估值精算
    fin_df = get_financial_statement_df(stock_id)
    fin_conclusion, pe_desc, sum_eps_4q = "📋 暫無足夠歷史季度財報對比數據。", "⚪ 數據不足無法計算估值", 0.0
    
    if not fin_df.empty and "Revenue" in fin_df.columns and "EPS" in fin_df.columns:
        for idx in range(len(fin_df)):
            r_amt = safe_float(fin_df.loc[idx, "Revenue"])
            fin_df.loc[idx, "gpm"] = (safe_float(fin_df.loc[idx, "GrossProfit"]) / r_amt * 100) if r_amt > 0 else 0.0
            fin_df.loc[idx, "opm"] = (safe_float(fin_df.loc[idx, "OperatingIncome"]) / r_amt * 100) if r_amt > 0 else 0.0
        
        last_fin = fin_df.iloc[-1]
        sum_eps_4q = pd.to_numeric(fin_df.tail(4)['EPS'], errors='coerce').sum()
        if sum_eps_4q > 0:
            pe_val = current_price / sum_eps_4q
            pe_desc = "🚨 估值瘋狂（高檔吹泡泡）" if pe_val > 35 else "🟢 價值鐵板（安全邊際高）" if pe_val < 13 else "⚖️ 估值合理區間"
        else: pe_val = 0.0

        if len(fin_df) >= 5:
            prev_fin = fin_df.iloc[-5]
            if last_fin["gpm"] > prev_fin["gpm"] and last_fin["opm"] > prev_fin["opm"]: fin_conclusion = "📈 【獲利年增：雙率雙升】 本業體質結構擴張良好。"
            elif last_fin["gpm"] < prev_fin["gpm"] and last_fin["opm"] < prev_fin["opm"]: fin_conclusion = "📉 【本業結構退步】 毛利與營益雙雙低於去年同期！"
            else: fin_conclusion = "⚖️ 【結構調整期】 獲利結構指標互有勝負。"
    else: pe_val = 0.0

    # 新聞輿情解析
    news_analysis_report = "⚪ 暫無最新重要輿情。"
    raw_news_list = []
    news_df = get_realtime_news_df(stock_id, stock_name)
    if news_df is not None and not news_df.empty:
        raw_news_list = news_df.head(8).to_dict('records')
        for n in raw_news_list:
            lbl, col = analyze_news_sentiment(n["title"])
            n["sentiment"] = lbl
            n["color"] = col
        pos_cnt = sum(1 for n in raw_news_list if "利多" in n["sentiment"])
        neg_cnt = sum(1 for n in raw_news_list if "利空" in n["sentiment"])
        if pos_cnt > neg_cnt: news_analysis_report = f"🔥 【輿情偏多】 利多消息主導市場情緒（多 {pos_cnt} 則 / 空 {neg_cnt} 則）。"
        elif neg_cnt > pos_cnt: news_analysis_report = f"🚨 【輿情偏空】 利空雜音浮現，嚴防拋壓（空 {neg_cnt} 則 / 多 {pos_cnt} 則）。"

    # 微觀動能定性
    tech_short = "中性觀望"
    if current_price >= real_resistance * 0.995 and vol_spike and is_compressed: tech_short = "🚀 準備起漲"
    elif rsi_now >= (75 if is_heavyweight else 85): tech_short = "⚠️ 短線過熱"
    elif float(hist_last["PLUS_DI"]) > float(hist_last["MINUS_DI"]): tech_short = "🚀 多頭成形"

    # 大腦矩陣串聯
    final_decision, final_color, final_desc = cross_factor_decoupling_engine(
        macro_bull, trend_phase, fin_conclusion, sitc_trend, margin_trend, tech_short, latest_yoy, pe_desc
    )

    # 雙軌交易藍圖精算
    t = tick_size(current_price)
    slip = float(slip_ticks) * t
    
    # 突破型策略藍圖 (Breakout Blueprint)
    target_atr_ratio = 4.0 if is_heavyweight else 5.5
    target_brk = round_to_tick(current_price + (target_atr_ratio * atr), t)
    stop_brk = round_to_tick(real_resistance - (1.5 * atr) - slip, t)
    if stop_brk >= current_price: stop_brk = round_to_tick(current_price - (1.0 * atr), t)
    rr1_brk = (target_brk - current_price) / (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0

    # 拉回潛伏型策略藍圖 (Pullback Blueprint)
    target_pb = round_to_tick(real_resistance, t)
    stop_pb = round_to_tick(ma20_val - atr - slip, t)
    if stop_pb >= current_price: stop_pb = round_to_tick(current_price - (1.5 * atr), t)
    rr1_pb = (target_pb - current_price) / (current_price - stop_pb) if (current_price - stop_pb) > 0 else 0

    # === 補足需求：精算預期價格（根據大腦決策情境動態對位） ===
    if final_color in ["purple", "red"] or current_price >= real_resistance * 0.98:
        expected_target_price = target_brk
        expected_stop_price = stop_brk
        strategy_route = "🚀 強勢突破前高劇本"
    else:
        expected_target_price = target_pb
        expected_stop_price = stop_pb
        strategy_route = "🛡️ 均線拉回低吸劇本"

    # 動態核心風控張數計算
    adjusted_risk = risk_per_trade
    if final_color == "red": adjusted_risk = 0.0
    elif final_color == "purple": adjusted_risk *= 1.5
    
    loss_per_share = current_price - expected_stop_price
    risk_money = total_capital * (adjusted_risk / 100) * 10000
    suggested_lots = int((risk_money / loss_per_share) / 1000) if (loss_per_share > 0 and adjusted_risk > 0) else 0

    return {
        "stock_id": stock_id, "stock_name": stock_name, "industry": industry, "current_price": current_price, "current_vol": current_vol,
        "ma20_val": ma20_val, "ma60_val": ma60_val, "vol_ma20_val": vol_ma20_val, "real_resistance": real_resistance,
        "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_bandwidth": current_bandwidth, "rsi_now": rsi_now, "adx_now": adx_now,
        "macd_hist": macd_hist, "macd_dif": macd_dif, "macd_signal": macd_signal, "plus_di": float(hist_last["PLUS_DI"]), "minus_di": float(hist_last["MINUS_DI"]),
        "macro_desc": macro_desc, "sitc_trend": sitc_trend, "margin_trend": margin_trend, "sitc_3d_sum": sitc_3d_sum,
        "latest_yoy": latest_yoy, "pe_val": pe_val, "pe_desc": pe_desc, "eps_4q": sum_eps_4q, "fin_conclusion": fin_conclusion,
        "fin_df": fin_df, "raw_news_list": raw_news_list, "news_analysis_report": news_analysis_report, "trend_phase": trend_phase,
        "short_term_trend": short_term_trend, "long_term_trend": long_term_trend, 
        "expected_target_price": expected_target_price, "expected_stop_price": expected_stop_price, "strategy_route": strategy_route,
        "final_decision": final_decision, "final_color": final_color, "final_desc": final_desc,
        "target_brk": target_brk, "stop_brk": stop_brk, "rr1_brk": rr1_brk,
        "target_pb": target_pb, "stop_pb": stop_pb, "rr1_pb": rr1_pb,
        "suggested_lots": suggested_lots, "trailing_stop_line": round_to_tick(current_price - (2.5 * atr), t),
        "rt_source": rt_source, "m_desc": m_desc, "m_color": m_color
    }

# ============ 8. 戰術抬頭顯示器（HUD）UI 呈現層 ============
st.title("🎛️ SOP v21 頂級量化交易決策主動儀表板")
st.caption("2026 高階完全體 — 主頁聚焦「趨勢、預期價、決策建議」三核心，細細部財務技術資產退居輔助漏斗")

with st.sidebar:
    st.header("⚙️ 風控核心參數")
    stock_input = st.text_input("台股個股代碼", "2330")
    capital = st.number_input("核心大資金池 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆核心風險限制 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("防守滑價滑移 (Ticks)", 0, 5, 1)

if st.button("🚀 執行五因子全串聯大腦診斷", use_container_width=True):
    with st.spinner("大腦正進行跨因子因果對位，生成即時戰略 HUD 抬頭顯示面..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input)
        
        if res is None:
            st.error("代碼解析失敗，請確認代碼是否正確。")
        else:
            # =========================================================
            # 【區塊一：主畫面動態儀表板 - 抬頭顯示牆 (HUD Metric Row)】
            # =========================================================
            st.markdown("### 📡 盤中動態數據抬頭顯示牆")
            m1, m2, m3, m4 = st.columns(4)
            with m1: 
                st.metric("💡 當前即時市價", f"{res['current_price']:.2f} 元", f"量: {res['current_vol']:.0f}張")
            with m2: 
                st.metric("⏱️ 短期動能趨勢", res["short_term_trend"])
            with m3: 
                st.metric("⏳ 長期波段底蘊", res["long_term_trend"])
            with m4: 
                st.metric("🎯 預期目標價位", f"{res['expected_target_price']:.2f} 元", f"劇本: {res['strategy_route']}")
                
            # =========================================================
            # 【區塊二：主畫面核心 - 結論建議與交叉裁決中心】
            # =========================================================
            st.markdown("### 🎯 決策大腦全串聯交叉裁決")
            color_hex = {"red": "#FF4B4B", "purple": "#7D3CFF", "green": "#2BD9A1", "blue": "#1C86EE", "gray": "#808080"}[res["final_color"]]
            st.markdown(f"""
            <div style="background-color:{color_hex}12; border-left: 6px solid {color_hex}; padding: 20px; border-radius: 6px; margin-bottom: 25px;">
                <h2 style="margin:0; color:{color_hex}; font-size:24px;">【{res['final_decision']}】</h2>
                <p style="margin: 12px 0 0 0; color:#111; font-size:16.5px; font-weight:600; line-height:1.65;">{res['final_desc']}</p>
            </div>
            """, unsafe_allow_html=True)

            # =========================================================
            # 【區塊三：主畫面戰術藍圖 - 風控開火指引】
            # =========================================================
            st.markdown("### 🛡️ 量化風控開火指引 (Trading Blueprint)")
            if res["suggested_lots"] == 0:
                st.error("🚨 【核心風控黃金紀律：拒絕交易】 大腦判定踩到策略核心地雷或結構破壞，子彈敞口關閉。請保持空倉觀望。")
            
            b1, b2, b3, b4 = st.columns(4)
            with b1: st.metric("精算風控配置張數", f"{res['suggested_lots']} 張", "依據大腦劇本加減碼結果")
            with b2: st.metric("硬性鐵板停損價位", f"{res['expected_stop_price']:.2f} 元", "觸價即刻執行")
            with b3: st.metric("盤中動態移動停利線", f"{res['trailing_stop_line']:.2f} 元", "最高收盤價回撤 2.5*ATR")
            with b4: st.metric("大盤系統風險安全網", "安全" if "站穩" in res["macro_desc"] else "高度風險", res["macro_desc"])

            st.markdown(f"<small>標的名稱：{res['stock_name']} ({res['stock_id']}) | 所屬板塊：{res['industry']} | 報價連線：{res['rt_source']} ({res['m_desc']})</small>", unsafe_allow_html=True)
            st.markdown("---")
            
            # =========================================================
            # 【區塊四：輔助底層資料漏斗 - 折疊式詳細排版（不擠佔主視線）】
            # =========================================================
            st.markdown("### 🔍 跨因子微觀底層驗證數據")
            
            with st.expander("📊 財務面核心表格：季度財務矩陣與估值細節"):
                fc1, fc2, fc3 = st.columns(3)
                with fc1: st.metric("最新單月營收年增率", f"{res['latest_yoy']:.1f}%")
                with fc2: st.metric("滾動滾動本益比 (PE)", f"{res['pe_val']:.1f} 倍", res["pe_desc"])
                with fc3: st.metric("近4季累積總 EPS", f"{res['eps_4q']:.2f} 元")
                st.markdown(f"> **財報解耦年增定論**：{res['fin_conclusion']}")
                
                if not res["fin_df"].empty:
                    clean_fin_show = res["fin_df"].copy().sort_values("date", ascending=False)
                    clean_fin_show.columns = ["季度日期", "單季 EPS", "營業收入", "營業毛利", "營業利益", "單季毛利率 (%)", "單季營益率 (%)"]
                    st.dataframe(clean_fin_show.style.format({
                        "單季 EPS": "{:.2f}", "營業收入": "{:,.0f}", "營業毛利": "{:,.0f}", 
                        "營業利益": "{:,.0f}", "單季毛利率 (%)": "{:.2f}%", "單季營益率 (%)": "{:.2f}%"
                    }), use_container_width=True)

            with st.expander("📈 技術面核心物理量：均線、布林通道與百萬動能指標流"):
                tc1, tc2, tc3 = st.columns(3)
                with tc1:
                    st.write(f"* **基準均線位階**：MA20 = `{res['ma20_val']:.2f}` 元 | MA60 = `{res['ma60_val']:.2f}` 元")
                    st.write(f"* **實質成交量能**：今日量 = `{res['current_vol']:.0f}` 張 | 20日均量 = `{res['vol_ma20_val']:.0f}` 張")
                with tc2:
                    st.write(f"* **布林通道物理量**：上軌 = `{res['bb_upper']:.2f}` | 下軌 = `{res['bb_lower']:.2f}` | 帶寬 = `{res['bb_bandwidth']:.4f}`")
                    st.write(f"* **實質箱頂前高壓力**：`{res['real_resistance']:.2f}` 元")
                with tc3:
                    st.write(f"* **擺動動能物理量**：RSI14 = `{res['rsi_now']:.1f}` | ADX14 = `{res['adx_now']:.1f}` | MACD柱體 = `{res['macd_hist']:.3f}`")
                    st.write(f"* **DMI趨向多空對位**：+DI = `{res['plus_di']:.1f}` | -DI = `{res['minus_di']:.1f}`")

            with st.expander("📰 資訊面與特有籌碼：24H 新聞監控流水線與籌碼內幕"):
                st.markdown(f"> **24H 輿情綜合報告**：{res['news_analysis_report']}")
                ch1, ch2 = st.columns(2)
                with ch1: st.metric("投信金流 (3日加總)", f"{res['sitc_3d_sum']:.0f} 張", res["sitc_trend"])
                with ch2: st.metric("散戶融資浮額位階", res["margin_trend"])
                
                st.markdown("#### 📥 Google RSS 24H 即時核心新聞流水線")
                if res["raw_news_list"]:
                    for n in res["raw_news_list"]:
                        st.markdown(f"* **[{n['date']}]** 【{n['source']}】  [{n['sentiment']}]  [{n['title']}]({n['link']})")
                else:
                    st.warning("⚠️ 24小時內該個股暫無相關核心財報、營收或主力新聞被 Google RSS 收錄。")

            with st.expander("🚀 完整雙軌量化交易藍圖（備用突破與拉回原始數據）"):
                st.markdown("當標的未出現在動態儀表板的特定劇本時，可調用此原始精密雙軌矩陣數據：")
                col_b, col_p = st.columns(2)
                with col_b:
                    st.markdown("#### 突破前高起漲劇本 (Breakout)")
                    st.write(f"* 進場點：≥ `{res['real_resistance']:.2f}`")
                    st.write(f"* 目標價：`{res['target_brk']:.2f}`")
                    st.write(f"* 停損價：`{res['stop_brk']:.2f}`")
                    st.write(f"* 風酬比 (R:R)：`{res['rr1_brk']:.2f}`")
                with col_p:
                    st.markdown("#### 均線拉回潛伏劇本 (Pullback)")
                    st.write(f"* 進場點：貼近 `{res['ma20_val']:.2f}`")
                    st.write(f"* 目標價：`{res['target_pb']:.2f}`")
                    st.write(f"* 停損價：`{res['stop_pb']:.2f}`")
                    st.write(f"* 風酬比 (R:R)：`{res['rr1_pb']:.2f}`")
