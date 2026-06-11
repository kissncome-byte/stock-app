import os
import time
import requests
import certifi
import urllib.parse
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
st.set_page_config(page_title="SOP v46 機構級現股決策系統 (自動路由版)", layout="wide")

# ============ 2. Global Constants ============
TZ = pytz.timezone("Asia/Taipei")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")
FUGLE_TOKEN = os.getenv("FUGLE_TOKEN", "") or st.secrets.get("FUGLE_TOKEN", "")

# ============ 3. Helper Functions & Utilities ============
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

def custom_hud_box(title, value, font_color="#1E293B"):
    return f"""
    <div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; padding: 12px; border-radius: 6px; min-height: 105px; box-shadow: 0 1px 2px rgba(0,0,0,0.02); margin-bottom: 10px;">
        <span style="color: #64748B; font-size: 12.5px; font-weight: 600; display: block; margin-bottom: 5px; letter-spacing: 0.02em;">{title}</span>
        <span style="color: {font_color}; font-size: 14px; font-weight: 700; display: block; line-height: 1.5; white-space: normal; word-break: break-all;">{value}</span>
    </div>
    """

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
    pos_words = ['創新高', '大賺', '暴增', '飆', '大成長', '利多', '優於預期', '加碼', '看旺', '強勢', '獲利', '突破', '轉盈', '充沛', '加持', '買超', '爆發', '新高', '雙率雙升', '三率三升', '扭虧為盈', '轉虧為盈', '急單', '擴產']
    neg_words = ['衰退', '虧損', '重挫', '低於預期', '縮水', '跌破', '警告', '利空', '下滑', '疲弱', '裁員', '大跌', '慘', '賣壓', '修正', '賣超', '暴跌', '逆風', '雙率雙降']
    pos_score = sum(1 for w in pos_words if w in title)
    neg_score = sum(1 for w in neg_words if w in title)
    if pos_score > neg_score: return "🟢 利多", "green"
    elif neg_score > pos_score: return "🔴 利空", "red"
    return "🟡 中性", "gray"

# ============ 4. Advanced Connection Layer ============
@st.cache_resource
def get_requests_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    })
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
    hist_last_vol_lots = hist_last_vol / 1000.0 if hist_last_vol > 0 else 0.0
    session = get_requests_session()
    is_otc_hint = any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "柜", "上櫃"])
    
    if FUGLE_TOKEN:
        try:
            url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{stock_id}"
            r = session.get(url, headers={"X-API-KEY": FUGLE_TOKEN}, timeout=2)
            if r.status_code == 200:
                res = r.json()
                p_close = safe_float(res.get("closePrice")) or safe_float(res.get("referencePrice"))
                p_open = safe_float(res.get("openPrice")) or p_close
                p_high = safe_float(res.get("highPrice")) or p_close
                p_low = safe_float(res.get("lowPrice")) or p_close
                v_shares = safe_float(res.get("total", {}).get("tradeVolume", 0))
                v_lots = v_shares / 1000.0 if v_shares > 0 else hist_last_vol_lots
                if p_close > 0:
                    return p_open, p_high, p_low, p_close, v_lots, True, "Fugle 富果雲端特快流", "realtime"
        except Exception: pass

    twse_channels = ["otc", "tse"] if is_otc_hint else ["tse", "otc"]
    twse_headers = {"Referer": "https://mis.twse.com.tw/stock/index.jsp", "X-Requested-With": "XMLHttpRequest"}
    for prefix in twse_channels:
        try:
            ts = int(time.time() * 1000)
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_id}.tw&json=1&delay=0&_={ts}"
            r = session.get(url, headers=twse_headers, timeout=2)
            if r.status_code == 200:
                data = r.json()
                if "msgArray" in data and len(data["msgArray"]) > 0:
                    info = data["msgArray"][0]
                    p_close = safe_float(info.get("z")) or safe_float(info.get("b", "").split("_")[0]) or safe_float(info.get("o"))
                    p_open = safe_float(info.get("o")) or p_close
                    p_high = safe_float(info.get("h")) or p_close
                    p_low = safe_float(info.get("l")) or p_close
                    v_lots = safe_float(info.get("v"))
                    v_lots = v_lots if v_lots > 0 else hist_last_vol_lots
                    if p_close > 0:
                        return p_open, p_high, p_low, p_close, v_lots, True, f"TWSE {prefix.upper()} 官方流", "realtime"
        except Exception: pass

    yahoo_suffixes = [".TWO", ".TW"] if is_otc_hint else [".TW", ".TWO"]
    for suffix in yahoo_suffixes:
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}?interval=1m&range=1d"
            r = session.get(url, timeout=2, verify=certifi.where())
            if r.status_code == 200:
                result = r.json().get("chart", {}).get("result", [])
                if result:
                    meta = result[0].get("meta", {})
                    p_close = safe_float(meta.get("regularMarketPrice"))
                    p_open = safe_float(meta.get("regularMarketDayOpen")) or p_close
                    p_high = p_close
                    p_low = p_close
                    try:
                        quotes = result[0].get("indicators", {}).get("quote", [{}])[0]
                        highs = [safe_float(h) for h in quotes.get("high", []) if h is not None]
                        lows = [safe_float(l) for l in quotes.get("low", []) if l is not None]
                        if highs: p_high = max(highs)
                        if lows: p_low = min(lows)
                    except Exception: pass
                    v_shares = safe_float(meta.get("regularMarketVolume", 0))
                    v_lots = v_shares / 1000.0 if v_shares > 0 else hist_last_vol_lots
                    if p_close > 0:
                        return p_open, p_high, p_low, p_close, v_lots, True, "Yahoo v8 K線流", "realtime"
        except Exception: pass
        
    return hist_last_close, hist_last_close, hist_last_close, hist_last_close, hist_last_vol_lots, False, "歷史收盤備援", "historical"

# ============ 6. Data Fetching Layers ============
@st.cache_data(ttl=1800)
def get_overnight_radar():
    session = get_requests_session()
    targets = {
        "台指期近月 (WTX=F)": "WTX=F", 
        "Nasdaq那指 (^IXIC)": "^IXIC",
        "費城半導體 (^SOX)": "^SOX",
        "台積電 ADR (TSM)": "TSM"
    }
    radar_results = {}
    is_us_panic = False
    panic_desc = ""
    wtx_change = 0.0
    
    for label, symbol in targets.items():
        for api_prefix in ["query2", "query1"]:
            try:
                url = f"https://{api_prefix}.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
                r = session.get(url, timeout=3, verify=certifi.where())
                if r.status_code == 200:
                    result_list = r.json().get("chart", {}).get("result")
                    if result_list and len(result_list) > 0:
                        quote = result_list[0].get("indicators", {}).get("quote", [{}])[0]
                        closes = [safe_float(c) for c in quote.get("close", []) if c is not None]
                        
                        if len(closes) >= 2:
                            current_price = closes[-1]
                            prev_close = closes[-2]
                        else:
                            meta = result_list[0].get("meta", {})
                            current_price = safe_float(meta.get("regularMarketPrice"))
                            prev_close = safe_float(meta.get("previousClose")) or safe_float(meta.get("chartPreviousClose"))
                        
                        if prev_close > 0:
                            change_pct = ((current_price - prev_close) / prev_close) * 100
                            radar_results[label] = change_pct
                            if symbol == "WTX=F":
                                wtx_change = change_pct
                            if symbol != "WTX=F" and change_pct <= -2.0:
                                is_us_panic = True
                                panic_desc = f"昨晚美股大震盪，{label} 慘跌 {change_pct:.1f}%"
                        else:
                            radar_results[label] = 0.0
                        break 
            except Exception:
                pass
                
    return radar_results, is_us_panic, panic_desc, wtx_change

@st.cache_data(ttl=3600)
def get_stock_info_df():
    api = get_api()
    df = api.taiwan_stock_info()
    if df is None or df.empty: return pd.DataFrame(columns=["stock_id", "stock_name", "type", "industry_category"])
    df = df.copy()
    df.columns = [c.strip() for c in df.columns]
    df["stock_id"] = df["stock_id"].astype(str).str.strip()
    df["industry_category"] = df["industry_category"].astype(str).str.strip()
    return df.copy()

@st.cache_data(ttl=900)
def get_daily_df(stock_id: str, market_type: str = "TSE", days: int = 450): 
    session = get_requests_session()
    is_otc_hint = any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "柜", "上櫃"])
    suffix = ".TWO" if is_otc_hint else ".TW"
    
    end_dt = datetime.now(TZ)
    start_dt = end_dt - timedelta(days=days)
    period1 = int(start_dt.timestamp())
    period2 = int(end_dt.timestamp())
    
    for api_prefix in ["query2", "query1"]:
        try:
            url = f"https://{api_prefix}.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}?period1={period1}&period2={period2}&interval=1d"
            r = session.get(url, timeout=5, verify=certifi.where())
            if r.status_code == 200:
                json_data = r.json()
                result_list = json_data.get("chart", {}).get("result")
                if result_list and len(result_list) > 0:
                    res_data = result_list[0]
                    timestamps = res_data.get("timestamp", [])
                    indicators = res_data.get("indicators", {})
                    quote = indicators.get("quote", [{}])[0]
                    adjclose_list = indicators.get("adjclose", [{}])[0].get("adjclose", [])
                    
                    dates = [datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d") for ts in timestamps]
                    opens = quote.get("open", [])
                    highs = quote.get("high", [])
                    lows = quote.get("low", [])
                    closes = quote.get("close", [])
                    volumes = quote.get("volume", [])
                    
                    raw_df = pd.DataFrame({
                        "date": dates, "open": opens, "high": highs, "low": lows, 
                        "close": closes, "vol": volumes, "adjclose": adjclose_list
                    }).dropna(subset=["close", "adjclose"])
                    
                    raw_df["factor"] = raw_df["adjclose"] / raw_df["close"].replace(0, 0.00001)
                    raw_df["open"] = raw_df["open"] * raw_df["factor"]
                    raw_df["high"] = raw_df["high"] * raw_df["factor"]
                    raw_df["low"] = raw_df["low"] * raw_df["factor"]
                    raw_df["close"] = raw_df["adjclose"]
                    raw_df["amount"] = raw_df["close"] * raw_df["vol"]
                    
                    df_final = raw_df[["date", "open", "high", "low", "close", "vol", "amount"]].copy()
                    if not df_final.empty:
                        return df_final
        except Exception:
            pass 
        
    api = get_api()
    start_date = start_dt.strftime("%Y-%m-%d")
    try:
        df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
        if df_raw is None or df_raw.empty: return None
        df = df_raw.copy()
        df.columns = [c.strip() for c in df.columns]
        df = df.rename(columns={"Trading_Volume": "vol", "Trading_money": "amount", "max": "high", "min": "low"})
        for c in ["open", "close", "high", "low", "vol", "amount"]:
            if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["close", "high", "low", "vol"]).copy()
    except Exception:
        pass
    return None

@st.cache_data(ttl=1800)
def get_market_macro_status():
    api = get_api()
    start_date = (datetime.now() - timedelta(days=150)).strftime("%Y-%m-%d")
    try:
        df = api.taiwan_stock_daily(stock_id="TAIEX", start_date=start_date)
        if df is not None and not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['MA20'] = df['close'].rolling(20).mean()
            df['MA60'] = df['close'].rolling(60).mean()
            
            last_row = df.iloc[-1]
            prev_row = df.iloc[-5] if len(df) >= 5 else df.iloc[0]
            
            market_5d_return = ((last_row['close'] - prev_row['close']) / prev_row['close']) * 100
            is_waterfall_panic = (last_row['close'] < last_row['MA20']) and (market_5d_return <= -3.5)
            
            bias_ma60 = ((last_row['close'] - last_row['MA60']) / last_row['MA60']) * 100
            is_market_overextended = bias_ma60 >= 8.5
            
            if is_waterfall_panic:
                return False, f"🚨 大盤瀑布重挫 ({last_row['close']:.1f})，近週跌 {market_5d_return:.1f}% 觸發【強勢股補跌危機】", True, False
            elif is_market_overextended:
                return True, f"⚠️ 大盤過熱警告 ({last_row['close']:.1f})，季線正乖離達 {bias_ma60:.1f}%【強制調降進場曝險】", False, True
            elif last_row['close'] >= last_row['MA20']:
                return True, f"加權指數 ({last_row['close']:.1f}) 站穩 20MA 多頭常態", False, False
            else:
                return False, f"加權指數 ({last_row['close']:.1f}) 跌破 20MA 空方警戒", False, False
    except Exception: pass
    return True, "🟢 多頭常態 (未取得大盤數據，預設寬鬆保護)", False, False

@st.cache_data(ttl=900)
def get_taiwan_enhanced_chips(stock_id: str, days: int = 30):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = "🟡 中性", "🟡 平穩", 0.0, 0.0
    try:
        inst_df = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
        if inst_df is not None and not inst_df.empty:
            inst_df = inst_df.sort_values("date")
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
            margin_df = margin_df.sort_values("date")
            margin_df['MarginPurchaseTodayBalance'] = pd.to_numeric(margin_df['MarginPurchaseTodayBalance'], errors='coerce')
            margin_diff = float(margin_df.iloc[-1]['MarginPurchaseTodayBalance'] - margin_df.iloc[-5]['MarginPurchaseTodayBalance'])
            if margin_diff > 1000: margin_trend = "🚨 散戶融資強套"
            elif margin_diff < -1000: margin_trend = "🟢 散戶融資大退"
    except Exception: pass
    return sitc_trend, margin_trend, sitc_3d_sum, margin_diff

@st.cache_data(ttl=900)
def get_rev_df(stock_id: str, days: int = 730):
    api = get_api()
    df = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"))
    return df.copy() if df is not None else None

@st.cache_data(ttl=86400)
def get_financial_statement_df(stock_id: str, years: int = 2):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
    try:
        df_raw = api.taiwan_stock_financial_statement(stock_id=stock_id, start_date=start_date)
        if df_raw is None or df_raw.empty: return pd.DataFrame()
        df = df_raw.copy()
        df["type"] = df["type"].replace({"OperatingRevenue": "Revenue"})
        targets = ["EPS", "Revenue", "GrossProfit", "OperatingIncome"]
        df = df[df["type"].isin(targets)]
        if df.empty: return pd.DataFrame()
        df_pivot = df.pivot_table(index="date", columns="type", values="value", aggfunc="last").reset_index()
        return df_pivot.copy()
    except Exception: return pd.DataFrame()

# 🌟 核心修正補丁：將回傳型態重構為 Primitive List 結構，徹底封印 Python 3.13 帶時區 Dataframe 序列化崩潰
@st.cache_data(ttl=300)
def get_realtime_news_list(stock_id: str, stock_name: str):
    news_list = []
    try:
        session = get_requests_session()
        query = f"{str(stock_name)} {str(stock_id)} when:1d"
        encoded_query = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
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
                df = df.sort_values(by="parsed_date", ascending=False)
                # 轉為純 Python List，完美繞過所有快取序列化地雷
                return df[["date", "title", "source", "link"]].to_dict('records')
    except Exception:
        pass
    return []

# ============ 7. Technical Engine ============
# (指標引擎保持不變)

# ============ 8. 操盤手自動路由型態與五維度整合決策大腦 (SOP v46) ============
def auto_strategy_classifier(res_dict):
    price = res_dict["current_price"]
    resistance = res_dict["real_resistance"]
    ma20_val = res_dict["ma20_val"]
    spring_verdict = res_dict["spring_verdict"]
    trend_phase = res_dict["trend_phase"]
    
    if "買點一成立" in spring_verdict or "買點二成立" in spring_verdict or "破底翻結構醞釀中" in spring_verdict:
        if price < resistance * 0.98:
            return "LEFT_SPRING", "🛡️ 左側交易：破底翻良性換手型態"

    if price >= resistance * 0.97 or (price > ma20_val and trend_phase == "🔥 波段多頭主升段"):
        return "RIGHT_BREAKOUT", "🚀 右側交易：強勢多頭突破型態"

    return "NEUTRAL_ZONE", "⚖️ 混沌常態：無極端共振型態（觀望為主）"

def unified_institutional_brain(res_dict, df_hist):
    strategy_type, strategy_name = auto_strategy_classifier(res_dict)
    
    price = res_dict["current_price"]
    vol = res_dict["current_vol"]
    vol_ma20 = res_dict["vol_ma20_val"]
    resistance = res_dict["real_resistance"]
    ma20_val = res_dict["ma20_val"]
    ma100_val = res_dict["ma100_val"]
    sitc_3d = res_dict["sitc_3d_sum"]
    macro_safe = "安全" in res_dict["macro_desc"] or "站穩" in res_dict["macro_desc"] or "過熱" in res_dict["macro_desc"]
    is_market_panic = res_dict.get("is_market_panic", False)
    is_market_overextended = res_dict.get("is_market_overextended", False)
    
    is_us_panic = res_dict.get("is_us_panic", False)
    us_panic_desc = res_dict.get("us_panic_desc", "")
    wtx_change = res_dict.get("wtx_change", 0.0)
    is_wtx_panic = wtx_change <= -1.0 
    
    final_decision = res_dict["final_decision"]
    atr = res_dict["atr"]
    
    f_is_good = "【財報年增擴張】" in res_dict["fin_conclusion"] or res_dict["latest_yoy"] >= 20
    f_is_bad = "【本業結構退步】" in res_dict["fin_conclusion"] and res_dict["latest_yoy"] < 5
    c_is_locked = "投信強力鎖碼" in res_dict["sitc_trend"] or "融資大量退場" in res_dict["margin_trend"]
    c_is_leaking = "投信高檔棄養" in res_dict["sitc_trend"] or "散戶融資強套" in res_dict["margin_trend"]
    t_is_strong = "起漲" in res_dict["short_term_trend"] or "多頭" in res_dict["short_term_trend"] or "噴發" in res_dict["short_term_trend"]
    
    peak_price_20d = float(df_hist["close"].tail(20).max())
    brk_trailing_stop = peak_price_20d - (2.5 * atr)
    recent_lowest = float(df_hist["low"].tail(10).min())

    is_kd_dead_cross = (df_hist["K9"].iloc[-1] < df_hist["D9"].iloc[-1]) and (df_hist["K9"].iloc[-2] >= df_hist["D9"].iloc[-2])
    is_high_risk_zone = df_hist["K9"].iloc[-1] > 75
    
    if "長上影" in final_decision or "金流陷阱" in final_decision or (is_kd_dead_cross and is_high_risk_zone):
        action_title = "🚨 🔴 【立即清倉 / 獲利了結】"
        if "長上影" in final_decision:
            verdict_msg = "❌ 爆量長上影：K 線顯示盤中衝高時有海量大戶資金瘋狂倒貨，上方套牢賣壓極其沉重，大腦無條件一票否決，立即獲利落袋離場！"
        elif "金流陷阱" in final_decision:
            verdict_msg = "🚨 惡性金流陷阱：早盤利用利多消息開極高誘騙散戶，終場大舉收在低檔且爆出歷史級巨量，明日必有續跌拋壓。現股全數退場！"
        else:
            verdict_msg = f"⚠️ 超買區高檔死亡交叉！技術指標在動能高位弱化，短線多頭主力獲利調節訊號明確。落袋為安，鎖住利潤！"

        return {
            "strategy_name": strategy_name, "color": "#FF4B4B", "action_now": action_title,
            "signal": "極端出貨與慣性改變訊號共振", "desc": verdict_msg,
            "blueprint": { "停損防守": "無（全面轉入清倉離場程序）", "移動停利": "無", "預期目標": "已見波段天花板，資金退場保全" }
        }

    if (is_wtx_panic or is_us_panic) and strategy_type == "RIGHT_BREAKOUT":
        return {
            "strategy_name": strategy_name, "color": "#F59E0B", "action_now": "⚠️ 🟡 【夜盤背離：沒收開火權觀望】",
            "signal": "🚨 跨市場金流断層：台指期夜盤與個股拉抬嚴重背離",
            "desc": f"危險！個股早盤試圖強行放量突破前高，但大腦深度縱向因果勾稽發現：昨晚台指期近月夜盤暴跌 {wtx_change:.2f}% 或美股科技股重挫（{us_panic_desc}）。在主力量化學中，這屬於典型的『期指要跌、拉抬現貨特定個股誘多出貨』的惡性背離。大腦直接沒收追高開火權，強制全面觀望，嚴防進場洗碗！",
            "blueprint": { "停損防守": "嚴禁開火進場", "移動停利": "無", "預期目標": "等待現貨市場完全消化跨市場夜盤利空" }
        }

    if is_wtx_panic and strategy_type == "LEFT_SPRING":
        return {
            "strategy_name": strategy_name, "color": "#EF4444", "action_now": "🛑 🔴 【期現貨跳空引信：取消低吸掛單】",
            "signal": "📉 夜盤引力崩塌：左側均線支撐全面失效",
            "desc": f"個股微觀型態雖符合高手低吸與築底特質，但昨晚台指期夜盤已實質暴跌 {wtx_change:.2f}%，預示今日現貨開盤加權指數將發動跳空低開。此時依據昨日支撐位進場拉回低吸，等同於雙手迎面阻擋高速火車（強勢股補跌）。大腦啟動最高防禦令，強制取消低吸計畫，靜待期貨現貨跌勢止穩！",
            "blueprint": { "停損防守": "禁止接飛刀", "移動停利": "無", "預期目標": "等待台指期5分K波段出現連3根紅K的止穩訊號" }
        }

    if is_market_panic and strategy_type != "RIGHT_BREAKOUT":
        return {
            "strategy_name": strategy_name, "color": "#EF4444", "action_now": "🛑 🔴 【強勢股補跌警戒：關閉低吸掛單】",
            "signal": "☠️ 總體流動性清算：多頭踩踏進行中",
            "desc": "警告！加權指數正處於快速失血重挫階段（5日內跌逾3.5%且失守月線），市場已進入非自願性清算期，極易觸發『投信被逼贖回核心持股』與『融資大斷頭踩踏』。此時任何強勢股回踩月線均為【致命價值陷阱】，高機率發生末跌段補跌。大腦已硬性關閉所有低吸試布局與加倉計畫，保留寶貴大資金實力！",
            "blueprint": { "停損防守": "禁止開火", "移動停利": "無", "預期目標": "靜待大盤流速減緩，出現連3日不破底的止穩訊號" }
        }

    if not macro_safe:
        return {
            "strategy_name": strategy_name, "color": "#FF4B4B", "action_now": "🚨 🔴 【強制空倉防禦 / 嚴禁開火】",
            "signal": "大盤空頭暴風雨警戒",
            "desc": f"個股短期架構雖為『{res_dict['short_term_trend']}』，但大盤跌破20MA生命線，大環境極不安全。強勢股此時的突破極易淪為主力『最後的拉高誘多出貨點』。基本面與技術動能在空頭陰影下無法產生向上共振，無條件一票否決！",
            "blueprint": { "停損防守": "禁止進場", "移動停利": "觀望", "預期目標": "等待加權指數重新站穩 20MA 多頭安全區" }
        }

    if strategy_type == "RIGHT_BREAKOUT":
        if macro_safe and price >= ma100_val and price >= resistance * 0.99 and res_dict["vol_spike"] and sitc_3d > 300 and f_is_good and c_is_locked:
            if is_market_overextended:
                return {
                    "strategy_name": strategy_name, "color": "#F59E0B", "action_now": "⚠️ 🟡 【大盤過熱：防守型控量輕倉開火】",
                    "signal": "⚡ 瘋狗浪末段逆勢突破：慎防高檔流動性陷阱",
                    "desc": f"個股觸發五維度黃金共振，架構極其強勢！但**加權指數目前與季線正乖離率已突破過熱天花板(>8.5%)**，全市場橡皮筋拉得極緊。此位階追高極易撞上大盤集體獲利回吐引發的假突破。大腦解鎖開火權，但硬性啟動降阻機制，將在底層扣減 60% 的進場資金曝險配額！",
                    "blueprint": {
                        "停損防守": f"收盤跌破前高支撐牆 {resistance:.2f} 元或硬性風控底線 {res_dict['stop_brk']:.2f} 元必須現股離場。",
                        "移動停利": f"盤中即時價若跌破動態 ATR 最高價回撤線 {brk_trailing_stop:.2f} 元啟動移動停利。",
                        "預期目標": f"波段獲利飽和擴張目標對位 {res_dict['target_brk']:.2f} 元。"
                    }
                }
            return {
                "strategy_name": strategy_name, "color": "#7D3CFF", "action_now": "🔮 🔮 【立即開火進場】",
                "signal": "🔮 頂級多頭共振：黃金主升飆股型態發動",
                "desc": f"五維度指標達成完美黃金交集！大盤多頭護航，月營收與財報同步確認為『基本面擴張』，疊加投信主力鎖碼（籌碼極淨）。技術面發動『{res_dict['short_term_trend']}』且實時量能確認碾壓 20 日前高壓力牆！上方無套牢怨魂，大膽切入！",
                "blueprint": {
                    "停損防守": f"收盤確認跌破前高支撐牆 {resistance:.2f} 元，或硬性滑價風控底線 {res_dict['stop_brk']:.2f} 元。",
                    "移動停利": f"盤中即時價若跌破動態 ATR 最高價回撤線 {brk_trailing_stop:.2f} 元，無條件啟動移動停利。",
                    "預期目標": f"第一階段波段獲利擴張目標對位 {res_dict['target_brk']:.2f} 元（盈虧比優異）。"
                }
            }
        
        if price < brk_trailing_stop:
            return {
                "strategy_name": strategy_name, "color": "#FF4B4B", "action_now": "⚠️ 🔴 【動態多頭防線破防：分批落袋】",
                "signal": "右側動能高位衰竭回撤",
                "desc": f"當前股價已跌破動態波動率移動安全防線 ({brk_trailing_stop:.2f} 元)。雖然型態上看似維持多頭，但微觀慣性已改，短線爆發力急凍，系統判定進入防守獲利落袋程序。",
                "blueprint": { "停損防守": "無", "移動停利": "已觸發", "預期目標": "鎖住波段利潤，資金騰出" }
            }
        
        if t_is_strong and f_is_good:
            return {
                "strategy_name": strategy_name, "color": "#1C86EE", "action_now": "🔥 🔵 【穩健波段主升：持股續抱】",
                "signal": "多方有序推進、基本面實質支撐",
                "desc": "個股短期與長期趨勢維持健康的多頭排列。月營收與獲利結構提供實質基本面支撐，大戶籌碼無異常失控撤退跡象。技術動能處於有序發散階段，手上已有部位者現股續抱，讓利潤在主升浪中奔跑。",
                "blueprint": {
                    "停損防守": f"技術硬性風控底線 {res_dict['stop_brk']:.2f} 元。",
                    "移動停利": f"當前波動率防守位置為 {brk_trailing_stop:.2f} 元，將隨盤中股價創高持續同步上移。",
                    "預期目標": f"波段持續看好對位目標價 {res_dict['target_brk']:.2f} 元。"
                }
            }

    return {
        "strategy_name": strategy_name, "color": "#1C86EE", "action_now": "⚖️ 🔵 【遵循量化紀律常規操作】",
        "signal": "後台因子互有勝負、未達極端背離",
        "desc": "後台財務與微觀動能因子未觸發極端的宏觀或籌碼背離共振。個股處於常態整理箱型區，請嚴格遵循下方精密雙軌交易藍圖精算之價位執行紀律操作。",
        "blueprint": {
            "停損防守": f"右側突破劇本防守線 {res_dict['stop_brk']:.2f} 元 | 左側低吸劇本防守線 {res_dict['stop_pb']:.2f} 元。",
            "移動停利": "未達強勢主升，暫不啟動動態停利線。",
            "預期目標": f"突破目標看 {res_dict['target_brk']:.2f} 元 | 拉回反彈目標看 {res_dict['target_pb']:.2f} 元。"
        }
    }

# ============ 9. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int):
    trend_phase = "⚖️ 綜合平衡盤整期"
    short_term_trend = "⚪ 技術因子調整中"
    long_term_trend = "⚪ 波段底蘊定型中"

    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    if match.empty: return None
    
    m_col = "type" if "type" in match.columns else "market_type" if "market_type" in match.columns else "market" if "market" in match.columns else None
    market_type = str(match[m_col].values[0]).strip().upper() if m_col else "TSE"
    stock_name = str(match["stock_name"].values[0])
    industry = str(match["industry_category"].values[0])

    df_raw = get_daily_df(stock_id, market_type=market_type, days=450)
    if df_raw is None or df_raw.empty: return None

    macro_bull, macro_desc, is_market_panic, is_market_overextended = get_market_macro_status()
    radar_results, is_us_panic, us_panic_desc, wtx_change = get_overnight_radar()
    
    hist_last_raw = df_raw.iloc[-1]
    hist_last_close = float(hist_last_raw["close"])
    hist_last_vol = float(hist_last_raw["vol"])
    
    rt_open, rt_high, rt_low, rt_close, rt_vol_lots, rt_success, rt_source, rt_type = compute_live_data(
        stock_id, market_type, hist_last_close, hist_last_vol
    )
    
    current_price = rt_close
    current_vol = rt_vol_lots 
    
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
            new_row = pd.DataFrame([{
                "date": today_str, "open": float(rt_open), "high": float(rt_high), "low": float(rt_low),
                "close": float(rt_close), "vol": float(rt_vol_lots * 1000.0), "amount": float(rt_close * rt_vol_lots * 1000.0)
            }])
            df_for_indicators = pd.concat([df_for_indicators, new_row], ignore_index=True)

    df = prepare_indicator_df(df_for_indicators)
    if df is None or df.empty: return None

    peak_price_20d = float(df["close"].tail(20).max())

    now_time = datetime.now(TZ).time()
    if datetime.strptime("09:00", "%H:%M").time() <= now_time <= datetime.strptime("13:30", "%H:%M").time():
        minutes_passed = (datetime.combine(datetime.today(), now_time) - datetime.combine(datetime.today(), datetime.strptime("09:00", "%H:%M").time())).total_seconds() / 60.0
        minutes_passed = max(1.0, minutes_passed)
        estimated_full_day_vol_lots = current_vol * (270.0 / minutes_passed)
    else:
        estimated_full_day_vol_lots = current_vol

    volume_poc = current_price
    hist_180 = df.tail(180)
    if len(hist_180) >= 20:
        counts, bins = np.histogram(hist_180["close"], bins=15, weights=hist_180["vol"])
        max_bin_idx = np.argmax(counts)
        volume_poc = float((bins[max_bin_idx] + bins[max_bin_idx + 1]) / 2)

    last_trade_date_str = str(df.iloc[-1]["date"])
    
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)
    if is_market_panic:
        m_desc = "🚨 大盤瀑布式清算恐慌潮"
        m_color = "red"
    elif wtx_change <= -1.0:
        m_desc = f"🚨 台指期夜盤崩盤 ({wtx_change:.2f}%)"
        m_color = "red"
    elif is_us_panic:
        m_desc = "🚨 盤前美股暴跌警戒中"
        m_color = "#F59E0B"
    elif is_market_overextended:
        m_desc = "⚠️ 大盤極端正乖離過熱"
        m_color = "orange"

    ma5_val, vol_ma5_val = float(df.iloc[-1]["MA5"]), float(df.iloc[-1]["MA5_Vol"])
    ma20_val, ma60_val, ma100_val = float(df.iloc[-1]["MA20"]), float(df.iloc[-1]["MA60"]), float(df.iloc[-1]["MA100"])
    vol_ma20_val, real_resistance = float(df.iloc[-1]["MA20_Vol"]), float(df.iloc[-1]["Res_20D"])
    current_bandwidth = float(df.iloc[-1]["BB_bandwidth"])
    bb_upper, bb_lower = float(df.iloc[-1]["BB_upper"]), float(df.iloc[-1]["BB_lower"])
    
    rsi_now = safe_float(df.iloc[-1].get("RSI14", 50.0))
    adx_now = safe_float(df.iloc[-1].get("ADX14", 20.0)) 
    macd_hist = safe_float(df.iloc[-1].get("MACD_HIST", 0.0))
    atr = safe_float(df.iloc[-1].get("ATR14", 1.0))
    k9_now = safe_float(df.iloc[-1].get("K9", 50.0))
    d9_now = safe_float(df.iloc[-1].get("D9", 50.0))

    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = get_taiwan_enhanced_chips(stock_id)
    turnover_std = df["vol"].tail(5).std() / vol_ma20_val if vol_ma20_val > 0 else 0
    main_force_score = 45.0
    if sitc_3d_sum > 500: main_force_score += 25.0
    if margin_diff < -1000: main_force_score += 15.0
    
    is_heavyweight = df["amount"].tail(20).mean() > 2000000000
    vol_multiplier, compress_quantile = (1.25, 0.35) if is_heavyweight else (2.2, 0.18)
    
    vol_spike = (estimated_full_day_vol_lots * 1000.0) > (vol_ma20_val * vol_multiplier)
    if vol_spike: main_force_score += 15.0
    if turnover_std > 0.4: main_force_score += 10.0
    main_force_label = f"🔥 強力控盤 ({main_force_score:.0f}%)" if main_force_score >= 65 else f"❄️ 籌碼散落 ({main_force_score:.0f}%)" if main_force_score <= 35 else f"⚖️ 常態調整 ({main_force_score:.0f}%)"
    is_compressed = current_bandwidth < df["BB_bandwidth"].tail(60).quantile(compress_quantile)

    bb_stage = "⚖️ 常態軌道整理中"
    kd_timing = "⚪ 進入常態整理區間"
    volume_verdict = "⚪ 常態量能交織"
    
    is_price_below_ma20_long = (df["close"].tail(10) < df["MA20"].tail(10)).sum() >= 7
    if is_compressed and is_price_below_ma20_long:
        bb_stage = "💤 打底觀望期：布林上下軌大幅收窄壓縮，股價長時運行於中軌下方。主力低檔吸籌洗盤【只觀察、絕不進場】"
    elif current_price > ma20_val and df["close"].iloc[-2] <= df["MA20"].iloc[-2] and df["close"].iloc[-1] > df["open"].iloc[-1]:
        bb_stage = "🔥 啟漲共振點：股價脫離下軌支撐，一根紅陽實體強勢突破藍色 MA20 中軌並收穩，趨勢正式由空轉多！"
    elif current_price >= bb_upper or (current_price > ma20_val and df["close"].tail(5).mean() > bb_upper * 0.95):
        bb_stage = "🚀 主升維持階段：強勢多頭沿布林黃色上軌持續推升，直至股價遠離軌道或滯漲拐頭才會進入調整"

    is_kd_had_low_cross_recently = False
    if len(df) >= 6:
        for i in range(-5, 0):
            if df["K9"].iloc[i] > df["D9"].iloc[i] and df["K9"].iloc[i-1] <= df["D9"].iloc[i-1] and df["K9"].iloc[i] < 35:
                is_kd_had_low_cross_recently = True
                break

    if k9_now < 20 and d9_now < 20:
        kd_timing = "📥 打底階段：KD 指鎖定於 20 以下超賣區，屬於典型超跌蓄力狀態，靜待共振反彈"
    elif "啟漲" in bb_stage and k9_now >= 45 and is_kd_had_low_cross_recently:
        kd_timing = "⚡ 共振啟漲點：符合歷史與指標物理順序！KD 先於低位金叉蓄勢落底，今日股價突破布林中軌時，KD 順勢衝破 50 多空分水嶺共振發動！"
    elif k9_now > 70:
        kd_timing = "🚨 高檔死亡交叉：超買區反轉弱化訊號觸發，短線多頭動能過熱衰退" if is_kd_dead_cross else "🦅 高檔判定：數值衝破 70 進入超買區，強勢主升浪出現長時間鈍化，無需看高盲目賣出，靜待死叉"

    is_volume_shrunk_long = (df["vol"].tail(10) < vol_ma20_val).sum() >= 7
    is_price_new_high_vol_drop = (df["close"].iloc[-1] > df["close"].tail(15).max() * 0.98) and ((current_vol * 1000.0) < vol_ma20_val * 0.8)
    
    if "打底" in bb_stage or is_volume_shrunk_long:
        volume_verdict = "📉 底部震盪期：成交量長期萎縮，代表浮動籌碼已被主力高度鎖定"
    elif vol_spike and ("啟漲" in bb_stage or current_price >= real_resistance * 0.95):
        volume_verdict = "🐳 共振突破點：突破關鍵位階且紅量柱連續堆高，遠高於均量，大資金真金白銀進場！"
    elif is_price_new_high_vol_drop:
        if main_force_score >= 65 or "強力鎖碼" in sitc_trend:
            volume_verdict = "🦅 強力鎖碼縮量主升：空氣單無量主升，續抱讓利潤奔跑！"
        else:
            volume_verdict = "🚨 散戶型量價背離風險：後續股價創新高或處高檔，但量能持續萎縮且缺乏特定大戶籌碼背書，多頭動能衰退，建議提前減倉防回調"

    spring_verdict = "⚪ 未觸發破底翻結構"
    spring_triggered = False
    detected_prior_low = 0.0
    detected_neckline = 0.0
    spring_lowest_low = 0.0
    
    if len(df) >= 40:
        past_slice = df.iloc[-40:-10]
        prior_low_candidate = float(past_slice["low"].min())
        prior_low_idx = past_slice["low"].idxmin()
        recent_slice = df.iloc[-10:]
        if not recent_slice.empty:
            spring_lowest_low = float(recent_slice["low"].min())
            
        for idx, (r_idx, row) in enumerate(recent_slice.iterrows()):
            if row["low"] < prior_low_candidate:
                r_pos = recent_slice.index.get_loc(r_idx)
                for offset in range(1, 4):
                    if r_pos + offset < len(recent_slice):
                        chk_idx = recent_slice.index[r_pos + offset]
                        if recent_slice.loc[chk_idx, "close"] > prior_low_candidate:
                            spring_triggered = True
                            detected_prior_low = prior_low_candidate
                            between_slice = df.loc[prior_low_idx:r_idx]
                            detected_neckline = float(between_slice["high"].max()) if not between_slice.empty else prior_low_candidate
                            break
                if spring_triggered: break
                    
    if spring_triggered:
        is_red_candle = df["close"].iloc[-1] > df["open"].iloc[-1]
        if current_price >= detected_prior_low and df["close"].iloc[-2] <= detected_prior_low and is_red_candle:
            spring_verdict = f"🟢 【破底翻：買點一成立】主力砸盤誘空完成！股價放量收陽線重新站回前低 {detected_prior_low:.2f} 元。第一安全切入點觸發，輕倉試布局！"
        elif current_price >= detected_neckline and vol_spike:
            spring_verdict = f"🔮 【破底翻：買點二成立】多頭翻轉爆發！股價放量強勢突破關鍵頸線 {detected_neckline:.2f} 元。確認開啟波段上漲，追加倉位穩健放大利潤！"
        else:
            spring_verdict = f"🔍 【破底翻結構醞釀中】觸發經典假破底洗盤（前低：{detected_prior_low:.2f}，關鍵頸線：{detected_neckline:.2f}），正等待多頭量能爆發之翻轉訊號。"

    kd_status = "黃金交叉" if k9_now > d9_now else "死亡交叉"
    if current_price >= ma5_val && ma5_val >= ma20_val: short_term_trend = f"🚀 五日線多頭噴發 (KD {kd_status})"
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
        rev_clean = rev_clean.dropna(subset=["revenue_year_growth_rate"]).sort_values("date")
        if not rev_clean.empty: latest_yoy = float(rev_clean.iloc[-1]["revenue_year_growth_rate"])

    fin_df = get_financial_statement_df(stock_id, years=2)
    fin_conclusion, pe_desc, pe_val, sum_eps_4q, gpm_now, opm_now = "📋 該標的暫無足夠季度財報歷史數據對比。", "⚪ 數據不足無法計算估值", 0.0, 0.0, 0.0, 0.0
    
    if not fin_df.empty and "Revenue" in fin_df.columns and "EPS" in fin_df.columns:
        fin_df = fin_df.sort_values("date").reset_index(drop=True)
        for col_name in ["Revenue", "EPS", "GrossProfit", "OperatingIncome"]:
            if col_name not in fin_df.columns: fin_df[col_name] = 0.0
                
        for idx in range(len(fin_df)):
            rev_amt = safe_float(fin_df.loc[idx, "Revenue"])
            fin_df.loc[idx, "gpm"] = (safe_float(fin_df.loc[idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df.loc[idx, "opm"] = (safe_float(fin_df.loc[idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        
        last_fin = fin_df.iloc[-1]
        eps_now, gpm_now, opm_now = safe_float(last_fin.get("EPS", 0.0)), safe_float(last_fin.get("gpm", 0.0)), safe_float(last_fin.get("opm", 0.0))
        sum_eps_4q = pd.to_numeric(fin_df.tail(4)['EPS'], errors='coerce').sum()
        if sum_eps_4q > 0:
            pe_val = current_price / sum_eps_4q
            
            dynamic_bubble_threshold = 35.0
            dynamic_cheap_threshold = 13.0
            if latest_yoy >= 30.0:
                dynamic_bubble_threshold = 55.0  
                dynamic_cheap_threshold = 22.0
            elif latest_yoy >= 15.0:
                dynamic_bubble_threshold = 45.0  
                dynamic_cheap_threshold = 18.0
                
            pe_desc = "🚨 估值瘋狂（高檔吹泡泡）" if pe_val > dynamic_bubble_threshold else "🟢 價值鐵板（安全邊際高）" if pe_val < dynamic_cheap_threshold else "⚖️ 估值合理區間"

    news_analysis_report = "⚪ 暫無最新重要輿情。"
    positive_catalysts_list = []
    
    # 🌟 修正整合：改為接收快取完畢的純 Python List，完美杜絕記憶體斷層
    raw_news_list = get_realtime_news_list(stock_id, stock_name)
    if raw_news_list:
        raw_news_list = raw_news_list[:8]
        for n in raw_news_list:
            lbl, col = analyze_news_sentiment(n["title"])
            n["sentiment"], n["color"] = lbl, col
            if "利多" in lbl: positive_catalysts_list.append(n["title"])
        pos_cnt = sum(1 for n in raw_news_list if "利多" in n["sentiment"])
        neg_cnt = sum(1 for n in raw_news_list if "利空" in n["sentiment"])
        if pos_cnt > neg_cnt: news_analysis_report = f"🔥 【輿情偏多】 利多消息主導市場情緒（多 {pos_cnt} 則 / 空 {neg_cnt} 則）。"
        elif neg_cnt > pos_cnt: news_analysis_report = f"🚨 【輿情偏空】 利空雜音浮現（空 {neg_cnt} 則 / 多 {pos_cnt} 則）。"

    recent_catalyst_summary = "⚪ 近 24H 內市場暫無顯著的突發消息面利多推升。"
    if positive_catalysts_list:
        recent_catalyst_summary = "<b>🎯 關鍵消息面利多題材：</b><br>" + "<br>".join([f"• {t}" for t in positive_catalysts_list[:2]])

    k_shadow_trap = bool(df.iloc[-1].get("is_long_upper_shadow", False)) and vol_spike
    open_gap_pct = ((safe_float(df.iloc[-1].get("open")) - safe_float(df.iloc[-2]["close"])) / safe_float(df.iloc[-2]["close"])) * 100 if len(df) > 1 else 0
    close_to_low_pct = ((current_price - rt_low) / (rt_high - rt_low)) if (rt_high - rt_low) > 0 else 1
    is_broker_dumping_risk = (open_gap_pct > 3.5) and (close_to_low_pct < 0.35) and (current_vol * 1000.0 > vol_ma20_val * 2.5)

    final_decision = "⚖️ 綜合評估"
    if k_shadow_trap: final_decision = "❌ 爆量長上影"
    elif is_broker_dumping_risk: final_decision = "🚨 惡性金流陷阱"

    t = tick_size(current_price)
    slip = float(slip_ticks) * t
    target_brk = round_to_tick(current_price + (4.0 * atr), t) if is_heavyweight else round_to_tick(current_price + (5.5 * atr), t)
    stop_brk = round_to_tick(real_resistance - (1.5 * atr) - slip, t)
    if stop_brk >= current_price: stop_brk = round_to_tick(current_price - (1.0 * atr), t)
    rr1_brk = (target_brk - current_price) / (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0

    target_pb = round_to_tick(real_resistance, t)
    stop_pb = round_to_tick(ma20_val - atr - slip, t)
    if stop_pb >= current_price: stop_pb = round_to_tick(current_price - (1.5 * atr), t)
    rr1_pb = (target_pb - current_price) / (current_price - stop_pb) if (current_price - stop_pb) > 0 else 0

    package = {
        "current_price": current_price, "current_vol": current_vol, "vol_ma20_val": vol_ma20_val,
        "real_resistance": real_resistance, "ma20_val": ma20_val, "ma100_val": ma100_val,
        "sitc_3d_sum": sitc_3d_sum, "margin_diff": margin_diff, "macro_desc": macro_desc,
        "is_market_panic": is_market_panic, 
        "is_market_overextended": is_market_overextended,
        "is_us_panic": is_us_panic,       
        "us_panic_desc": us_panic_desc,   
        "wtx_change": wtx_change,         
        "spring_verdict": spring_verdict, "final_decision": final_decision, "trend_phase": trend_phase,
        "vol_spike": vol_spike, "pe_desc": pe_desc, "margin_trend": margin_trend,
        "target_brk": target_brk, "stop_brk": stop_brk, "target_pb": target_pb, "stop_pb": stop_pb,
        "atr": atr, "fin_conclusion": fin_conclusion, "latest_yoy": latest_yoy,
        "sitc_trend": sitc_trend, "short_term_trend": short_term_trend, "volume_poc": volume_poc
    }
    
    tactical_blueprint = unified_institutional_brain(package, df.copy())
    
    if "破底翻" in tactical_blueprint["strategy_name"] and ("買點一成立" in spring_verdict or "買點二成立" in spring_verdict):
        tactical_blueprint["desc"] = f"{spring_verdict} 此型態屬於主力經典的誘空套路：砸破低點營造崩盤恐慌，誘騙散戶割肉後拉回。大腦已硬性將技術停損點對位在假破底最低價 {spring_lowest_low:.2f} 元。"
        expected_stop_price = round_to_tick(spring_lowest_low - t, t)
        if expected_stop_price >= current_price: 
            expected_stop_price = round_to_tick(current_price - (1.0 * atr), t)
        strategy_route = "🔮 破底翻底吸試 layout/加倉劇本"
    else:
        expected_stop_price = stop_brk if "突破" in tactical_blueprint["strategy_name"] else stop_pb
        strategy_route = "🚀 強勢突破前高劇本" if "突破" in tactical_blueprint["strategy_name"] else "🛡️ 均線拉回低吸劇本"

    if "突破" in tactical_blueprint["strategy_name"] or "夜盤背離" in tactical_blueprint["action_now"]:
        expected_target_price = target_brk
    else:
        expected_target_price = target_pb

    adjusted_risk = risk_per_trade
    if "立即" in tactical_blueprint["action_now"] and "清倉" in tactical_blueprint["action_now"]: adjusted_risk = 0.0
    elif "🛑" in tactical_blueprint["action_now"]: adjusted_risk = 0.0 
    elif "夜盤背離" in tactical_blueprint["action_now"]: adjusted_risk = 0.0 
    elif "期現貨跳空" in tactical_blueprint["action_now"]: adjusted_risk = 0.0 
    elif "防守型控量" in tactical_blueprint["action_now"]: adjusted_risk *= 0.4 
    elif "🔮" in tactical_blueprint["action_now"]: adjusted_risk *= 1.5 
    
    loss_per_share = current_price - expected_stop_price
    risk_money = total_capital * (adjusted_risk / 100) * 10000
    risk_lots = int((risk_money / loss_per_share) / 1000) if (loss_per_share > 0 and adjusted_risk > 0) else 0
    max_cash_lots = int((total_capital * 10000) / (current_price * 1000))
    suggested_lots = min(risk_lots, max_cash_lots) if risk_lots > 0 else 0

    return {
        "stock_id": stock_id, "stock_name": stock_name, "industry": industry, "current_price": current_price, "current_vol": current_vol,
        "ma5_val": ma5_val, "vol_ma5_val": vol_ma5_val, "ma20_val": ma20_val, "ma60_val": ma60_val, "ma100_val": ma100_val, "vol_ma20_val": vol_ma20_val, "real_resistance": real_resistance,
        "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_bandwidth": current_bandwidth, "rsi_now": rsi_now, "adx_now": adx_now,
        "macd_hist": macd_hist, "plus_di": float(df.iloc[-1].get("PLUS_DI", 0.0)), "minus_di": float(df.iloc[-1].get("MINUS_DI", 0.0)),
        "macro_desc": macro_desc, "sitc_trend": sitc_trend, "margin_trend": margin_trend, "sitc_3d_sum": sitc_3d_sum, "margin_diff": margin_diff,
        "latest_yoy": latest_yoy, "pe_val": pe_val, "pe_desc": pe_desc, "eps_4q": sum_eps_4q, "fin_conclusion": fin_conclusion,
        "gpm_now": gpm_now, "opm_now": opm_now, "is_compressed": is_compressed, "vol_spike": vol_spike,
        "news_analysis_report": news_analysis_report, "raw_news_list": raw_news_list, "trend_phase": trend_phase,
        "short_term_trend": short_term_trend, "long_term_trend": long_term_trend,
        "target_brk": target_brk, "stop_brk": stop_brk, "rr1_brk": rr1_brk,
        "target_pb": target_pb, "stop_pb": stop_pb, "rr1_pb": rr1_pb,
        "suggested_lots": suggested_lots, "expected_stop_price": expected_stop_price, "strategy_route": strategy_route,
        "expected_target_price": expected_target_price, 
        "trailing_stop_line": round_to_tick(peak_price_20d - (2.5 * atr), t),
        "rt_source": rt_source, "m_desc": m_desc, "m_color": m_color,
        "volume_poc": volume_poc, "main_force_label": main_force_label,
        "recent_catalyst_summary": recent_catalyst_summary, "fin_df": fin_df,
        "k9_now": k9_now, "d9_now": d9_now,
        "spring_verdict": spring_verdict, "bb_stage": bb_stage, "kd_timing": kd_timing, "volume_verdict": volume_verdict,
        "tactical_blueprint": tactical_blueprint,
        "radar_results": radar_results 
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
                scan_results.append({
                    "代碼": res["stock_id"], "股名": res["stock_name"], "盤中市價": f"{res['current_price']:.2f} 元",
                    "大腦路由分類": bp_data["strategy_name"].split("：")[-1], "當下即時動作": bp_data["action_now"],
                    "短期動能": res["short_term_trend"], "波段底蘊": res["long_term_trend"], "color_code": bp_data["color"]
                })
        if scan_results:
            df_scan = pd.DataFrame(scan_results)
            def highlight_verdict(row):
                return [f'background-color: {row["color_code"]}15; font-weight: 600;'] * len(row)
            st.dataframe(df_scan.style.apply(highlight_verdict, axis=1), column_order=["代碼", "股名", "盤中市價", "大腦路由分類", "當下即時動作", "短期動能", "波段底蘊"], use_container_width=True, height=360)

if diag_trigger or (not scan_trigger and stock_input):
    with st.spinner("五維度大腦深度因果解耦中..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input)
        if res is None: 
            st.error("該個股代碼數據獲取失敗，請確認編號是否正確。")
        else:
            bp_data = res["tactical_blueprint"]
            bp = bp_data["blueprint"]
            
            st.html(f"""
            <div style="background-color: {bp_data['color']}10; border: 2px solid {bp_data['color']}; padding: 22px; border-radius: 8px; margin-bottom: 25px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.03);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <span style="color: {bp_data['color']}; font-size: 14px; font-weight: 900; letter-spacing: 0.05em;">
                        📢 系統自動標籤分類：{bp_data['strategy_name']}
                    </span>
                    <span style="background-color: {bp_data['color']}; color: white; padding: 4px 12px; border-radius: 4px; font-size: 13px; font-weight:800; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                        {bp_data['action_now']}
                    </span>
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
                        color = "#10B981" if val >= 0 else "#EF4444"
                        arrow = "🔺" if val >= 0 else "🔻"
                        st.markdown(f"""
                        <div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:10px; border-radius:6px; text-align:center;">
                            <span style="font-size:12px; color:#64748B; font-weight:600;">{lbl}</span>
                            <h4 style="margin:4px 0 0 0; color:{color}; font-weight:800;">{arrow} {val:.2f}%</h4>
                        </div>
                        """, unsafe_allow_html=True)
            else:
                st.warning("⚠️ 跨市場夜盤雷達連線逾時或盤前伺服器維護中，暫無即時戰報數據。")

            st.markdown("<br>", unsafe_allow_html=True)

            st.markdown(f"""
            <div style="background-color: #1F2937; padding: 18px; border-radius: 8px; border: 2px solid #3B82F6; margin-bottom: 20px;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;">
                    <div>
                        <span style="color: #9CA3AF; font-size: 13px; font-weight: 600; letter-spacing: 0.05em;">DIAGNOSTIC TARGET</span>
                        <h1 style="margin: 4px 0 0 0; color: #FFFFFF; font-size: 28px; font-weight: 800;">{res['stock_name']} <span style="color: #3B82F6;">({res['stock_id']})</span></h1>
                    </div>
                    <div>
                        <span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">大類板塊歸屬</span>
                        <h3 style="margin: 4px 0 0 0; color: #F3F4F6; font-size: 18px; font-weight: 700;">{res['industry']}</h3>
                    </div>
                    <div style="text-align: right; background-color: rgba(255,255,255,0.05); padding: 6px 12px; border-radius: 6px;">
                        <span style="color: #9CA3AF; font-size: 11px; font-weight: 600; display:block;">即時流報價狀態</span>
                        <span style="color: #F9FAFB; font-weight: 600; font-size: 13px;">來源: {res['rt_source']} | 狀態: </span>
                        <span style="color: {res['m_color']}; font-weight: 700; font-size: 13px;">{res['m_desc']}</span>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            c1, c2, c3, c4 = st.columns(4)
            with c1: st.markdown(custom_hud_box("💡 當前即市價 (K線精密流解碼)", f"<span style='font-size:20px; color:#0F172A;'>{res['current_price']:.2f} 元</span><br><small style='color:#64748B; font-weight:500;'>今日成交: {res['current_vol']:.0f} 張</small>"), unsafe_allow_html=True)
            with c2: st.markdown(custom_hud_box("⏱️ 短期動能趨勢 (含MA5週線)", res["short_term_trend"], font_color="#10B981" if "多頭" in res["short_term_trend"] or "噴發" in res["short_term_trend"] else "#EF4444"), unsafe_allow_html=True)
            with c3: st.markdown(custom_hud_box("⏳ 長期波段底蘊", res["long_term_trend"], font_color="#7C3AED" if "主升段" in res["long_term_trend"] else "#64748B"), unsafe_allow_html=True)
            with c4: st.markdown(custom_hud_box("🎯 核心開火預期價", f"<span style='font-size:15px; color:#2563EB;'>{res['expected_target_price']:.2f} 元</span><br><small style='color:#64748B; font-weight:500;'>最佳對位: {res['strategy_route']}</small>"), unsafe_allow_html=True)

            st.markdown("### 🏛️ 四維度因子核心動態曝光面板")
            f1, f2, f3, f4 = st.columns(4)
            with f1:
                st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #10B981; min-height:185px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#065F46; font-size:14px; font-weight:700;">💎 財務面基本結構</h5>
                    <ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;">
                        <li>最新月營收YoY: <span style="color:#10B981; font-weight:700;">""" + f"{res['latest_yoy']:.1f}%" + """</span></li>
                        <li>單季毛利率: """ + f"{res['gpm_now']:.1f}%" + """</li>
                        <li>單季營益率: """ + f"{res['opm_now']:.1f}%" + """</li>
                        <li>體質定性: """ + res['fin_conclusion'].replace("📈", "").replace("📉", "").replace("⚖️", "").strip() + """</li>
                    </ul>
                </div>""", unsafe_allow_html=True)
            with f2:
                st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #3B82F6; min-height:185px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#1E40AF; font-size:14px; font-weight:700;">🦅 籌碼面核心金流</h5>
                    <ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;">
                        <li><b>神祕主力控盤度</b>: <span style="color:#2563EB; font-weight:700;">""" + res["main_force_label"] + """</span></li>
                        <li>投信3日進出: """ + f"{res['sitc_3d_sum']:.0f} 張" + """</li>
                        <li>散戶融資5日增減: """ + f"{res['margin_diff']:.0f} 張" + """</li>
                        <li>浮額沉澱狀態: """ + res['margin_trend'].replace("🚨", "").replace("🟢", "").replace("🟡", "").strip() + """</li>
                    </ul>
                </div>""", unsafe_allow_html=True)
            with f3:
                st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #F59E0B; min-height:175px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#92400E; font-size:14px; font-weight:700;">📊 估值面歷史位階</h5>
                    <ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;">
                        <li>滾動本益比: <span style="color:#D97706; font-weight:700;">""" + f"{res['pe_val']:.1f} 倍" + """</span></li>
                        <li>近四季總EPS: """ + f"{res['eps_4q']:.2f} 元" + """</li>
                        <li>位階判定: """ + res['pe_desc'].replace("🚨", "").replace("🟢", "").replace("⚖️", "").strip() + """</li>
                        <li>防禦邊際: """ + ("高鐵板 (便宜)" if res['pe_val']<13 else "常態區間" if res['pe_val']<=35 else "危險區 (泡沫)") + """</li>
                    </ul>
                </div>""", unsafe_allow_html=True)
            with f4:
                st.markdown("""<div style="background-color:#FDF4FF; padding:12px; border-radius:6px; border-top:4px solid #7C3AED; min-height:185px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#5B21B6; font-size:14px; font-weight:700;">⏱️ 微觀技術與消息面利多</h5>
                    <ul style="margin:6px 0 0 0; padding-left:16px; font-size:13px; color:#1E293B; line-height:1.45; font-weight:600;">
                        <li>五日攻擊線(MA5): <span style="color:#7C3AED;">""" + f"{res['ma5_val']:.2f} 元" + """</span></li>
                        <li>分價量密集牆(POC): """ + f"{res['volume_poc']:.2f} 元" + """</li>
                        <li>強弱指標: RSI=""" + f"{res['rsi_now']:.1f}" + """ / <b>KD=""" + f"{res['k9_now']:.1f}/{res['d9_now']:.1f}</b>" + """</li>
                    </ul>
                    <hr style="margin:6px 0; border:0; border-top:1px solid #E2E8F0;">
                    <p style="margin:0; padding:0; font-size:12px; color:#6B21A8; line-height:1.45; font-weight:600;">
                        """ + res["recent_catalyst_summary"] + """
                    </p>
                </div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            st.markdown("### 🗺️ 精密雙軌量化交易藍圖對照區")
            bl1, bl2 = st.columns(2)
            with bl1:
                st.markdown(f"""
                <div style="background-color: #F8FAFC; padding: 16px; border-radius: 6px; border-left: 5px solid #2563EB; border-top: 1px solid #E2E8F0; border-right: 1px solid #E2E8F0; border-bottom: 1px solid #E2E8F0;">
                    <h4 style="margin: 0 0 12px 0; color: #1E40AF; font-weight:800;">🚀 流派一：突破前高起漲劇本 (Breakout)</h4>
                    <p style="font-size: 14px; margin: 5px 0;"><b>精密建倉觸發點</b>：&le; {res['real_resistance']:.2f} 元</p>
                    <p style="font-size: 14px; margin: 5px 0;"><b>期望波段獲利目標</b>：<span style="color:#2563EB; font-weight:700;">{res['target_brk']:.2f} 元</span></p>
                    <p style="font-size: 14px; margin: 5px 0;"><b>技術硬性防守停損</b>：{res['stop_brk']:.2f} 元</p>
                    <p style="font-size: 14px; margin: 5px 0;"><b>期望風險報酬比 (R:R)</b>：{res['rr1_brk']:.2f}</p>
                </div>
                """, unsafe_allow_html=True)
            with bl2:
                st.markdown(f"""
                <div style="background-color: #F8FAFC; padding: 16px; border-radius: 6px; border-left: 5px solid #10B981; border-top: 1px solid #E2E8F0; border-right: 1px solid #E2E8F0; border-bottom: 1px solid #E2E8F0;">
                    <h4 style="margin: 0 0 12px 0; color: #065F46; font-weight:800;">🛡️ 流派二：均線拉回低吸劇本 (Pullback)</h4>
                    <p style="font-size: 14px; margin: 5px 0;"><b>精密低吸型買點</b>：貼近 {res['ma20_val']:.2f} 元</p>
                    <p style="font-size: 14px; margin: 5px 0;"><b>期望反彈獲利目標</b>：<span style="color:#10B981; font-weight:700;">{res['target_pb']:.2f} 元</span></p>
                    <p style="font-size: 14px; margin: 5px 0;"><b>技術硬性防守停損</b>：{res['stop_pb']:.2f} 元</p>
                    <p style="font-size: 14px; margin: 5px 0;"><b>期望風險報酬比 (R:R)</b>：{res['rr1_pb']:.2f}</p>
                </div>
                """, unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("### 🛡️ 量化核心風控配額開火劇本")
            
            if res["suggested_lots"] == 0:
                if "#FF4B4B" in bp_data["color"] or "#EF4444" in bp_data["color"] or "#F59E0B" in bp_data["color"]:
                    st.error("🚨 【核心風控最高警戒：大腦策略拒絕進場】 敞口強制關閉！跨市場美股或台指期夜盤重挫，系統已判定為背離出貨盤，強制禁止手癢開火。")
                else:
                    st.warning("⚠️ 【風控提示：資金配額不足 1 張】 當前標的趨勢極度健康！但因『核心大資金池』較小或『風險承受％』設得太嚴格，導致容許虧損金額小於單張股票的停損價差。系統為保護帳戶不給予強行開火建議。請至側邊欄調高資金池或放寬風險％數。")

            b1, b2, b3, b4 = st.columns(4)
            with b1: st.metric("精算風控進場配置", f"{res['suggested_lots']} 張", "大腦依劇本自動加減碼")
            with b2: st.metric("當前劇本硬停損價", f"{res['expected_stop_price']:.2f} 元")
            with b3: st.metric("盤中動態移動停利線", f"{res['trailing_stop_line']:.2f} 元")
            with b4: st.metric("大盤加權指數防禦網", "多頭過熱" if is_market_overextended else "多頭安全" if macro_bull else "空頭高風險", res["macro_desc"])

            st.markdown("---")
            st.markdown("### 🔍 跨因子微觀底層驗證數據")
            
            with st.expander("🧱 破底翻特徵與布林通道骨架大腦解碼", expanded=True):
                st.markdown(f"**⚡ 破底翻結構驗證裁決**：{res['spring_verdict']}")
                st.markdown(f"**🟡 布林通道大趨勢骨架**：{res['bb_stage']}")
                st.markdown(f"**⏱️ KDJ 時機捕捉定位**：{res['kd_timing']}")
                st.markdown(f"**🐳 真假主力資金成交量辨識**：{res['volume_verdict']}")
                st.markdown("""---""")
                st.markdown("""
                <small style='color:#64748B;'>
                <b>💡 戰術執行官專家提示：</b><br>
                • <b>跨市場因果連動</b>：昨晚美股費半與台積電ADR若重挫暴跌，系統在早盤開盤階段會**硬性封鎖並沒收右側追高劇本的資金配額（直接歸零）**，杜絕散戶被內資隔日沖主力利用開高誘多抓交替。<br>
                • <b>大盤過熱降阻</b>：當大盤與季線正乖離率拉得太緊（>8.5%）時，系統將判定為吹泡泡行情末段。此時一票否決所有左側抄底，且右側突破開火規模將**強制削減 60% 曝險資金**，強迫執行輕倉戰術防禦。<br>
                • <b>鎖碼豁免權</b>：當遇到量縮創新高時，若主力控盤度高，系統將判定為良性的『無量空氣單主升段』，無須恐慌減倉。
                </small>
                """, unsafe_allow_html=True)

            with st.expander("📊 財務基本面完整財務矩陣大表"):
                if not res["fin_df"].empty:
                    clean_fin_show = res["fin_df"].copy().sort_values("date", ascending=False)
                    clean_fin_show.columns = ["季度日期", "單季 EPS", "營業收入", "營業毛利", "營業利益", "單季毛利率 (%)", "單季營益率 (%)"]
                    st.dataframe(clean_fin_show.style.format({
                        "單季 EPS": "{:.2f}", "營業收入": "{:,.0f}", "營業毛利": "{:,.0f}", 
                        "營業利益": "{:,.0f}", "單季毛利率 (%)": "{:.2f}%", "單季營益率 (%)": "{:.2f}%"
                    }), use_container_width=True)

            with st.expander("📈 技術面後台詳細物理量"):
                st.write(f"**分價量密集牆(POC)** = `{res['volume_poc']:.2f}` 元 | **5日移動線 MA5** = `{res['ma5_val']:.2f}` 元 | **月生命線 MA20** = `{res['ma20_val']:.2f}` 元 | **20週代理線 MA100** = `{res['ma100_val']:.2f}` 元")
                st.write(f"**微觀指標物理量**: MACD 柱狀體 = `{res['macd_hist']:.3f}` | 通道帶寬 = `{res['bb_bandwidth']:.4f}` | ADX14 = `{res['adx_now']:.2f}` | +DI = `{res['plus_di']:.1f}` | -DI = `{res['minus_di']:.1f}`")

            with st.expander("📰 資訊面 24H 網路輿情即時新聞流水線"):
                st.markdown(f"> **24H 網路即時輿情綜合定論**：`{res['news_analysis_report']}`")
                if isinstance(res["raw_news_list"], list) and res["raw_news_list"]:
                    for n in res["raw_news_list"]: 
                        st.markdown(f"* **[{n['date']}]** 【{n['source']}】  [{n['sentiment']}]  [{n['title']}]({n['link']})")

if auto_refresh:
    time.sleep(5)
    st.rerun()
