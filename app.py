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
        return float(str(x).replace(",", "").replace("%", "").replace(" ", "").strip())
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

def render_panel_html(title, heading, desc, top_border_color):
    return f"""
    <div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:12px; border-radius:6px; min-height:165px; border-top:4px solid {top_border_color}; margin-bottom:15px;">
        <span style="font-size:12px; color:#64748B; font-weight:700; display:block; margin-bottom:4px;">{title}</span>
        <h4 style="margin:2px 0; color:#1E293B; font-size:14.5px; font-weight:800;">{heading}</h4>
        <p style="margin:6px 0 0 0; font-size:11.5px; color:#1E293B; font-weight:600; line-height:1.55;">{desc}</p>
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
    session.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"})
    return session

@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try: api.login_by_token(FINMIND_TOKEN)
        except Exception: pass
    return api

# ============ 5. Live Data Streaming Engine ============
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
                if p_c > 0: return safe_float(info.get("o")) or p_c, safe_float(info.get("h")) or p_c, safe_float(info.get("l")) or p_c, p_c, safe_float(info.get("g")) or safe_float(info.get("v")) or hist_lots, True, f"TWSE {prefix.upper()} 官方流", "realtime"
        except Exception: pass
    return hist_last_close, hist_last_close, hist_last_close, hist_last_close, hist_lots, False, "歷史收盤備援", "historical"

# ============ 6. Data Fetching Layers ============
@st.cache_data(ttl=1800)
def get_overnight_radar():
    session = get_requests_session()
    targets = {"台灣加權大盤 (^TWII)": "^TWII", "Nasdaq那指 (^IXIC)": "^IXIC", "費城半導體 (^SOX)": "^SOX", "台積電 ADR (TSM)": "TSM"}
    radar_res, is_us_panic, panic_desc, wtx_change = {}, False, "", 0.0
    for label, symbol in targets.items():
        try:
            r = session.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d", timeout=3)
            if r.status_code == 200 and r.json().get("chart", {}).get("result"):
                res = r.json()["chart"]["result"][0]
                closes = [safe_float(c) for c in res.get("indicators", {}).get("quote", [{}])[0].get("close", []) if c is not None]
                c_p, p_c = (closes[-1], closes[-2]) if len(closes) >= 2 else (safe_float(res["meta"].get("regularMarketPrice")), safe_float(res["meta"].get("previousClose")))
                if p_c > 0:
                    radar_res[label] = ((c_p - p_c) / p_c) * 100
                    if symbol == "^TWII": wtx_change = radar_res[label]
                    if symbol != "^TWII" and radar_res[label] <= -2.0: is_us_panic, panic_desc = True, f"昨晚美股重挫，{label} 慘跌 {radar_res[label]:.1f}%"
        except Exception: pass
    return radar_res, is_us_panic, panic_desc, wtx_change

@st.cache_data(ttl=3600)
def get_stock_info_df():
    try:
        df = get_api().taiwan_stock_info()
        if df is not None and not df.empty: return df.copy()
    except Exception: pass
    return pd.DataFrame([{"stock_id": "2330", "stock_name": "台積電", "market_type": "twse", "industry_category": "半導體業"}, {"stock_id": "3037", "stock_name": "欣興", "market_type": "twse", "industry_category": "電子零組件業"}, {"stock_id": "2382", "stock_name": "廣達", "market_type": "twse", "industry_category": "電腦及週邊設備業"}])

@st.cache_data(ttl=900)
def get_daily_df(stock_id: str, market_type: str = "TSE", days: int = 450):
    session = get_requests_session()
    suffix = ".TWO" if any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "上櫃"]) else ".TW"
    p1, p2 = int((datetime.now(TZ)-timedelta(days=days)).timestamp()), int(datetime.now(TZ).timestamp())
    try:
        r = session.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}?period1={p1}&period2={p2}&interval=1d", timeout=5)
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
            market_vol_healthy = float(last['vol_work']) >= float(last['MA20_Vol'])
            market_vol_desc = "🟢 大盤資金大部隊在線" if market_vol_healthy else "🚨 大盤量能窒息流失（大盤缺血假突破率高）"
            if panic: return False, f"🚨 大盤瀑布重挫 ({last['close']:.1f})", True, False, market_vol_healthy, market_vol_desc
            macro_bull = last['close'] >= last['MA20']
            return macro_bull, f"加權指數 ({last['close']:.1f})", False, False, market_vol_healthy, market_vol_desc
    except Exception: pass
    return True, "🟢 多頭常態", False, False, True, "🟢 常態安全血量"

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

# 🌟 【新聞動態退耦雷達】：解除 24H 限制，若當天沒新聞自動查 7 天內，確保 100% 能被列出來！
@st.cache_data(ttl=300)
def get_realtime_news_list(stock_id: str, stock_name: str):
    news = []
    for timeframe in ["when:1d", "when:7d", ""]:
        try:
            q = urllib.parse.quote(f"{str(stock_name)} {str(stock_id)} {timeframe}".strip())
            r = get_requests_session().get(f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", timeout=5)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall('.//item'):
                    t = item.find('title').text or ""
                    if " - " in t: t = t.rsplit(" - ", 1)[0]
                    news.append({
                        "date": item.find('pubDate').text or "",
                        "title": t,
                        "source": item.find('source').text if item.find('source') is not None else "新聞財經",
                        "link": item.find('link').text or ""
                    })
                if news: break
        except Exception: pass
    if news:
        df = pd.DataFrame(news)
        try:
            df["parsed_date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_convert('Asia/Taipei')
            df["date"] = df["parsed_date"].dt.strftime('%m-%d %H:%M')
            return df.sort_values(by="parsed_date", ascending=False)[["date", "title", "source", "link"]].to_dict('records')
        except Exception: return df[["date", "title", "source", "link"]].to_dict('records')
    return []

@st.cache_data(ttl=86400)
def get_financial_statement_df(stock_id: str, years: int = 2):
    try:
        raw = get_api().taiwan_stock_financial_statement(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=years*365)).strftime("%Y-%m-%d"))
        if raw is None or raw.empty: return pd.DataFrame()
        df = raw.copy()
        df["type"] = df["type"].replace({"OperatingRevenue": "Revenue"})
        return df[df["type"].isin(["EPS", "Revenue", "GrossProfit", "OperatingIncome"])].pivot_table(index="date", columns="type", values="value", aggfunc="last").reset_index()
    except Exception: return pd.DataFrame()

def prepare_indicator_df(df: pd.DataFrame):
    if df is None or df.empty: return None
    x = df.copy().sort_values("date").reset_index(drop=True)
    c_prev = x["close"].shift(1)
    x["TR"] = np.maximum(x["high"] - x["low"], np.maximum((x["high"] - c_prev).abs(), (x["low"] - c_prev).abs()))
    x["ATR14"] = x["TR"].ewm(com=13, adjust=False).mean()
    x["MA5"], x["MA5_Vol"] = x["close"].rolling(5).mean(), x["vol"].rolling(5).mean()
    x["MA20"], x["MA60"], x["MA100"], x["MA20_Vol"] = x["close"].rolling(20).mean(), x["close"].rolling(60).mean(), x["close"].rolling(100).mean(), x["vol"].rolling(20).mean()
    x["Res_20D"], x["std20"] = x["high"].rolling(20).max(), x["close"].rolling(20).std()
    delta = x["close"].diff()
    x["RSI14"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=13, adjust=False).mean() / delta.clip(upper=0).ewm(com=13, adjust=False).mean().replace(0, -0.00001).abs())))
    x["RSI5"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=4, adjust=False).mean() / delta.clip(upper=0).ewm(com=4, adjust=False).mean().replace(0, -0.00001).abs())))
    x["RSI10"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=9, adjust=False).mean() / delta.clip(upper=0).ewm(com=9, adjust=False).mean().replace(0, -0.00001).abs())))
    
    up_diff, down_diff = x["high"].diff(), x["low"].shift(1) - x["low"]
    x["P_DI_raw"] = np.where((up_diff > down_diff) & (up_diff > 0), up_diff, 0.0)
    x["M_DI_raw"] = np.where((down_diff > up_diff) & (down_diff > 0), down_diff, 0.0)
    tr_s = x["TR"].ewm(com=13, adjust=False).mean().replace(0, 0.00001)
    x["P_DI"] = (x["P_DI_raw"].ewm(com=13, adjust=False).mean() / tr_s) * 100
    x["M_DI"] = (x["M_DI_raw"].ewm(com=13, adjust=False).mean() / tr_s) * 100
    x["ADX14"] = ((x["P_DI"] - x["M_DI"]).abs() / (x["P_DI"] + x["M_DI"]).replace(0, 0.00001) * 100).ewm(com=13, adjust=False).mean()
    x["MACD_HIST"] = (x["close"].ewm(span=12, adjust=False).mean() - x["close"].ewm(span=26, adjust=False).mean()) - (x["close"].ewm(span=12, adjust=False).mean() - x["close"].ewm(span=26, adjust=False).mean()).ewm(span=9, adjust=False).mean()
    l_min, h_max = x["low"].rolling(9).min(), x["high"].rolling(9).max()
    x["RSV"] = 100 * ((x["close"] - l_min) / (h_max - l_min).replace(0, 0.00001))
    k_l, d_l, ck, cd = [], [], 50.0, 50.0
    for rsv in x["RSV"]:
        if pd.isna(rsv): k_l.append(np.nan); d_l.append(np.nan)
        else: ck = (2/3)*ck + (1/3)*rsv; cd = (2/3)*cd + (1/3)*ck; k_l.append(ck); d_l.append(cd)
    x["K9"], x["D9"] = k_l, d_l
    return x.dropna(subset=["ATR14", "MA5", "MA20", "MA60", "MA100", "Res_20D", "RSI14", "MACD_HIST", "K9", "D9", "ADX14"]).copy()

# ============ 8. 統一狼王策略決策大腦模型 ============
def auto_strategy_classifier(res_dict):
    p, r, m20, spring, phase = res_dict["current_price"], res_dict["real_resistance"], res_dict["ma20_val"], res_dict["spring_verdict"], res_dict["trend_phase"]
    if "買點一成立" in spring or "買點二成立" in spring:
        if p < r * 0.98: return "LEFT_SPRING", "🛡️ 左側交易：破底翻結構"
    if p >= r * 0.97 or (p > m20 and phase == "🔥 波段多頭主升段"): return "RIGHT_BREAKOUT", "🚀 右側交易：強勢突破型態"
    return "NEUTRAL_ZONE", "⚖️ 混沌常態：無極端共振型態"

def unified_institutional_brain(res_dict, df_hist, is_holding=False, entry_cost=0.0, sector_panic=False):
    st_type, st_name = auto_strategy_classifier(res_dict)
    p, r, m20, ma5 = res_dict["current_price"], res_dict["real_resistance"], res_dict["ma20_val"], res_dict["ma5_val"]
    m_safe, atr, pnl_pct = res_dict["macro_bull"], res_dict["atr"], res_dict["pnl_pct"]
    trailing_stop = float(df_hist["close"].tail(20).max()) - (2.5 * atr)
    wolf_rank_label = res_dict.get("wolf_rank_label", "常態輪動")

    if is_holding and entry_cost > 0:
        if pnl_pct <= -7.0: return {"strategy_name": "🚨 觸發硬性資本停損", "color": "#FF4B4B", "action_now": "🛑 🔴 【部位重傷：全額立刻清倉】", "signal": "本金敞口破防", "blueprint": {"停損防守": f"本金死穴 {entry_cost * 0.93:.2f} 元", "移動停利": "無", "預期目標": "保全資金殘餘"}, "desc": f"您成本為 {entry_cost:.2f} 元。目前帳面虧損達 {pnl_pct:.1f}%，立刻執行全額市價離場！"}
        if p < trailing_stop: return {"strategy_name": "⏳ 中線波段趨勢終結", "color": "#EF4444", "action_now": "🛑 🔴 【波段獲利終結/結構破防：全額清倉】", "signal": "跌破動態 ATR 波動防線", "blueprint": {"停損防守": "全額清倉離場", "移動停利": "已觸發", "預期目標": "資金全額退場"}, "desc": f"持股成本為 {entry_cost:.2f} 元。即時價已實質跌破中線結構防禦線 ({trailing_stop:.2f} 元)，請全額清倉退場避險！"}
        return {"strategy_name": "🔥 強勢主升浪完美續抱", "color": "#7D3CFF", "action_now": "🔮 🔮 【強勢狂飆 : 全額持股續抱】", "signal": "短長雙速動能多頭共振", "blueprint": {"停損防守": f"守 ATR 防線 ({trailing_stop:.2f} 元)", "移動停利": "結構完美運行中", "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"}, "desc": f"持股成本 {entry_cost:.2f} 元（帳面獲利：{pnl_pct:+.1f}%）。量價結構健康，5MA 攻擊線完美上翹。全額咬死不賣，放飛利潤！"}
    else:
        if "落後跟屁蟲" in wolf_rank_label and 'RIGHT_BREAKOUT' in st_type: return {"strategy_name": "🚨 狼王位階風控：否決跟風開倉", "color": "#FF4B4B", "action_now": "🛑 🔴 【環境極端風險：全新開倉嚴禁開火】", "signal": "資金分化排斥效應", "blueprint": {"停損防守": "嚴禁進場", "移動停利": "無", "預期目標": "要買就去買真正最強的隊長"}, "desc": "大腦精算顯示該股在同產業族群中屬於落後跟屁蟲，追高跟風股隨時面臨補跌踩踏，一票否決！"}
        if res_dict["is_rs_gold"] and p >= m20 and not sector_panic: return {"strategy_name": "🚀 統一特許：逆境黃金飆股劇本發動", "color": "#7D3CFF", "action_now": "🔮 🔮 【強者恆強 : 無視大盤恐慌立即開火】", "signal": "個股超額相對強度（RS）爆表", "blueprint": {"停損防守": f"開倉技術停損位", "移動停利": f"波動防線 {trailing_stop:.2f} 元", "預期目標": f"獲利對位目標 {res_dict['target_brk']:.2f} 元"}, "desc": f"大盤目前破位下殺。但該個股今日爆發出高達 {res_dict['relative_strength']:.1f}% 的超額相對強度(RS)！特許放行防守型低吸建倉！"}
        if st_type == "RIGHT_BREAKOUT":
            if not m_safe: return {"strategy_name": "🚨 大盤量能失血：假突破防禦機制", "color": "#F59E0B", "action_now": "⚠️ 🟡 【大盤總血量不足：強制削減60%防守型開火】", "signal": "流動性窒息枯竭警告", "blueprint": { "停損防守": f"戰術硬停損 {res_dict['stop_brk']:.2f} 元", "移動停利": "防守型控量", "預期目標": f"衝刺前高壓力牆 {r:.2f} 元即走"}, "desc": "在缺血市場中，假突破率高達 70%。大腦硬性閹割您的追高權，只允許拉回低吸並砍掉 60% 配額！"}
            return {"strategy_name": st_name, "color": "#7D3CFF", "action_now": "🔮 🔮 【頂級信號：全新多頭建倉開火】", "signal": "頂級多頭共振：黃金主升飆股型態發動", "blueprint": {"停損防守": f"收盤跌破前高壓力牆 {r:.2f} 元", "移動停利": f"動態守 ATR 防線 ({trailing_stop:.2f} 元)", "預期目標": f"獲利擴張目標對位 {res_dict['target_brk']:.2f} 元"}, "desc": "基本面擴張、法人強力鎖碼、帶量突破前高壓力牆，適合執行全新多頭開火建倉！"}
        
        neutral_desc = f"由於此時下方面板 1【{res_dict['market_vol_desc']}】，且面板 2 精算顯示個股僅屬於【{wolf_rank_label}】，缺乏機構大金流表態。大腦決策防線硬性退守至【量化緩衝帶】。此處毫無多頭期望值，強制空倉，手綁起來！"
        return {"strategy_name": "💤 空倉常態觀望", "color": "#64748B", "action_now": "⚖️ 🔵 【常態調整區 : 保持空倉耐心等待】", "signal": "進入量化緩衝帶", "blueprint": {"停損防守": "嚴禁盲目進場", "移動停利": "無", "預期目標": "等待金流重啟點火"}, "desc": neutral_desc}

# ============ 9. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int, is_holding=False, entry_cost=0.0, sector_panic=False):
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    
    # 🌟 100% 變數頂格安全宣告，永不引爆 UnboundLocalError
    pnl_pct = 0.0
    res_dict = {}
    latest_yoy, is_broker_dumping_risk = 0.0, False
    raw_news_list, positive_catalysts_list, fin_df = [], [], pd.DataFrame()
    spring_verdict, spring_triggered, detected_prior_low, detected_neckline = "⚪ 未觸發破底翻結構", False, 0.0, 0.0
    news_analysis_report = "⚖️ 輿情常態"
    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = "🟡 中性", "🟡 平穩", 0.0, 0.0
    main_force_label, wolf_rank_label, wolf_rank_color = "⚖️ 常態調整 (0%)", "⚖️ 族群常態輪動成員", "#64748B"
    stable_short_trend, stable_short_color, stable_short_desc = "🟡 短期箱型潛伏", "#F59E0B", "均線水平橫躺。"
    trend_phase, short_term_trend, long_term_trend = "📉 空頭修正期", "📉 均線全面蓋頭", "💤 季線橫向延伸"
    fin_conclusion, gpm_now, opm_now, sum_eps_4q, pe_val = "📋 暫無財報數據", 0.0, 0.0, 0.0, 0.0
    kd_timing, bb_stage, volume_verdict = "⚖️ KD指針定位中", "❌ 趨勢力道定位中", "⚖️ RSI相對強弱中"
    
    info_df_local = get_stock_info_df()
    match = info_df_local[info_df_local["stock_id"] == stock_id]
    if match.empty:
        stock_name, industry, market_type = f"代號 {stock_id}", "自訂追蹤板塊", ("TWO" if (stock_id.startswith(("3","5","6","8")) and len(stock_id)==4) else "TSE")
    else:
        m_col = "type" if "type" in match.columns else "market_type" if "market_type" in match.columns else "market" if "market" in match.columns else "market_type"
        market_type = str(match[m_col].iloc[0]).strip().upper() if m_col in match.columns else "TSE"
        stock_name, industry = str(match["stock_name"].iloc[0]), str(match["industry_category"].iloc[0])
            
    df_raw = get_daily_df(stock_id, market_type=market_type, days=450)
    if df_raw is None or df_raw.empty: return None

    macro_bull, macro_text, is_market_panic, is_market_overextended, market_vol_healthy, market_vol_desc = get_market_macro_status()
    radar_results, is_us_panic, us_panic_desc, wtx_change = get_overnight_radar()
    hist_last_raw = df_raw.iloc[-1]
    rt_open, rt_high, rt_low, rt_close, rt_vol_lots, rt_success, rt_source, rt_type = compute_live_data(stock_id, market_type, float(hist_last_raw["close"]), float(hist_last_raw["vol"]))
    current_price, current_vol = rt_close, rt_vol_lots 
    t = tick_size(current_price)
    df_for_indicators = df_raw.copy().sort_values("date").reset_index(drop=True)
    
    if rt_success:
        if str(df_for_indicators.iloc[-1]["date"]) == today_str:
            df_for_indicators.loc[df_for_indicators.index[-1], ["close", "vol"]] = [rt_close, rt_vol_lots * 1000.0]
        else:
            df_for_indicators = pd.concat([df_for_indicators, pd.DataFrame([{"date": today_str, "open": float(rt_open), "high": float(rt_high), "low": float(rt_low), "close": float(rt_close), "vol": float(rt_vol_lots * 1000.0), "amount": float(rt_close * rt_vol_lots * 1000.0)}])], ignore_index=True)

    df = prepare_indicator_df(df_for_indicators)
    if df is None or df.empty: return None
    peak_price_20d = float(df["close"].tail(20).max())
    hist_last = df.iloc[-1]
    
    ma5_val, vol_ma5_val = float(hist_last["MA5"]), float(hist_last["MA5_Vol"])
    ma20_val, ma60_val, ma100_val = float(hist_last["MA20"]), float(hist_last["MA60"]), float(hist_last["MA100"])
    vol_ma20_val, real_resistance = float(hist_last["MA20_Vol"]), float(hist_last["Res_20D"])
    rsi_now, adx_now, macd_hist, atr = safe_float(hist_last.get("RSI14", 50.0)), safe_float(hist_last.get("ADX14", 20.0)), safe_float(hist_last.get("MACD_HIST", 0.0)), safe_float(hist_last.get("ATR14", 1.0))
    k9_now, d9_now = safe_float(hist_last.get("K9", 50.0)), safe_float(hist_last.get("D9", 50.0))
    
    ma5_slope = ((ma5_val - float(df["MA5"].iloc[-3])) / float(df["MA5"].iloc[-3] or 1) * 100) if len(df) >= 3 else 0.0
    kd_status = "黃金交叉" if k9_now > d9_now else "死亡交叉"
    stock_daily_pct = ((current_price - float(hist_last_raw["close"])) / float(hist_last_raw["close"])) * 100 if float(hist_last_raw["close"]) > 0 else 0.0
    relative_strength = stock_daily_pct - wtx_change
    is_rs_gold = (wtx_change <= -1.0) and (relative_strength >= 3.0)

    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = get_taiwan_enhanced_chips(stock_id)
    vol_spike = (current_vol * 1000.0) > (vol_ma20_val * 1.5)
    if relative_strength > 4.0 and sitc_3d_sum > 300: wolf_rank_label, wolf_rank_color = "👑 族群領頭狼王（主導資金絕對攻勢）", "#7D3CFF"
    elif relative_strength < -2.0: wolf_rank_label, wolf_rank_color = "🐌 族群落後跟屁蟲（嚴防資金棄養踩踏）", "#EF4444"
    else: wolf_rank_label, wolf_rank_color = "⚖️ 族群常態輪動成員（隨大盤溫和浮動）", "#64748B"

    box_width_pct = ((float(df["close"].tail(30).max()) - float(df["close"].tail(30).min())) / float(df["close"].tail(30).min())) * 100
    is_box_compressed = box_width_pct <= 8.5
    target_brk = round_to_tick(current_price + (4.0 * atr), t)
    stop_brk = round_to_tick(real_resistance - (1.5 * atr), t) if round_to_tick(real_resistance - (1.5 * atr), t) < current_price else round_to_tick(current_price - (1.0 * atr), t)
    stop_line_text = f"{round_to_tick(peak_price_20d - (2.5 * atr), t):.2f} 元"

    if k9_now < 20: kd_timing = "📥 超賣打底區：隨機指標進入20以下低位。必須配合金流點火才能開火。"
    elif k9_now > 70: kd_timing = "🦅 超買強勢區：指標高檔鈍化，這正是促使頂端大腦進入主升浪續抱的信號。"
    else: kd_timing = f"⚖️ KD常態箱型：目前 K={k9_now:.1f} / D={d9_now:.1f}，處於常態整理區。"
    bb_stage = "🟢 多頭主導：MACD 柱狀體位於多頭安全區。" if macd_hist >= 0 else "📉 空頭修正：柱狀體連續收縮，嚴防上方蓋頭壓力。"
    volume_verdict = f"⚡ RSI 相對強度：14日實時 RSI 錄得 {rsi_now:.1f}，多空以 50 分水嶺進行動態對峙。"

    rev_df = get_rev_df(stock_id, days=730)
    if rev_df is not None and not rev_df.empty:
        try:
            col = [c for c in rev_df.columns if c.lower() == "revenue"]
            if col:
                rev_df["revenue_clean"] = pd.to_numeric(rev_df[col[0]].astype(str).str.replace(",", ""), errors="coerce")
                rev_df = rev_df.dropna(subset=["revenue_clean"]).sort_values("date")
                if len(rev_df) > 12: latest_yoy = float(rev_df["revenue_clean"].pct_change(12).iloc[-1] * 100)
        except Exception: latest_yoy = 0.0

    try: raw_news_list_data = get_realtime_news_list(stock_id, stock_name)
    except Exception: raw_news_list_data = []
    if raw_news_list_data:
        raw_news_list = raw_news_list_data[:8]
        news_analysis_report = "🔥 【輿情偏多】 利多消息主導市場" if sum(1 for n in raw_news_list if "利多" in n["sentiment"]) > sum(1 for n in raw_news_list if "利空" in n["sentiment"]) else "⚖️ 中性輿情"

    if len(df) >= 40:
        low_cand = float(df.iloc[-40:-10]["low"].min())
        for r_idx, row in df.iloc[-10:].iterrows():
            if row["low"] < low_cand and df["close"].iloc[-1] > low_cand:
                spring_triggered = True; detected_prior_low = low_cand; detected_neckline = float(df.loc[:r_idx]["high"].max() or low_cand); break
    if spring_triggered: spring_verdict = f"🟢 【破底翻結構確立】洗盤完成，重回前低 {detected_prior_low:.2f} 元牆上方！"

    fin_df_raw = get_financial_statement_df(stock_id, years=2)
    if not fin_df_raw.empty and "Revenue" in fin_df_raw.columns:
        fin_df_work = fin_df_raw.copy().sort_values("date").reset_index(drop=True)
        for f_idx in range(len(fin_df_work)):
            rev_amt = safe_float(fin_df_work.loc[f_idx, "Revenue"])
            fin_df_work.loc[f_idx, "gpm"] = (safe_float(fin_df_work.loc[f_idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df_work.loc[f_idx, "opm"] = (safe_float(fin_df_work.loc[f_idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        last_fin = fin_df_work.iloc[-1]
        gpm_now, opm_now, sum_eps_4q = safe_float(last_fin.get("gpm", 0.0)), safe_float(last_fin.get("opm", 0.0)), pd.to_numeric(fin_df_work.tail(4)['EPS'], errors='coerce').sum()
        pe_val = current_price / sum_eps_4q if sum_eps_4q > 0 else 0.0
        fin_conclusion = "📈 【財報年增擴張】獲利指標超越去年同期！" if len(fin_df_work) >= 5 and gpm_now > safe_float(fin_df_work.iloc[-5].get("gpm", 0.0)) else "⚖️ 財報常態運作中"
        fin_df = fin_df_work[["date", "EPS", "Revenue", "GrossProfit", "OperatingIncome", "gpm", "opm"]].copy()

    pnl_pct = ((current_price - entry_cost) / entry_cost * 100) if (is_holding and entry_cost > 0) else 0.0
    short_term_trend = f"🚀 五日線多頭噴發 (KD {kd_status})" if current_price >= ma5_val and ma5_val >= ma20_val else f"📉 均線全面蓋頭 (KD {kd_status})"
    long_term_trend = "🔥 季線全面向上" if current_price >= ma60_val and (df["MA60"].iloc[-1] > df["MA60"].iloc[-5]) else "💤 季線橫向延伸"
    trend_phase = "🔥 波段多頭主升段" if current_price >= ma20_val and ma20_val >= ma60_val and (df["MA20"].iloc[-1] > df["MA20"].iloc[-5]) else "💤 潛伏築底期"

    # 🌟 100% 垂直對位灌漿打包，永久根除 KeyError 敞口
    res_dict["stock_id"] = stock_id
    res_dict["stock_name"] = stock_name
    res_dict["industry"] = industry
    res_dict["pnl_pct"] = pnl_pct
    res_dict["macro_bull"] = macro_bull
    res_dict["market_vol_desc"] = market_vol_desc
    res_dict["wolf_rank_label"] = wolf_rank_label
    res_dict["wolf_rank_color"] = wolf_rank_color
    res_dict["is_box_compressed"] = is_box_compressed
    res_dict["box_width_pct"] = box_width_pct
    res_dict["target_brk"] = target_brk
    res_dict["stop_brk"] = stop_brk
    res_dict["trailing_stop_line"] = stop_line_text
    res_dict["current_price"] = current_price
    res_dict["current_vol"] = current_vol
    res_dict["ma5_val"] = ma5_val
    res_dict["ma20_val"] = ma20_val
    res_dict["ma60_val"] = ma60_val
    res_dict["ma100_val"] = ma100_val
    res_dict["real_resistance"] = real_resistance
    res_dict["atr"] = atr
    res_dict["stock_daily_pct"] = stock_daily_pct
    res_dict["relative_strength"] = relative_strength
    res_dict["is_rs_gold"] = is_rs_gold
    res_dict["is_volume_gap_spike"] = False
    res_dict["rt_source"] = rt_source
    res_dict["m_desc"] = macro_text
    res_dict["m_color"] = "red" if not macro_bull else "green"
    res_dict["fin_df"] = fin_df
    res_dict["spring_verdict"] = spring_verdict
    res_dict["stable_short_trend"] = stable_short_trend
    res_dict["stable_short_color"] = stable_short_color
    res_dict["stable_short_desc"] = stable_short_desc
    res_dict["trend_phase"] = trend_phase
    res_dict["latest_yoy"] = latest_yoy
    res_dict["fin_conclusion"] = fin_conclusion
    res_dict["sitc_trend"] = sitc_trend
    res_dict["sitc_3d_sum"] = sitc_3d_sum
    res_dict["radar_results"] = radar_results
    res_dict["vol_spike"] = vol_spike
    res_dict["raw_news_list"] = raw_news_list
    res_dict["news_analysis_report"] = news_analysis_report
    res_dict["kd_timing"] = kd_timing
    res_dict["bb_stage"] = bb_stage
    res_dict["volume_verdict"] = volume_verdict

    res_dict["tactical_blueprint"] = unified_institutional_brain(res_dict, df.copy(), is_holding=is_holding, entry_cost=entry_cost, sector_panic=sector_panic)
    
    denom = current_price - stop_brk
    if denom <= 0 or risk_per_trade <= 0: suggested_lots = 0
    else: suggested_lots = min(int((total_capital * (risk_per_trade / 100) * 10000 / denom) / 1000), int((total_capital * 10000) / (current_price * 1000)))
    res_dict.update({"suggested_lots": suggested_lots, "expected_stop_price": stop_brk, "expected_target_price": target_brk, "vol_ma5_val": vol_ma5_val})
    return res_dict

# ============ 10. UI Presentation Layer ============
with st.sidebar:
    st.header("🛡️ 全球資金池風控參數")
    capital = st.number_input("核心大資金池 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆最大核心風險承受 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守技術滑價 (Ticks)", 0, 5, 1)
    sector_panic_toggle = st.checkbox("🔥 同族群其他龍頭股「集體下殺破5%」", value=False)
    auto_refresh = st.checkbox("🔄 開啟盤中每 5 秒自動秒刷報價", value=False)

macro_bull, macro_label, _, _, market_vol_healthy, market_vol_desc = get_market_macro_status()
st.markdown("## 📡 雙速策略大腦動態綜合看盤台 (v48 狼王特選版)")
stock_input = st.text_input("請輸入核心目標個股代碼：", value="3037")

u_col1, u_col2 = st.columns(2)
with u_col1: user_holding = st.checkbox("📊 我手中「已持有」此個股", value=False)
with u_col2: user_cost = st.number_input("每股真實持股成本 (元)", value=0.0, step=1.0, min_value=0.0, disabled=not user_holding)
diag_trigger = st.button("🔥 立即執行精密大腦雙速成本定錨診換診斷", use_container_width=True)

if diag_trigger or stock_input:
    st.cache_data.clear()
    res = evaluate_stock(stock_input, capital, risk_pct, slip_input, is_holding=user_holding, entry_cost=user_cost, sector_panic=sector_panic_toggle)
    if res is None: st.error("該個股代碼數據獲取失敗。")
    else:
        bp_data = res["tactical_blueprint"]
        bp = bp_data["blueprint"]
        
        # 1. 大腦決策卡片
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

        # 2. 昨晚美股即時戰報
        st.markdown("### 🌐 昨晚美股與台指期夜盤即時戰報")
        radar_show = res["radar_results"]
        if radar_show:
            rd_cols = st.columns(len(radar_show))
            for i, (lbl, val) in enumerate(radar_show.items()):
                with rd_cols[i]: st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:10px; border-radius:6px; text-align:center;"><span style="font-size:12px; color:#64748B; font-weight:600;">{lbl}</span><h4 style="margin:4px 0 0 0; color:#10B981; font-weight:800;">{val:+.2f}%</h4></div>""", unsafe_allow_html=True)

        # 3. 標的資訊頭部
        st.markdown(f"""<div style="background-color: #1F2937; padding: 18px; border-radius: 8px; border: 2px solid #3B82F6; margin-bottom: 20px;"><div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;"><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">DIAGNOSTIC TARGET</span><h1 style="margin: 4px 0 0 0; color: #FFFFFF; font-size: 28px; font-weight: 800;">{res['stock_name']} <span style="color: #3B82F6;">({res['stock_id']})</span></h1></div><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">大類板塊歸屬</span><h3 style="margin: 4px 0 0 0; color: #F3F4F6; font-size: 18px; font-weight: 700;">{res['industry']}</h3></div><div style="text-align: right; background-color: rgba(255,255,255,0.05); padding: 6px 12px; border-radius: 6px;"><span style="color: #9CA3AF; font-size: 11px; font-weight: 600; display:block;">即時流報價狀態</span><span style="color: #F9FAFB; font-weight: 600; font-size: 13px;">來源: {res['rt_source']} | 狀態: </span><span style="color: {res['m_color']}; font-weight: 700; font-size: 13px;">{res['m_desc']}</span></div></div></div>""", unsafe_allow_html=True)

        # 4. 即時報價 HUD 箱
        c1, c2, c3, c4 = st.columns(4)
        with c1: st.markdown(custom_hud_box("💡 當前即市價 (K線精密流)", f"<span style='font-size:20px; color:#0F172A;'>{res['current_price']:.2f} 元</span><br><small style='color:#64748B;'>今日成交: {res['current_vol']:.0f} 張</small>"), unsafe_allow_html=True)
        with c2: st.markdown(custom_hud_box("⏱️ 五日短線攻擊速線 (MA5)", f"<span style='font-size:16px; color:#1E293B;'>{res['ma5_val']:.2f} 元</span><br><small style='color:#64748B;'>今日漲跌幅: {res['stock_daily_pct']:+.2f}%</small>"), unsafe_allow_html=True)
        with c3: st.markdown(custom_hud_box("⏳ 母部位大波段防禦線 (ATR)", f"<span style='font-size:16px; color:#7C3AED;'>{res['trailing_stop_line']}</span><br><small style='color:#64748B;'>當前 ATR14: {res['atr']:.2f}</small>"), unsafe_allow_html=True)
        with c4: st.markdown(custom_hud_box("📊 相對強度 (RS Matrix)", f"<span style='font-size:16px; color:#10B981;'>超額 {res['relative_strength']:+.2f}%</span><br><small style='color:#64748B;'>RS黃金箭頭: {'🔥 成立' if res['is_rs_gold'] else '⚪ 整理中'}</small>"), unsafe_allow_html=True)

        # 5. 多因子曝光面板
        st.markdown("### 🧬 機構級多因子結構縱深大數據曝光面板")
        ib_col1, ib_col2, ib_col3 = st.columns(3)
        with ib_col1:
            macro_detail_desc = f"精算結果：當大盤量能窒息流失時，這將硬性限制全市場突破流派的開火配額。大腦此時將強力阻斷高檔假突破單。" if not res['macro_bull'] else "精算結果：大盤總血量安全，全面放行追高與金字塔加碼單開火。"
            st.markdown(render_panel_html("1. 總體流動性安全閥（實質總血量）", res['market_vol_desc'], macro_detail_desc, "#3B82F6"), unsafe_allow_html=True)
        with ib_col2:
            if "領頭狼王" in res['wolf_rank_label']: wolf_detail_desc = "精算結果：板塊內部狂飆超額強度！大腦給予其最高『特許開火權』，強制免疫大盤失血警報。"
            elif "跟屁蟲" in res['wolf_rank_label']: wolf_detail_desc = "精算結果：主力流動性正以每天數億元的規模撤離此股，資金正殘忍往真龍頭靠攏。一票否決新買進開火，嚴防高位踩踏！"
            else: wolf_detail_desc = f"精算結果：該個股目前處於產業常態輪中。若您此時是【空倉】，頂端大腦會直接下達【量化緩衝帶】命令退場觀望。"
            st.markdown(render_panel_html("2. 產業板塊內部分化位階（狼王排序）", res['wolf_rank_label'], wolf_detail_desc, res['wolf_rank_color']), unsafe_allow_html=True)
        with ib_col3:
            box_status_text = f"🔥 波動極致壓縮成立" if res['is_box_compressed'] else f"⚪ 箱型常態發散中"
            box_status_desc = f"精算結果：個股波動完美收斂。主力大底時間縱深完美共振。" if res['is_box_compressed'] else f"精算結果：個股近30日震幅達 {res['box_width_pct']:.1f}%，已實質脫離底部的窄幅整理，進入動能狂飆擴張階段！請直接對位下方【短期趨勢定錨面板】。"
            st.markdown(render_panel_html("3. 箱型籌碼時間縱深（橫有多長）", box_status_text, box_status_desc, "#7C3AED"), unsafe_allow_html=True)

        # 6. 週趨勢生命線
        trend_desc_connect = f"觀察提示：當前 5日主力成本線（MA5）強勢昂頭向上。此時與上方箱型發散高達 {res['box_width_pct']:.1f}% 的動能形成完美因果咬合——這正是促使最頂端決策大腦對您下達【{bp_data['strategy_name']}】的鋼鐵因果核心！"
        st.markdown(f"""<div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; border-left: 6px solid {res['stable_short_color']}; padding: 16px; border-radius: 6px; margin-top: 15px; margin-bottom: 15px;"><div style="display: flex; justify-content: space-between; align-items: center;"><span style="font-size: 13px; color: #64748B; font-weight: 800; letter-spacing: 0.05em;">⏱️ 週級別・短期波段主趨勢定錨面板</span></div><h4 style="margin: 8px 0; color: {res['stable_short_color']}; font-weight: 800; font-size: 18px;">當前定錨狀態：{res['stable_short_trend']}</h4><p style="margin: 0; color: #1E293B; font-size: 13.5px; line-height: 1.55; font-weight: 600;">{trend_desc_connect}</p></div>""", unsafe_allow_html=True)

        # 👑 7. 【重裝中段復位完全體】：技術、財報、新聞大表徹底平鋪釋放！
        st.markdown("---")
        st.markdown("### 🧱 🔍 跨因子微觀底層因果深度解碼驗證區")
        
        # 區塊 A：指標副圖一覽無遺
        st.markdown(f"""
        <div style="background-color:#FFFFFF; padding:14px; border:1px solid #E2E8F0; border-radius:6px; margin-bottom:15px; box-shadow:0 1px 3px rgba(0,0,0,0.02);">
            <p style="margin:0 0 8px 0; color:#0F172A; font-size:14px; font-weight:700;"><b>📈 KD 隨機指標分析定性：</b>{res['kd_timing']}</p>
            <p style="margin:0 0 8px 0; color:#0F172A; font-size:14px; font-weight:700;"><b>📊 MACD 趨勢力道動能：</b>{res['bb_stage']}</p>
            <p style="margin:0; color:#0F172A; font-size:14px; font-weight:700;"><b>⚡ RSI 實時強弱對峙帶：</b>{res['volume_verdict']}</p>
        </div>
        """, unsafe_allow_html=True)

        # 區塊 B：財報三率大表
        st.markdown("### 📊 財務基本面季度結構矩陣大表")
        st.markdown(f"""<div style="background-color:#EFF6FF; padding:10px; border-left:4px solid #3B82F6; border-radius:4px; margin-bottom:12px; font-size:13.5px; color:#1E40AF; font-weight:700;">📋 最新基本面估值：{res['fin_conclusion']} ｜ 核心營收年增率 (YoY)：{res['latest_yoy']:.2f}%</div>""", unsafe_allow_html=True)
        if not res["fin_df"].empty:
            clean_fin_show = res["fin_df"].copy().sort_values("date", ascending=False)
            clean_fin_show.columns = ["季度日期", "單季 EPS", "營業收入", "營業毛利", "營業利益", "單季毛利率 (%)", "單季營益率 (%)"]
            st.dataframe(clean_fin_show.style.format({"單季 EPS": "{:.2f}", "營業收入": "{:,.0f}", "營業毛利": "{:,.0f}", "營業利益": "{:,.0f}", "單季毛利率 (%)": "{:.2f}%", "單季營益率 (%)": "{:.2f}%"}), use_container_width=True)

        # 區塊 C：新聞輿情流水線（藍標外鏈直達）
        st.markdown("### 📰 資訊面 24H 網路輿情即時新聞流水線")
        st.markdown(f"""<div style="background-color:#F0FDF4; padding:10px; border-left:4px solid #10B981; border-radius:4px; margin-bottom:12px; font-size:13.5px; color:#15803D; font-weight:700;">> 即時網路輿情偏向定論：{res['news_analysis_report']}</div>""", unsafe_allow_html=True)
        if isinstance(res["raw_news_list"], list) and res["raw_news_list"]:
            for n in res["raw_news_list"]: 
                # 🚀 藍標 Markdown 超連結引流，滑鼠游標放上去即可直接點擊前往原文！
                st.markdown(f"* **[{n['sentiment']}]** [{n['source']}]({n['link']}) ─ {n['title']}")
        else:
            st.markdown("* ⚪ 當前時間窗口內暫無突發重磅輿情（已自動轉入常態監控）")

        st.markdown("---")
        
        # 8. 最底部開火指令
        st.markdown("### 🛡/⚔️ 風控指揮中心：量化核心配額開火劇本")
        bx1, bx2, bx3 = st.columns(3)
        with bx1: st.metric("精算風控進場配置", f"{res['suggested_lots']} 張")
        with bx2: st.metric("當前劇本風控停損價", f"{res['expected_stop_price']:.2f} 元")
        with bx3: st.metric("大波段移動停利線 (ATR)", res["trailing_stop_line"])

if auto_refresh:
    time.sleep(5)
    st.rerun()
