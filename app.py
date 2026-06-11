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
st.set_page_config(page_title="SOP v44 機構級現股短中波段決策系統", layout="wide")

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
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    })
    return session

@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try: api.login_by_token(FINMIND_TOKEN)
        except Exception: pass
    return api

# ============ 5. Standardized Live Data Engine (Fugle Core Integration) ============
def compute_live_data_pro(stock_id: str, market_type: str, hist_last_close: float, hist_last_vol: float):
    """
    機構級標準化即時串流引擎 (修正成交量單位地雷：統一輸出為『張數 Lots』)
    回傳：(open, high, low, close, volume_lots, success, source)
    """
    hist_last_vol_lots = hist_last_vol / 1000.0 if hist_last_vol > 0 else 0.0
    session = get_requests_session()
    is_otc_hint = any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "柜", "上櫃"])
    
    # 🌟 第一優先線：Fugle 富果核心 REST API (單位修正：股 -> 張)
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
                
                # 富果回傳為原始股數 -> 強制標準化轉換為『張數』
                v_shares = safe_float(res.get("total", {}).get("tradeVolume", 0))
                v_lots = v_shares / 1000.0 if v_shares > 0 else hist_last_vol_lots
                
                if p_close > 0:
                    return p_open, p_high, p_low, p_close, v_lots, True, "Fugle 富果雲端特快流"
        except Exception: pass

    # ⚡ 第二線：臺灣證交所官方 API (原生定義即為張數)
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
                    
                    # 證交所官方 API 的 v 欄位原生即為『張數』
                    v_lots = safe_float(info.get("v"))
                    v_lots = v_lots if v_lots > 0 else hist_last_vol_lots
                    
                    if p_close > 0:
                        return p_open, p_high, p_low, p_close, v_lots, True, f"TWSE {prefix.upper()} 官方流"
        except Exception: pass

    # 🚀 第三線：Yahoo Finance v8 K線解碼流 (單位修正：股 -> 張)
    yahoo_suffixes = [".TWO", ".TW"] if is_otc_hint else [".TW", ".TWO"]
    for suffix in yahoo_suffixes:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}?interval=1m&range=1d"
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
                    
                    # Yahoo 回傳為股數 -> 轉為張數
                    v_shares = safe_float(meta.get("regularMarketVolume", 0))
                    v_lots = v_shares / 1000.0 if v_shares > 0 else hist_last_vol_lots
                    
                    if p_close > 0:
                        return p_open, p_high, p_low, p_close, v_lots, True, "Yahoo v8 K線流"
        except Exception: pass
        
    return hist_last_close, hist_last_close, hist_last_close, hist_last_close, hist_last_vol_lots, False, "歷史收盤備援"

# ============ 6. Data Fetching Layers ============
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
def get_daily_df(stock_id: str, days: int = 450): # 擴大天數以完整計算20週線(100日MA)
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
    if df_raw is None or df_raw.empty: return None
    df = df_raw.copy()
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"Trading_Volume": "vol", "Trading_money": "amount", "max": "high", "min": "low"})
    for c in ["open", "close", "high", "low", "vol", "amount"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close", "high", "low", "vol"]).copy()

@st.cache_data(ttl=1800)
def get_market_macro_status():
    api = get_api()
    start_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    try:
        df = api.taiwan_stock_daily(stock_id="TAIEX", start_date=start_date)
        if df is not None and not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['MA20'] = df['close'].rolling(20).mean()
            last_row = df.iloc[-1]
            return (last_row['close'] >= last_row['MA20']), f"加權指數 ({last_row['close']:.1f}) 站穩 20MA 多頭安全區" if last_row['close'] >= last_row['MA20'] else f"加權指數 ({last_row['close']:.1f}) 跌破 20MA 空方暴風雨警戒"
    except Exception: pass
    return True, "🟢 多頭常態 (未取得大盤數據，預設寬鬆保護)"

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
def get_realtime_news_df(stock_id: str, stock_name: str):
    session = get_requests_session()
    try:
        query = f"{str(stock_name)} {str(stock_id)} when:1d"
        encoded_query = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        r = session.get(url, timeout=5)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            news_list = []
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
                return df.sort_values(by="parsed_date", ascending=False).drop(columns=["parsed_date"]).copy()
    except Exception: pass
    return pd.DataFrame(columns=["date", "title", "source", "link"])

# ============ 7. Technical Engine ============
def prepare_indicator_df(df: pd.DataFrame):
    if df is None or df.empty: return None
    x = df.copy().sort_values("date").reset_index(drop=True)
    
    close_prev = x["close"].shift(1)
    x["TR"] = np.maximum(x["high"] - x["low"], np.maximum((x["high"] - close_prev).abs(), (x["low"] - close_prev).abs()))
    x["ATR14"] = x["TR"].ewm(com=13, adjust=False).mean()
    
    x["MA5"] = x["close"].rolling(5).mean()
    x["MA5_Vol"] = x["vol"].rolling(5).mean()
    x["MA20"] = x["close"].rolling(20).mean()
    x["MA60"] = x["close"].rolling(60).mean()
    x["MA100"] = x["close"].rolling(100).mean() # 🌟 引進 100 日均線作爲 20 週線(長線保護防線)之機構代理指標
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

    x["EMA12"] = x["close"].ewm(span=12, adjust=False).mean()
    x["EMA26"] = x["close"].ewm(span=26, adjust=False).mean()
    x["MACD_DIF"] = x["EMA12"] - x["EMA26"]
    x["MACD_SIGNAL"] = x["MACD_DIF"].ewm(span=9, adjust=False).mean()
    x["MACD_HIST"] = x["MACD_DIF"] - x["MACD_SIGNAL"]

    low_min = x["low"].rolling(9).min()
    high_max = x["high"].rolling(9).max()
    x["RSV"] = 100 * ((x["close"] - low_min) / (high_max - low_min).replace(0, 0.00001))
    k_list, d_list = [], []
    current_k, current_d = 50.0, 50.0
    for rsv in x["RSV"]:
        if pd.isna(rsv):
            k_list.append(np.nan)
            d_list.append(np.nan)
        else:
            current_k = (2/3) * current_k + (1/3) * rsv
            current_d = (2/3) * current_d + (1/3) * current_k
            k_list.append(current_k)
            d_list.append(current_d)
    x["K9"] = k_list
    x["D9"] = d_list

    if "open" in x.columns:
        x["upper_shadow"] = x["high"] - np.maximum(x["open"], x["close"])
        x["k_body"] = (x["open"] - x["close"]).abs()
        x["total_range"] = x["high"] - x["low"].replace(0, 0.00001)
        x["is_long_upper_shadow"] = (x["upper_shadow"] > x["k_body"]) & (x["upper_shadow"] / x["total_range"] > 0.4)
    else:
        x["is_long_upper_shadow"] = False

    return x.dropna(subset=["ATR14", "MA5", "MA20", "MA60", "MA100", "Res_20D", "BB_bandwidth", "RSI14", "MACD_HIST", "K9", "D9"]).copy()

# ============ 8. 操盤手四色戰術紅綠燈大腦 (現股短中波段專用) ============
def institutional_execution_brain(res_dict):
    """
    高盛自營部級別 - 現股短中波段絕對硬核進出場燈號決策模組
    """
    price = res_dict["current_price"]
    vol = res_dict["current_vol"]
    vol_ma20 = res_dict["vol_ma20_val"]
    resistance = res_dict["real_resistance"]
    ma20_val = res_dict["ma20_val"]
    ma100_val = res_dict["ma100_val"] # 週線代理指標
    sitc_3d = res_dict["sitc_3d_sum"]
    margin_diff = res_dict["margin_diff"]
    macro_safe = "安全" in res_dict["macro_desc"]
    spring = res_dict["spring_verdict"]
    final_decision = res_dict["final_decision"]
    
    # 🌟 絕對防線 A：長線保護短線原則 (週K線20MA硬性過濾)
    weekly_bull = price >= ma100_val
    
    # 🚨 出場訊號一：【🔴 紅燈 - 致命陷阱/主力高檔出貨】
    if "長上影" in final_decision or "金流陷阱" in final_decision or "世紀價值陷阱" in final_decision:
        return {
            "signal": "🛑 🔴 紅燈：高檔主力出貨/當沖踩踏（強制全數出場）",
            "color": "#FF4B4B",
            "action_code": "RED_SELL",
            "desc": "觸發極端爆量反轉或惡性金流陷阱！盤中強勢誘多後主力資金瘋狂拋售，所有追價者集體重傷。現股波段應立即無條件清倉避險，嚴禁死扛！"
        }
        
    # 🚨 出場訊號二：【🟡 黃燈 - 慣性改變/觸發移動停利線】
    if price < res_dict["trailing_stop_line"] or (price < ma20_val and df_close_tail_2_below_ma20(res_dict)):
        return {
            "signal": "⚠️ 🟡 黃燈：波段慣性改變/移動停利觸發（分批退場）",
            "color": "#F59E0B",
            "action_code": "YELLOW_EXIT",
            "desc": f"現價 ({price:.2f} 元) 已跌破動態波動率停利防線 ({res_dict['trailing_stop_line']:.2f} 元)，或是收盤確認跌破月生命線。波段上攻動能已宣告休克，現股落袋為安，鎖住利潤。"
        }

    # 🔥 進場劇本一：【🔮 紫燈 - 頂級多頭主升段強突破】(劇本A)
    if macro_safe and weekly_bull and price >= resistance * 0.99 and vol > (vol_ma20 * 1.8) and sitc_3d > 300:
        return {
            "signal": "🔮 🔮 紫燈：強勢突破創高、黃金主升段發動（現股買進開火）",
            "color": "#7D3CFF",
            "action_code": "PURPLE_BUY",
            "desc": "完美達成【大盤多頭＋20週線長線護航＋投信強力鎖碼＋帶量碾壓前高壓力牆】之黃金交集！上方無套牢怨魂，現股波段勝率極高，大膽切入。"
        }

    # 🔥 進場劇本二：【🟢 綠燈 - 良性破底翻右腳低吸】(劇本B)
    if "買點一成立" in spring or "買點二成立" in spring:
        return {
            "signal": "🟢 🟢 綠燈：經典假破底真洗盤成立（精密低吸建倉）",
            "color": "#10B981",
            "action_code": "GREEN_BUY",
            "desc": f"{spring} 主力砸盤誘空完成，散戶肉拋售，籌碼洗滌極淨。現股進場享有市場上最極致的風險報酬比，死穴停損極小。"
        }

    # ⚖️ 狀態三：【🔵 藍燈 - 常態持股續抱 / 牛皮觀望】
    return {
        "signal": "⚖️ 🔵 藍燈：未達極端共振訊號（持股續抱/死水觀望）",
        "color": "#1C86EE",
        "action_code": "BLUE_HOLD",
        "desc": "後台數據互有勝負，未觸發極端買賣點。若手上已有部位且未破防守線，現股持股續抱；若空倉則保持耐心，等待戰術紅綠燈亮起。"
    }

def df_close_tail_2_below_ma20(res_dict):
    # 輔助判定：是否連續兩天收盤在月線下方
    return "🛡️ 多頭架獲拉回洗盤期" in res_dict["trend_phase"] or "📉 空頭波段修正期" in res_dict["trend_phase"]

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

    df_raw = get_daily_df(stock_id, days=450)
    if df_raw is None or df_raw.empty: return None

    macro_bull, macro_desc = get_market_macro_status()
    hist_last_raw = df_raw.iloc[-1]
    hist_last_close = float(hist_last_raw["close"])
    hist_last_vol = float(hist_last_raw["vol"])
    
    # 🌟 呼叫重構後的標準化 K 線即時流引擎
    rt_open, rt_high, rt_low, rt_close, rt_vol_lots, rt_success, rt_source = compute_live_data_pro(
        stock_id, market_type, hist_last_close, hist_last_vol
    )
    
    current_price = rt_close
    current_vol = rt_vol_lots  # 盤中真實標準化張數
    
    df_for_indicators = df_raw.copy().sort_values("date").reset_index(drop=True)
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    
    # 🌟 修正 lookahead bias 地雷，將盤中真實高低點無縫拼接至日K序列
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

    # 分價量密集表天花板 (POC) 計算
    volume_poc = current_price
    hist_180 = df.tail(180)
    if len(hist_180) >= 20:
        counts, bins = np.histogram(hist_180["close"], bins=15, weights=hist_180["vol"])
        max_bin_idx = np.argmax(counts)
        volume_poc = float((bins[max_bin_idx] + bins[max_bin_idx + 1]) / 2)

    hist_last = df.iloc[-1]
    last_trade_date_str = str(hist_last["date"])
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

    ma5_val, vol_ma5_val = float(hist_last["MA5"]), float(hist_last["MA5_Vol"])
    ma20_val, ma60_val, ma100_val = float(hist_last["MA20"]), float(hist_last["MA60"]), float(hist_last["MA100"])
    vol_ma20_val, real_resistance = float(hist_last["MA20_Vol"]), float(hist_last["Res_20D"])
    current_bandwidth = float(hist_last["BB_bandwidth"])
    bb_upper, bb_lower = float(hist_last["BB_upper"]), float(hist_last["BB_lower"])
    
    rsi_now = safe_float(hist_last.get("RSI14", 50.0))
    macd_hist = safe_float(hist_last.get("MACD_HIST", 0.0))
    atr = safe_float(hist_last.get("ATR14", 1.0))
    k9_now = safe_float(hist_last.get("K9", 50.0))
    d9_now = safe_float(hist_last.get("D9", 50.0))

    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = get_taiwan_enhanced_chips(stock_id)
    
    # 神秘控盤標籤計算
    main_force_score = 45.0
    if sitc_3d_sum > 500: main_force_score += 25.0
    if margin_diff < -1000: main_force_score += 15.0
    
    is_heavyweight = df["amount"].tail(20).mean() > 2000000000
    vol_multiplier, compress_quantile = (1.25, 0.35) if is_heavyweight else (2.2, 0.18)
    vol_spike = (current_vol * 1000.0) > (vol_ma20_val * vol_multiplier)
    if vol_spike: main_force_score += 15.0
    main_force_label = f"🔥 強力控盤 ({main_force_score:.0f}%)" if main_force_score >= 65 else f"❄️ 籌碼散落 ({main_force_score:.0f}%)" if main_force_score <= 35 else f"⚖️ 常態調整 ({main_force_score:.0f}%)"
    is_compressed = current_bandwidth < df["BB_bandwidth"].tail(60).quantile(compress_quantile)

    # 布林通道/KDJ與成交量多維狀態定性
    bb_stage = "⚖️ 常態軌道整理中"
    kd_timing = "⚪ 進入常態整理區間"
    volume_verdict = "⚪ 常態量能交織"
    
    is_price_below_ma20_long = (df["close"].tail(10) < df["MA20"].tail(10)).sum() >= 7
    if is_compressed and is_price_below_ma20_long:
        bb_stage = "💤 打底觀望期：布林上下軌大幅收窄壓縮，主力低檔吸籌洗盤。"
    elif current_price > ma20_val and df["close"].iloc[-2] <= df["MA20"].iloc[-2] and hist_last["close"] > hist_last["open"]:
        bb_stage = "🔥 啟漲共振點：一根陽線實體突破中軌並收穩，趨勢正式由空轉多！"
    elif current_price >= bb_upper:
        bb_stage = "🚀 主升維持階段：強勢多頭沿布林上軌持續推升。"

    is_kd_dead_cross = (df["K9"].iloc[-1] < df["D9"].iloc[-1]) and (df["K9"].iloc[-2] >= df["D9"].iloc[-2])
    if k9_now < 20: kd_timing = "📥 打底階段：KD 指標落入超賣區，靜待共振反彈"
    elif k9_now > 70: kd_timing = "🚨 高檔死亡交叉" if is_kd_dead_cross else "🦅 高檔鈍化強勢主升浪"

    is_price_new_high_vol_drop = (df["close"].iloc[-1] > df["close"].tail(15).max() * 0.98) and ((current_vol * 1000.0) < vol_ma20_val * 0.8)
    if vol_spike and ("啟漲" in bb_stage or current_price >= real_resistance * 0.95):
        volume_verdict = "🐳 共振突破點：大資金真金白銀進場突破壓力壁壘！"
    elif is_price_new_high_vol_drop:
        volume_verdict = "🦅 強力鎖碼縮量主升（籌碼洗淨空氣單）" if main_force_score >= 65 else "🚨 散戶型量價背離風險"

    # 破底翻演算法優化
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
        if current_price >= detected_prior_low and df["close"].iloc[-2] <= detected_prior_low:
            spring_verdict = f"🟢 【破底翻：買點一成立】股價重新站回前低 {detected_prior_low:.2f} 元！主力洗盤結束。"
        elif current_price >= detected_neckline and vol_spike:
            spring_verdict = f"🔮 【破底翻：買點二成立】放量強勢突破關鍵頸線 {detected_neckline:.2f} 元！"
        else:
            spring_verdict = f"🔍 【破底翻結構醞釀中】（前低：{detected_prior_low:.2f}，關鍵頸線：{detected_neckline:.2f}）"

    # 均線多空定性
    kd_status = "黃金交叉" if k9_now > d9_now else "死亡交叉"
    if current_price >= ma5_val and ma5_val >= ma20_val: short_term_trend = f"🚀 五日線多頭噴發 (KD {kd_status})"
    elif current_price >= ma5_val: short_term_trend = f"📈 週線跌深反彈 (KD {kd_status})"
    else: short_term_trend = f"📉 均線全面蓋頭 (KD {kd_status})"
        
    if current_price >= ma60_val and (df["MA60"].iloc[-1] > df["MA60"].iloc[-5]): long_term_trend = "🔥 季線多頭主升排列"
    else: long_term_trend = "📉 季線下彎蓋頭修正"

    if current_price >= ma20_val and ma20_val >= ma60_val: trend_phase = "🔥 波段多頭主升段"
    elif current_price < ma20_val and ma20_val >= ma60_val: trend_phase = "🛡️ 多頭架獲拉回洗盤期"
    else: trend_phase = "📉 空頭波段修正期"

    # 財務基本面與財報精準 YoY 對位
    latest_yoy = 0.0
    rev_df = get_rev_df(stock_id, days=730)
    if rev_df is not None and not rev_df.empty and "revenue" in rev_df.columns:
        rev_clean = rev_df.copy()
        rev_clean["revenue"] = pd.to_numeric(rev_clean["revenue"].astype(str).str.replace(",", ""), errors="coerce")
        rev_clean["revenue_year_growth_rate"] = rev_clean["revenue"].pct_change(12) * 100
        rev_clean = rev_clean.dropna(subset=["revenue_year_growth_rate"]).sort_values("date")
        if not rev_clean.empty: latest_yoy = float(rev_clean.iloc[-1]["revenue_year_growth_rate"])

    fin_df = get_financial_statement_df(stock_id, years=2)
    fin_conclusion, pe_desc, pe_val, sum_eps_4q, gpm_now, opm_now = "📋 暫無季度財報數據", "⚪ 數據不足", 0.0, 0.0, 0.0, 0.0
    
    if not fin_df.empty and "Revenue" in fin_df.columns and "EPS" in fin_df.columns:
        fin_df = fin_df.sort_values("date").reset_index(drop=True)
        for col_name in ["Revenue", "EPS", "GrossProfit", "OperatingIncome"]:
            if col_name not in fin_df.columns: fin_df[col_name] = 0.0
                
        for idx in range(len(fin_df)):
            rev_amt = safe_float(fin_df.loc[idx, "Revenue"])
            fin_df.loc[idx, "gpm"] = (safe_float(fin_df.loc[idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df.loc[idx, "opm"] = (safe_float(fin_df.loc[idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        
        last_fin = fin_df.iloc[-1]
        gpm_now, opm_now = safe_float(last_fin.get("gpm", 0.0)), safe_float(last_fin.get("opm", 0.0))
        sum_eps_4q = pd.to_numeric(fin_df.tail(4)['EPS'], errors='coerce').sum()
        if sum_eps_4q > 0:
            pe_val = current_price / sum_eps_4q
            pe_desc = "🚨 估值偏高" if pe_val > 35 else "🟢 價值低窪" if pe_val < 13 else "⚖️ 估值合理"

        # 季度對位比對
        fin_df['dt'] = pd.to_datetime(fin_df['date'], errors='coerce')
        if len(fin_df) >= 5 and not fin_df['dt'].isnull().all():
            latest_q = fin_df.iloc[-1]
            t_year, t_month = latest_q['dt'].year - 1, latest_q['dt'].month
            match_prev = fin_df[(fin_df['dt'].dt.year == t_year) & (fin_df['dt'].dt.month == t_month)]
            if not match_prev.empty:
                prev_fin = match_prev.iloc[0]
                if safe_float(latest_q.get("EPS")) > safe_float(prev_fin.get("EPS")):
                    fin_conclusion = "📈 【財報年增擴張】本業獲利體質優於去年同期。"
                else:
                    fin_conclusion = "📉 【本業結構退步】單季財報獲利遜於去年同期。"

    # 消息輿情層
    news_analysis_report, recent_catalyst_summary, raw_news_list = "⚪ 暫無輿情", "⚪ 暫無突發利多", []
    news_df = get_realtime_news_df(stock_id, stock_name)
    if news_df is not None and not news_df.empty:
        raw_news_list = news_df.head(8).to_dict('records')
        pos_cnt = neg_cnt = 0
        for n in raw_news_list:
            lbl, col = analyze_news_sentiment(n["title"])
            n["sentiment"], n["color"] = lbl, col
            if "利多" in lbl: pos_cnt += 1
            elif "利空" in lbl: neg_cnt += 1
        news_analysis_report = f"🔥 輿情偏多 (多 {pos_cnt} / 空 {neg_cnt})" if pos_cnt > neg_cnt else f"🚨 輿情偏空 (空 {neg_cnt} / 多 {pos_cnt})"

    # 偵測微觀反轉特徵
    k_shadow_trap = bool(hist_last.get("is_long_upper_shadow", False)) and vol_spike
    open_gap_pct = ((safe_float(hist_last.get("open")) - safe_float(df.iloc[-2]["close"])) / safe_float(df.iloc[-2]["close"])) * 100 if len(df) > 1 else 0
    close_to_low_pct = ((current_price - rt_low) / (rt_high - rt_low)) if (rt_high - rt_low) > 0 else 1
    is_broker_dumping_risk = (open_gap_pct > 3.5) and (close_to_low_pct < 0.35) and (current_vol * 1000.0 > vol_ma20_val * 2.5)

    # 串聯舊版定性結論，做為輔助
    tech_short_status = "🚀 準備起漲" if (current_price >= real_resistance * 0.99 and vol_spike) else "🚀 多頭成形"
    final_decision, final_color, final_desc = "⚖️ 綜合評估", "blue", "常規藍圖執行"
    if k_shadow_trap: final_decision, final_color = "❌ 爆量長上影", "red"
    elif is_broker_dumping_risk: final_decision, final_color = "🚨 惡性金流陷阱", "red"

    # 計價常規劇本價位
    t = tick_size(current_price)
    slip = float(slip_ticks) * t
    target_brk = round_to_tick(current_price + (4.0 * atr), t)
    stop_brk = round_to_tick(real_resistance - (1.5 * atr) - slip, t)
    if stop_brk >= current_price: stop_brk = round_to_tick(current_price - (1.0 * atr), t)
    rr1_brk = (target_brk - current_price) / (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0

    target_pb = round_to_tick(real_resistance, t)
    stop_pb = round_to_tick(ma20_val - atr - slip, t)
    if stop_pb >= current_price: stop_pb = round_to_tick(current_price - (1.5 * atr), t)
    rr1_pb = (target_pb - current_price) / (current_price - stop_pb) if (current_price - stop_pb) > 0 else 0

    expected_target_price = target_brk if current_price >= real_resistance * 0.98 else target_pb
    expected_stop_price = stop_brk if current_price >= real_resistance * 0.98 else stop_pb
    strategy_route = "🚀 強勢突破前高劇本" if current_price >= real_resistance * 0.98 else "🛡️ 均線拉回低吸劇本"

    # 塞入基礎打包字典，供戰術紅綠燈大腦研判
    package = {
        "current_price": current_price, "current_vol": current_vol, "vol_ma20_val": vol_ma20_val,
        "real_resistance": real_resistance, "ma20_val": ma20_val, "ma100_val": ma100_val,
        "sitc_3d_sum": sitc_3d_sum, "margin_diff": margin_diff, "macro_desc": macro_desc,
        "spring_verdict": spring_verdict, "final_decision": final_decision, "trend_phase": trend_phase,
        "trailing_stop_line": round_to_tick(current_price - (2.5 * atr), t)
    }
    
    # 🌟 呼叫機構級四色戰術大腦
    tactical_light = institutional_execution_brain(package)
    
    # 🌟 【破底翻最高權限戰略重組覆蓋】
    if "買點一成立" in spring_verdict or "買點二成立" in spring_verdict:
        expected_stop_price = round_to_tick(spring_lowest_low - t, t)
        if expected_stop_price >= current_price: expected_stop_price = round_to_tick(current_price - (1.0 * atr), t)
        expected_target_price = target_pb
        strategy_route = "🔮 破底翻精密低吸劇本"

    # 🌟 【現股真實容量控制限制公式】：引入購買力天花板限制，防堵現股爆倉交割危機
    adjusted_risk = risk_per_trade
    if tactical_light["action_code"] == "RED_SELL": adjusted_risk = 0.0
    
    loss_per_share = current_price - expected_stop_price
    risk_money = total_capital * (adjusted_risk / 100) * 10000  # 核心風險核心資金(元)
    
    # 依風控金額計算之張數
    risk_lots = int((risk_money / loss_per_share) / 1000) if (loss_per_share > 0 and adjusted_risk > 0) else 0
    # 依現股實質購買力現金上限限制計算之最高張數 (天花板限制)
    max_cash_lots = int((total_capital * 10000) / (current_price * 1000))
    # 機構級雙軌取其小限制
    suggested_lots = min(risk_lots, max_cash_lots) if risk_lots > 0 else 0

    return {
        "stock_id": stock_id, "stock_name": stock_name, "industry": industry, "current_price": current_price, "current_vol": current_vol,
        "ma5_val": ma5_val, "vol_ma5_val": vol_ma5_val, "ma20_val": ma20_val, "ma60_val": ma60_val, "ma100_val": ma100_val, "vol_ma20_val": vol_ma20_val, "real_resistance": real_resistance,
        "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_bandwidth": current_bandwidth, "rsi_now": rsi_now, "macd_hist": macd_hist,
        "macro_desc": macro_desc, "sitc_trend": sitc_trend, "margin_trend": margin_trend, "sitc_3d_sum": sitc_3d_sum, "margin_diff": margin_diff,
        "latest_yoy": latest_yoy, "pe_val": pe_val, "pe_desc": pe_desc, "eps_4q": sum_eps_4q, "fin_conclusion": fin_conclusion,
        "gpm_now": gpm_now, "opm_now": opm_now, "is_compressed": is_compressed, "vol_spike": vol_spike,
        "fin_df": fin_df, "raw_news_list": raw_news_list, "news_analysis_report": news_analysis_report, "trend_phase": trend_phase,
        "short_term_trend": short_term_trend, "long_term_trend": long_term_trend, 
        "expected_target_price": expected_target_price, "expected_stop_price": expected_stop_price, "strategy_route": strategy_route,
        "target_brk": target_brk, "stop_brk": stop_brk, "rr1_brk": rr1_brk,
        "target_pb": target_pb, "stop_pb": stop_pb, "rr1_pb": rr1_pb,
        "suggested_lots": suggested_lots, "trailing_stop_line": package["trailing_stop_line"],
        "rt_source": rt_source, "m_desc": m_desc, "m_color": m_color,
        "volume_poc": volume_poc, "main_force_label": main_force_label,
        "k9_now": k9_now, "d9_now": d9_now,
        "spring_verdict": spring_verdict, "bb_stage": bb_stage, "kd_timing": kd_timing, "volume_verdict": volume_verdict,
        "tactical_light": tactical_light
    }

# ============ 10. UI Presentation Layer ============
with st.sidebar:
    st.header("🛡️ 機構級現股大資金風控池")
    capital = st.number_input("核心現股可動用大資金池 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆最大核心風險承受金 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守技術滑價 (Ticks)", 0, 5, 1)
    st.markdown("---")
    st.info("💡 系統已硬性啟動【現股實質容量控制限制公式】，大腦在精算建議張數時，會自動受限於你的實質購買力天花板，絕對防止超額交割爆倉。")

macro_bull, macro_label = get_market_macro_status()
full_info_df = get_stock_info_df()
all_industries = sorted([str(i) for i in full_info_df["industry_category"].unique() if i != "nan" and i != ""])

st.markdown("## 📡 現股短中波段 - 機構級綜合決策大腦看盤台")
top_col1, top_col2 = st.columns(2)

with top_col1:
    st.markdown("""<div style='background-color:#F0FDF4; padding:8px; border-radius:6px; border-left:4px solid #10B981; margin-bottom:8px;'><b style='color:#065F46; font-size:13.5px;'>流派 A：多策略/全板塊當下即時策略掃描選股池</b></div>""", unsafe_allow_html=True)
    scan_mode = st.selectbox(
        "選擇類股掃描池：", 
        ["🔥 大盤市值前15大權值股"] + all_industries
    )
    if "前15大" in scan_mode:
        industry_stocks = ["2330", "2454", "2308", "2317", "3711", "2383", "3037", "2345", "2881", "2382", "2882", "3017", "2412", "2891", "2303", "8069"]
        scan_label = "大盤特選權值股"
    else:
        industry_stocks = full_info_df[full_info_df["industry_category"] == scan_mode]["stock_id"].tolist()[:10]
        scan_label = scan_mode
    scan_trigger = st.button(f"🔍 啟動 【{scan_label}】 矩陣掃描排行", use_container_width=True)

with top_col2:
    st.markdown("""<div style='background-color:#EFF6FF; padding:8px; border-radius:6px; border-left:4px solid #3B82F6; margin-bottom:8px;'><b style='color:#1E40AF; font-size:13.5px;'>流派 B：個股五維度縱向因果深度診斷與策略開火</b></div>""", unsafe_allow_html=True)
    stock_input = st.text_input("輸入目標台股代碼：", value="8069")
    diag_trigger = st.button(f"🔥 執行 【{stock_input}】 全串聯深度診斷與利多監測", use_container_width=True)

st.markdown("---")

if scan_trigger:
    st.subheader(f"📊 【{scan_label}】即時動態排行矩陣")
    with st.spinner("操盤手矩陣洗滌中..."):
        scan_results = []
        for sid in industry_stocks:
            res = evaluate_stock(sid, capital, risk_pct, slip_input)
            if res:
                t_light = res["tactical_light"]
                scan_results.append({
                    "代碼": res["stock_id"], "股名": res["stock_name"], "盤中市價": f"{res['current_price']:.2f} 元",
                    "戰術紅綠燈號指令": t_light["signal"], "開火預期價": f"{res['expected_target_price']:.2f} 元", 
                    "風控防守價": f"{res['expected_stop_price']:.2f} 元", "建議配置張數": f"{res['suggested_lots']} 張", "color_code": t_light["color"]
                })
        if scan_results:
            df_scan = pd.DataFrame(scan_results)
            def highlight_verdict(row):
                return [f'background-color: {row["color_code"]}20; font-weight: 600;'] * len(row)
            st.dataframe(df_scan.style.apply(highlight_verdict, axis=1), column_order=["代碼", "股名", "盤中市價", "戰術紅綠燈號指令", "開火預期價", "風控防守價", "建議配置張數"], use_container_width=True, height=360)

if diag_trigger or (not scan_trigger and stock_input):
    with st.spinner("深度因果因子解耦中..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input)
        if res is None: st.error("代碼錯誤或數據庫連線失敗。")
        else:
            # 🌟 頂部重磅登場：機構級操盤手抬頭顯示器 (HUD 戰術四色燈)
            t_light = res["tactical_light"]
            st.markdown(f"""
            <div style="background-color: {t_light['color']}15; border: 2px solid {t_light['color']}; padding: 22px; border-radius: 8px; margin-bottom: 25px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);">
                <span style="color: {t_light['color']}; font-size: 13px; font-weight: 800; letter-spacing: 0.15em; display: block; margin-bottom: 4px;">INSTITUTIONAL TACTICAL COMMAND</span>
                <h2 style="margin: 0; color: {t_light['color']}; font-size: 25px; font-weight: 900;">{t_light['signal']}</h2>
                <hr style="margin: 12px 0; border: 0; border-top: 1px solid {t_light['color']}40;">
                <p style="margin: 0; color: #1E293B; font-size: 14.5px; font-weight: 600; line-height: 1.6; text-align: justify;"><b>戰術核心研判：</b>{t_light['desc']}</p>
            </div>
            """, unsafe_allow_html=True)

            # 基本資料橫幅
            st.markdown(f"""
            <div style="background-color: #1F2937; padding: 14px; border-radius: 6px; margin-bottom: 15px;">
                <div style="display: flex; justify-content: space-between; align-items: center; color: white;">
                    <div><span style="color: #9CA3AF; font-size: 11px;">TARGET</span><br><b style="font-size: 20px;">{res['stock_name']} ({res['stock_id']})</b></div>
                    <div><span style="color: #9CA3AF; font-size: 11px;">板塊</span><br><b style="font-size: 15px;">{res['industry']}</b></div>
                    <div style="text-align: right;"><span style="color: #9CA3AF; font-size: 11px;">串流狀態 ({res['rt_source']})</span><br><b style="color: {res['m_color']}; font-size: 13px;">{res['m_desc']}</b></div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            c1, c2, c3, c4 = st.columns(4)
            with c1: st.markdown(custom_hud_box("💡 盤中當前價", f"<span style='font-size:20px; color:#0F172A;'>{res['current_price']:.2f} 元</span><br><small style='color:#64748B;'>今日實時量: {res['current_vol']:.0f} 張</small>"), unsafe_allow_html=True)
            with c2: st.markdown(custom_hud_box("⏱️ 短期均線攻防", res["short_term_trend"], font_color="#10B981" if "多頭" in res["short_term_trend"] else "#EF4444"), unsafe_allow_html=True)
            with c3: st.markdown(custom_hud_box("⏳ 長期波段底蘊", res["long_term_trend"], font_color="#7C3AED" if "主升" in res["long_term_trend"] else "#64748B"), unsafe_allow_html=True)
            with c4: st.markdown(custom_hud_box("🎯 核心開火戰術劇本", f"<span style='font-size:14px; color:#2563EB;'>{res['strategy_route']}</span><br><small style='color:#64748B;'>期望對位目標: {res['expected_target_price']:.2f} 元</small>"), unsafe_allow_html=True)

            st.markdown("### 🏛️ 機構四維度底層因子面板")
            f1, f2, f3, f4 = st.columns(4)
            with f1:
                st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #10B981; min-height:175px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#065F46; font-size:13.5px; font-weight:700;">💎 財務基本面結構</h5>
                    <ul style="margin:8px 0 0 0; padding-left:14px; font-size:12.5px; color:#334155; line-height:1.5; font-weight:600;">
                        <li>營收 YoY: <span style="color:#10B981;">""" + f"{res['latest_yoy']:.1f}%" + """</span></li>
                        <li>毛利率 / 營益率: """ + f"{res['gpm_now']:.1f}% / {res['opm_now']:.1f}%" + """</li>
                        <li>體質定性: """ + res['fin_conclusion'].replace("📈", "").replace("📉", "").strip() + """</li>
                    </ul>
                </div>""", unsafe_allow_html=True)
            with f2:
                st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #3B82F6; min-height:175px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#1E40AF; font-size:13.5px; font-weight:700;">🦅 內資核心籌碼金流</h5>
                    <ul style="margin:8px 0 0 0; padding-left:14px; font-size:12.5px; color:#334155; line-height:1.5; font-weight:600;">
                        <li>大戶控盤度: """ + res["main_force_label"] + """</li>
                        <li>投信 3 日買賣: """ + f"{res['sitc_3d_sum']:.0f} 張" + """</li>
                        <li>融資 5 日增減: """ + f"{res['margin_diff']:.0f} 張" + """</li>
                    </ul>
                </div>""", unsafe_allow_html=True)
            with f3:
                st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #F59E0B; min-height:175px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#92400E; font-size:13.5px; font-weight:700;">📊 滾動歷史估值</h5>
                    <ul style="margin:8px 0 0 0; padding-left:14px; font-size:12.5px; color:#334155; line-height:1.5; font-weight:600;">
                        <li>動態 PE 值: <span style="color:#D97706;">""" + f"{res['pe_val']:.1f} 倍" + """</span></li>
                        <li>近四季總 EPS: """ + f"{res['eps_4q']:.2f} 元" + """</li>
                        <li>位階判定: """ + res['pe_desc'].strip() + """</li>
                    </ul>
                </div>""", unsafe_allow_html=True)
            with f4:
                st.markdown("""<div style="background-color:#FDF4FF; padding:12px; border-radius:6px; border-top:4px solid #7C3AED; min-height:175px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#5B21B6; font-size:13.5px; font-weight:700;">⏱️ 微觀技術與即時輿情</h5>
                    <ul style="margin:6px 0 0 0; padding-left:14px; font-size:12px; color:#1E293B; line-height:1.45; font-weight:600;">
                        <li>分價量密集牆(POC): """ + f"{res['volume_poc']:.2f} 元" + """</li>
                        <li>20週代理線(MA100): """ + f"{res['ma100_val']:.2f} 元" + """</li>
                        <li>強弱度: KD=""" + f"{res['k9_now']:.1f}/{res['d9_now']:.1f}" + """</li>
                    </ul>
                    <hr style="margin:4px 0; border:0; border-top:1px solid #E2E8F0;">
                    <span style="font-size:11px; color:#6B21A8; font-weight:700;">""" + res["news_analysis_report"] + """</span>
                </div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            # 雙軌量化交易藍圖對照區
            st.markdown("### 🗺️ 現股精密雙軌量化交易藍圖")
            bl1, bl2 = st.columns(2)
            with bl1:
                st.markdown(f"""
                <div style="background-color: #F8FAFC; padding: 14px; border-radius: 6px; border-left: 5px solid #2563EB; border-top: 1px solid #E2E8F0; border-right: 1px solid #E2E8F0; border-bottom: 1px solid #E2E8F0;">
                    <h4 style="margin: 0 0 8px 0; color: #1E40AF; font-weight:800; font-size:14px;">🚀 劇本 A：強勢突破前高主升段劇本 (Breakout)</h4>
                    <p style="font-size: 13px; margin: 4px 0;"><b>精密建倉突破點</b>：&ge; {res['real_resistance']:.2f} 元</p>
                    <p style="font-size: 13px; margin: 4px 0;"><b>期望波段獲利目標</b>：<span style="color:#2563EB; font-weight:700;">{res['target_brk']:.2f} 元</span></p>
                    <p style="font-size: 13px; margin: 4px 0;"><b>技術硬性防守停損</b>：{res['stop_brk']:.2f} 元</p>
                    <p style="font-size: 13px; margin: 4px 0;"><b>期望風險報酬比 (R:R)</b>：{res['rr1_brk']:.2f}</p>
                </div>
                """, unsafe_allow_html=True)
            with bl2:
                st.markdown(f"""
                <div style="background-color: #F8FAFC; padding: 14px; border-radius: 6px; border-left: 5px solid #10B981; border-top: 1px solid #E2E8F0; border-right: 1px solid #E2E8F0; border-bottom: 1px solid #E2E8F0;">
                    <h4 style="margin: 0 0 8px 0; color: #065F46; font-weight:800; font-size:14px;">🛡️ 劇本 B：破底翻良性換手洗盤劇本 (Spring Pullback)</h4>
                    <p style="font-size: 13px; margin: 4px 0;"><b>精密低吸確認點</b>：站穩 {res['ma20_val']:.2f} 元或觸發破底翻</p>
                    <p style="font-size: 13px; margin: 4px 0;"><b>期望反彈獲利目標</b>：<span style="color:#10B981; font-weight:700;">{res['target_pb']:.2f} 元</span></p>
                    <p style="font-size: 13px; margin: 4px 0;"><b>技術硬性防守停損</b>：{res['stop_pb']:.2f} 元</p>
                    <p style="font-size: 13px; margin: 4px 0;"><b>期望風險報酬比 (R:R)</b>：{res['rr1_pb']:.2f}</p>
                </div>
                """, unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("### 🛡️ 量化核心風控配額開火劇本")
            
            if res["suggested_lots"] == 0:
                st.error("🚨 【風控最高警戒：系統強制關閉開火權】標的觸發致命高檔賣壓，或你的大資金池無法承擔此現股位階之風險。")
            
            b1, b2, b3, b4 = st.columns(4)
            with b1: st.metric("精算現股進場配置", f"{res['suggested_lots']} 張", "已受購買力容量上限控制")
            with b2: st.metric("當前劇本硬停損價", f"{res['expected_stop_price']:.2f} 元")
            with b3: st.metric("盤中動態移動停利線", f"{res['trailing_stop_line']:.2f} 元")
            with b4: st.metric("大盤加權指數安全網", "安全區" if "安全" in res["macro_desc"] else "高風險空方區", res["macro_desc"])

            # 跨因子底層驗證數據
            st.markdown("---")
            with st.expander("🔍 跨因子微觀底層驗證數據檢視"):
                st.markdown(f"**⚡ 破底翻結構驗證裁決**：{res['spring_verdict']}")
                st.markdown(f"**🟡 布林通道大趨勢骨架**：{res['bb_stage']}")
                st.markdown(f"**⏱️ KDJ 時機捕捉定位**：{res['kd_timing']}")
                st.markdown(f"**🐳 真假主力資金成交量辨識**：{res['volume_verdict']}")
            
            with st.expander("📰 資訊面 24H 網路輿情即時新聞流水線"):
                if isinstance(res["raw_news_list"], list) and res["raw_news_list"]:
                    for n in res["raw_news_list"]: st.markdown(f"* **[{n['date']}]** 【{n['source']}】  [{n['sentiment']}]  [{n['title']}]({n['link']})")
