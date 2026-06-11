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
    
    for api_prefix in ["query2", "query1"]:
        try:
            url = f"https://{api_prefix}.finance.yahoo.com/v8/finance/chart/{stock_id}"
            if FUGLE_TOKEN and api_prefix == "query2":
                url_fugle = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{stock_id}"
                r = session.get(url_fugle, headers={"X-API-KEY": FUGLE_TOKEN}, timeout=2)
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
                return df[["date", "title", "source", "link"]].to_dict('records')
    except Exception:
        pass
    return []

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
            "signal": "🚨 跨市場金流斷層：台指期夜盤與個股拉抬嚴重背離",
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
                "signal": "🔮 頂級多頭共振：黃金主升飆股型型態發動",
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
    turnover_std = df["vol"].tail(5).std() / vol_ma20_val if vol_ma20_
