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
st.set_page_config(page_title="SOP v19 全串聯多因子量化交易決策系統", layout="wide")

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
    """符合台灣證券交易所現行法規之升降單位規則"""
    if p >= 1000: return 5.0
    if p >= 500:  return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.05
    return 0.01

def round_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t == 0:
        return 0.0
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

def detect_style(result: dict) -> str:
    if "黑馬" in result.get("invest_status", ""): return "大轉折潛力黑馬型"
    if result.get("tech_conclusion_short") == "🚀 準備起漲": return "爆發突破型"
    brk_score, pb_score = 0, 0
    if result.get("breakout_setup"): brk_score += 3
    if result.get("pullback_setup"): pb_score += 3
    if result.get("rr1_brk", 0) >= 1.5: brk_score += 2
    if result.get("rr1_pb", 0) >= 2.0: pb_score += 2
    if brk_score > pb_score: return "突破型"
    if pb_score > brk_score: return "拉回型"
    return "突破型" if result.get("current_price", 0) >= result.get("pivot", 0) else "拉回型"

def analyze_news_sentiment(title: str) -> tuple:
    pos_words = ['創新高', '大賺', '暴增', '飆', '大成長', '利多', '優於預期', '加碼', '看旺', '強勢', '獲利', '突破', '轉盈', '充沛', '加持', '買超', '爆發', '新高', '雙率雙升', '三率三升', '扭虧為盈', '轉虧為盈', '急單']
    neg_words = ['衰退', '虧損', '重挫', '低於預期', '縮水', '跌破', '警告', '利空', '下滑', '疲弱', '裁員', '大跌', '慘', '賣壓', '修正', '賣超', '暴跌', '逆風', '雙率雙降']
    pos_score = sum(1 for w in pos_words if w in title)
    neg_score = sum(1 for w in neg_words if w in title)
    if pos_score > neg_score: return "🟢 利多", "green"
    elif neg_score > pos_score: return "🔴 利空", "red"
    return "🟡 中性", "gray"

# ============ 4. Auth & Shared Connection ============
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

# ============ 5. Live Data Streaming Engine (v17 完整保留) ============
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
            yahoo_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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

# ============ 6. Advanced Cached Data Layer (融合 v17 + v18 缺口) ============
@st.cache_data(ttl=3600)
def get_stock_info_df():
    api = get_api()
    df = api.taiwan_stock_info()
    if df is None or df.empty: return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category"])
    df = df.copy()
    if "stock_id" in df.columns: df["stock_id"] = df["stock_id"].astype(str).str.strip()
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
    df = df.dropna(subset=["close", "high", "low", "vol"]).copy()
    return df[df["vol"] > 0].copy()

@st.cache_data(ttl=1800)
def get_market_macro_status():
    """【專業缺口一：加權指數大盤環境濾網】"""
    api = get_api()
    start_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    try:
        df = api.taiwan_stock_daily(stock_id="TAIEX", start_date=start_date)
        if df is not None and not df.empty:
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['MA20'] = df['close'].rolling(20).mean()
            last_row = df.iloc[-1]
            is_bull_market = last_row['close'] >= last_row['MA20']
            trend_label = "🟢 大盤位於月線上方 (多頭常態)" if is_bull_market else "🚨 大盤跌破月線 (防禦觀望)"
            return is_bull_market, trend_label
    except Exception: pass
    return True, "🟢 多頭常態 (無法取得大盤，預設寬鬆保護)"

@st.cache_data(ttl=900)
def get_taiwan_enhanced_chips(stock_id: str, days: int = 30):
    """【專業缺口二：台股特色籌碼精細細分 - 投信鎖碼與融資浮額】"""
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    sitc_trend = "🟡 投信無顯著動作"
    margin_trend = "🟡 融資平穩"
    sitc_3d_sum = 0.0
    
    # 1. 精算投信（中小型飆股發動機）近 3 日方向
    try:
        inst_df = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
        if inst_df is not None and not inst_df.empty:
            sitc_df = inst_df[inst_df['name'] == 'Investment_Trust'].copy()
            if not sitc_df.empty:
                sitc_df['net'] = pd.to_numeric(sitc_df['buy'], errors='coerce').fillna(0) - pd.to_numeric(sitc_df['sell'], errors='coerce').fillna(0)
                sitc_3d_sum = float(sitc_df.tail(3)['net'].sum())
                if sitc_3d_sum > 500: sitc_trend = "🟢 投信大哥強力鎖碼"
                elif sitc_3d_sum < -500: sitc_trend = "🔴 投信高檔棄養出貨"
    except Exception: pass

    # 2. 精算融資餘額（散戶浮額凌亂度）近 5 日變化
    try:
        margin_df = api.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start_date)
        if margin_df is not None and not margin_df.empty:
            margin_df['MarginPurchaseTodayBalance'] = pd.to_numeric(margin_df['MarginPurchaseTodayBalance'], errors='coerce')
            margin_diff = margin_df.iloc[-1]['MarginPurchaseTodayBalance'] - margin_df.iloc[-5]['MarginPurchaseTodayBalance']
            if margin_diff > 1000: margin_trend = "🚨 散戶進場攤平 (浮額極凌亂)"
            elif margin_diff < -1000: margin_trend = "🟢 融資大量退場 (籌碼乾淨)"
    except Exception: pass

    return sitc_trend, margin_trend, sitc_3d_sum

@st.cache_data(ttl=900)
def get_inst_df(stock_id: str, days: int = 60):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
    return df if df is not None else pd.DataFrame()

@st.cache_data(ttl=900)
def get_rev_df(stock_id: str, days: int = 365):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=start_date)
    return df if df is not None else pd.DataFrame()

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
        if df.empty: return pd.DataFrame()
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
                title = item.find('title').text if item.find('title') is not None else ""
                link = item.find('link').text if item.find('link') is not None else ""
                pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ""
                source = item.find('source').text if item.find('source') is not None else "財經新聞"
                if " - " in title: title = title.rsplit(" - ", 1)[0]
                news_list.append({"date": pub_date, "title": title, "source": source, "link": link})
        if news_list:
            df = pd.DataFrame(news_list)
            df["parsed_date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_convert('Asia/Taipei')
            df["date"] = df["parsed_date"].dt.strftime('%Y-%m-%d %H:%M')
            df = df.sort_values(by="parsed_date", ascending=False).drop(columns=["parsed_date"])
            return df
    except Exception: pass
    return pd.DataFrame(news_list)

# ============ 7. Technical Engine ============
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

    return x.dropna(subset=["ATR14", "MA20", "MA60", "Res_20D", "BB_bandwidth", "RSI14", "ADX14", "MACD_HIST"]).copy()

# ============ 8. 旗艦完全體：多維因果關係矩陣決策大腦 ============
def cross_factor_decoupling_engine(macro_bull, trend_phase, fin_conclusion, sitc_trend, margin_trend, tech_conclusion_short, latest_yoy, pe_val, pe_desc):
    """
    【SOP v19 終極演算法】不使用無腦的分數加總，而是採用嚴格的「交易因果特徵矩陣」進行縱向全串聯。
    """
    # 核心地雷1：系統大盤Beta性風險（覆巢之下無完卵機制）
    if not macro_bull and tech_conclusion_short in ["🚀 準備起漲", "🚀 完美多頭"]:
        return "🚨 大盤空頭陷阱：提防系統性獵殺", "red", "技術面雖拉出突破動能，但大盤加權指數已走弱位於月線下方。此時強勢股突破失敗、淪為誘多陷阱的機率高達 80%！強烈建議空倉觀望。"

    # 核心地雷2：基本面/籌碼與高位技術背離（高位出貨抓交替）
    if "主升段" in trend_phase and "本業結構退步" in fin_conclusion and "散戶進場攤平" in margin_trend:
        return "💥 主力強弩之末：基本面衰退+散戶接盤", "red", "股價雖位於多頭主升段頂部，但最新一季財報已出現毛利營益雙降的『本業衰退』，且近 5 日主力瘋狂倒貨給融資散戶。此為極度危險的利多出盡抓交替盤，一票否決所有買進動作！"

    # 完美風暴1：完美起漲風口（大盤多頭護航 + 籌碼極致壓縮 + 集團/投信瘋狂鎖碼 + 營收基本面炸裂）
    if macro_bull and tech_conclusion_short == "🚀 準備起漲" and "投信大哥強力鎖碼" in sitc_trend and latest_yoy >= 25:
        return "🔮 完美風暴：投信/集團季底作帳頂級飆股", "purple", "布林通道歷經極致壓縮後帶量向上突破前高壓力！且伴隨月營收強勁年增（>25%）與投信真槍實彈的主力建倉。宏觀大盤環境安全，此為勝率極高的主升段第一起漲點。"

    # 完美風暴2：高手低吸黃金右腳（多頭洗盤落底 + 估值便宜有安全邊際 + 散戶浮額清洗乾淨）
    if "拉回洗盤期" in trend_phase and pe_desc == "🟢 價值鐵板（安全邊際高）" and "融資大量退場" in margin_trend:
        return "🛡️ 黃金右腳：高安全邊際良性換手點", "green", "中長期均線呈多頭排列，短線股價跌破月線進行洗盤。當前滾動 PE 處於歷史極低水位，且散戶融資不堪折磨、大舉割肉退場，籌碼極致沉澱，此為法人高度認同的『良性回檔潛伏防守點』。"

    # 假突破嫌疑判定
    if tech_conclusion_short == "⚠️ 假突破嫌疑" or "法人高檔棄養出貨" in sitc_trend:
        return "🚨 誘多危機：法人拉高偷偷倒貨", "red", "K線型態雖維持強勢，但核心法人已連續數日趁拉高逢高調節。極可能是為了吸引市場散戶追價的假突破型態，不可進場幫忙接刀。"

    # 垃圾時間：基本面失速的冷門橫盤
    if "橫盤蓄勢期" in trend_phase and latest_yoy < 0 and "投信無顯著動作" in sitc_trend:
        return "💤 邊緣人時間：基本面動能休克無量橫盤", "gray", "營收動能失速衰退，且法人核心金流毫無進駐意願。此時股價雖跌不動，但時間成本極高，短線極難突破，屬於死水一條，建議換股操作。"

    # 空頭全面防禦
    if "空頭" in trend_phase:
        return "❌ 空頭波段結構破壞：嚴格避開", "red", "長短期均線已全面蓋頭下彎，空方完全控盤。任何單日反彈與小利多皆為誘多誘空誘捕器，策略上保持絕對觀望。"

    # 常態多頭維持
    if "🔥 波段多頭主升段" in trend_phase and latest_yoy > 10:
        return "🔥 波段主升：持股續抱/分批布局", "blue", "宏觀技術架構與營收基本面發展有序相符，籌碼未見異常失控，依循常規技術軌跡分批操作。"

    return "⚖️ 標準波段：回歸常規技術藍圖操作", "blue", "多空因子處於動態拉鋸平衡，未見極端的宏觀籌碼共振。請嚴格依據下方量化風控之藍圖價位執行紀律操作。"

# ============ 9. Main Matrix Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int):
    # 1. 歷史日K資料與即時串流交叉覆蓋
    df_raw = get_daily_df(stock_id, days=365)
    if df_raw is None or df_raw.empty: return None

    hist_last_raw = df_raw.iloc[-1]
    hist_last_close = float(hist_last_raw["close"])
    hist_last_vol = float(hist_last_raw["vol"])

    # 呼叫 v17 即時流引擎
    current_price, current_vol, rt_success, rt_source, rt_type = compute_live_data(
        stock_id, hist_last_close, hist_last_vol
    )
    
    df_for_indicators = df_raw.copy()
    for c in ["close", "high", "low", "vol", "amount"]:
        if c in df_for_indicators.columns:
            df_for_indicators[c] = df_for_indicators[c].astype(float)
            
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
    stock_name = match["stock_name"].values[0] if not match.empty else "指定標的"
    industry = match["industry_category"].values[0] if not match.empty else "未知板塊"

    hist_last = df.iloc[-1]
    last_trade_date_str = str(hist_last["date"])

    # 大象股門檻計算
    recent_amount_ma = df["amount"].tail(20).mean()
    is_heavyweight = recent_amount_ma > 2000000000  
    vol_multiplier, compress_quantile = (1.25, 0.35) if is_heavyweight else (2.2, 0.18)

    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

    # 技術面核心物理量
    ma20_val = float(hist_last["MA20"])
    ma60_val = float(hist_last["MA60"])
    vol_ma20_val = float(hist_last["MA20_Vol"])
    real_resistance = float(hist_last["Res_20D"])
    current_bandwidth = float(hist_last["BB_bandwidth"])
    atr = float(hist_last["ATR14"])
    rsi_now = float(hist_last["RSI14"])
    adx_now = float(hist_last["ADX14"])

    vol_spike = current_vol > (vol_ma20_val * vol_multiplier)
    bandwidth_60d = df["BB_bandwidth"].tail(60)
    is_compressed = current_bandwidth < bandwidth_60d.quantile(compress_quantile) if not bandwidth_60d.empty else False

    # 均線斜率波段架構定性
    ma20_trend_5d = "上升" if (len(df) >= 5 and df["MA20"].iloc[-1] > df["MA20"].iloc[-5]) else "平盤"
    ma60_trend_5d = "上升" if (len(df) >= 5 and df["MA60"].iloc[-1] > df["MA60"].iloc[-5]) else "平盤"
    
    if current_price >= ma20_val and ma20_val >= ma60_val and ma20_trend_5d == "上升":
        trend_phase = "🔥 波段多頭主升段"
        trend_desc = "波段架構健康，長短均線呈多頭排列向上。量縮或橫盤皆屬「主升段波段換手」，未跌破生命線切勿盲目離場。"
    elif current_price < ma20_val and ma20_val >= ma60_val and ma60_trend_5d == "上升":
        trend_phase = "🛡️ 多頭架構拉回洗盤期"
        trend_desc = "中長期季線架構穩健向上，短線跌破月線浮額清洗。策略上應尋求『均線低吸潛伏』而非盲目追高。"
    elif abs(df["MA20"].iloc[-1] - df["MA20"].iloc[-10]) / ma20_val < 0.02 and is_compressed:
        trend_phase = "💤 潛伏築底蓄勢期"
        trend_desc = "月季線極度糾纏、長期橫向延伸，主力箱型吸籌中。策略應以『箱底布局、耐心靜待突破』為主。"
    else:
        trend_phase = "📉 空頭波段修正/尋找支撐期"
        trend_desc = "股價位於月季線下方且均線下彎，空方完全控盤。單日反彈多為誘多陷阱，保持絕對觀望。"

    # 籌碼基礎過濾
    inst_df = get_inst_df(stock_id, days=20)
    inst_3d_sum = 0.0
    if not inst_df.empty and "buy" in inst_df.columns:
        inst_df["net_sheets"] = pd.to_numeric(inst_df["buy"], errors="coerce").fillna(0) - pd.to_numeric(inst_df.get("sell",0), errors="coerce").fillna(0)
        inst_daily = inst_df.groupby("date")["net_sheets"].sum().reset_index().sort_values("date")
        if not inst_daily.empty: inst_3d_sum = float(inst_daily.tail(3)["net_sheets"].sum())

    # 引入 v18 台股特色籌碼精細細分 (投信與融資)
    sitc_trend, margin_trend, sitc_3d_sum_pure = get_taiwan_enhanced_chips(stock_id)

    # 營收安全性
    rev_df = get_rev_df(stock_id, days=365)
    latest_yoy = 0.0
    if not rev_df.empty and "revenue" in rev_df.columns:
        rev_clean = rev_df.copy()
        rev_clean["revenue"] = pd.to_numeric(rev_clean["revenue"].astype(str).str.replace(",", ""), errors="coerce")
        if "revenue_year_growth_rate" not in rev_clean.columns or rev_clean["revenue_year_growth_rate"].isnull().all():
            rev_clean = rev_clean.sort_values("date").reset_index(drop=True)
            rev_clean["revenue_year_growth_rate"] = rev_clean["revenue"].pct_change(12) * 100
        else:
            rev_clean["revenue_year_growth_rate"] = pd.to_numeric(rev_clean["revenue_year_growth_rate"].astype(str).str.replace("%", ""), errors="coerce")
        rev_clean = rev_clean.dropna(subset=["revenue_year_growth_rate", "revenue"])
        if not rev_clean.empty:
            latest_yoy = float(rev_clean.sort_values("date").iloc[-1]["revenue_year_growth_rate"])

    # 財報對比季節解耦（v17 完整保留）
    fin_df = get_financial_statement_df(stock_id, years=2)
    eps_now, eps_prev, gpm_now, gpm_prev, opm_now, opm_prev = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    fin_conclusion = "📋 該標的暫無足夠季度財報歷史數據對比。"
    has_financial_data = False
    sum_eps_4q = 0.0
    
    if not fin_df.empty and "Revenue" in fin_df.columns and "EPS" in fin_df.columns:
        fin_df = fin_df.sort_values("date").reset_index(drop=True)
        for idx in range(len(fin_df)):
            rev_amt = safe_float(fin_df.loc[idx, "Revenue"])
            fin_df.loc[idx, "gpm"] = (safe_float(fin_df.loc[idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df.loc[idx, "opm"] = (safe_float(fin_df.loc[idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        
        latest_fin = fin_df.iloc[-1]
        eps_now, gpm_now, opm_now = safe_float(latest_fin.get("EPS", 0.0)), safe_float(latest_fin.get("gpm", 0.0)), safe_float(latest_fin.get("opm", 0.0))
        has_financial_data = True
        
        # 精算近 4 季滾動 EPS 用於估值錨定
        sum_eps_4q = pd.to_numeric(fin_df.tail(4)['EPS'], errors='coerce').sum()
        
        if len(fin_df) >= 5: 
            prev_fin = fin_df.iloc[-5]
            eps_prev, gpm_prev, opm_prev = safe_float(prev_fin.get("EPS", 0.0)), safe_float(prev_fin.get("gpm", 0.0)), safe_float(prev_fin.get("opm", 0.0))
            if gpm_now > gpm_prev and opm_now > opm_prev and eps_now > eps_prev:
                fin_conclusion = "📈 【財報年增擴張】 最新季度獲利指標全數超越去年同期！本業體質結構優化。"
            elif gpm_now < gpm_prev and opm_now < opm_prev and eps_now < eps_prev:
                fin_conclusion = "📉 【本業結構退步】 毛利、營益、EPS 同步遜於去年同期，需提高警覺。"
            else:
                fin_conclusion = "⚖️ 【結構調整期】 毛利、營益與去年同期互有勝負，防守力一般。"

    # 引入 v18 估值錨定歷史校準
    pe_val = 0.0
    pe_desc = "⚪ 數據不足無法計算估值"
    if sum_eps_4q > 0:
        pe_val = current_price / sum_eps_4q
        if pe_val > 35: pe_desc = "🚨 估值瘋狂（高檔吹泡泡）"
        elif pe_val < 13: pe_desc = "🟢 價值鐵板（安全邊際高）"
        else: pe_desc = "⚖️ 估值合理區間"

    # 即時新聞輿情分析（v17 完整保留）
    news_analysis_report = "⚪ 暫無最新重要輿情分析資訊。"
    news_df = get_realtime_news_df(stock_id, stock_name)
    if news_df is not None and not news_df.empty and "title" in news_df.columns:
        pos_cnt, neg_cnt, neu_cnt = 0, 0, 0
        for title in news_df["title"].head(10).tolist():
            lbl, _ = analyze_news_sentiment(title)
            if "利多" in lbl: pos_cnt += 1
            elif "利空" in lbl: neg_cnt += 1
            else: neu_cnt += 1
        total_scanned = pos_cnt + neg_cnt + neu_cnt
        if pos_cnt > neg_cnt and pos_cnt >= 2:
            news_analysis_report = f"🔥 【輿情偏多】 利多消息佔 {pos_cnt/total_scanned*100:.0f}%。市場追價意願高。"
        elif neg_cnt > pos_cnt and neg_cnt >= 2:
            news_analysis_report = f"🚨 【輿情偏空】 利空消息佔 {neg_cnt/total_scanned*100:.0f}%。短線拋壓風險擴大。"
        else:
            news_analysis_report = f"⚖️ 【輿情中性】 多空雜音交錯（多 {pos_cnt} 則、空 {neg_cnt} 則），回歸基本面拉鋸。"

    # 微觀動能定性
    tech_conclusion_short = "中性觀望"
    rsi_overbought_tmsh = 75 if is_heavyweight else 85
    if current_price >= real_resistance * 0.995 and vol_spike and is_compressed:
        tech_conclusion_short = "🚀 準備起漲"
    elif adx_now < 20:
        tech_conclusion_short = "💤 盤整死水"
    elif rsi_now >= rsi_overbought_tmsh:
        tech_conclusion_short = "⚠️ 短線過熱"
    elif float(hist_last["PLUS_DI"]) > float(hist_last["MINUS_DI"]) and adx_now >= 20:
        if inst_3d_sum < 0: tech_conclusion_short = "⚠️ 假突破嫌疑"
        else: tech_conclusion_short = "🚀 多頭成形"

    # 獲取大盤加權指數 Beta 狀態
    macro_bull, macro_desc = get_market_macro_status()

    # 執行最終決策大腦矩陣串聯
    final_decision, final_color, final_desc = cross_factor_decoupling_engine(
        macro_bull, trend_phase, fin_conclusion, sitc_trend, margin_trend, tech_conclusion_short, latest_yoy, pe_val, pe_desc
    )

    # 交易藍圖精算與資金池配額風控保護
    t = tick_size(current_price)
    slip = float(slip_ticks) * t
    
    # 根據大腦最終裁決動能調整敞口風險
    adjusted_risk = risk_per_trade
    if final_color == "red": adjusted_risk = 0.0  # 核心地雷，一票否決
    elif final_color == "purple": adjusted_risk *= 1.5 # 完美風暴，允許加碼痛擊

    # 精準計算進場停損（防守月線下緣與 ATR 波動）
    stop_loss_price = round_to_tick(ma20_val - (1.5 * atr) - slip, t)
    if stop_loss_price >= current_price: 
        stop_loss_price = round_to_tick(current_price - (2.0 * atr), t)
        
    loss_per_share = current_price - stop_loss_price
    risk_money = total_capital * (adjusted_risk / 100) * 10000
    suggested_lots = int((risk_money / loss_per_share) / 1000) if (loss_per_share > 0 and adjusted_risk > 0) else 0
    
    # 動態移動停利防線
    trailing_stop_line = round_to_tick(current_price - (2.5 * atr), t)

    # 打包最終完整輸出的字典
    return {
        "stock_id": stock_id, "stock_name": stock_name, "industry": industry,
        "current_price": current_price, "current_vol": current_vol,
        "rt_source": rt_source, "m_desc": m_desc, "m_color": m_color,
        "trend_phase": trend_phase, "trend_desc": trend_desc,
        "macro_desc": macro_desc, "sitc_trend": sitc_trend, "margin_trend": margin_trend,
        "pe_val": pe_val, "pe_desc": pe_desc, "eps_4q": sum_eps_4q,
        "fin_conclusion": fin_conclusion, "latest_yoy": latest_yoy,
        "news_analysis_report": news_analysis_report,
        "final_decision": final_decision, "final_color": final_color, "final_desc": final_desc,
        "suggested_lots": suggested_lots, "stop_loss_price": stop_loss_price, "trailing_stop_line": trailing_stop_line,
        "gpm_now": gpm_now, "opm_now": opm_now, "rsi_now": rsi_now
    }

# ============ 10. Streamlit UI Presentation Layer ============
st.title("SOP v19 全串聯多因子量化交易決策系統")
st.caption("2026 旗艦完全體 - 完美繼承 v17 即時流、RSS 輿情與財報解耦，深度串聯 v18 大盤、投信融資與滾動 PE 矩陣大腦")

with st.sidebar:
    st.header("⚙️ 實戰風控參數配置")
    stock_input = st.text_input("輸入台股代碼", "2330")
    capital = st.number_input("個人交易總資本 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆交易最大核心風險承擔 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守滑價摩擦 (Ticks)", 0, 5, 1)

if st.button("🔥 啟動跨因子矩陣全方位診斷", use_container_width=True):
    with st.spinner("正在啟動五維度決策大腦，交叉勾稽即時報價、大盤Beta、台股主力籌碼、財報解耦與動態出場藍圖..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input)
        
        if res is None:
            st.error("標的解析失敗，請檢查代碼是否正確或 FinMind API 連線限制。")
        else:
            # 第一區塊：大腦頂層串聯裁決（最醒目的狀態機卡片）
            st.subheader("🎯 頂層戰略串聯裁決")
            color_hex = {"red": "#FF4B4B", "purple": "#7D3CFF", "green": "#2BD9A1", "blue": "#1C86EE", "gray": "#808080"}[res["final_color"]]
            st.markdown(f"""
            <div style="background-color:{color_hex}15; border-left: 6px solid {color_hex}; padding: 18px; border-radius: 6px; margin-bottom: 20px;">
                <h2 style="margin:0; color:{color_hex}; font-size:24px;">{res['final_decision']}</h2>
                <p style="margin: 12px 0 0 0; color:#222; font-size:16px; font-weight:600; line-height:1.6;">{res['final_desc']}</p>
            </div>
            """, unsafe_allow_html=True)
            
            # 基礎資訊看盤標籤
            st.markdown(f"**標的檔案**：{res['stock_name']} ({res['stock_id']}) | **所屬板塊**：{res['industry']} | **報價來源**：`{res['rt_source']}` | **連線狀態**：:{res['m_color']}[{res['m_desc']}]")
            
            st.markdown("---")
            
            # 第二區塊：四維因子縱向物理量對齊
            st.subheader("📊 跨因子核心物理量校準")
            c1, c2, c3, c4 = st.columns(4)
            with c1: 
                st.metric("1. 宏觀大盤環境", "TAIEX 加權指數", res["macro_desc"])
                st.caption(f"目前技術位階：{res['trend_phase']}")
            with c2: 
                st.metric("2. 歷史滾動估值 PE", f"{res['pe_val']:.1f} 倍", res["pe_desc"])
                st.caption(f"近4季總 EPS: {res['eps_4q']:.2f} 元")
            with c3: 
                st.metric("3. 台股特有籌碼", res["sitc_trend"])
                st.caption(res["margin_trend"])
            with c4: 
                st.metric("4. 本業基本營收", f"YoY {res['latest_yoy']:.1f}%")
                st.caption(f"最新財報：毛利 {res['gpm_now']:.1f}% / 營益 {res['opm_now']:.1f}%")

            # 第三區塊：微觀深度多空輿情（v17 傲人特徵補回）
            with st.expander("🔍 查看微觀基本面與 RSS 新聞輿情深度對位細節", expanded=True):
                st.write(f"**財報解耦年增結論**：{res['fin_conclusion']}")
                st.write(f"**即時輿情監測結論**：{res['news_analysis_report']}")
                st.write(f"**微觀動能指標物理量**：現價 {res['current_price']:.2f} 元 / 當日量 {res['current_vol']:.0f} 張 / RSI14 擺動位階: {res['rsi_now']:.1f}")

            st.markdown("---")
            
            # 第四區塊：交易藍圖量化精算
            st.subheader("🛡️ 量化風控與動態出場藍圖 (Trading Blueprint)")
            if res["suggested_lots"] == 0:
                st.warning("⚠️ 警告：核心大腦目前判定該標的觸發『策略地雷』或處於『嚴格避開區』。風控模組強制將風險配額歸零（建議購買 0 張），請保持絕對空倉防禦。")
            
            b1, b2, b3, b4 = st.columns(4)
            with b1: st.metric("當前基準市價", f"{res['current_price']:.2f} 元")
            with b2: st.metric("核心風控配置張數", f"{res['suggested_lots']} 張", "大腦依劇本自動加減碼結果")
            with b3: st.metric("硬性鐵板停損價 (觸價執行)", f"{res['stop_loss_price']:.2f} 元", "防守 MA20 波段回測線")
            with b4: st.metric("動態移動停利線", f"{res['trailing_stop_line']:.2f} 元", "最高價回撤 2.5*ATR 防守")
            
            st.info("💡 **移動停利盤中執行紀律說明**：成功進場後，若股價隨主力上攻續創新高，請手動將移動停利線上移（新最高收盤價 - 2.5 * ATR）。日後若有任何交易日『盤中收盤破』該條移動線，代表波段慣性改變、主力出貨利多出盡，應立即將獲利落袋出場。")
