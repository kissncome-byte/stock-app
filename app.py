import os, time, requests, pytz, urllib.parse
import pandas as pd
import numpy as np
import streamlit as st
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from FinMind.data import DataLoader

# ============ 1. Page Config & Constants ============
st.set_page_config(page_title="SOP v49 機構級雙速狼王決策系統 (AI適配版)", layout="wide")
TZ = pytz.timezone("Asia/Taipei")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")
FUGLE_TOKEN = os.getenv("FUGLE_TOKEN", "") or st.secrets.get("FUGLE_TOKEN", "")

# 💡 新增：AI 時代自定義供應鏈血緣字典 (可自行擴充)
AI_SECTOR_MAP = {
    "2330": "先進製程/CoWoS", "2317": "GB200 伺服器整機", "3231": "AI 伺服器代工", 
    "2382": "AI 伺服器/散熱", "3324": "水冷散熱模組", "3017": "水冷散熱模組", 
    "3037": "ABF 載板 (CoWoS 相關)", "2454": "邊緣 AI 運算", "3450": "AI 伺服器導軌",
    "3081": "光通訊/矽光子", "3363": "光通訊/矽光子", "2360": "散熱與機構件"
}

# ============ 2. Helper Functions & Utilities ============
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
    if now.weekday() >= 5: return "CLOSED_WEEKEND", f"市場休市 (週末) | 數據: {last_trade_date_str}", "gray"
    start, end = datetime.strptime("09:00", "%H:%M").time(), datetime.strptime("13:35", "%H:%M").time()
    if rt_success:
        if start <= now.time() <= end: return "OPEN", "市場交易中 (即時更新)", "red"
        return ("PRE_MARKET", "盤前準備中", "blue") if now.time() < start else ("POST_MARKET", "今日已收盤 (即時報價)", "green")
    else:
        if start <= now.time() <= end: return "API_WAIT", f"連線受限改用歷史價 | 歷史: {last_trade_date_str}", "orange"
        return ("PRE_MARKET", f"盤前準備中 | 歷史: {last_trade_date_str}", "blue") if now.time() < start else ("POST_MARKET", f"今日已收盤 | 歷史: {last_trade_date_str}", "green")

def analyze_news_sentiment(title: str) -> tuple:
    pos = ['創新高', '大賺', '暴增', '飆', '大成長', '利多', '優於預期', '加碼', '看旺', '強勢', '獲利', '突破', '轉盈', '買超', '爆發', '新高', '三率三升']
    neg = ['衰退', '虧損', '重挫', '低於預期', '縮水', '跌破', '警告', '利空', '下滑', '疲弱', '裁員', '大跌', '慘', '賣壓', '修正', '暴跌', '逆風']
    p_s, n_s = sum(1 for w in pos if w in title), sum(1 for w in neg if w in title)
    return ("🟢 利多", "green") if p_s > n_s else ("🔴 利空", "red") if n_s > p_s else ("🟡 中性", "gray")

# ============ 3. API Connection Layer ============
@st.cache_resource
def get_requests_session():
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504]))
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

# ============ 4. Data Fetching Layers ============
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
                if p_c > 0: return safe_float(data.get("openPrice")) or p_c, safe_float(data.get("highPrice")) or p_c, safe_float(data.get("lowPrice")) or p_c, p_c, v_s if v_s > 0 else hist_lots, True, "Fugle 富果快流", "realtime"
        except Exception: pass
    for prefix in ["otc", "tse"] if is_otc else ["tse", "otc"]:
        try:
            r = session.get(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_id}.tw&json=1&delay=0&_={int(time.time()*1000)}", headers={"Referer": "https://mis.twse.com.tw/"}, timeout=2)
            if r.status_code == 200 and "msgArray" in r.json() and r.json()["msgArray"]:
                info = r.json()["msgArray"][0]
                if str(info.get("c")).strip() == str(stock_id).strip():
                    p_c = safe_float(info.get("z")) or safe_float(info.get("b", "").split("_")[0]) or safe_float(info.get("o"))
                    if p_c > 0: 
                        return safe_float(info.get("o")) or p_c, safe_float(info.get("h")) or p_c, safe_float(info.get("l")) or p_c, p_c, safe_float(info.get("v")) or hist_lots, True, f"TWSE {prefix.upper()} 官方流", "realtime"
        except Exception: pass
    return hist_last_close, hist_last_close, hist_last_close, hist_last_close, hist_lots, False, "歷史收盤備援", "historical"

@st.cache_data(ttl=1800)
def get_overnight_radar():
    session = get_requests_session()
    targets = {"台灣加權大盤 (^TWII)": "^TWII", "Nasdaq那指 (^IXIC)": "^IXIC", "費城半導體 (^SOX)": "^SOX", "台積電 ADR (TSM)": "TSM"}
    radar_res, is_us_panic, panic_desc, wtx_change = {}, False, "", 0.0
    for label, symbol in targets.items():
        try:
            r = session.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d", timeout=3)
            if r.status_code == 200 and r.json().get("chart", {}).get("result"):
                res = r.json()["chart"]["result"][0]
                closes = [safe_float(c) for c in res.get("indicators", {}).get("quote", [{}])[0].get("close", []) if c is not None]
                if len(closes) >= 2:
                    current_p, previous_c = closes[-1], closes[-2]
                    pct = ((current_p - previous_c) / previous_c) * 100
                    radar_res[label] = pct
                    if symbol == "^TWII": wtx_change = pct
                    if symbol != "^TWII" and pct <= -2.5: is_us_panic, panic_desc = True, f"美股重挫，{label} 跌 {pct:.1f}%"
        except Exception: pass
    return radar_res, is_us_panic, panic_desc, wtx_change

@st.cache_data(ttl=86400)
def get_stock_info_df():
    try:
        api = get_api()
        df = api.taiwan_stock_info()
        if df is not None and not df.empty: return df
    except Exception: pass
    return pd.DataFrame([{"stock_id": "2330", "stock_name": "台積電", "type": "twse", "industry_category": "半導體業"}])

@st.cache_data(ttl=900)
def get_daily_df(stock_id: str, market_type: str = "TSE", days: int = 600):
    try:
        df_raw = get_api().taiwan_stock_daily(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d"))
        if df_raw is not None and not df_raw.empty:
            df = df_raw.rename(columns={"Trading_Volume": "vol", "Trading_money": "amount", "max": "high", "min": "low"})
            for c in ["open", "close", "high", "low", "vol", "amount"]: df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.dropna(subset=["close", "vol"]).copy()
    except Exception: pass
    return None

@st.cache_data(ttl=1800)
def get_market_macro_status():
    try:
        df = get_api().taiwan_stock_daily(stock_id="TAIEX", start_date=(datetime.now()-timedelta(days=150)).strftime("%Y-%m-%d"))
        if df is not None and not df.empty:
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['MA20'], df['MA60'] = df['close'].rolling(20).mean(), df['close'].rolling(60).mean()
            last, prev = df.iloc[-1], df.iloc[-5] if len(df) >= 5 else df.iloc[0]
            ret, bias = ((last['close'] - prev['close']) / prev['close']) * 100, ((last['close'] - last['MA60']) / last['MA60']) * 100
            if (last['close'] < last['MA20']) and (ret <= -3.5): return False, f"🚨 大盤瀑布重挫，近週跌 {ret:.1f}%", True, False
            if bias >= 8.5: return True, f"⚠️ 大盤極度過熱，季線正乖離 {bias:.1f}%", False, True
            return (True, f"加權指數穩健站上 20MA", False, False) if last['close'] >= last['MA20'] else (False, f"加權指數跌破 20MA 空方警戒", False, False)
    except Exception: pass
    return True, "🟢 數據受限，預設保護模式", False, False

@st.cache_data(ttl=900)
def get_taiwan_enhanced_chips(stock_id: str, days: int = 30):
    # 💡 升級：外資籌碼納入運算
    s_trend, m_trend, s_3d, f_3d, m_diff = "🟡 中性", "🟡 平穩", 0.0, 0.0, 0.0
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        idf = get_api().taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start)
        if idf is not None and not idf.empty:
            idf['net'] = pd.to_numeric(idf['buy'], errors='coerce').fillna(0) - pd.to_numeric(idf['sell'], errors='coerce').fillna(0)
            sdf = idf[idf['name'] == 'Investment_Trust']
            fdf = idf[idf['name'] == 'Foreign_Investor']
            s_3d = float(sdf.tail(3)['net'].sum()) if not sdf.empty else 0.0
            f_3d = float(fdf.tail(3)['net'].sum()) if not fdf.empty else 0.0
            s_trend = "🟢 投信強力鎖碼" if s_3d > 500 else "🔴 投信高檔棄養" if s_3d < -500 else "🟡 中性"
    except Exception: pass
    try:
        mdf = get_api().taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start)
        if mdf is not None and not mdf.empty:
            mdf['MarginPurchaseTodayBalance'] = pd.to_numeric(mdf['MarginPurchaseTodayBalance'], errors='coerce')
            m_diff = float(mdf.iloc[-1]['MarginPurchaseTodayBalance'] - mdf.iloc[-5]['MarginPurchaseTodayBalance'])
            m_trend = "🚨 散戶強套" if m_diff > 1000 else "🟢 散戶退潮" if m_diff < -1000 else "🟡 平穩"
    except Exception: pass
    return s_trend, m_trend, s_3d, f_3d, m_diff

@st.cache_data(ttl=900)
def get_rev_df(stock_id: str, days: int = 730):
    try: return get_api().taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"))
    except Exception: return None

@st.cache_data(ttl=86400)
def get_financial_statement_df(stock_id: str, years: int = 2):
    try:
        raw = get_api().taiwan_stock_financial_statement(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=years*365)).strftime("%Y-%m-%d"))
        if raw is not None and not raw.empty:
            raw["type"] = raw["type"].replace({"OperatingRevenue": "Revenue"})
            return raw[raw["type"].isin(["EPS", "Revenue", "GrossProfit", "OperatingIncome"])].pivot_table(index="date", columns="type", values="value", aggfunc="last").reset_index()
    except Exception: pass
    return pd.DataFrame()

# ============ 5. Technical Engine ============
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
    x["BB_bandwidth"] = np.where(x["MA20"] == 0, 0, (x["BB_upper"] - x["BB_lower"]) / x["MA20"])
    
    delta = x["close"].diff()
    gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    # 💡 升級：徹底解決分母為零的 RSI 計算問題
    loss_14 = -delta.clip(upper=0).ewm(com=13, adjust=False).mean()
    rs_14 = np.where(loss_14 == 0, 100, gain / loss_14)
    x["RSI14"] = np.where(loss_14 == 0, 100, 100 - (100 / (1 + rs_14)))
    
    x["up"], x["down"] = x["high"].diff(), x["low"].shift(1) - x["low"]
    x["p_dm"] = np.where((x["up"] > x["down"]) & (x["up"] > 0), x["up"], 0)
    x["m_dm"] = np.where((x["down"] > x["up"]) & (x["down"] > 0), x["down"], 0)
    
    tr_s = x["TR"].ewm(com=13, adjust=False).mean()
    # 💡 升級：徹底解決分母為零的 ADX 計算問題
    x["P_DI"] = np.where(tr_s == 0, 0, (x["p_dm"].ewm(com=13, adjust=False).mean() / tr_s) * 100)
    x["M_DI"] = np.where(tr_s == 0, 0, (x["m_dm"].ewm(com=13, adjust=False).mean() / tr_s) * 100)
    di_sum = x["P_DI"] + x["M_DI"]
    x["ADX14"] = np.where(di_sum == 0, 0, ((x["P_DI"] - x["M_DI"]).abs() / di_sum * 100).ewm(com=13, adjust=False).mean())
    
    x["EMA12"], x["EMA26"] = x["close"].ewm(span=12, adjust=False).mean(), x["close"].ewm(span=26, adjust=False).mean()
    x["MACD_DIF"] = x["EMA12"] - x["EMA26"]
    x["MACD_SIGNAL"] = x["MACD_DIF"].ewm(span=9, adjust=False).mean()
    x["MACD_HIST"] = x["MACD_DIF"] - x["MACD_SIGNAL"]
    
    l_min, h_max = x["low"].rolling(9).min(), x["high"].rolling(9).max()
    rsv_denom = h_max - l_min
    x["RSV"] = np.where(rsv_denom == 0, 50, 100 * ((x["close"] - l_min) / rsv_denom))
    k_l, d_l, ck, cd = [], [], 50.0, 50.0
    for rsv in x["RSV"]:
        if pd.isna(rsv): k_l.append(np.nan); d_l.append(np.nan)
        else: ck = (2/3)*ck + (1/3)*rsv; cd = (2/3)*cd + (1/3)*ck; k_l.append(ck); d_l.append(cd)
    x["K9"], x["D9"] = k_l, d_l
    
    if "open" in x.columns:
        x["u_shadow"] = x["high"] - np.maximum(x["open"], x["close"])
        body_size = (x["open"] - x["close"]).abs()
        range_size = x["high"] - x["low"]
        x["is_long_upper_shadow"] = (x["u_shadow"] > body_size) & (np.where(range_size == 0, 0, x["u_shadow"] / range_size) > 0.4)
    else: x["is_long_upper_shadow"] = False
    return x.dropna(subset=["ATR14", "MA20", "RSI14", "K9"]).copy()

# ============ 6. Strategy Brain ============
def auto_strategy_classifier(res_dict):
    p, r, m20, spring = res_dict["current_price"], res_dict["real_resistance"], res_dict["ma20_val"], res_dict["spring_verdict"]
    if "買點一成立" in spring or "買點二成立" in spring: return "LEFT_SPRING", "🛡️ 左側交易：破底翻結構"
    if p >= r * 0.97 or p > m20: return "RIGHT_BREAKOUT", "🚀 右側交易：強勢突破型態"
    return "NEUTRAL_ZONE", "⚖️ 混沌常態：無極端共振型態"

def unified_institutional_brain(res_dict, df_hist, is_holding=False, entry_cost=0.0, sector_panic=False):
    st_type, st_name = auto_strategy_classifier(res_dict)
    p, r, ma5, poc = res_dict["current_price"], res_dict["real_resistance"], res_dict["ma5_val"], res_dict["volume_poc"]
    m_safe, panic = res_dict["macro_bull"], res_dict.get("is_market_panic", False)
    u_panic, wtx = res_dict.get("is_us_panic", False), res_dict.get("wtx_change", 0.0)
    atr = res_dict["atr"]
    
    trailing_stop = float(df_hist["close"].tail(20).max()) - (2.5 * atr)
    f_good = res_dict.get("ai_exemption", False) or res_dict["latest_yoy"] >= 20
    is_rs_gold, vol_spike = res_dict["is_rs_gold"], res_dict["vol_spike"]
    
    pnl_pct = ((p - entry_cost) / entry_cost * 100) if (is_holding and entry_cost > 0) else 0.0

    if is_holding and entry_cost > 0:
        if pnl_pct <= -7.0:
            return {"strategy_name": "🚨 硬性資本停損", "color": "#FF4B4B", "action_now": "🛑 🔴 全額立刻清倉", "signal": "本金敞口破防", "desc": f"觸發 -7% 硬性清算底線。", "blueprint": {"停損防守": f"{entry_cost * 0.93:.2f} 元", "移動停利": "無", "預期目標": "保全資金"}}
        if sector_panic and not is_rs_gold:
            return {"strategy_name": "🚨 族群共振危機", "color": "#EF4444", "action_now": "🚨 🔴 全面減碼 50%", "signal": "板塊集體踩踏", "desc": "同族群下殺，尚未發射 RS 黃金箭頭，先落袋防身。", "blueprint": {"停損防守": f"{trailing_stop:.2f} 元", "移動停利": "已減碼", "預期目標": "保全資產"}}
        if p < trailing_stop:
            return {"strategy_name": "⏳ 波段趨勢終結", "color": "#EF4444", "action_now": "🛑 🔴 剩餘部位清倉", "signal": "跌破 ATR 防線", "desc": "結構轉惡，全額清倉嚴防虧損。", "blueprint": {"停損防守": "全額離場", "移動停利": "觸發", "預期目標": "資金退場"}}
        
        # 💡 升級：智能均線防守，RS 強勢股容忍跌破 5MA，改守 POC
        dynamic_short_term_support = poc if is_rs_gold else ma5
        if pnl_pct > 0 and p < dynamic_short_term_support:
            support_desc = "密集籌碼區 POC" if is_rs_gold else "5MA 攻擊線"
            return {"strategy_name": "🚀 短線達標落袋", "color": "#F59E0B", "action_now": "⚠️ 🟡 減碼 50% 鎖定價差", "signal": f"跌破 {support_desc}", "desc": f"跌破短線防線 ({dynamic_short_term_support:.2f})，賣出 50%。", "blueprint": {"停損防守": "鎖定利潤", "移動停利": f"守 {trailing_stop:.2f} 元", "預期目標": f"看 {res_dict['target_brk']:.2f} 元"}}
        
        return {"strategy_name": "🔥 多頭持股常態", "color": "#7D3CFF", "action_now": "🔮 🔮 強勢狂飆續抱", "signal": "趨勢良性洗盤", "desc": "完美運行於防線內，放飛波段利潤！", "blueprint": {"停損防守": f"{entry_cost * 0.93:.2f} 元", "移動停利": f"破 {dynamic_short_term_support:.2f} 減碼", "預期目標": f"{res_dict['target_brk']:.2f} 元"}}

    else:
        # 空倉邏輯
        if wtx <= -1.2 and res_dict.get("relative_strength", 0) >= 4.0 and p > ma5:
            return {"strategy_name": "🔮 Alpha 逆境劇本", "color": "#7D3CFF", "action_now": "🔮 🔮 特許輕倉狙擊", "signal": "大盤崩防+獨立強勢", "desc": "大盤重挫但個股 RS 爆表，給予 30% 風控配額開火權！", "blueprint": {"停損防守": "當日低點", "移動停利": f"{ma5:.2f} 元", "預期目標": f"{res_dict['target_brk']:.2f} 元"}}
        if "季底法人清算結帳期" in res_dict.get("macro_season", ""):
            return {"strategy_name": "🚨 結帳踩踏防禦", "color": "#FF4B4B", "action_now": "🛑 🔴 嚴禁全新開倉", "signal": "季底流動性風險", "desc": "正值季底清算，手握現金拒絕接盤。", "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "等待新季度"}}
        if st_type == "RIGHT_BREAKOUT":
            if not m_safe and not f_good:
                return {"strategy_name": st_name, "color": "#FF4B4B", "action_now": "🚨 🔴 環境高風險禁開火", "signal": "空頭警戒", "desc": "大盤失守生命線，且個股無基本面特許防護。", "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "觀望"}}
            return {"strategy_name": st_name, "color": "#7D3CFF", "action_now": "🔮 🔮 全新多頭建倉", "signal": "多頭共振發動", "desc": "帶量突破，適合執行全新多頭開火建倉！", "blueprint": {"停損防守": f"{r:.2f} 元", "移動停利": f"{trailing_stop:.2f} 元", "預期目標": f"{res_dict['target_brk']:.2f} 元"}}
        return {"strategy_name": "💤 空倉常態觀望", "color": "#64748B", "action_now": "⚖️ 🔵 保持空倉耐心等待", "signal": "進入量化緩衝帶", "desc": "無方向性整理區，盲目進場易被洗盤。", "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "等待點火"}}

# ============ 7. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int, is_holding=False, entry_cost=0.0, sector_panic=False):
    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    
    # 💡 升級：AI 族群動態標籤覆蓋
    if stock_id in AI_SECTOR_MAP:
        industry = f"🔥 {AI_SECTOR_MAP[stock_id]} (AI核心供應鏈)"
        stock_name = str(match["stock_name"].iloc[0]) if not match.empty else f"代號 {stock_id}"
        market_type = "TSE"
    elif match.empty:
        stock_name, industry, market_type = f"代號 {stock_id}", "自訂追蹤板塊", "TWO" if (stock_id.startswith(("3", "5", "6", "8")) and len(stock_id) == 4) else "TSE"
    else:
        m_col = "type" if "type" in match.columns else "market"
        market_type = str(match[m_col].iloc[0]).strip().upper() if m_col else "TSE"
        stock_name, industry = str(match["stock_name"].iloc[0]), str(match["industry_category"].iloc[0])
    
    df_raw = get_daily_df(stock_id, market_type=market_type, days=600)
    if df_raw is None or df_raw.empty: return None

    macro_bull, macro_desc, is_market_panic, is_market_overextended = get_market_macro_status()
    radar_results, is_us_panic, us_panic_desc, wtx_change = get_overnight_radar()
    
    hist_last_raw = df_raw.iloc[-1]
    rt_open, rt_high, rt_low, rt_close, rt_vol, rt_success, rt_source, rt_type = compute_live_data(stock_id, market_type, float(hist_last_raw["close"]), float(hist_last_raw["vol"]))
    
    df_for_indicators = df_raw.copy().sort_values("date").reset_index(drop=True)
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    
    if rt_success:
        if str(df_for_indicators.iloc[-1]["date"]) == today_str:
            idx = df_for_indicators.index[-1]
            df_for_indicators.loc[idx, ["close", "high", "low", "vol"]] = [rt_close, max(rt_high, df_for_indicators.loc[idx, "high"]), min(rt_low, df_for_indicators.loc[idx, "low"]), rt_vol * 1000.0]
        else:
            new_row = pd.DataFrame([{"date": today_str, "open": rt_open, "high": rt_high, "low": rt_low, "close": rt_close, "vol": rt_vol * 1000.0, "amount": rt_close * rt_vol * 1000.0}])
            # 💡 修復：使用 concat 避免 SettingWithCopyWarning
            df_for_indicators = pd.concat([df_for_indicators, new_row], ignore_index=True)

    df = prepare_indicator_df(df_for_indicators)
    if df is None or df.empty: return None

    # POC 計算
    hist_recent = df.copy().tail(90)
    counts, bins = np.histogram(hist_recent["close"], bins=15, weights=hist_recent["amount"])
    volume_poc = (bins[np.argmax(counts)] + bins[np.argmax(counts) + 1]) / 2

    hist_last = df.iloc[-1]
    ma5_val, ma20_val, real_res = float(hist_last["MA5"]), float(hist_last["MA20"]), float(hist_last["Res_20D"])
    atr, k9_now, d9_now = safe_float(hist_last.get("ATR14", 1.0)), safe_float(hist_last.get("K9", 50.0)), safe_float(hist_last.get("D9", 50.0))
    
    # 籌碼與估值
    s_trend, m_trend, s_3d, f_3d, m_diff = get_taiwan_enhanced_chips(stock_id)
    main_force_score = 45.0 + (15.0 if s_3d > 500 else 0) + (15.0 if f_3d > 2000 else 0) + (10.0 if m_diff < -1000 else 0)
    main_force_label = f"🔥 強力控盤" if main_force_score >= 65 else f"⚖️ 籌碼常態"

    latest_yoy, latest_mom = 0.0, 0.0
    rev_df = get_rev_df(stock_id, days=730)
    if rev_df is not None and "revenue" in rev_df.columns:
        rev_clean = rev_df.copy()
        rev_clean["revenue"] = pd.to_numeric(rev_clean["revenue"].astype(str).str.replace(",", ""), errors="coerce")
        rev_clean["yoy"] = rev_clean["revenue"].pct_change(12) * 100
        rev_clean["mom"] = rev_clean["revenue"].pct_change(1) * 100
        if not rev_clean.dropna().empty:
            latest_yoy = float(rev_clean.dropna().iloc[-1]["yoy"])
            latest_mom = float(rev_clean.dropna().iloc[-1]["mom"])
            
    # 💡 升級：AI 成長股估值豁免機制
    ai_exemption = (latest_yoy >= 20.0 and latest_mom > 0) or (stock_id in AI_SECTOR_MAP and latest_yoy > 10.0)
    
    fin_df_raw = get_financial_statement_df(stock_id)
    pe_val, sum_eps_4q = 0.0, 0.0
    if not fin_df_raw.empty and "EPS" in fin_df_raw.columns:
        sum_eps_4q = pd.to_numeric(fin_df_raw.tail(4)['EPS'], errors='coerce').sum()
        if sum_eps_4q > 0: pe_val = rt_close / sum_eps_4q
    
    pe_desc = "🟢 AI 成長特許擴張期 (無視PE)" if ai_exemption else "🚨 傳統估值偏高" if pe_val > 25 else "⚖️ 估值合理"

    stock_daily_pct = ((rt_close - float(hist_last_raw["close"])) / float(hist_last_raw["close"])) * 100 if float(hist_last_raw["close"]) > 0 else 0.0
    relative_strength = stock_daily_pct - wtx_change
    is_rs_gold = (wtx_change <= -1.0) and (relative_strength >= 3.0)
    
    t = tick_size(rt_close)
    target_brk = round_to_tick(rt_close + (5.0 * atr), t)
    
    package = {
        "current_price": rt_close, "real_resistance": real_res, "ma20_val": ma20_val, "ma5_val": ma5_val,
        "volume_poc": volume_poc, "macro_bull": macro_bull, "is_market_panic": is_market_panic,
        "is_us_panic": is_us_panic, "wtx_change": wtx_change, "spring_verdict": "",
        "atr": atr, "latest_yoy": latest_yoy, "ai_exemption": ai_exemption,
        "is_rs_gold": is_rs_gold, "vol_spike": False, "target_brk": target_brk
    }
    
    tactical_blueprint = unified_institutional_brain(package, df, is_holding, entry_cost, sector_panic)
    
    # 建構最終回傳結果
    res_dict = package.copy()
    res_dict.update({
        "stock_id": stock_id, "stock_name": stock_name, "industry": industry,
        "rt_source": rt_source, "stock_daily_pct": stock_daily_pct,
        "relative_strength": relative_strength, "main_force_label": main_force_label,
        "s_3d": s_3d, "f_3d": f_3d, "m_diff": m_diff, "pe_val": pe_val, "pe_desc": pe_desc,
        "tactical_blueprint": tactical_blueprint, "k9_now": k9_now, "d9_now": d9_now
    })
    return res_dict

# ============ 8. UI Presentation Layer ============
with st.sidebar:
    st.header("🛡️ 全球資金池風控參數")
    capital = st.number_input("核心大資金池 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆最大核心風險承受 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守技術滑價 (Ticks)", 0, 5, 1)
    st.markdown("---")
    st.subheader("🌐 族群板塊即時連線監控")
    sector_panic_toggle = st.checkbox("🔥 同族群其他龍頭股「集體下殺破5%」", value=False)
    if st.button("♻️ 手動強制重整快取"):
        st.cache_data.clear()
        st.rerun()

st.markdown("## 📡 雙速策略大腦動態綜合看盤台 (v49 AI 狂潮適配版)")
stock_input = st.text_input("輸入要精細診斷的目標個股代碼：", value="2317")

u_col1, u_col2 = st.columns(2)
with u_col1: user_holding = st.checkbox("📊 我目前手中「已持有」此個股", value=False)
with u_col2: user_cost = st.number_input("每股真實持股成本 (元)", value=0.0, step=1.0, disabled=not user_holding)

if st.button("🔥 執行精密大腦雙速成本定錨診斷", use_container_width=True) or stock_input:
    with st.spinner("AI 供應鏈與五維度大腦解耦中..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input, is_holding=user_holding, entry_cost=user_cost, sector_panic=sector_panic_toggle)
        if res is None: st.error("數據獲取失敗，請確認代碼與網路狀態。")
        else:
            bp_data = res["tactical_blueprint"]
            bp = bp_data["blueprint"]
            
            # 主力戰略區塊
            st.html(f"""
            <div style="background-color: {bp_data['color']}10; border: 2px solid {bp_data['color']}; padding: 22px; border-radius: 8px; margin-bottom: 25px;">
                <h3 style="color: {bp_data['color']}; margin-top:0;">{bp_data['strategy_name']} | {bp_data['action_now']}</h3>
                <p><b>實戰決策研判：</b>{bp_data['desc']}</p>
                <ul>
                    <li>🛑 防守底線: {bp['停損平倉'] if '停損平倉' in bp else bp.get('停損防守', '無')}</li>
                    <li>⚠️ 移動停利: {bp['移動停利']}</li>
                    <li>🚀 預期目標: {bp['預期目標']}</li>
                </ul>
            </div>
            """)
            
            # 數據面板區塊
            c1, c2, c3, c4 = st.columns(4)
            with c1: st.markdown(custom_hud_box("💡 當前即市價", f"{res['current_price']:.2f} 元<br><small>漲跌: {res['stock_daily_pct']:+.2f}%</small>"), unsafe_allow_html=True)
            with c2: st.markdown(custom_hud_box("⏱️ 智能短線支撐 (POC/5MA)", f"{res['volume_poc']:.2f} / {res['ma5_val']:.2f}<br><small>防禦彈性切換</small>"), unsafe_allow_html=True)
            with c3: st.markdown(custom_hud_box("🔥 AI 估值與成長", f"YoY: {res['latest_yoy']:+.1f}%<br><small>{res['pe_desc']}</small>"), unsafe_allow_html=True)
            with c4: st.markdown(custom_hud_box("🦅 外資與投信籌碼 (近3日)", f"投: {res['s_3d']:.0f} | 外: {res['f_3d']:.0f}<br><small>{res['main_force_label']}</small>"), unsafe_allow_html=True)
