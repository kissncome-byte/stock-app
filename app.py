import os, time, math, requests, certifi, pytz, urllib.parse
import pandas as pd
import numpy as np
import streamlit as st
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from FinMind.data import DataLoader

# ============ 1. Page Config ============
st.set_page_config(page_title="SOP v50 台股多週期趨勢、價量與拉回決策系統", layout="wide")

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
    if x is None or pd.isna(x) or t <= 0: return 0.0
    return round(x / t) * t

def floor_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t <= 0: return 0.0
    return math.floor((x + 1e-12) / t) * t

def ceil_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t <= 0: return 0.0
    return math.ceil((x - 1e-12) / t) * t

def log_error(area: str, exc: Exception):
    # 正式部署可改接 logging / Sentry；前台不暴露金鑰與完整堆疊。
    print(f"[{area}] {type(exc).__name__}: {exc}")

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
    session.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
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
    """回傳統一單位：成交量一律為張，並附前收與資料時間。"""
    hist_lots = hist_last_vol / 1000.0 if hist_last_vol > 0 else 0.0
    session = get_requests_session()
    is_otc = any(x in str(market_type).upper() for x in ["OTC", "TWO", "櫃", "上櫃"])
    fallback = {"open": hist_last_close, "high": hist_last_close, "low": hist_last_close,
                "close": hist_last_close, "volume_lots": hist_lots, "previous_close": hist_last_close,
                "success": False, "source": "歷史收盤備援", "quote_time": None, "is_stale": True}
    if FUGLE_TOKEN:
        try:
            r = session.get(f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{stock_id}", headers={"X-API-KEY": FUGLE_TOKEN}, timeout=3)
            if r.status_code == 200:
                data = r.json().get("data", r.json())
                price = safe_float(data.get("closePrice")) or safe_float(data.get("referencePrice"))
                prev = safe_float(data.get("previousClose")) or safe_float(data.get("referencePrice")) or hist_last_close
                vol_shares = safe_float(data.get("total", {}).get("tradeVolume", 0))
                quote_time = data.get("lastUpdated") or data.get("closeTime") or data.get("date")
                if price > 0:
                    return {"open": safe_float(data.get("openPrice")) or price, "high": safe_float(data.get("highPrice")) or price,
                            "low": safe_float(data.get("lowPrice")) or price, "close": price,
                            "volume_lots": vol_shares / 1000.0 if vol_shares > 0 else hist_lots,
                            "previous_close": prev, "success": True, "source": "Fugle 即時行情",
                            "quote_time": quote_time, "is_stale": False}
        except Exception as exc:
            log_error("Fugle quote", exc)
    for prefix in (["otc", "tse"] if is_otc else ["tse", "otc"]):
        try:
            r = session.get(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_id}.tw&json=1&delay=0&_={int(time.time()*1000)}", headers={"Referer": "https://mis.twse.com.tw/"}, timeout=3)
            payload = r.json() if r.status_code == 200 else {}
            if payload.get("msgArray"):
                info = payload["msgArray"][0]
                price = safe_float(info.get("z")) or safe_float(str(info.get("b", "")).split("_")[0]) or safe_float(info.get("o"))
                # TWSE MIS 的 v 為累計成交量，實務上通常以張呈現；不再使用語意不明的 g 欄位。
                vol_lots = safe_float(info.get("v")) or hist_lots
                prev = safe_float(info.get("y")) or hist_last_close
                if price > 0:
                    return {"open": safe_float(info.get("o")) or price, "high": safe_float(info.get("h")) or price,
                            "low": safe_float(info.get("l")) or price, "close": price, "volume_lots": vol_lots,
                            "previous_close": prev, "success": True, "source": f"TWSE {prefix.upper()} 即時行情",
                            "quote_time": info.get("t") or info.get("d"), "is_stale": False}
        except Exception as exc:
            log_error("TWSE quote", exc)
    return fallback

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
    return pd.DataFrame([{"stock_id": "3037", "stock_name": "欣興", "market_type": "twse", "industry_category": "電子零組件業"}, {"stock_id": "2330", "stock_name": "台積電", "market_type": "twse", "industry_category": "半導體業"}, {"stock_id": "2382", "stock_name": "廣達", "market_type": "twse", "industry_category": "電腦及週邊設備業"}])

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
    except Exception as exc:
        log_error("market macro", exc)
    return None, "⚪ 大盤資料取得失敗", None, None, None, "⚪ 大盤量能資料不足"

# 🌟 誠實分流修正：抓不到就直接回報 None，絕不用 0.00% 呼弄交易員 🌟
@st.cache_data(ttl=43200)
def get_weekly_large_holders(stock_id: str):
    try:
        start_date = (datetime.now(TZ) - timedelta(days=90)).strftime("%Y-%m-%d")
        df_holder = get_api().taiwan_stock_holding_shares_per(stock_id=stock_id, start_date=start_date)
        if df_holder is not None and not df_holder.empty:
            df_1000 = df_holder[df_holder["Difference"] == "1,000,000以上"].sort_values("date")
            if len(df_1000) >= 2:
                latest_pct = safe_float(df_1000.iloc[-1]["Percent"])
                prev_pct = safe_float(df_1000.iloc[-2]["Percent"])
                diff_pct = latest_pct - prev_pct
                trend = "📈 千張以上持股級距占比增加" if diff_pct > 0.2 else "📉 千張以上持股級距占比下降" if diff_pct < -0.2 else "⚖️ 千張以上持股級距占比變化不大"
                return trend, diff_pct, latest_pct
    except Exception: pass
    return None, None, None

@st.cache_data(ttl=900)
def get_taiwan_enhanced_chips(stock_id: str, avg_daily_volume_shares: float, days: int = 30):
    s_trend, m_trend, s_3d, m_diff = "⚪ 資料不足", "⚪ 資料不足", 0.0, 0.0
    start = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    base = max(float(avg_daily_volume_shares or 0), 1.0)
    try:
        idf = get_api().taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start)
        if idf is not None and not idf.empty:
            sdf = idf[idf['name'] == 'Investment_Trust'].copy()
            if not sdf.empty:
                sdf['net'] = pd.to_numeric(sdf['buy'], errors='coerce').fillna(0) - pd.to_numeric(sdf['sell'], errors='coerce').fillna(0)
                s_3d = float(sdf.sort_values('date').tail(3)['net'].sum())
                intensity = s_3d / base
                s_trend = "🟢 投信近三日明顯偏買" if intensity >= 0.15 else "🔴 投信近三日明顯偏賣" if intensity <= -0.15 else "🟡 投信動向中性"
    except Exception as exc:
        log_error("investment trust", exc)
    try:
        mdf = get_api().taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start)
        if mdf is not None and len(mdf) >= 5:
            mdf = mdf.sort_values("date")
            bal = pd.to_numeric(mdf['MarginPurchaseTodayBalance'], errors='coerce')
            m_diff = float(bal.iloc[-1] - bal.iloc[-5])
            intensity = (m_diff * 1000.0) / base
            m_trend = "🟠 融資增加偏快" if intensity >= 0.30 else "🟢 融資明顯下降" if intensity <= -0.30 else "🟡 融資變化平穩"
    except Exception as exc:
        log_error("margin", exc)
    return s_trend, m_trend, s_3d, m_diff

@st.cache_data(ttl=900)
def get_institutional_trading_df(stock_id: str, days: int = 30):
    try:
        start_date = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        df = get_api().taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
        if df is not None and not df.empty:
            df = df.copy()
            df['buy'] = pd.to_numeric(df['buy'], errors='coerce').fillna(0)
            df['sell'] = pd.to_numeric(df['sell'], errors='coerce').fillna(0)
            df['net'] = (df['buy'] - df['sell']) / 1000.0
            name_map = {"Foreign_Investor": "外資(張)", "Investment_Trust": "投信(張)", "Dealer": "自營商總計(張)"}
            df['name'] = df['name'].map(name_map).fillna(df['name'])
            pdf = df.pivot_table(index="date", columns="name", values="net", aggfunc="sum").reset_index()
            cols = ["date", "外資(張)", "投信(張)", "自營商總計(張)"]
            return pdf[[c for c in cols if c in pdf.columns]].sort_values("date", ascending=False).reset_index(drop=True)
    except Exception: pass
    return pd.DataFrame()

@st.cache_data(ttl=3600)
def get_industry_peer_candidates(stock_id: str, industry_category: str, max_peers: int = 8):
    """由完整上市櫃清單動態建立同業池，適用所有有產業分類的股票。"""
    info = get_stock_info_df().copy()
    if info.empty or "industry_category" not in info.columns:
        return []
    info["stock_id"] = info["stock_id"].astype(str)
    peers = info[(info["industry_category"].astype(str) == str(industry_category)) & info["stock_id"].str.match(r"^\d{4,6}$")].copy()
    if peers.empty:
        return []
    # 固定排序確保快取結果穩定；目標股必定納入，其餘最多 max_peers-1 檔。
    peers = peers.sort_values("stock_id")
    target = peers[peers["stock_id"] == str(stock_id)]
    others = peers[peers["stock_id"] != str(stock_id)].head(max_peers - 1)
    return pd.concat([target, others], ignore_index=True).to_dict("records")

def analyze_peer_resonance(stock_id: str, industry_category: str):
    candidates = get_industry_peer_candidates(stock_id, industry_category, max_peers=8)
    if len(candidates) < 2:
        return "⚪ 此產業目前可取得的同業資料不足，暫不判斷共振。", None, 0
    returns = {}
    names = {}
    for row in candidates:
        pid = str(row.get("stock_id", ""))
        market = str(row.get("type") or row.get("market_type") or row.get("market") or "TSE")
        pdf = get_daily_df(pid, market_type=market, days=100)
        if pdf is not None and len(pdf) >= 45:
            close = pd.to_numeric(pdf.set_index("date")["close"], errors="coerce")
            returns[pid] = close.pct_change().dropna().tail(60)
            names[pid] = str(row.get("stock_name", pid))
    if stock_id not in returns or len(returns) < 2:
        return "⚪ 同業行情資料不足，暫不判斷共振。", None, len(returns)
    try:
        corr = pd.DataFrame(returns).corr(min_periods=30)
        mine = corr[stock_id].drop(stock_id).dropna()
        if mine.empty:
            return "⚪ 同業共同交易日不足，暫不判斷共振。", None, len(returns)
        strongest = mine.idxmax()
        val = float(mine.max())
        label = "同向明顯" if val >= 0.6 else "中度同向" if val >= 0.3 else "走勢分化"
        return f"🔗 近60日報酬率與 {names.get(strongest, strongest)}（{strongest}）{label}，相關係數 {val:.2f}；共比較 {len(returns)} 檔同產業股票。", val, len(returns)
    except Exception as exc:
        log_error("peer correlation", exc)
        return "⚪ 同業相關性計算失敗，暫不判斷。", None, len(returns)

# 🌟 實時分析師登記數據聯網模組 🌟
@st.cache_data(ttl=1800)
def get_broker_consensus_data(stock_id: str, current_price: float):
    session = get_requests_session()
    suffix = ".TWO" if (stock_id.startswith(("3","5","6","8")) and len(stock_id)==4) else ".TW"
    symbol = f"{stock_id}{suffix}"
    
    # 🌟 查無資料時的鋼鐵留白：前台直接反映無外資報告 Facts 🌟
    res_not_found = {
        "mean": None, "high": None, "low": None, "is_real": False,
        "list": []
    }
    
    try:
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=financialData"
        r = session.get(url, timeout=5)
        if r.status_code == 200:
            result = r.json().get("quoteSummary", {}).get("result")
            if result:
                fin_data = result[0].get("financialData", {})
                t_mean = safe_float(fin_data.get("targetMeanPrice", {}).get("raw"))
                t_high = safe_float(fin_data.get("targetHighPrice", {}).get("raw"))
                t_low = safe_float(fin_data.get("targetLowPrice", {}).get("raw"))
                rec_key = str(fin_data.get("recommendationKey", "N/A")).upper()
                
                if t_mean > 0:
                    rating_map = {"BUY": "🟢 建議買進", "STRONG_BUY": "👑 強烈加碼", "HOLD": "🟡 持有/中性", "SELL": "🔴 減碼/賣出"}
                    final_rating = rating_map.get(rec_key, "🟢 買進/加碼")
                    return {
                        "mean": t_mean, "high": t_high if t_high > 0 else t_mean, "low": t_low if t_low > 0 else t_mean, "is_real": True,
                        "list": [
                            {"firm": "Yahoo Finance 彙整分析師平均目標價", "rating": final_rating, "target": t_mean, "date": "資料彙整值"},
                            {"firm": "Yahoo Finance 彙整最高目標價", "rating": "🚀 多頭擴張", "target": t_high if t_high > 0 else t_mean, "date": "資料彙整值"},
                            {"firm": "Yahoo Finance 彙整最低目標價", "rating": "🛡️ 價值定錨", "target": t_low if t_low > 0 else t_mean, "date": "資料彙整值"}
                        ]
                    }
    except Exception: pass
    return res_not_found

def calculate_dynamic_pb(current_price: float, fin_df: pd.DataFrame):
    if fin_df.empty or "Equity" not in fin_df.columns or "ShareCapital" not in fin_df.columns:
        return None, None
    try:
        latest_eq = safe_float(fin_df.iloc[0]["Equity"])
        latest_cap = safe_float(fin_df.iloc[0]["ShareCapital"])
        if latest_cap > 0:
            bvps = latest_eq / (latest_cap / 10)
            current_pb = current_price / bvps
            return current_pb, bvps
    except Exception as exc:
        log_error("PB calculation", exc)
    return None, None

@st.cache_data(ttl=900)
def get_rev_df(stock_id: str, days: int = 730):
    try: return get_api().taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"))
    except Exception: return None

@st.cache_data(ttl=86400)
def get_financial_statement_df(stock_id: str, years: int = 2):
    try:
        raw = get_api().taiwan_stock_financial_statement(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=years*365)).strftime("%Y-%m-%d"))
        if raw is None or raw.empty: return pd.DataFrame()
        df = raw.copy()
        df["type"] = df["type"].replace({"OperatingRevenue": "Revenue"})
        target_types = ["EPS", "Revenue", "GrossProfit", "OperatingIncome", "Equity", "ShareCapital"]
        return df[df["type"].isin(target_types)].pivot_table(index="date", columns="type", values="value", aggfunc="last").reset_index()
    except Exception: return pd.DataFrame()

@st.cache_data(ttl=300)
def get_realtime_news_list(stock_id: str, stock_name: str):
    news = []
    for tf in ["when:1d", "when:7d", ""]:
        try:
            q = urllib.parse.quote(f"{str(stock_name)} {str(stock_id)} {tf}".strip())
            r = get_requests_session().get(f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", timeout=5)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall('.//item'):
                    t = item.find('title').text or ""
                    if " - " in t: t = t.rsplit(" - ", 1)[0]
                    news.append({"date": item.find('pubDate').text or "", "title": t, "source": item.find('source').text if item.find('source') is not None else "財經", "link": item.find('link').text or ""})
                if news: break
        except Exception: pass
    if news:
        df = pd.DataFrame(news)
        df["parsed_date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_convert('Asia/Taipei')
        df["date"] = df["parsed_date"].dt.strftime('%m-%d %H:%M')
        return df.sort_values(by="parsed_date", ascending=False)[["date", "title", "source", "link"]].to_dict('records')
    return []

def prepare_indicator_df(df: pd.DataFrame):
    """建立日線技術、價量、趨勢強度與結構欄位。"""
    if df is None or df.empty: return None
    x = df.copy().sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "vol"]:
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x.dropna(subset=["high", "low", "close", "vol"])
    c_prev = x["close"].shift(1)
    x["TR"] = np.maximum(x["high"] - x["low"], np.maximum((x["high"] - c_prev).abs(), (x["low"] - c_prev).abs()))
    x["ATR14"] = x["TR"].ewm(alpha=1/14, adjust=False).mean()
    for n in [5, 10, 20, 60, 120, 240]:
        x[f"MA{n}"] = x["close"].rolling(n).mean()
    x["MA5_Vol"], x["MA20_Vol"], x["MA60_Vol"] = x["vol"].rolling(5).mean(), x["vol"].rolling(20).mean(), x["vol"].rolling(60).mean()
    x["Res_20D"] = x["high"].shift(1).rolling(20).max()
    x["Res_60D"] = x["high"].shift(1).rolling(60).max()
    x["Sup_20D"] = x["low"].shift(1).rolling(20).min()
    x["Sup_60D"] = x["low"].shift(1).rolling(60).min()
    x["std20"] = x["close"].rolling(20).std()
    delta = x["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    x["RSI14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    ema12, ema26 = x["close"].ewm(span=12, adjust=False).mean(), x["close"].ewm(span=26, adjust=False).mean()
    x["MACD"], x["MACD_SIGNAL"] = ema12 - ema26, (ema12 - ema26).ewm(span=9, adjust=False).mean()
    x["MACD_HIST"] = x["MACD"] - x["MACD_SIGNAL"]
    l_min, h_max = x["low"].rolling(9).min(), x["high"].rolling(9).max()
    x["RSV"] = 100 * ((x["close"] - l_min) / (h_max - l_min).replace(0, np.nan))
    k_l, d_l, ck, cd = [], [], 50.0, 50.0
    for rsv in x["RSV"]:
        if pd.isna(rsv): k_l.append(np.nan); d_l.append(np.nan)
        else:
            ck = (2/3)*ck + (1/3)*rsv; cd = (2/3)*cd + (1/3)*ck
            k_l.append(ck); d_l.append(cd)
    x["K9"], x["D9"] = k_l, d_l

    # ADX：判斷有沒有趨勢，而非只判斷方向。
    up_move, down_move = x["high"].diff(), -x["low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=x.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=x.index)
    atr_wilder = x["TR"].ewm(alpha=1/14, adjust=False).mean().replace(0, np.nan)
    x["PLUS_DI"] = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_wilder
    x["MINUS_DI"] = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_wilder
    dx = 100 * (x["PLUS_DI"] - x["MINUS_DI"]).abs() / (x["PLUS_DI"] + x["MINUS_DI"]).replace(0, np.nan)
    x["ADX14"] = dx.ewm(alpha=1/14, adjust=False).mean()

    # 價量：OBV、CMF、上漲日量/下跌日量、換手代理與量價背離。
    direction = np.sign(x["close"].diff()).fillna(0)
    x["OBV"] = (direction * x["vol"]).cumsum()
    x["OBV_MA20"] = x["OBV"].rolling(20).mean()
    mfm = ((x["close"] - x["low"]) - (x["high"] - x["close"])) / (x["high"] - x["low"]).replace(0, np.nan)
    x["CMF20"] = (mfm.fillna(0) * x["vol"]).rolling(20).sum() / x["vol"].rolling(20).sum().replace(0, np.nan)
    x["UP_VOL20"] = x["vol"].where(x["close"] > c_prev, 0).rolling(20).sum()
    x["DOWN_VOL20"] = x["vol"].where(x["close"] < c_prev, 0).rolling(20).sum()
    x["VOL_RATIO20"] = x["vol"] / x["MA20_Vol"].replace(0, np.nan)
    x["RET_5D"] = x["close"].pct_change(5) * 100
    x["RET_20D"] = x["close"].pct_change(20) * 100
    for n in [20, 60, 120]:
        x[f"MA{n}_SLOPE"] = (x[f"MA{n}"] / x[f"MA{n}"].shift(5) - 1) * 100
    x["PRICE_HIGH_20"] = x["close"] >= x["close"].rolling(20).max().shift(1)
    x["OBV_HIGH_20"] = x["OBV"] >= x["OBV"].rolling(20).max().shift(1)
    x["BEARISH_VOL_DIVERGENCE"] = x["PRICE_HIGH_20"] & (~x["OBV_HIGH_20"])
    return x.dropna(subset=["ATR14", "MA20", "MA60", "Res_20D", "RSI14", "K9", "D9", "ADX14"]).copy()

def build_weekly_indicators(df_raw: pd.DataFrame):
    """將日線轉為週線，降低單日雜訊。"""
    if df_raw is None or df_raw.empty: return None
    w = df_raw.copy()
    w["date"] = pd.to_datetime(w["date"], errors="coerce")
    w = w.dropna(subset=["date"]).set_index("date").sort_index()
    weekly = w.resample("W-FRI").agg({"open":"first", "high":"max", "low":"min", "close":"last", "vol":"sum"}).dropna(subset=["close"]).reset_index()
    if len(weekly) < 30: return None
    weekly["MA10W"] = weekly["close"].rolling(10).mean()
    weekly["MA20W"] = weekly["close"].rolling(20).mean()
    weekly["MA40W"] = weekly["close"].rolling(40).mean()
    weekly["MA20W_SLOPE"] = (weekly["MA20W"] / weekly["MA20W"].shift(3) - 1) * 100
    return weekly

def detect_swing_structure(df: pd.DataFrame, window: int = 3):
    """以局部高低點辨識 HH/HL、LH/LL，避免只看均線。"""
    if df is None or len(df) < 25:
        return {"label":"資料不足", "higher_high":False, "higher_low":False, "last_swing_high":None, "last_swing_low":None}
    highs, lows = [], []
    for i in range(window, len(df)-window):
        if df["high"].iloc[i] >= df["high"].iloc[i-window:i+window+1].max(): highs.append((i, float(df["high"].iloc[i])))
        if df["low"].iloc[i] <= df["low"].iloc[i-window:i+window+1].min(): lows.append((i, float(df["low"].iloc[i])))
    hh = len(highs)>=2 and highs[-1][1] > highs[-2][1]
    hl = len(lows)>=2 and lows[-1][1] > lows[-2][1]
    lh = len(highs)>=2 and highs[-1][1] < highs[-2][1]
    ll = len(lows)>=2 and lows[-1][1] < lows[-2][1]
    label = "高點墊高、低點墊高" if hh and hl else "高點降低、低點降低" if lh and ll else "結構整理中"
    return {"label":label, "higher_high":hh, "higher_low":hl, "lower_high":lh, "lower_low":ll,
            "last_swing_high":highs[-1][1] if highs else None, "last_swing_low":lows[-1][1] if lows else None}

def classify_trend_and_models(df: pd.DataFrame, weekly: pd.DataFrame, current_price: float, current_vol_shares: float):
    last = df.iloc[-1]
    structure = detect_swing_structure(df.tail(150).reset_index(drop=True))
    ma10, ma20, ma60 = map(float, [last.get("MA10", np.nan), last["MA20"], last["MA60"]])
    ma120, ma240 = safe_float(last.get("MA120"), np.nan), safe_float(last.get("MA240"), np.nan)
    slope20, slope60, slope120 = safe_float(last.get("MA20_SLOPE")), safe_float(last.get("MA60_SLOPE")), safe_float(last.get("MA120_SLOPE"))
    adx, plus_di, minus_di = safe_float(last.get("ADX14")), safe_float(last.get("PLUS_DI")), safe_float(last.get("MINUS_DI"))
    atr, vol_ma20 = safe_float(last.get("ATR14"),1), safe_float(last.get("MA20_Vol"),1)
    peak60 = float(df["high"].tail(60).max())
    drawdown = (current_price/peak60-1)*100 if peak60>0 else 0
    volume_ratio = current_vol_shares/vol_ma20 if vol_ma20>0 else 0
    pullback_volume_ratio = float(df["vol"].tail(5).mean()/vol_ma20) if vol_ma20>0 else 0
    weekly_ok = False
    weekly_desc = "週線資料不足"
    if weekly is not None and not weekly.empty:
        wl=weekly.iloc[-1]
        weekly_ok = safe_float(wl["close"]) >= safe_float(wl["MA20W"]) and safe_float(wl["MA20W_SLOPE"]) > 0
        weekly_desc = "週線維持多頭" if weekly_ok else "週線尚未確認多頭"
    long_bull = weekly_ok and current_price >= ma60 and slope60>0 and (pd.isna(ma120) or ma60>=ma120 or slope120>=0)
    long_bear = current_price < ma60 and slope60<0 and (structure.get("lower_low") or minus_di>plus_di)
    long_label = "長期多頭" if long_bull else "長期空頭" if long_bear else "長期整理／轉折"
    medium_bull = ma20>=ma60 and slope20>0 and current_price>=ma60
    medium_label = "主升段" if medium_bull and current_price>=ma20 and adx>=25 and plus_di>minus_di else "多頭正常拉回" if long_bull and current_price<ma20 and current_price>=ma60 and drawdown>=-15 else "高檔整理" if long_bull and abs(slope20)<1 else "築底" if not long_bear and slope20>=0 and structure.get("higher_low") else "反彈" if current_price>=ma20 and not long_bull else "下跌段" if long_bear else "區間整理"
    short_label = "短線轉強" if current_price>=ma10 and safe_float(last.get("K9"))>safe_float(last.get("D9")) else "短線拉回" if long_bull and current_price<ma10 else "短線偏弱"
    trend_strength = "強趨勢" if adx>=25 else "趨勢形成中" if adx>=18 else "震盪為主"

    real_res20, real_res60 = safe_float(last["Res_20D"]), safe_float(last["Res_60D"])
    prior_breakout = float(df["close"].iloc[-21:-1].max()) >= float(df["Res_20D"].iloc[-21:-1].max()) if len(df)>25 else False
    breakout = current_price>=real_res20 and volume_ratio>=1.3 and medium_bull
    retest = prior_breakout and abs(current_price-real_res20)/max(real_res20,0.01)<=0.035 and pullback_volume_ratio<=0.9 and current_price>=ma20*0.98
    pullback = long_bull and medium_bull and -15<=drawdown<=-3 and current_price>=ma60 and pullback_volume_ratio<=0.9 and not structure.get("lower_low")
    base_turn = not long_bear and slope20>=0 and structure.get("higher_low") and current_price>=real_res20 and volume_ratio>=1.2
    stop_candle = (float(last["close"])>float(last["open"]) and float(last["close"])>=float(last["low"])+0.6*(float(last["high"])-float(last["low"]))) or (safe_float(last.get("K9"))>safe_float(last.get("D9")) and safe_float(df["K9"].iloc[-2])<=safe_float(df["D9"].iloc[-2]))
    model = "突破進場" if breakout else "突破後回測" if retest else "多頭拉回" if pullback else "築底轉強" if base_turn else "等待"
    model_ready = breakout or (retest and stop_candle) or (pullback and stop_candle) or base_turn

    upv, dnv = safe_float(last.get("UP_VOL20")), safe_float(last.get("DOWN_VOL20"))
    cmf, obv = safe_float(last.get("CMF20")), safe_float(last.get("OBV")); obvma=safe_float(last.get("OBV_MA20"))
    if current_price>=ma20 and volume_ratio>=1.3: price_volume="價漲量增，買盤積極"
    elif current_price<ma20 and pullback_volume_ratio<=0.9 and long_bull: price_volume="價跌量縮，較像多頭拉回"
    elif current_price<ma20 and volume_ratio>=1.3: price_volume="價跌量增，賣壓需警戒"
    elif current_price>=ma20 and volume_ratio<0.8: price_volume="價漲量縮，追價力道不足"
    else: price_volume="價量關係中性"
    accumulation = "資金偏累積" if cmf>0.05 and obv>=obvma and upv>=dnv else "資金偏流出" if cmf<-0.05 and obv<obvma and dnv>upv else "資金平衡"
    divergence = "出現價格創高但OBV未創高的量價背離" if bool(last.get("BEARISH_VOL_DIVERGENCE",False)) else "未見明顯空方量價背離"
    return {"long_term":long_label, "medium_term":medium_label, "short_term":short_label, "weekly_desc":weekly_desc,
            "trend_strength":trend_strength, "adx":adx, "structure":structure, "drawdown_pct":drawdown,
            "volume_ratio":volume_ratio, "pullback_volume_ratio":pullback_volume_ratio, "price_volume":price_volume,
            "accumulation":accumulation, "volume_divergence":divergence, "entry_model":model, "entry_ready":model_ready,
            "breakout_model":breakout, "pullback_model":pullback, "retest_model":retest, "base_model":base_turn,
            "stop_candle":stop_candle, "ma10":ma10, "ma120":ma120, "ma240":ma240,
            "slope20":slope20, "slope60":slope60, "slope120":slope120}

def resolve_trend_state(stock_id: str, analysis: dict, current_price: float, structure_stop: float, ma20: float, ma60: float, volume_ratio: float):
    """狀態機有遲滯：單日跌破短均線不直接翻空。"""
    key=f"trend_state_{stock_id}"
    prev=st.session_state.get(key, {"state":"觀察", "weak_days":0, "break_days":0})
    state=prev["state"]; weak_days=int(prev.get("weak_days",0)); break_days=int(prev.get("break_days",0))
    structural_break = current_price < structure_stop and current_price < ma60
    warning = current_price < ma20 and (analysis["slope20"]<0 or volume_ratio>=1.3)
    if structural_break:
        break_days += 1
    else: break_days=0
    if warning: weak_days+=1
    else: weak_days=max(0,weak_days-1)
    if analysis["long_term"]=="長期空頭": state="空頭"
    elif break_days>=2 or (structural_break and volume_ratio>=1.5): state="趨勢破壞"
    elif weak_days>=2: state="多頭轉弱警戒"
    elif analysis["medium_term"]=="多頭正常拉回": state="多頭正常拉回"
    elif analysis["entry_model"]=="突破進場" and analysis["entry_ready"]: state="突破確認"
    elif analysis["medium_term"]=="主升段": state="多頭持有"
    elif analysis["entry_model"]=="築底轉強": state="趨勢轉強"
    elif analysis["medium_term"]=="築底": state="築底"
    else: state="觀察"
    reason = f"長期={analysis['long_term']}；中期={analysis['medium_term']}；短期={analysis['short_term']}；量比={volume_ratio:.2f}；結構停損={structure_stop:.2f}"
    now={"state":state,"weak_days":weak_days,"break_days":break_days,"reason":reason}
    if prev.get("state") != state:
        log_key=f"trend_log_{stock_id}"
        logs=st.session_state.get(log_key, [])
        logs.append({"時間":datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), "原狀態":prev.get("state","觀察"), "新狀態":state, "原因":reason})
        st.session_state[log_key]=logs[-30:]
    st.session_state[key]=now
    return now

def unified_institutional_brain(res_dict, df_hist, is_holding=False, entry_cost=0.0, sector_panic=False):
    p=res_dict["current_price"]; q=res_dict.get("data_quality_score",0); state=res_dict.get("trend_state","觀察")
    ta=res_dict.get("trend_analysis",{}); chip=f"投信：{res_dict.get('sitc_trend')}；融資：{res_dict.get('margin_trend')}。"
    structure_stop=res_dict.get("structure_stop",res_dict["stop_brk"])
    if q<60 or res_dict.get("macro_bull") is None:
        return {"strategy_name":"⚪ 資料不足","color":"#64748B","action_now":"只觀察，不產生方向","signal":"關鍵資料未完整","blueprint":{"停損防守":"待資料恢復","移動停利":"不適用","預期目標":"不提供"},"desc":f"資料完整度 {q:.0f}%，不足以形成可靠方向。"}
    if sector_panic:
        return {"strategy_name":"🟠 族群風險升高","color":"#F59E0B","action_now":"暫停新增部位","signal":"同產業集體轉弱","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":"縮小風險","預期目標":"待族群止穩"},"desc":"族群同步下跌時，個股拉回較可能演變成趨勢破壞。"}
    if is_holding and entry_cost>0:
        if state in ["趨勢破壞","空頭"]:
            return {"strategy_name":"🔴 波段結構已破壞","color":"#EF4444","action_now":"依計畫減碼或退出","signal":"結構低點與中期趨勢同時失守","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":"已觸發","預期目標":"先控制風險"},"desc":"不是因為單日跌破5日線，而是波段低點、60日線或放量賣壓已共同惡化。"}
        if state=="多頭轉弱警戒":
            return {"strategy_name":"🟠 多頭轉弱警戒","color":"#F59E0B","action_now":"續抱觀察，必要時分批減碼","signal":"連續出現中期弱化條件","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":f"ATR線 {res_dict['trailing_stop_value']:.2f} 元","預期目標":"等待重新站回MA20"},"desc":"尚未直接判定空頭，但弱化已非單日雜訊。"}
        if state=="多頭正常拉回":
            return {"strategy_name":"🟢 多頭趨勢正常拉回","color":"#10B981","action_now":"續抱，不因短線跌破而殺出","signal":"週線與中長期結構仍完整，拉回量縮","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":f"ATR線 {res_dict['trailing_stop_value']:.2f} 元","預期目標":f"前高 {res_dict['real_resistance']:.2f} 元"},"desc":f"目前損益 {res_dict['pnl_pct']:+.1f}%。短線降溫不等於趨勢反轉；{chip}"}
        return {"strategy_name":"🟢 趨勢持有","color":"#10B981","action_now":"續抱並上移防守","signal":f"狀態：{state}","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":f"ATR線 {res_dict['trailing_stop_value']:.2f} 元","預期目標":f"情境價 {res_dict['target_brk']:.2f} 元"},"desc":f"趨勢尚未被結構性破壞。{chip}"}
    model=ta.get("entry_model","等待")
    if model=="多頭拉回":
        action="可小額分批" if ta.get("entry_ready") else "等待止跌確認"
        return {"strategy_name":"🟢 多頭拉回機會","color":"#10B981","action_now":action,"signal":"長中期多頭、回檔量縮且未破前低","blueprint":{"停損防守":f"結構線 {structure_stop:.2f} 元","移動停利":"站回MA20後再上移","預期目標":f"前高 {res_dict['real_resistance']:.2f} 元"},"desc":"這是低價拉回模型，不必等到再創新高才追價；仍建議分批，而非一次買滿。"}
    if model=="突破後回測":
        return {"strategy_name":"🟢 突破後回測","color":"#10B981","action_now":"確認止跌後分批","signal":"原壓力轉支撐且回測量縮","blueprint":{"停損防守":f"突破失效線 {structure_stop:.2f} 元","移動停利":"續強後上移","預期目標":f"情境價 {res_dict['target_brk']:.2f} 元"},"desc":"通常比直接追突破有較好的風險報酬。"}
    if model=="突破進場":
        return {"strategy_name":"🟢 放量突破","color":"#10B981","action_now":"小額分批，不追過度乖離","signal":"價格與量能越過壓力","blueprint":{"停損防守":f"突破失效線 {structure_stop:.2f} 元","移動停利":"依結構與ATR上移","預期目標":f"情境價 {res_dict['target_brk']:.2f} 元"},"desc":"突破成立，但若距MA20過遠，應等待回測而不是追高。"}
    if model=="築底轉強":
        return {"strategy_name":"🟡 築底轉強","color":"#F59E0B","action_now":"僅適合小部位試單","signal":"低點墊高、均線走平後突破","blueprint":{"停損防守":f"底部結構線 {structure_stop:.2f} 元","移動停利":"待趨勢形成","預期目標":f"前壓 {res_dict['real_resistance']:.2f} 元"},"desc":"這是較積極的轉折模型，可靠度低於成熟多頭拉回。"}
    return {"strategy_name":"⚪ 等待更好位置","color":"#64748B","action_now":"不追價，等待拉回或確認","signal":"尚未符合四種進場模型","blueprint":{"停損防守":"未進場不設定","移動停利":"不適用","預期目標":"等待條件"},"desc":f"目前不必勉強交易。{chip}"}

# ============ 9. Main Core Executor ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int, is_holding=False, entry_cost=0.0, sector_panic=False):
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    pnl_pct = 0.0
    res_dict = {}
    latest_yoy = 0.0
    raw_news_list, fin_df, institutional_df = [], pd.DataFrame(), pd.DataFrame()
    spring_verdict, spring_triggered, detected_prior_low = "⚪ 未觸發破底翻結構", False, 0.0
    news_analysis_report = "⚖️ 新聞文字傾向僅供參考"
    fin_conclusion = "⚪ 財報資料不足，暫不判斷"
    pe_val, pb_ratio, bvps = None, None, None
    broker_consensus = {"mean": None, "high": None, "low": None, "is_real": False, "list": [], "error": None}
    
    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = "🟡 中性", "🟡 平穩", 0.0, 0.0
    wolf_rank_label, wolf_rank_color = "⚖️ 族群常態輪動成員", "#64748B"
    
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
    quote = compute_live_data(stock_id, market_type, float(hist_last_raw["close"]), float(hist_last_raw["vol"]))
    rt_open, rt_high, rt_low, rt_close = quote["open"], quote["high"], quote["low"], quote["close"]
    rt_vol_lots, rt_success, rt_source = quote["volume_lots"], quote["success"], quote["source"]
    previous_close = quote["previous_close"]
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
    
    ma5_val = float(hist_last["MA5"])
    ma20_val, ma60_val = float(hist_last["MA20"]), float(hist_last["MA60"])
    vol_ma20_val, real_resistance = float(hist_last["MA20_Vol"]), float(hist_last["Res_20D"])
    rsi_now, macd_hist, atr = safe_float(hist_last.get("RSI14", 50.0)), safe_float(hist_last.get("MACD_HIST", 0.0)), safe_float(hist_last.get("ATR14", 1.0))
    k9_now, d9_now = safe_float(hist_last.get("K9", 50.0)), safe_float(hist_last.get("D9", 50.0))
    weekly_df = build_weekly_indicators(df_for_indicators)
    trend_analysis = classify_trend_and_models(df, weekly_df, current_price, current_vol * 1000.0)
    swing = trend_analysis["structure"]
    structure_stop_raw = swing.get("last_swing_low") or float(hist_last.get("Sup_20D", current_price - 2*atr))
    structure_stop = floor_to_tick(min(structure_stop_raw, ma20_val - 0.5*atr) if trend_analysis["long_term"]=="長期多頭" else structure_stop_raw, t)
    trend_state_data = resolve_trend_state(stock_id, trend_analysis, current_price, structure_stop, ma20_val, ma60_val, trend_analysis["volume_ratio"])
    
    kd_status = "黃金交叉" if k9_now > d9_now else "死亡交叉"
    stock_daily_pct = ((current_price - previous_close) / previous_close) * 100 if previous_close > 0 else 0.0
    relative_strength = stock_daily_pct - wtx_change
    is_rs_gold = (wtx_change <= -1.0) and (relative_strength >= 3.0)

    large_holder_trend, large_holder_diff, large_holder_pct = get_weekly_large_holders(stock_id)
    peer_resonance_text, peer_corr_val, peer_count = analyze_peer_resonance(stock_id, industry)
    avg_daily_volume_shares = float(df["vol"].tail(20).mean())
    sitc_trend, margin_trend, sitc_3d_sum, margin_diff = get_taiwan_enhanced_chips(stock_id, avg_daily_volume_shares)
    
    try: institutional_df = get_institutional_trading_df(stock_id, days=30)
    except Exception: pass
    try:
        broker_consensus = get_broker_consensus_data(stock_id, current_price)
    except Exception as exc:
        broker_consensus["error"] = str(exc)
        log_error("broker consensus", exc)

    vol_spike = (current_vol * 1000.0) > (vol_ma20_val * 1.5)
    attempted_breakout = current_price >= real_resistance
    confirmed_breakout = attempted_breakout and vol_spike and datetime.now(TZ).time() >= datetime.strptime("13:25", "%H:%M").time()
    if relative_strength > 4.0 and sitc_3d_sum > 300: wolf_rank_label, wolf_rank_color = "👑 族群領頭狼王（主導資金絕對攻勢）", "#7D3CFF"
    elif relative_strength < -2.0: wolf_rank_label, wolf_rank_color = "🐌 族群落後跟屁蟲（嚴防資金棄養踩踏）", "#EF4444"
    else: wolf_rank_label, wolf_rank_color = "⚖️ 族群常態輪動成員（隨大盤溫和浮動）", "#64748B"

    box_width_pct = ((float(df["close"].tail(30).max()) - float(df["close"].tail(30).min())) / float(df["close"].tail(30).min())) * 100
    is_box_compressed = box_width_pct <= 8.5
    target_brk = floor_to_tick(current_price + (3.0 * atr), t)
    stop_candidate = min(real_resistance - (1.5 * atr), current_price - atr)
    stop_brk = floor_to_tick(stop_candidate, t)
    trailing_stop_value = floor_to_tick(peak_price_20d - (2.5 * atr), t)
    stop_line_text = f"{trailing_stop_value:.2f} 元"

    if k9_now < 20: kd_timing = "隨機指標進入 20 以下低檔區（超賣打底）。"
    elif k9_now > 70: kd_timing = "隨機指標在 70 以上高檔鈍化（超買強勢）。"
    else: kd_timing = f"KD 指標目前在 20~70 之間常態區洗盤 (K={k9_now:.1f} / D={d9_now:.1f})。"
    bb_stage = "多頭主導（MACD 柱狀體在零軸上方安全區）。" if macd_hist >= 0 else "空頭修正（MACD 柱狀體在零軸下方收縮）。"
    volume_verdict = f"{trend_analysis['price_volume']}；{trend_analysis['accumulation']}；{trend_analysis['volume_divergence']}。RSI14={rsi_now:.1f}，量比={trend_analysis['volume_ratio']:.2f}。"

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
        for n in raw_news_list: n["sentiment"], n["color"] = analyze_news_sentiment(n["title"])
        news_analysis_report = "利多消息主導市場輿情" if sum(1 for n in raw_news_list if "利多" in n["sentiment"]) > sum(1 for n in raw_news_list if "利空" in n["sentiment"]) else "市場網路輿情呈現中性平衡"

    if len(df) >= 40:
        low_cand = float(df.iloc[-40:-10]["low"].min())
        for r_idx, row in df.iloc[-10:].iterrows():
            if row["low"] < low_cand and df["close"].iloc[-1] > low_cand:
                spring_triggered = True; detected_prior_low = low_cand; break
    if spring_triggered: spring_verdict = f"🟢 成功收復前波低點 {detected_prior_low:.2f} 元，形成破底後收復型態；仍需後續量價確認。"

    fin_df_raw = get_financial_statement_df(stock_id, years=2)
    if not fin_df_raw.empty and "Revenue" in fin_df_raw.columns:
        fin_df_work = fin_df_raw.copy().sort_values("date").reset_index(drop=True)
        for f_idx in range(len(fin_df_work)):
            rev_amt = safe_float(fin_df_work.loc[f_idx, "Revenue"])
            fin_df_work.loc[f_idx, "gpm"] = (safe_float(fin_df_work.loc[f_idx, "GrossProfit"]) / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df_work.loc[f_idx, "opm"] = (safe_float(fin_df_work.loc[f_idx, "OperatingIncome"]) / rev_amt * 100) if rev_amt > 0 else 0.0
        
        fin_df = fin_df_work.sort_values("date", ascending=False).reset_index(drop=True)
        last_fin = fin_df.iloc[0]
        gpm_now, opm_now = safe_float(last_fin.get("gpm", 0.0)), safe_float(last_fin.get("opm", 0.0))
        # FinMind EPS 欄位須確認為單季值；此處僅在四筆皆有效時顯示參考 TTM。
        eps4 = pd.to_numeric(fin_df.head(4).get('EPS'), errors='coerce') if 'EPS' in fin_df.columns else pd.Series(dtype=float)
        sum_eps_4q = float(eps4.sum()) if len(eps4) == 4 and eps4.notna().all() else 0.0
        pe_val = current_price / sum_eps_4q if sum_eps_4q > 0 else 0.0
        if len(fin_df) >= 5:
            yoy_row = fin_df.iloc[4]
            fin_conclusion = "📈 最新季度毛利率高於約一年前同期" if gpm_now > safe_float(yoy_row.get("gpm", 0.0)) else "⚖️ 最新季度毛利率未高於約一年前同期"
        else:
            fin_conclusion = "⚪ 可比較季度不足，暫不做年同期判斷"
        
        try: pb_ratio, bvps = calculate_dynamic_pb(current_price, fin_df)
        except Exception: pass

    pnl_pct = ((current_price - entry_cost) / entry_cost * 100) if (is_holding and entry_cost > 0) else 0.0
    
    short_term_trend = f"{trend_analysis['short_term']}（KD：{kd_status}）"
    long_term_trend = f"{trend_analysis['long_term']}；{trend_analysis['weekly_desc']}；MA60五日斜率 {trend_analysis['slope60']:+.2f}%"
    trend_phase = f"{trend_analysis['medium_term']}｜{trend_analysis['trend_strength']}｜波段結構：{trend_analysis['structure']['label']}"

    # 打包
    res_dict["stock_id"] = stock_id
    res_dict["stock_name"] = stock_name
    res_dict["industry"] = industry
    res_dict["pnl_pct"] = pnl_pct
    res_dict["macro_bull"] = macro_bull
    res_dict["market_vol_desc"] = market_vol_desc
    res_dict["wolf_rank_label"] = wolf_rank_label
    res_dict["wolf_rank_color"] = wolf_rank_color
    res_dict["target_brk"] = target_brk
    res_dict["stop_brk"] = stop_brk
    res_dict["trailing_stop_line"] = stop_line_text
    res_dict["current_price"] = current_price
    res_dict["current_vol"] = current_vol
    res_dict["ma5_val"] = ma5_val
    res_dict["ma20_val"] = ma20_val
    res_dict["ma60_val"] = ma60_val
    res_dict["real_resistance"] = real_resistance
    res_dict["atr"] = atr
    res_dict["stock_daily_pct"] = stock_daily_pct
    res_dict["relative_strength"] = relative_strength
    res_dict["is_rs_gold"] = is_rs_gold
    res_dict["rt_source"] = rt_source
    res_dict["m_desc"] = macro_text
    res_dict["m_color"] = "gray" if macro_bull is None else ("red" if not macro_bull else "green")
    res_dict["fin_df"] = fin_df
    res_dict["spring_verdict"] = spring_verdict
    
    # 趨勢分析封裝
    res_dict["short_term_trend"] = short_term_trend
    res_dict["long_term_trend"] = long_term_trend
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
    res_dict["institutional_df"] = institutional_df
    res_dict["broker_consensus"] = broker_consensus
    res_dict["margin_trend"] = margin_trend
    res_dict["box_width_pct"] = box_width_pct
    res_dict["market_vol_healthy"] = market_vol_healthy
    res_dict["is_box_compressed"] = is_box_compressed
    res_dict["attempted_breakout"] = attempted_breakout
    res_dict["confirmed_breakout"] = confirmed_breakout
    res_dict["trailing_stop_value"] = trailing_stop_value
    
    res_dict["large_holder_trend"] = large_holder_trend
    res_dict["large_holder_diff"] = large_holder_diff
    res_dict["large_holder_pct"] = large_holder_pct
    res_dict["peer_resonance_text"] = peer_resonance_text
    res_dict["peer_corr_val"] = peer_corr_val
    res_dict["peer_count"] = peer_count
    res_dict["pb_ratio"] = pb_ratio
    res_dict["bvps"] = bvps
    res_dict["trend_analysis"] = trend_analysis
    res_dict["trend_state"] = trend_state_data["state"]
    res_dict["trend_state_detail"] = trend_state_data
    res_dict["structure_stop"] = structure_stop
    res_dict["weekly_df"] = weekly_df
    res_dict["ma10_val"] = trend_analysis["ma10"]
    res_dict["ma120_val"] = trend_analysis["ma120"]
    res_dict["ma240_val"] = trend_analysis["ma240"]

    quality_flags = {
        "價格": current_price > 0, "成交量": current_vol >= 0, "大盤": macro_bull is not None,
        "財報": not fin_df.empty, "法人": not institutional_df.empty, "大戶級距": large_holder_trend is not None,
        "同業": peer_corr_val is not None, "新聞": bool(raw_news_list)
    }
    quality_weights = {"價格": 25, "成交量": 10, "大盤": 15, "財報": 15, "法人": 10, "大戶級距": 5, "同業": 10, "新聞": 10}
    quality_score = sum(quality_weights[k] for k, ok in quality_flags.items() if ok)
    missing_data = [k for k, ok in quality_flags.items() if not ok]
    res_dict["data_quality_score"] = quality_score
    res_dict["missing_data"] = missing_data

    res_dict["tactical_blueprint"] = unified_institutional_brain(res_dict, df.copy(), is_holding=is_holding, entry_cost=entry_cost, sector_panic=sector_panic)
    
    slippage = slip_ticks * t
    estimated_entry = ceil_to_tick(current_price + slippage, t)
    estimated_stop_fill = floor_to_tick(structure_stop - slippage, t)
    # 粗估雙邊手續費與賣出證交稅；實際折扣及商品稅率仍依券商/商品而異。
    estimated_cost_per_share = estimated_entry * (0.001425 * 2 + 0.003)
    risk_per_share = max(estimated_entry - estimated_stop_fill + estimated_cost_per_share, 0)
    capital_ntd = total_capital * 10000
    risk_budget = capital_ntd * (risk_per_trade / 100)
    max_shares_by_risk = int(risk_budget / risk_per_share) if risk_per_share > 0 else 0
    max_shares_by_cash = int(capital_ntd / max(estimated_entry * 1.001425, 0.01))
    suggested_shares = max(0, min(max_shares_by_risk, max_shares_by_cash))
    suggested_lots = suggested_shares // 1000
    suggested_odd_lot = suggested_shares % 1000
    res_dict.update({"suggested_lots": suggested_lots, "suggested_odd_lot": suggested_odd_lot,
                     "suggested_shares": suggested_shares, "expected_entry_price": estimated_entry,
                     "expected_stop_price": estimated_stop_fill, "expected_target_price": target_brk,
                     "estimated_cost_per_share": estimated_cost_per_share})
    return res_dict

# ============ 10. UI Presentation Layer ============
with st.sidebar:
    st.header("🛡️ 全球資金池風控參數")
    capital = st.number_input("核心大資金池 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆最大核心風險承受 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守技術滑價 (Ticks)", 0, 5, 1)
    sector_panic_toggle = st.checkbox("🔥 同族群其他龍頭股「集體下殺破5%」", value=False)
    auto_refresh = st.checkbox("🔄 開啟盤中每 15 秒更新報價", value=False)

st.markdown("## 📡 台股多週期趨勢、價量與拉回決策系統（v50）")
st.caption("本工具整理公開資訊與技術指標，不保證獲利，也不取代個人投資判斷。盤中訊號需等待收盤確認。")
stock_input = st.text_input("請輸入核心目標個股代碼：", value="3037")

u_col1, u_col2 = st.columns(2)
with u_col1: user_holding = st.checkbox("📊 我手中「已持有」此個股", value=False)
with u_col2: user_cost = st.number_input("每股真實持股成本 (元)", value=0.0, step=1.0, min_value=0.0, disabled=not user_holding)

if stock_input:
    res = evaluate_stock(stock_input, capital, risk_pct, slip_input, is_holding=user_holding, entry_cost=user_cost, sector_panic=sector_panic_toggle)
    if res is None: st.error("該個股代碼數據獲取失敗。")
    else:
        bp_data = res["tactical_blueprint"]
        bp = bp_data["blueprint"]
        missing_text = "、".join(res["missing_data"]) if res["missing_data"] else "無"
        st.info(f"資料完整度：{res['data_quality_score']:.0f}%｜缺少：{missing_text}。資料不足的項目不納入方向判斷。")
        
        # 1. 綜合結論卡片
        st.markdown(f"""
        <div style="background-color: {bp_data['color']}10; border: 2px solid {bp_data['color']}; padding: 22px; border-radius: 8px; margin-bottom: 25px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <span style="color: {bp_data['color']}; font-size: 14px; font-weight: 900;">📢 決策標籤：{bp_data['strategy_name']}</span>
                <span style="background-color: {bp_data['color']}; color: white; padding: 4px 12px; border-radius: 4px; font-size: 13px; font-weight:800;">{bp_data['action_now']}</span>
            </div>
            <h3 style="margin: 5px 0; color: {bp_data['color']}; font-size: 23px; font-weight: 900;">即時策略防線：{bp_data['signal']}</h3>
            <div style="margin: 12px 0 18px 0; color: #0F172A; font-size: 15.5px; line-height: 1.65; text-align: justify; font-weight: 700; background-color: #FFFFFF; padding: 14px; border-radius: 6px; border: 2px solid #E2E8F0;">
                <span style="color: #0F172A; font-weight: 900;">⚡ 狼王核心實戰研判令：</span>{bp_data['desc']}
            </div>
            <div style="background-color: white; border: 1px solid #E2E8F0; padding: 15px; border-radius: 6px; margin-top: 10px;">
                <span style="color: #475569; font-size: 13px; font-weight: 800; display: block; margin-bottom: 8px;">🎯 技術出場計畫藍圖 [解算依據: 本地 K 線歷史波動率分布]</span>
                <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px;">
                    <div style="background-color: #FFF5F5; padding: 10px; border-radius: 4px; border-left: 3px solid #EF4444;"><small style="color: #DC2626; font-weight: 800;">🛑 1. 核心停損防線</small><p style="margin:3px 0 0 0; font-size:13px; font-weight:bold; color:#1E293B;">{bp['停損防守']}</p></div>
                    <div style="background-color: #FFFBEB; padding: 10px; border-radius: 4px; border-left: 3px solid #F59E0B;"><small style="color: #D97706; font-weight: 800;">⚠️ 2. 移動鎖利基準</small><p style="margin:3px 0 0 0; font-size:13px; font-weight:bold; color:#1E293B;">{bp['移動停利']}</p></div>
                    <div style="background-color: #F0FDF4; padding: 10px; border-radius: 4px; border-left: 3px solid #10B981;"><small style="color: #16A34A; font-weight: 800;">🚀 3. 波段預期目標</small><p style="margin:3px 0 0 0; font-size:13px; font-weight:bold; color:#1E293B;">{bp['預期目標']}</p></div>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # 2. 多週期趨勢、線型與價量診斷
        st.markdown("### ⏱️ 多週期趨勢與線型診斷")
        st.info(f"**【短期趨勢】**：{res['short_term_trend']}\n\n"
                f"**【長期趨勢】**：{res['long_term_trend']}\n\n"
                f"**【中期波段階段】**：{res['trend_phase']}\n\n"
                f"**【狀態機】**：{res['trend_state']}（弱化確認 {res['trend_state_detail']['weak_days']} 日／結構跌破確認 {res['trend_state_detail']['break_days']} 日）")
        ta = res['trend_analysis']
        tc1, tc2, tc3, tc4 = st.columns(4)
        with tc1: st.markdown(custom_hud_box("週線與長期方向", f"{ta['weekly_desc']}<br>{ta['long_term']}"), unsafe_allow_html=True)
        with tc2: st.markdown(custom_hud_box("波段線型", f"{ta['structure']['label']}<br>結構防守：{res['structure_stop']:.2f} 元"), unsafe_allow_html=True)
        with tc3: st.markdown(custom_hud_box("趨勢強度", f"{ta['trend_strength']}<br>ADX14：{ta['adx']:.1f}"), unsafe_allow_html=True)
        with tc4: st.markdown(custom_hud_box("價量狀態", f"{ta['price_volume']}<br>{ta['accumulation']}"), unsafe_allow_html=True)
        st.markdown(f"""
        <div style="background:#F8FAFC;border:1px solid #CBD5E1;border-left:6px solid #2563EB;padding:14px;border-radius:6px;margin-bottom:14px;line-height:1.7;">
        <b>目前進場模型：</b>{ta['entry_model']}｜<b>模型確認：</b>{'是' if ta['entry_ready'] else '尚未'}｜<b>近60日高點回檔：</b>{ta['drawdown_pct']:.1f}%<br>
        <b>近5日拉回量：</b>20日均量的 {ta['pullback_volume_ratio']:.2f} 倍｜<b>當日量比：</b>{ta['volume_ratio']:.2f} 倍｜<b>量價背離：</b>{ta['volume_divergence']}
        </div>
        """, unsafe_allow_html=True)
        with st.expander("查看四種進場模型與訊號變更紀錄", expanded=False):
            model_df = pd.DataFrame([
                {"模型":"突破進場", "成立":ta['breakout_model'], "說明":"放量越過20日壓力；避免乖離過大時追價"},
                {"模型":"多頭拉回", "成立":ta['pullback_model'], "說明":"長中期向上、回檔量縮且未破波段低點"},
                {"模型":"突破後回測", "成立":ta['retest_model'], "說明":"原壓力轉支撐，回測量縮並等待止跌"},
                {"模型":"築底轉強", "成立":ta['base_model'], "說明":"低點墊高、均線走平後突破，風險較高"},
            ])
            st.dataframe(model_df, use_container_width=True, hide_index=True)
            state_logs=st.session_state.get(f"trend_log_{res['stock_id']}", [])
            if state_logs: st.dataframe(pd.DataFrame(state_logs), use_container_width=True, hide_index=True)
            else: st.caption("本次工作階段尚無狀態變更紀錄。")

        if res['large_holder_trend'] is not None and res['large_holder_diff'] < -0.3:
            st.warning(f"⚠️ 【大摩籌碼預警】千張以上持股級距占比近期下降 (變動: {res['large_holder_diff']:+.2f}%)。數據來源：台灣集中保管結算所股權分散表。")
        if res['peer_corr_val'] is not None and res['peer_corr_val'] < 0.3:
            st.info(f"⚠️ 【大摩共振預警】當前個股與同業龍頭相關性極低 ({res['peer_corr_val']:.2f})。數據來源：同產業股票近60日報酬率 Pearson 相關係數。")

        # 昨晚美股即時戰報
        st.markdown("### 🌐 海外市場與台股大盤參考 [數據來源：Yahoo Finance；本區不含台指期夜盤]")
        radar_show = res["radar_results"]
        if radar_show:
            rd_cols = st.columns(len(radar_show))
            for i, (lbl, val) in enumerate(radar_show.items()):
                with rd_cols[i]: st.markdown(f"""<div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:10px; border-radius:6px; text-align:center;"><span style="font-size:12px; color:#64748B; font-weight:600;">{lbl}</span><h4 style="margin:4px 0 0 0; color:#10B981; font-weight:800;">{val:+.2f}%</h4></div>""", unsafe_allow_html=True)

        # 3. 標對資訊頭部
        st.markdown(f"""<div style="background-color: #1F2937; padding: 18px; border-radius: 8px; border: 2px solid #3B82F6; margin-bottom: 20px;"><div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;"><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">DIAGNOSTIC TARGET</span><h1 style="margin: 4px 0 0 0; color: #FFFFFF; font-size: 28px; font-weight: 800;">{res['stock_name']} <span style="color: #3B82F6;">({res['stock_id']})</span></h1></div><div><span style="color: #9CA3AF; font-size: 13px; font-weight: 600;">大類板塊歸屬</span><h3 style="margin: 4px 0 0 0; color: #F3F4F6; font-size: 18px; font-weight: 700;">{res['industry']}</h3></div><div style="text-align: right; background-color: rgba(255,255,255,0.05); padding: 6px 12px; border-radius: 6px;"><span style="color: #9CA3AF; font-size: 11px; font-weight: 600; display:block;">實時流狀態</span><span style="color: #F9FAFB; font-weight: 600; font-size: 13px;">真實數據源: {res['rt_source']} | </span><span style="color: {res['m_color']}; font-weight: 700; font-size: 13px;">{res['m_desc']}</span></div></div></div>""", unsafe_allow_html=True)

        # 4. 即時報價 HUD 箱
        c1, c2, c3, c4 = st.columns(4)
        with c1: st.markdown(custom_hud_box("💡 當前即市價 [來源: 富果/證交所快流]", f"<span style='font-size:20px; color:#0F172A;'>{res['current_price']:.2f} 元</span><br><small style='color:#64748B;'>今日成交: {res['current_vol']:.0f} 張</small>"), unsafe_allow_html=True)
        with c2: st.markdown(custom_hud_box("⏱️ 5日主力均線 [來源: 歷史K線滾動計算]", f"<span style='font-size:16px; color:#1E293B;'>{res['ma5_val']:.2f} 元</span><br><small style='color:#64748B;'>今日漲跌幅: {res['stock_daily_pct']:+.2f}%</small>"), unsafe_allow_html=True)
        with c3: st.markdown(custom_hud_box("⏳ 移動防禦線 [來源: ATR波動率公式]", f"<span style='font-size:16px; color:#7C3AED;'>{res['trailing_stop_line']}</span><br><small style='color:#64748B;'>當前 ATR14: {res['atr']:.2f}</small>"), unsafe_allow_html=True)
        with c4: st.markdown(custom_hud_box("📊 超額強度 [來源: 個股與大盤漲跌幅差值]", f"<span style='font-size:16px; color:#10B981;'>超額 {res['relative_strength']:+.2f}%</span><br><small style='color:#64748B;'>大盤共振: {'🔥 成立' if res['is_rs_gold'] else '⚪ 整理中'}</small>"), unsafe_allow_html=True)

        # 多因子曝光面板
        st.markdown("### 🧬 多因子資訊面板")
        ib_col1, ib_col2, ib_col3 = st.columns(3)
        with ib_col1:
            macro_detail_desc = f"數據來源：加權指數日成交金額。市場量能不足時，突破訊號通常較不穩定，但實際結果仍需回測驗證。"
            st.markdown(render_panel_html("1. 總體流動性安全閥 [來源: 證交所TAIEX日報]", res['market_vol_desc'], macro_detail_desc, "#3B82F6"), unsafe_allow_html=True)
        with ib_col2:
            # 🌟 誠實數據判斷：抓不到就直接回報無法查詢，不瞎猜 🌟
            if res['large_holder_trend'] is not None:
                holder_desc = f"最新千張持股比率: <b>{res['large_holder_pct']:.2f}%</b><br>持股級距週增減變動: <b>{res['large_holder_diff']:+.2f}%</b>"
                st.markdown(render_panel_html("2. 千張以上持股級距變化", res['large_holder_trend'], holder_desc, "#10B981"), unsafe_allow_html=True)
            else:
                st.markdown(render_panel_html("2. 千張以上持股級距變化", "❌ 該個股目前集體保管交易所查無最新分散表", "FinMind 聯網超時或該股歷史週資料未開放，系統拒絕假造與通膨數字。", "#64748B"), unsafe_allow_html=True)
        with ib_col3:
            st.markdown(render_panel_html("3. [板塊動能] 產業群聚共振定位", "追蹤同業有沒有集體進攻", res['peer_resonance_text'], "#7C3AED"), unsafe_allow_html=True)

        # 7. 底層因果深度解碼驗證區
        st.markdown("---")
        st.markdown("### 🧱 🔍 跨因子微觀底層因果深度解碼驗證區")
        
        # 口語化籌碼與估值說明
        pb_text = f"{res['pb_ratio']:.2f} 倍" if res['pb_ratio'] is not None and res['bvps'] else "資料不足"
        bvps_text = f"{res['bvps']:.2f} 元" if res['bvps'] else "資料不足"
        st.markdown("#### ⚡ 籌碼與估值重點 [數據源：FinMind；僅供資訊整理]")
        st.markdown(f"""
        <div style="background-color:#FFFFFF; padding:16px; border:2px solid #7D3CFF; border-left:8px solid #7D3CFF; border-radius:6px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,0.02);">
            <p style="margin:0 0 12px 0; color:#0F172A; font-size:14.5px; font-weight:700; line-height:1.65;">
                <span style="color:#7D3CFF; font-weight:900; font-size:15px;">📊 【估值與買賣大戶老實說】➔ </span>
                目前這檔股票的最新股價，股價淨值比參考為 <b>{pb_text}</b>（每股淨值參考：{bvps_text}）。不同產業不宜只用同一估值指標判斷。
                最近一個月，投信的態度是【<b>{res['sitc_trend']}</b>】，一般散戶的融資熱度則是【<b>{res['margin_trend']}</b>】。
            </p>
            <p style="margin:0; color:#0F172A; font-size:14.5px; font-weight:700; line-height:1.65;">
                <span style="color:#2563EB; font-weight:900; font-size:15px;">⏱️ 【技術指標動能解讀】➔ </span>
                <b>1. 隨機指標(KD)：</b>{res['kd_timing']}<br>
                <b>2. 主力多空力道(MACD)：</b>{res['bb_stage']}<br>
                <b>3. 買賣雙方力道(RSI)：</b>{res['volume_verdict']}
            </p>
        </div>
        """, unsafe_allow_html=True)

        # 區塊 B：三大法人明細大表
        with st.expander("🦅 三大法人每日實時進出買賣超佈局明細大表 (近30日現況) ─ 點擊展開明細 [數據來源: 證交所三大法人日報]", expanded=False):
            if not res["institutional_df"].empty:
                st.dataframe(res["institutional_df"].style.format({"外資(張)": "{:+,.1f}", "投信(張)": "{:+,.1f}", "自營商總計(張)": "{:+,.1f}"}), use_container_width=True)

        # 區塊 C：外資與本土投顧目標價矩陣
        st.markdown("### 🏛️ 🧮 頂級外資券商與本土投顧最新的研究報告目標價矩陣")
        bc = res["broker_consensus"]
        
        # 🌟 🌟 🌟 誠實留白分流印出：查不到就直接說查不到，拒絕假裝 🌟 🌟 🌟
        if bc.get("is_real", True):
            st.markdown(f"""<div style="background-color:#F5F3FF; padding:12px; border-left:4px solid #7C3AED; border-radius:4px; margin-bottom:12px; font-size:14px; color:#5B21B6; font-weight:700;">🎯 法人共識平均目標價：{bc['mean']:.2f} 元 ｜ 機構最高看好價：{bc['high']:.2f} 元 ｜ 最低防守估值：{bc['low']:.2f} 元<br><small style='color:#6D28D9; font-weight:600;'>[資料來源：Yahoo Finance financialData 彙整欄位；非逐份券商報告]</small></div>""", unsafe_allow_html=True)
            if bc["list"]:
                for b in bc["list"]:
                    st.markdown(f"* **[{b['date']}]** <span style='color:#7C3AED; font-weight:800;'>{b['firm']}</span> 給予 ➔ **【{b['rating']}】** 評等 ｜ 預估溢價目標：<span style='color:#0F172A; font-weight:900; font-size:15px;'>{b['target']:.2f} 元</span>", unsafe_allow_html=True)
        else:
            st.markdown(f"""<div style="background-color:#F1F5F9; padding:12px; border-left:4px solid #64748B; border-radius:4px; margin-bottom:12px; font-size:14px; color:#334155; font-weight:700;">❌ 【資料不足，該股目前未獲得國際外資公開報告覆蓋】<br><small style='color:#475569; font-weight:600;'>[資料來源：Yahoo Finance financialData；查無資料時不推估]</small></div>""", unsafe_allow_html=True)

        # 區塊 D：財務基本面季度結構矩陣大表
        st.markdown("### 📊 財務基本面季度結構矩陣大表")
        with st.expander("📊 點擊此處展開 / 收合財務基本面季度數據細項明細表 [數據來源: 臺灣證券交易所公開資訊觀測站]", expanded=False):
            st.markdown(f"""<div style="background-color:#EFF6FF; padding:10px; border-left:4px solid #3B82F6; border-radius:4px; margin-bottom:12px; font-size:13.5px; color:#1E40AF; font-weight:700;">📋 最新基本面狀態：{res['fin_conclusion']} ｜ 核心營收年增率 (YoY)：{res['latest_yoy']:.2f}%</div>""", unsafe_allow_html=True)
            if not res["fin_df"].empty:
                clean_fin_show = res["fin_df"].copy()
                show_cols = ["date", "EPS", "Revenue", "GrossProfit", "OperatingIncome", "gpm", "opm"]
                clean_fin_show = clean_fin_show[[c for c in show_cols if c in clean_fin_show.columns]]
                clean_fin_show.columns = ["季度日期", "單季 EPS", "營業收入", "營業毛利", "營業利益", "單季毛利率 (%)", "單季營益率 (%)"]
                st.dataframe(clean_fin_show.style.format({"單季 EPS": "{:.2f}", "營業收入": "{:,.0f}", "營業毛利": "{:,.0f}", "營業利益": "{:,.0f}", "單季毛利率 (%)": "{:.2f}%", "單季營益率 (%)": "{:.2f}%"}), use_container_width=True)

        # 區塊 E：新聞輿情流水線
        st.markdown("### 📰 資訊面 24H 網路輿情即時新聞流水線")
        st.markdown(f"""<div style="background-color:#F0FDF4; padding:10px; border-left:4px solid #10B981; border-radius:4px; margin-bottom:12px; font-size:13.5px; color:#15803D; font-weight:700;">> 新聞標題文字傾向（非股價預測）：{res['news_analysis_report']} | [底層數據源: Google News RSS 實時檢索引擎]</div>""", unsafe_allow_html=True)
        if isinstance(res["raw_news_list"], list) and res["raw_news_list"]:
            for n in res["raw_news_list"]: 
                st.markdown(f"* **[{n['sentiment']}]** [{n['source']}]({n['link']}) ─ {n['title']}")
        else:
            st.markdown("* ⚪ 當前時間窗口內暫無網路公開輿情新聞（已自動轉入常態監控）")

        st.markdown("---")
        
        # 9. 最底部開火指令
        st.markdown("### 🛡/⚔️ 風控指揮中心：量化核心配額開火劇本")
        bx1, bx2, bx3 = st.columns(3)
        with bx1: st.metric("風險預算可容納部位（含粗估成本與滑價）", f"{res['suggested_lots']} 張 + {res['suggested_odd_lot']} 股")
        with bx2: st.metric("結構停損估計成交價（含滑價）", f"{res['expected_stop_price']:.2f} 元")
        with bx3: st.metric("大波段移動停利線 (ATR)", res["trailing_stop_line"])

if auto_refresh:
    time.sleep(15)
    st.rerun()
