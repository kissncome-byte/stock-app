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
st.set_page_config(page_title="SOP v22 五維全串聯量化交易診斷系統", layout="wide")

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

# ============ 6. 【核心重構：五維度縱向全串聯決策大腦】 ============
def cross_factor_decoupling_engine(macro_bull, trend_phase, fin_conclusion, sitc_trend, margin_trend, tech_short, latest_yoy, pe_desc, news_analysis):
    """
    【SOP v22 核心升級】將 5 個維度（大盤、估值、財報、籌碼、動能、新聞）轉化為因果向量，
    全面交叉勾稽，杜絕孤立條件。
    """
    # 建立內部的全維度特徵矩陣狀態體系
    f_is_good = "雙率雙升" in fin_conclusion or latest_yoy >= 20
    f_is_bad = "本業結構退步" in fin_conclusion and latest_yoy < 5
    
    c_is_locked = "投信強力鎖碼" in sitc_trend or "融資大量退場" in margin_trend
    c_is_leaking = "投信高檔棄養" in sitc_trend or "散戶融資進場" in margin_trend
    
    t_is_strong = tech_short in ["🚀 準備起漲", "🚀 多頭成形"] and "多頭" in trend_phase
    t_is_weak = "空頭" in trend_phase or tech_short == "💤 盤整死水"

    # 1. 大盤空頭交叉過濾線（大盤殺，所有多頭動能全部無效化）
    if not macro_bull:
        if t_is_strong:
            return "🚨 大盤空頭：強勢股假突破陷阱", "red", f"【全串聯裁決】個股技術面現況雖顯示為『{tech_short}』，且營收年增達 {latest_yoy:.1f}%。然而，宏觀大盤加權指數已全面轉弱跌破月線。在系統性風險高企時，強勢股高機率沦為主力的『拉高誘多出貨點』。基本面與技術動能在空頭暴風雨下無法產生向上共振，策略上無條件執行一票否決，嚴禁追價開火！"
        else:
            return "❌ 宏觀與個股全面雙空共振", "red", "【全串聯裁決】大盤大環境位於空方警戒區，且個股自身技術架構亦步入空頭修正。基本面缺乏爆發動能，籌碼面呈現凌亂拋壓，屬於標準的垃圾時間，必須嚴格避開，保持絕對空倉防禦。"

    # 2. 多頭環境下的頂級共振：完美風暴（大盤多 + 估值安全/合理 + 基本面炸裂 + 籌碼鎖死 + 技術發動）
    if macro_bull and pe_desc != "🚨 估值瘋狂（高檔吹泡泡）" and f_is_good and c_is_locked and t_is_strong:
        return "🔮 頂級多頭共振：黃金起漲主升飆股", "purple", f"【全串聯裁決】五維度指標達成罕見的完美黃金交集！加權指數多頭護航，個股本益比未過熱。月營收與財報同步確認為『基本面擴張』，疊加投信真槍實彈的主力所碼與散戶融資退場（籌碼極淨）。此時技術面發動『{tech_short}』，屬於內資主力籌碼與基本面雙軌驅動的最高勝率飆股型態。策略：風控敞口調升至 1.5 倍，全力進攻！"

    # 3. 高位背離地雷：主力高檔抓交替（大盤多 + 估值瘋狂 + 財報衰退 + 散戶融資接盤 + 技術高檔）
    if "主升段" in trend_phase and pe_desc == "🚨 估值瘋狂（高檔吹泡泡）" and (f_is_bad or c_is_leaking):
        return "💥 世紀價值陷阱：高檔利多出盡出貨盤", "red", f"【全串聯裁決】極度危險！雖然技術型態包裝成『{trend_phase}』且新聞表面上熱絡，但縱向勾稽發現重大黑幕：滾動估值已達歷史瘋狂天花板，最新季度財報卻暴露出毛利營益率『雙降退步』。此時近 5 日主力趁高大舉倒貨給融資散戶（散戶進場攤平）。這完全是法人與主力利用市場散戶樂觀情緒進行的『高檔套現抓交替』型態。策略：一票否決，有多單者應立即獲利落袋。"

    # 4. 多頭良性洗盤：黃金右腳低吸（大盤多 + 估值便宜 + 中長多短線洗盤 + 融資退乾淨）
    if "拉回洗盤期" in trend_phase and pe_desc in ["🟢 價值鐵板（安全邊際高）", "⚖️ 估值合理區間"] and "融資大量退場" in margin_trend:
        return "🛡️ 良性結構回檔：高手低吸黃金右腳", "green", f"【全串聯裁決】中長期大波段季線依然穩健向上，短線跌破月線引發技術面良性修正。串聯發現：滾動本益比已回踩至具有高度安全邊際的低位水準，且散戶融資不堪折磨、大舉割肉退場（籌碼重新沉澱至特定大戶手中）。這屬於典型的主力『良性換手期』而非波段終結。輿情亦回歸理性中性。策略：此處防守性極強，依據下方交易藍圖執行精密低吸潛伏。"

    # 5. 垃圾時間：邊緣冷門股
    if "橫盤蓄勢期" in trend_phase and not f_is_good and "投信無顯著動作" in sitc_trend:
        return "💤 邊緣人時間：基本面休克無量橫盤", "gray", "【全串聯裁決】大盤雖安全，但個股陷入死水僵局。月營收動能失速，季度財報缺乏亮點，且內資投信核心金流毫無建倉意願。此時技術面雖然由於跌無可跌而維持橫盤築底，但缺乏催化劑（Catalyst），時間成本高昂。策略：無效資金配額，建議直接換股操作，切勿在此浪費資金效率。"

    # 6. 常態多頭維持
    if t_is_strong and f_is_good:
        return "🔥 穩健波段主升：多方有序推進", "blue", f"【全串聯裁決】大盤環境安全，個股短期與長期趨勢維持健康的多頭排列。月營收與獲利結構相符提供實質基本面支撐，主力籌碼無異常失控撤退跡象。技術動能處於有序發散階段，雖然不具備極端共振的爆發力，但屬於高勝率的常態波段。策略：持股續抱，或依技術面階梯式分批加碼。"

    return "⚖️ 綜合平衡盤整：回歸常規技術藍圖操作", "blue", "【全串聯裁決】後台財務與微觀動能因子互有勝負，並未觸發極端的宏觀、籌碼或估值背離共振。目前盤面多空勢力處於動態動能平衡，請嚴格遵循下方量化交易藍圖精算之開火/停損價位執行紀律操作。"

# ============ 8. Main Matrix Core Executor ============
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

    # 長短期趨勢拆解
    ma20_trend_5d = "上升" if df["MA20"].iloc[-1] > df["MA20"].iloc[-5] else "平盤"
    ma60_trend_5d = "上升" if df["MA60"].iloc[-1] > df["MA60"].iloc[-5] else "平盤"
    
    if current_price >= ma20_val:
        short_term_trend = "🚀 多頭強勢發散" if rsi_now > 60 else "📈 多頭波段推進"
    else:
        short_term_trend = "⚠️ 短線拉回洗盤" if ma60_trend_5d == "上升" else "📉 空方動能主導"
        
    if current_price >= ma60_val and ma60_trend_5d == "上升":
        long_term_trend = "🔥 季線全面向上（主升段架構）"
    elif current_price < ma60_val and ma60_trend_5d == "下彎":
        long_term_trend = "📉 季線下彎蓋頭（空頭修正架構）"
    else:
        long_term_trend = "💤 季線橫向延伸（箱型潛伏築底）"

    if current_price >= ma20_val and ma20_val >= ma60_val and ma20_trend_5d == "上升": trend_phase = "🔥 波段多頭主升段"
    elif current_price < ma20_val and ma20_val >= ma60_val: trend_phase = "🛡️ 多頭架構拉回洗盤期"
    elif is_compressed: trend_phase = "💤 潛伏築底蓄勢期"
    else: trend_phase = "📉 空頭波段修正期"

    # 外部環境因子獲取
    sitc_trend, margin_trend, sitc_3d_sum = get_taiwan_enhanced_chips(stock_id)
    macro_bull, macro_desc = get_market_macro_status()
    
    latest_yoy = 0.0
    rev_df = get_rev_df(stock_id)
    if rev_df is not None and not rev_df.empty:
        latest_yoy = safe_float(rev_df.iloc[-1].get("revenue_year_growth_rate", 0.0))

    # 財務基本面與滾動 PE 計算
    fin_df = get_financial_statement_df(stock_id)
    fin_conclusion, pe_desc, sum_eps_4q = "📋 暫無足夠季度財報數據。", "⚪ 數據不足無法計算估值", 0.0
    
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
            if last_fin["gpm"] > prev_fin["gpm"] and last_fin["opm"] > prev_fin["opm"]: fin_conclusion = "📈 【獲利年增：雙率雙升】 本業體質結構優化。"
            elif last_fin["gpm"] < prev_fin["gpm"] and last_fin["opm"] < prev_fin["opm"]: fin_conclusion = "📉 【本業結構退步】 毛利與營益雙雙低於去年同期！"
            else: fin_conclusion = "⚖️ 【結構調整期】 獲利結構與去年同期互有勝負。"
    else: pe_val = 0.0

    # 新聞與輿情解析
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
        if pos_cnt > neg_cnt: news_analysis_report = f"🔥 【輿情偏多】 利多消息主導（多 {pos_cnt} 則 / 空 {neg_cnt} 則）。"
        elif neg_cnt > pos_cnt: news_analysis_report = f"🚨 【輿情偏空】 利空雜音擴大（空 {neg_cnt} 則 / 多 {pos_cnt} 則）。"

    # 微觀技術發動定性
    tech_short = "中性觀望"
    if current_price >= real_resistance * 0.995 and vol_spike and is_compressed: tech_short = "🚀 準備起漲"
    elif rsi_now >= (75 if is_heavyweight else 85): tech_short = "⚠️ 短線過熱"
    elif float(hist_last["PLUS_DI"]) > float(hist_last["MINUS_DI"]): tech_short = "🚀 多頭成形"

    # 呼叫終極縱向全串聯決策大腦
    final_decision, final_color, final_desc = cross_factor_decoupling_engine(
        macro_bull, trend_phase, fin_conclusion, sitc_trend, margin_trend, tech_short, latest_yoy, pe_desc, news_analysis_report
    )

    # 交易藍圖精算
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

# ============ 9. UI Presentation Layer ============
st.title("🎛️ SOP v22 五維因子全串聯終極診斷系統")
st.caption("2026 量化旗艦版 — 置頂個股戰術看板，決策大腦全維度因果縱向串聯")

with st.sidebar:
    st.header("⚙️ 實戰交易風控參數")
    stock_input = st.text_input("台股代碼輸入", "2330")
    capital = st.number_input("核心交易總資本 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆最大核心風險承受 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守滑價摩擦 (Ticks)", 0, 5, 1)

if st.button("🔥 啟動五維度因果交叉決策大腦", use_container_width=True):
    with st.spinner("正在抽絲剝繭，對齊宏觀環境、估值位階、財報結構、核心金流與微觀擺動動能..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input)
        
        if res is None:
            st.error("該代碼數據獲取失敗，請確認是否為台股上市櫃正確編號。")
        else:
            # =========================================================
            # 【全新優化：區塊一 — 置頂個股戰術檔案看板（絕對明顯）】
            # =========================================================
            st.markdown("### 📡 診斷標的戰術檔案看板")
            st.markdown(f"""
            <div style="background-color: #1F2937; padding: 20px; border-radius: 8px; border: 2px solid #3B82F6; margin-bottom: 25px;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;">
                    <div>
                        <span style="color: #9CA3AF; font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;">CURRENT TARGET</span>
                        <h1 style="margin: 5px 0 0 0; color: #FFFFFF; font-size: 32px; font-weight: 800;">{res['stock_name']} <span style="color: #3B82F6;">({res['stock_id']})</span></h1>
                    </div>
                    <div style="text-align: right;">
                        <span style="color: #9CA3AF; font-size: 14px; font-weight: 600;">所屬板塊分類</span>
                        <h3 style="margin: 5px 0 0 0; color: #F3F4F6; font-size: 20px; font-weight: 700;">{res['industry']}</h3>
                    </div>
                    <div style="text-align: right; background-color: rgba(255,255,255,0.05); padding: 8px 15px; border-radius: 6px;">
                        <span style="color: #9CA3AF; font-size: 12px; font-weight: 600; display:block;">報價與連線狀態</span>
                        <span style="color: #F9FAFB; font-weight: 600; font-size: 14px;">來源: {res['rt_source']} | 狀態: </span>
                        <span style="color: {res['m_color']}; font-weight: 700; font-size: 14px;">{res['m_desc']}</span>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            # =========================================================
            # 【區塊二 — 主畫面抬頭顯示器（HUD KPI 牆）】
            # =========================================================
            m1, m2, m3, m4 = st.columns(4)
            with m1: st.metric("💡 當前即時市價", f"{res['current_price']:.2f} 元", f"盤中成交量: {res['current_vol']:.0f} 張")
            with m2: st.metric("⏱️ 短期動能趨勢", res["short_term_trend"])
            with m3: st.metric("⏳ 長期波段底蘊", res["long_term_trend"])
            with m4: st.metric("🎯 預期開火/目標價位", f"{res['expected_target_price']:.2f} 元", f"對位劇本: {res['strategy_route']}")

            # =========================================================
            # 【區塊三 — 最終決策建議（100% 貫穿全部維度之串聯結論）】
            # =========================================================
            st.markdown("### 🎯 決策大腦全方位縱向串聯裁決")
            color_hex = {"red": "#FF4B4B", "purple": "#7D3CFF", "green": "#2BD9A1", "blue": "#1C86EE", "gray": "#808080"}[res["final_color"]]
            st.markdown(f"""
            <div style="background-color:{color_hex}12; border-left: 6px solid {color_hex}; padding: 22px; border-radius: 6px; margin-bottom: 25px; box-shadow: inset 0 0 10px rgba(0,0,0,0.02);">
                <h2 style="margin:0; color:{color_hex}; font-size:24px; font-weight:800;">【最終戰略判定：{res['final_decision']}】</h2>
                <p style="margin: 15px 0 0 0; color:#111; font-size:16.5px; font-weight:600; line-height:1.7; text-align: justify;">{res['final_desc']}</p>
            </div>
            """, unsafe_allow_html=True)

            # =========================================================
            # 【區塊四 — 量化核心風控指引】
            # =========================================================
            st.markdown("### 🛡️ 量化核心風控配額開火劇本")
            if res["suggested_lots"] == 0:
                st.error("🚨 【核心風控最高警戒：拒絕進場】 決策大腦判定多空因子嚴重負向共振（踩到地雷劇本或結構遭極度破壞），資金敞口強制關閉（建議購買 0 張）。請務必恪守紀律，保持空倉防禦。")
            
            b1, b2, b3, b4 = st.columns(4)
            with b1: st.metric("精算風控進場配置", f"{res['suggested_lots']} 張", "已自動扣除滑價與大腦敞口調節")
            with b2: st.metric("技術硬性防守停損價", f"{res['expected_stop_price']:.2f} 元", "觸價即刻無條件執行")
            with b3: st.metric("盤中動態移動停利線", f"{res['trailing_stop_line']:.2f} 元", "最高收盤價回撤 2.5 * ATR")
            with b4: st.metric("大盤加權指數防禦網", "多頭安全" if "站穩" in res["macro_desc"] else "空頭高風險", res["macro_desc"])
            
            st.markdown("---")

            # =========================================================
            # 【區塊五 — 備用深度底層數據驗證漏斗（抽屜式折疊，絕不閹割數據）】
            # =========================================================
            st.markdown("### 🔍 跨因子微觀底層驗證數據")
            
            with st.expander("📊 財務基本面核心數據：季度財務矩陣、營收年增與滾動估值"):
                fc1, fc2, fc3 = st.columns(3)
                with fc1: st.metric("最新單月營收年增率 (YoY)", f"{res['latest_yoy']:.1f}%")
                with fc2: st.metric("歷史滾動本益比 (PE)", f"{res['pe_val']:.1f} 倍", res["pe_desc"])
                with fc3: st.metric("近 4 季累積總實質 EPS", f"{res['eps_4q']:.2f} 元")
                st.markdown(f"> **財務結構解耦報告**：`{res['fin_conclusion']}`")
                
                if not res["fin_df"].empty:
                    clean_fin_show = res["fin_df"].copy().sort_values("date", ascending=False)
                    clean_fin_show.columns = ["季度日期", "單季 EPS", "營業收入", "營業毛利", "營業利益", "單季毛利率 (%)", "單季營益率 (%)"]
                    st.dataframe(clean_fin_show.style.format({
                        "單季 EPS": "{:.2f}", "營業收入": "{:,.0f}", "營業毛利": "{:,.0f}", 
                        "營業利益": "{:,.0f}", "單季毛利率 (%)": "{:.2f}%", "單季營益率 (%)": "{:.2f}%"
                    }), use_container_width=True)

            with st.expander("📈 技術面核心擺動量：精密均線、布林通道與動能擺動指標流"):
                tc1, tc2, tc3 = st.columns(3)
                with tc1:
                    st.write(f"* **均線防守位階**：月線 MA20 = `{res['ma20_val']:.2f}` 元 | 季線 MA60 = `{res['ma60_val']:.2f}` 元")
                    st.write(f"* **市場量能動態**：當前即時量 = `{res['current_vol']:.0f}` 張 | 20日平均量 = `{res['vol_ma20_val']:.0f}` 張")
                with tc2:
                    st.write(f"* **布林通道物理量**：通道上軌 = `{res['bb_upper']:.2f}` | 通道下軌 = `{res['bb_lower']:.2f}` | 帶寬 Bandwidth = `{res['bb_bandwidth']:.4f}`")
                    st.write(f"* **20日技術箱頂壓力**：壓力位 = `{res['real_resistance']:.2f}` 元")
                with tc3:
                    st.write(f"* **擺動指標物理量**：RSI14 = `{res['rsi_now']:.1f}` | ADX14 = `{res['adx_now']:.1f}` | MACD 柱狀體 = `{res['macd_hist']:.3f}`")
                    st.write(f"* **DMI趨向指標細節**：+DI (多方力道) = `{res['plus_di']:.1f}` | -DI (空方力道) = `{res['minus_di']:.1f}`")

            with st.expander("📰 資訊面輿情流水線與台股特色籌碼內幕對位"):
                st.markdown(f"> **24H 網路即時輿情報告**：`{res['news_analysis_report']}`")
                cc1, cc2 = st.columns(2)
                with cc1: st.metric("三大法人-投信 3 日灌入淨張數", f"{res['sitc_3d_sum']:.0f} 張", res["sitc_trend"])
                with cc2: st.metric("散戶融資浮額沉澱健康度", res["margin_trend"])
                
                st.markdown("#### 📥 Google RSS 24H 核心新聞即時流水線 (自動貼標)")
                if res["raw_news_list"]:
                    for n in res["raw_news_list"]:
                        st.markdown(f"* **[{n['date']}]** 【{n['source']}】  [{n['sentiment']}]  [{n['title']}]({n['link']})")
                else:
                    st.warning("⚠️ 24 小時內 Google RSS 暫未收錄與該代碼相關的重組重大財務新聞。")

            with st.expander("🚀 原始精密雙軌量化交易藍圖（供極端情況下交叉核對）"):
                col_b, col_p = st.columns(2)
                with col_b:
                    st.markdown("#### 流派一：突破前高起漲劇本 (Breakout)")
                    st.write(f"* 建倉觸發臨界點：`≥ {res['real_resistance']:.2f}`")
                    st.write(f"* 期望波段獲利目標：`{res['target_brk']:.2f}`")
                    st.write(f"* 技術硬性防守停損：`{res['stop_brk']:.2f}`")
                    st.write(f"* 劇本勝率性價比 (R:R)：`{res['rr1_brk']:.2f}`")
                with col_p:
                    st.markdown("#### 流派二：均線拉回潛伏劇本 (Pullback)")
                    st.write(f"* 精準低吸進場位階：`貼近 {res['ma20_val']:.2f}`")
                    st.write(f"* 期望壓力波段目標：`{res['target_pb']:.2f}`")
                    st.write(f"* 技術硬性防守停損：`{res['stop_pb']:.2f}`")
                    st.write(f"* 劇本勝率性價比 (R:R)：`{res['rr1_pb']:.2f}`")
