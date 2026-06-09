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
st.set_page_config(page_title="SOP v27.1 五維全串聯即時策略掃描系統", layout="wide")

# ============ 2. Global Constants ============
TZ = pytz.timezone("Asia/Taipei")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

# 預設大環境策略選股池（依大環境動態分類）
STRATEGY_POOLS = {
    "⚡ 多頭成長：科技 AI 主攻浪潮": ["2330", "2317", "2454", "2382", "3231", "2308"],
    "🔥 籌碼鎖碼：投信集團季底作帳": ["3450", "3034", "3661", "2603", "2379", "6271"],
    "🛡️ 價值防禦：低位階高股息抗震": ["2412", "1216", "2881", "2002", "1301", "2105"],
    "🔮 大轉折潛力：營收炸裂扭虧黑馬": ["2337", "2408", "3037", "2344", "4919", "6488"]
}

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

# ============ 4. Advanced Connection Layer ============
@st.cache_resource
def get_requests_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    return session

@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try: api.login_by_token(FINMIND_TOKEN)
        except Exception: pass
    return api

# ============ 5. Live Data Streaming Engine ============
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
                
                # 影子防線：打破證交所鎖漲跌停時成交價歸 0 的 Bug
                if z == 0:
                    h_val = safe_float(info.get("h"))
                    b_list = str(info.get("b", "")).split("_")
                    b_val = safe_float(b_list[0]) if b_list and b_list[0] != "" else 0
                    
                    if h_val > 0 and h_val >= safe_float(info.get("o")):
                        z = h_val  
                    elif b_val > 0:
                        z = b_val  
                    else:
                        z = safe_float(info.get("o")) if safe_float(info.get("o")) > 0 else hist_last_close
                
                if z > 0:
                    rt_price = z
                    rt_vol = v if v > 0 else hist_last_vol
                    rt_success = True
                    rt_source, rt_type = "TWSE 即時", "realtime"
    except Exception: pass

    if not rt_success:
        try:
            yahoo_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
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

# ============ 6. Data Fetching Layers ============
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
            df = df.sort_values("date").reset_index(drop=True)
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['MA20'] = df['close'].rolling(20).mean()
            last_row = df.iloc[-1]
            return (last_row['close'] >= last_row['MA20']), f"加權指數 ({last_row['close']:.1f}) 站穩 20MA 多頭常態" if last_row['close'] >= last_row['MA20'] else f"加權指數 ({last_row['close']:.1f}) 跌破 20MA 空方警戒"
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
        return df_pivot
    except Exception: return pd.DataFrame()

# 🛠️ 【核心修正二】使用標準 urllib.parse 並加固 try-except 防止特殊字串 hash 失敗閃退
@st.cache_data(ttl=300)
def get_realtime_news_df(stock_id: str, stock_name: str):
    news_list = []
    session = get_requests_session()
    try:
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
            return df.sort_values(by="parsed_date", ascending=False).drop(columns=["parsed_date"])
    except Exception: pass
    return pd.DataFrame(columns=["date", "title", "source", "link"])

# ============ 7. Technical Engine ============
def prepare_indicator_df(df: pd.DataFrame):
    if df is None or df.empty: return None
    x = df.copy().sort_values("date").reset_index(drop=True)
    
    close_prev = x["close"].shift(1)
    x["TR"] = np.maximum(x["high"] - x["low"], np.maximum((x["high"] - close_prev).abs(), (x["low"] - close_prev).abs()))
    x["ATR14"] = x["TR"].ewm(com=13, adjust=False).mean()
    
    # 🛠️ 【核心修正一：全無損補齊 5 日攻擊線與量能均線】
    x["MA5"] = x["close"].rolling(5).mean()
    x["MA5_Vol"] = x["vol"].rolling(5).mean()
    
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

    return x.dropna(subset=["ATR14", "MA5", "MA20", "MA60", "Res_20D", "BB_bandwidth", "RSI14"]).copy()

# ============ 8. 五維度因果縱向串聯決策大腦 ============
def cross_factor_decoupling_engine(macro_bull, trend_phase, fin_conclusion, sitc_trend, margin_trend, tech_short, latest_yoy, pe_desc):
    f_is_good = "【財報年增擴張】" in fin_conclusion or latest_yoy >= 20
    f_is_bad = "【本業結構退步】" in fin_conclusion and latest_yoy < 5
    c_is_locked = "投信強力鎖碼" in sitc_trend or "融資大量退場" in margin_trend
    c_is_leaking = "投信高檔棄養" in sitc_trend or "散戶融資強套" in margin_trend
    t_is_strong = tech_short in ["🚀 準備起漲", "🚀 多頭成形"] and "多頭" in trend_phase

    if not macro_bull:
        if t_is_strong:
            return "🚨 大盤空頭陷阱：提防假突破", "red", f"個股技術現況雖顯示為『{tech_short}』，且營收年增達 {latest_yoy:.1f}%。然而，宏觀加權指數跌破月線。系統風險高企時，強勢股突破高機率淪為主力的『拉高誘多出貨點』。基本面與技術動能在空頭暴風雨下無法產生向上共振，策略上無條件一票否決，嚴禁追價！"
        else:
            return "❌ 宏觀與個股全面雙空共振", "red", "大盤大環境位於空方警戒區，且個股自身技術架構亦步步下沉。基本面缺乏爆發動能，籌碼面呈現凌亂拋壓，屬於標準空頭波段，必須嚴格避開，保持絕對空倉防禦。"

    if macro_bull and pe_desc != "🚨 估值瘋狂（高檔吹泡泡）" and f_is_good and c_is_locked and t_is_strong:
        return "🔮 頂級多頭共振：黃金主升飆股", "purple", f"五維度指標達成完美黃金交集！加權指數多頭護航，個股本益比未過熱。月營收與財報同步確認為『基本面擴張』，疊加投信主力鎖碼與散戶融資退場（籌碼極淨）。此時技術面發動『{tech_short}』，屬於內資主力籌碼與基本面雙軌驅動的最高勝率飆股型態。策略：敞口調升至 1.5 倍，全力進攻！"

    if "主升段" in trend_phase and pe_desc == "🚨 估值瘋狂（高檔吹泡泡）" and (f_is_bad or c_is_leaking):
        return "💥 世紀價值陷阱：高檔出貨盤", "red", f"極度危險！雖然技術型態包裝成『{trend_phase}』且新聞表面熱絡，但縱向勾稽發現重大背離：滾動估值已達歷史瘋狂天花板，最新季度財報卻暴露出毛利營益率『雙降退步』。此時主力趁高大舉倒貨給融資散戶（融資暴增）。這完全是主力利用市場散戶樂觀情緒進行的『高檔套現抓交替』型態。策略：一票否決。"

    if "拉回洗盤期" in trend_phase and pe_desc in ["🟢 價值鐵板（安全邊際高）", "⚖️ 估值合理區間"] and "融資大量退場" in margin_trend:
        return "🛡️ 良性回檔：高手低吸黃金右腳", "green", f"中長期大波段季線穩健向上，短線股價跌破月線洗盤。串聯發現：滾動本益比已回踩至具有高度安全邊際的低位水準，且散戶融資不堪折磨、大舉割肉退場（籌碼重新沉澱至特定大戶手中）。這屬於典型的主力『良性換手期』而非波段終結。策略：防守性極強，精密低吸潛伏。"

    if "橫盤蓄勢期" in trend_phase and not f_is_good and "投信無顯著動作" in sitc_trend:
        return "💤 邊緣人時間：動能休克無量橫盤", "gray", "大盤雖安全，長線死水一條。月營收動能失速，季度財報缺乏亮點，且內資投信核心金流毫無建倉意願。此時技術面雖然維持橫盤築底，但缺乏催化劑（Catalyst），時間成本高昂。策略：無效資金配額，建議直接換股操作。"

    if t_is_strong and f_is_good:
        return "🔥 穩健波段主升：多方有序推進", "blue", f"大盤安全，個股短期與長期趨勢維持健康的多頭排列。月營收與獲利結構相符提供實質基本面支撐，主力籌碼無異常失控撤退跡象。技術動能處於有序發散階段，屬於高勝率的常態波段。策略：持股續抱。"

    return "⚖️ 綜合平衡：常規技術藍圖操作", "blue", "後台財務與微觀動能因子互有勝負，並未觸發極端的宏觀、籌碼或估值背離共振。請嚴格遵循下方量化交易藍圖精算之價位執行紀律操作。"

# ============ 9. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int):
    df_raw = get_daily_df(stock_id, days=365)
    if df_raw is None or df_raw.empty: return None

    hist_last_raw = df_raw.iloc[-1]
    hist_last_close = float(hist_last_raw["close"])
    hist_last_vol = float(hist_last_raw["vol"])
    
    current_price, current_vol, rt_success, rt_source, rt_type = compute_live_data(
        stock_id, hist_last_close, hist_last_vol
    )
    
    df_for_indicators = df_raw.copy()
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    
    if rt_success and (rt_type in ["realtime", "delayed"]):
        if str(df_for_indicators.iloc[-1]["date"]) == today_str:
            df_for_indicators.iloc[-1, df_for_indicators.columns.get_loc("close")] = current_price
            df_for_indicators.iloc[-1, df_for_indicators.columns.get_loc("high")] = max(current_price, float(df_for_indicators.iloc[-1]["high"]))
            df_for_indicators.iloc[-1, df_for_indicators.columns.get_loc("low")] = min(current_price, float(df_for_indicators.iloc[-1]["low"]))
            df_for_indicators.iloc[-1, df_for_indicators.columns.get_loc("vol")] = current_vol
        else:
            new_row = pd.DataFrame([{
                "date": today_str, "close": float(current_price),
                "high": float(max(current_price, hist_last_close)), "low": float(min(current_price, hist_last_close)),
                "vol": float(current_vol), "amount": float(current_price * current_vol * 1000) 
            }])
            df_for_indicators = pd.concat([df_for_indicators, new_row], ignore_index=True)

    df = prepare_indicator_df(df_for_indicators)
    if df is None or df.empty: return None

    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    
    # 🛠️ 【核心修正二】防護轉型，杜絕 NumPy 字串雜湊導致緩存異常閃退
    stock_name = str(match["stock_name"].values[0]) if not match.empty else "指定標的"
    industry = str(match["industry_category"].values[0]) if not match.empty else "未知板塊"

    hist_last = df.iloc[-1]
    last_trade_date_str = str(hist_last["date"])
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

    # 物理量提取
    ma5_val, vol_ma5_val = float(hist_last["MA5"]), float(hist_last["MA5_Vol"])
    ma20_val, ma60_val = float(hist_last["MA20"]), float(hist_last["MA60"])
    vol_ma20_val, real_resistance = float(hist_last["MA20_Vol"]), float(hist_last["Res_20D"])
    bb_upper, bb_lower, current_bandwidth = float(hist_last["BB_upper"]), float(hist_last["BB_lower"]), float(hist_last["BB_bandwidth"])
    rsi_now, adx_now, macd_hist = float(hist_last["RSI14"]), float(hist_last["ADX14"]), float(hist_last["MACD_HIST"])
    atr = float(hist_last["ATR14"])

    is_heavyweight = df["amount"].tail(20).mean() > 2000000000
    vol_multiplier, compress_quantile = (1.25, 0.35) if is_heavyweight else (2.2, 0.18)
    vol_spike = current_vol > (vol_ma20_val * vol_multiplier)
    is_compressed = current_bandwidth < df["BB_bandwidth"].tail(60).quantile(compress_quantile)

    # 🛠️ 【核心修正一：短期動能趨勢完全對齊五日攻擊線架構】
    if current_price >= ma5_val and ma5_val >= ma20_val:
        short_term_trend = "🚀 五日線多頭噴發 (MA5 > MA20)"
    elif current_price >= ma5_val and current_price < ma20_val:
        short_term_trend = "📈 短線跌深反彈 (站上 MA5 週線)"
    elif current_price < ma5_val and current_price >= ma20_val:
        short_term_trend = "⚠️ 短線跌破週線 (回踩月線 MA20)"
    else:
        short_term_trend = "📉 短期均線蓋頭 (現價 < MA5 < MA20)"
        
    if current_price >= ma60_val and (df["MA60"].iloc[-1] > df["MA60"].iloc[-5]):
        long_term_trend = "🔥 季線向上（主升段架構）"
    elif current_price < ma60_val and (df["MA60"].iloc[-1] < df["MA60"].iloc[-5]):
        long_term_trend = "📉 季線下彎（空頭修正波段）"
    else:
        long_term_trend = "💤 季線橫盤（箱型潛伏築底）"

    if current_price >= ma20_val and ma20_val >= ma60_val and (df["MA20"].iloc[-1] > df["MA20"].iloc[-5]): trend_phase = "🔥 波段多頭主升段"
    elif current_price < ma20_val and ma20_val >= ma60_val: trend_phase = "🛡️ 多頭架構拉回洗盤期"
    elif is_compressed: trend_phase = "💤 潛伏築底蓄勢期"
    else: trend_phase = "📉 空頭波段修正期"

    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = get_taiwan_enhanced_chips(stock_id)
    macro_bull, macro_desc = get_market_macro_status()
    
    # 月營收清洗與強制排序
    latest_yoy = 0.0
    rev_df = get_rev_df(stock_id, days=365)
    if rev_df is not None and not rev_df.empty and "revenue" in rev_df.columns:
        rev_clean = rev_df.copy()
        rev_clean["revenue"] = pd.to_numeric(rev_clean["revenue"].astype(str).str.replace(",", ""), errors="coerce")
        if "revenue_year_growth_rate" not in rev_clean.columns or rev_clean["revenue_year_growth_rate"].isnull().all():
            rev_clean = rev_clean.sort_values("date").reset_index(drop=True)
            rev_clean["revenue_year_growth_rate"] = rev_clean["revenue"].pct_change(12) * 100
        else:
            rev_clean["revenue_year_growth_rate"] = pd.to_numeric(rev_clean["revenue_year_growth_rate"].astype(str).str.replace("%", ""), errors="coerce")
        rev_clean = rev_clean.dropna(subset=["revenue_year_growth_rate", "revenue"])
        rev_clean = rev_clean[rev_clean["revenue"] > 0].sort_values("date")
        if not rev_clean.empty:
            latest_yoy = float(rev_clean.iloc[-1]["revenue_year_growth_rate"])

    # 季度財報清洗
    fin_df = get_financial_statement_df(stock_id, years=2)
    fin_conclusion = "📋 該標的暫無足夠季度財報歷史數據對比。"
    pe_desc = "⚪ 數據不足無法計算估值"
    pe_val = 0.0
    sum_eps_4q = 0.0
    gpm_now, opm_now = 0.0, 0.0
    
    if not fin_df.empty and "Revenue" in fin_df.columns and "EPS" in fin_df.columns:
        fin_df = fin_df.sort_values("date").reset_index(drop=True)
        for idx in range(len(fin_df)):
            rev_amt = safe_float(fin_df.loc[idx, "Revenue"])
            fin_df.loc[idx, "gpm"] = (safe_float(fin_df.loc[idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df.loc[idx, "opm"] = (safe_float(fin_df.loc[idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        
        last_fin = fin_df.iloc[-1]
        eps_now, gpm_now, opm_now = safe_float(last_fin.get("EPS", 0.0)), safe_float(last_fin.get("gpm", 0.0)), safe_float(last_fin.get("opm", 0.0))
        sum_eps_4q = pd.to_numeric(fin_df.tail(4)['EPS'], errors='coerce').sum()
        
        if sum_eps_4q > 0:
            pe_val = current_price / sum_eps_4q
            pe_desc = "🚨 估值瘋狂（高檔吹泡泡）" if pe_val > 35 else "🟢 價值鐵板（安全邊際高）" if pe_val < 13 else "⚖️ 估值合理區間"

        if len(fin_df) >= 5:
            prev_fin = fin_df.iloc[-5] 
            eps_prev, gpm_prev, opm_prev = safe_float(prev_fin.get("EPS", 0.0)), safe_float(prev_fin.get("gpm", 0.0)), safe_float(prev_fin.get("opm", 0.0))
            gpm_text = "優於去年" if gpm_now > gpm_prev else "遜於去年" if gpm_now < gpm_prev else "持平"
            opm_text = "優於去年" if opm_now > opm_prev else "遜於去年" if opm_now < opm_prev else "持平"
            eps_lbl = "多賺" if eps_now > eps_prev else "少賺" if eps_now < eps_prev else "持平"
            if gpm_now > gpm_prev and opm_now > opm_prev and eps_now > eps_prev:
                fin_conclusion = "📈 【財報年增擴張】 最新季度獲利指標全數超越去年同期！本業體質結構優化。"
            elif gpm_now < gpm_prev and opm_now < opm_prev and eps_now < eps_prev:
                fin_conclusion = "📉 【本業結構退步】 毛利、營益、EPS 同步遜於去年同期，需提高警覺。"
            else:
                fin_conclusion = f"⚖️ 【結構調整期】 對比去年同期：毛利率『{gpm_text}』、營益率『{opm_text}』、EPS『{eps_lbl}』。"

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
        elif neg_cnt > pos_cnt: news_analysis_report = f"🚨 【輿情偏空】 利空雜音浮現（空 {neg_cnt} 則 / 多 {pos_cnt} 則）。"

    final_decision, final_color, final_desc = cross_factor_decoupling_engine(
        macro_bull, trend_phase, fin_conclusion, sitc_trend, margin_trend, tech_short, latest_yoy, pe_desc
    )

    t = tick_size(current_price)
    slip = float(slip_ticks) * t
    
    target_atr_ratio = 4.0 if is_heavyweight else 5.5
    target_brk = round_to_tick(current_price + (target_atr_ratio * atr), t)
    stop_brk = round_to_tick(real_resistance - (1.5 * atr) - slip, t)
    if stop_brk >= current_price: stop_brk = round_to_tick(current_price - (1.0 * atr), t)
    rr1_brk = (target_brk - current_price) / (current_price - stop_brk) if (current_price - stop_brk) > 0 else 0

    target_pb = round_to_tick(real_resistance, t)
    stop_pb = round_to_tick(ma20_val - atr - slip, t)
    if stop_pb >= current_price: stop_pb = round_to_tick(current_price - (1.5 * atr), t)
    rr1_pb = (target_pb - current_price) / (current_price - stop_pb) if (current_price - stop_pb) > 0 else 0

    if final_color in ["purple", "red"] or current_price >= real_resistance * 0.98:
        expected_target_price = target_brk
        expected_stop_price = stop_brk
        strategy_route = "🚀 強勢突破前高劇本"
    else:
        expected_target_price = target_pb
        expected_stop_price = stop_pb
        strategy_route = "🛡️ 均線拉回低吸劇本"

    adjusted_risk = risk_per_trade
    if final_color == "red": adjusted_risk = 0.0
    elif final_color == "purple": adjusted_risk *= 1.5
    
    loss_per_share = current_price - expected_stop_price
    risk_money = total_capital * (adjusted_risk / 100) * 10000
    suggested_lots = int((risk_money / loss_per_share) / 1000) if (loss_per_share > 0 and adjusted_risk > 0) else 0

    return {
        "stock_id": stock_id, "stock_name": stock_name, "industry": industry, "current_price": current_price, "current_vol": current_vol,
        "ma5_val": ma5_val, "vol_ma5_val": vol_ma5_val, "ma20_val": ma20_val, "ma60_val": ma60_val, "vol_ma20_val": vol_ma20_val, "real_resistance": real_resistance,
        "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_bandwidth": current_bandwidth, "rsi_now": rsi_now, "adx_now": adx_now,
        "macd_hist": macd_hist, "plus_di": float(hist_last["PLUS_DI"]), "minus_di": float(hist_last["MINUS_DI"]),
        "macro_desc": macro_desc, "sitc_trend": sitc_trend, "margin_trend": margin_trend, "sitc_3d_sum": sitc_3d_sum, "margin_diff": margin_diff,
        "latest_yoy": latest_yoy, "pe_val": pe_val, "pe_desc": pe_desc, "eps_4q": sum_eps_4q, "fin_conclusion": fin_conclusion,
        "gpm_now": gpm_now, "opm_now": opm_now, "is_compressed": is_compressed, "vol_spike": vol_spike,
        "fin_df": fin_df, "raw_news_list": raw_news_list, "news_analysis_report": news_analysis_report, "trend_phase": trend_phase,
        "short_term_trend": short_term_trend, "long_term_trend": long_term_trend, 
        "expected_target_price": expected_target_price, "expected_stop_price": expected_stop_price, "strategy_route": strategy_route,
        "final_decision": final_decision, "final_color": final_color, "final_desc": final_desc,
        "target_brk": target_brk, "stop_brk": stop_brk, "rr1_brk": rr1_brk,
        "target_pb": target_pb, "stop_pb": stop_pb, "rr1_pb": rr1_pb,
        "suggested_lots": suggested_lots, "trailing_stop_line": round_to_tick(current_price - (2.5 * atr), t),
        "rt_source": rt_source, "m_desc": m_desc, "m_color": m_color
    }

# ============ 10. UI Layout UI components ============
# 🛠️ 【核心修正：自適應無縮碼 HTML 盒子】完全杜絕 st.metric 對長中文字字體的截斷
def custom_hud_box(title, value, font_color="#1E293B"):
    return f"""
    <div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; padding: 14px; border-radius: 6px; min-height: 105px; box-shadow: 0 1px 2px rgba(0,0,0,0.02); margin-bottom: 10px;">
        <span style="color: #64748B; font-size: 13px; font-weight: 600; display: block; margin-bottom: 5px; letter-spacing: 0.02em;">{title}</span>
        <span style="color: {font_color}; font-size: 15px; font-weight: 700; display: block; line-height: 1.45; white-space: normal; word-break: break-all;">{value}</span>
    </div>
    """

# 優先拉取大盤位階
macro_bull, macro_label = get_market_macro_status()

with st.sidebar:
    st.header("🦅 大環境多策略自選選股掃描")
    
    # 大環境策略自選漏斗
    strategy_choice = st.selectbox(
        "選擇當前市場大環境類別：", 
        list(STRATEGY_POOLS.keys())
    )
    selected_pool = STRATEGY_POOLS[strategy_choice]
    
    st.markdown("---")
    st.header("⚙️ 個股精密風控配置")
    stock_input = st.text_input("手動查閱/代入個股代碼", value=selected_pool[0])
    capital = st.number_input("核心交易總資本 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("单筆最大核心風險承受 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守滑價摩擦 (Ticks)", 0, 5, 1)

# =========================================================
# 【主畫面主控台邏輯】
# =========================================================
st.markdown("## 📡 策略大腦主動式綜合看板")

scan_trigger = st.button(f"🔍 執行【{strategy_choice}】全批次環境篩選掃描", use_container_width=True)

if scan_trigger:
    st.subheader(f"📊 大環境篩選排行榜：{strategy_choice}")
    with st.spinner("策略大腦正在對該策略池進行全因子因果勾稽..."):
        scan_results = []
        for sid in selected_pool:
            res = evaluate_stock(sid, capital, risk_pct, slip_input)
            if res:
                scan_results.append({
                    "個股代碼": res["stock_id"],
                    "個股名稱": res["stock_name"],
                    "即時現價": f"{res['current_price']:.2f} 元",
                    "戰略決策建議": res["final_decision"],
                    "短線動能": res["short_term_trend"],
                    "波段底蘊": res["long_term_trend"],
                    "預期目標價": f"{res['expected_target_price']:.2f} 元",
                    "核心風控張數": f"{res['suggested_lots']} 張",
                    "color_code": res["final_color"]
                })
        
        if scan_results:
            df_scan = pd.DataFrame(scan_results)
            def highlight_verdict(row):
                color_map = {"purple": "#7D3CFF22", "green": "#2BD9A122", "blue": "#1C86EE22", "red": "#FF4B4B22", "gray": "#80808022"}
                return [f'background-color: {color_map.get(row["color_code"], "#ffffff")}; font-weight: 600;'] * len(row)
            
            st.dataframe(
                df_scan.drop(columns=["color_code"]).style.apply(highlight_verdict, axis=1),
                use_container_width=True,
                height=250
            )
            st.success("💡 掃描完成！請在側邊欄輸入你想深度解析的個股代碼。")

st.markdown("---")

# 個股深度診斷區
if st.button("🔥 啟動【單一個股】五維度全串聯深度診斷", use_container_width=True) or (not scan_trigger and stock_input):
    with st.spinner("深度因果漏斗啟動中，全數物理量大對齊..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input)
        
        if res is None:
            st.error("代碼數據獲取失敗，請確認代碼是否正確。")
        else:
            # === 置頂檔案看板 ===
            st.markdown(f"""
            <div style="background-color: #1F2937; padding: 18px; border-radius: 8px; border: 2px solid #3B82F6; margin-bottom: 20px;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;">
                    <div>
                        <span style="color: #9CA3AF; font-size: 13px; font-weight: 600; letter-spacing: 0.05em;">DIAGNOSTIC TARGET</span>
                        <h1 style="margin: 4px 0 0 0; color: #FFFFFF; font-size: 28px; font-weight: 800;">{res['stock_name']} <span style="color: #3B82F6;">({res['stock_id']})</span></h1>
                    </div>
                    <div style="text-align: right;">
                        <span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">板塊分類</span>
                        <h3 style="margin: 4px 0 0 0; color: #F3F4F6; font-size: 18px; font-weight: 700;">{res['industry']}</h3>
                    </div>
                    <div style="text-align: right; background-color: rgba(255,255,255,0.05); padding: 6px 12px; border-radius: 6px;">
                        <span style="color: #9CA3AF; font-size: 11px; font-weight: 600; display:block;">即時流狀態</span>
                        <span style="color: #F9FAFB; font-weight: 600; font-size: 13px;">來源: {res['rt_source']} | 狀態: </span>
                        <span style="color: {res['m_color']}; font-weight: 700; font-size: 13px;">{res['m_desc']}</span>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            # === 前端 HUD 抬頭顯示牆（100% 自適應不漏字） ===
            c1, c2, c3, c4 = st.columns(4)
            with c1: 
                st.markdown(custom_hud_box("💡 當前即時市價", f"{res['current_price']:.2f} 元<br><small style='color:#64748B; font-weight:500;'>今日成交: {res['current_vol']:.0f} 張</small>"), unsafe_allow_html=True)
            with c2: 
                st.markdown(custom_hud_box("⏱️ 短期動能趨勢 (對齊MA5)", res["short_term_trend"], font_color="#10B981" if "多頭" in res["short_term_trend"] else "#EF4444"), unsafe_allow_html=True)
            with c3: 
                st.markdown(custom_hud_box("⏳ 長期波段底蘊", res["long_term_trend"], font_color="#7C3AED" if "主升段" in res["long_term_trend"] else "#64748B"), unsafe_allow_html=True)
            with c4: 
                st.markdown(custom_hud_box("🎯 預期目標價位", f"{res['expected_target_price']:.2f} 元<br><small style='color:#64748B; font-weight:500;'>{res['strategy_route']}</small>", font_color="#3B82F6"), unsafe_allow_html=True)

            # === 最終決策建議 ===
            st.markdown("### 🎯 決策大腦全方位縱向串聯裁決")
            color_hex = {"red": "#FF4B4B", "purple": "#7D3CFF", "green": "#2BD9A1", "blue": "#1C86EE", "gray": "#808080"}[res["final_color"]]
            st.markdown(f"""
            <div style="background-color:{color_hex}10; border-left: 6px solid {color_hex}; padding: 18px; border-radius: 6px; margin-bottom: 20px;">
                <h3 style="margin:0; color:{color_hex}; font-size:20px; font-weight:800;">【最終戰略判定：{res['final_decision']}】</h3>
                <p style="margin: 12px 0 0 0; color:#1E293B; font-size:14.5px; font-weight:600; line-height:1.6; text-align: justify;">{res['final_desc']}</p>
            </div>
            """, unsafe_allow_html=True)

            # === 四維度核心因子主畫面曝光面板 ===
            st.markdown("### 🏛️ 四維度因子核心動態曝光面板")
            f1, f2, f3, f4 = st.columns(4)
            
            with f1:
                st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #10B981; min-height:175px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#065F46; font-size:14px; font-weight:700;">💎 財務面基本結構</h5>
                    <ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;">
                        <li>最新月營收YoY: <span style="color:#10B981; font-weight:700;">""" + f"{res['latest_yoy']:.1f}%" + """</span></li>
                        <li>單季毛利率: """ + f"{res['gpm_now']:.1f}%" + """</li>
                        <li>單季營益率: """ + f"{res['opm_now']:.1f}%" + """</li>
                        <li>體質定性: """ + res['fin_conclusion'].replace("📈", "").replace("📉", "").replace("⚖️", "").strip() + """</li>
                    </ul>
                </div>""", unsafe_allow_html=True)
                
            with f2:
                st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #3B82F6; min-height:175px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#1E40AF; font-size:14px; font-weight:700;">🦅 籌碼面核心金流</h5>
                    <ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.5; font-weight:600;">
                        <li>投信3日進出: <span style="color:#3B82F6; font-weight:700;">""" + f"{res['sitc_3d_sum']:.0f} 張" + """</span></li>
                        <li>投信評等: """ + res['sitc_trend'].replace("🟢", "").replace("🔴", "").replace("🟡", "").strip() + """</li>
                        <li>融資5日增減: """ + f"{res['margin_diff']:.0f} 張" + """</li>
                        <li>散戶浮額: """ + res['margin_trend'].replace("🚨", "").replace("🟢", "").replace("🟡", "").strip() + """</li>
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
                # 🛠️ 【核心修正一：在核心面板中主動補齊 5日均線、5日均量曝光】
                st.markdown("""<div style="background-color:#F8FAFC; padding:12px; border-radius:6px; border-top:4px solid #7C3AED; min-height:165px; border-left:1px solid #E2E8F0; border-right:1px solid #E2E8F0; border-bottom:1px solid #E2E8F0;">
                    <h5 style="margin:0; color:#5B21B6; font-size:14px; font-weight:700;">⏱️ 微觀技術與 5日線動能</h5>
                    <ul style="margin:8px 0 0 0; padding-left:16px; font-size:13px; color:#334155; line-height:1.4; font-weight:600;">
                        <li><b>五日攻擊線(MA5)</b>: <span style="color:#7C3AED; font-weight:700;">""" + f"{res['ma5_val']:.2f} 元" + """</span></li>
                        <li><b>五日移動均量</b>: """ + f"{res['vol_ma5_val']:.0f} 張" + """</li>
                        <li>擺動強弱RSI14: """ + f"{res['rsi_now']:.1f}" + """</li>
                        <li>趨勢強度ADX14: """ + f"{res['adx_now']:.1f}" + """</li>
                        <li>布林通道帶寬: """ + f"{res['bb_bandwidth']:.4f} " + ("(緊縮)" if res['is_compressed'] else "(發散)") + """</li>
                    </ul>
                </div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            # === 量化防禦風控配額 ===
            st.markdown("### 🛡️ 量化核心風控配額開火劇本")
            if res["suggested_lots"] == 0:
                st.error("🚨 【核心風控最高警戒：拒絕進場】 決策大腦判定多空因子嚴重負向共振，資金敞口強制關閉（0張），請保持空倉防禦。")
            
            b1, b2, b3, b4 = st.columns(4)
            with b1: st.metric("精算風控進場配置", f"{res['suggested_lots']} 張", "已自動扣除滑價與大腦敞口調節")
            with b2: st.metric("技術硬性防守停損價", f"{res['expected_stop_price']:.2f} 元", "觸價即刻無條件執行")
            with b3: st.metric("盤中動態移動停利線", f"{res['trailing_stop_line']:.2f} 元", "最高收盤價回撤 2.5 * ATR")
            with b4: st.metric("大盤加權指數防禦網", "多頭安全" if "站穩" in res["macro_desc"] else "空頭高風險", res["macro_desc"])
            
            st.markdown("---")

            # === 備用底層數據驗證漏斗 ===
            st.markdown("### 🔍 跨因子微觀底層驗證數據")
            
            with st.expander("📊 財務基本面完整財務矩陣大表"):
                if not res["fin_df"].empty:
                    clean_fin_show = res["fin_df"].copy().sort_values("date", ascending=False)
                    clean_fin_show.columns = ["季度日期", "單季 EPS", "營業收入", "營業毛利", "營業利益", "單季毛利率 (%)", "單季營益率 (%)"]
                    st.dataframe(clean_fin_show.style.format({
                        "單季 EPS": "{:.2f}", "營業收入": "{:,.0f}", "營業毛利": "{:,.0f}", 
                        "營業利益": "{:,.0f}", "單季毛利率 (%)": "{:.2f}%", "單季營益率 (%)": "{:.2f}%"
                    }), use_container_width=True)

            with st.expander("📈 技術面後台詳細物理量"):
                tc1, tc2 = st.columns(2)
                with tc1:
                    st.write(f"* **均線基準**: 5日 MA5 = `{res['ma5_val']:.2f}` 元 | 月線 MA20 = `{res['ma20_val']:.2f}` 元 | 季線 MA60 = `{res['ma60_val']:.2f}` 元")
                    st.write(f"* **量能波段**: 今日成交 = `{res['current_vol']:.0f}` 張 | 5日均量 = `{res['vol_ma5_val']:.0f}` 張 | 20日均量 = `{res['vol_ma20_val']:.0f}` 張")
                with tc2:
                    st.write(f"* **布林軌道**: 上軌 = `{res['bb_upper']:.2f}` | 下軌 = `{res['bb_lower']:.2f}` | 帶寬 = `{res['bb_bandwidth']:.4f}`")
                    st.write(f"* **動能指標**: +DI = `{res['plus_di']:.1f}` | -DI = `{res['minus_di']:.1f}` | 20日高點壓力 = `{res['real_resistance']:.2f}`")

            with st.expander("📰 資訊面 24H 網路輿情即時新聞流水線"):
                st.markdown(f"> **24H 網路即時輿情報告**：`{res['news_analysis_report']}`")
                if not res["raw_news_list"].empty if isinstance(res["raw_news_list"], pd.DataFrame) else res["raw_news_list"]:
                    for n in res["raw_news_list"]:
                        st.markdown(f"* **[{n['date']}]** 【{n['source']}】  [{n['sentiment']}]  [{n['title']}]({n['link']})")
                else:
                    st.warning("⚠️ 24 小時內 Google RSS 暫未收錄相關重大新聞。")
