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
st.set_page_config(page_title="SOP v49 台股多因子資訊與風險提示系統", layout="wide")

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
    if df is None or df.empty: return None
    x = df.copy().sort_values("date").reset_index(drop=True)
    c_prev = x["close"].shift(1)
    x["TR"] = np.maximum(x["high"] - x["low"], np.maximum((x["high"] - c_prev).abs(), (x["low"] - c_prev).abs()))
    x["ATR14"] = x["TR"].ewm(com=13, adjust=False).mean()
    x["MA5"], x["MA5_Vol"] = x["close"].rolling(5).mean(), x["vol"].rolling(5).mean()
    x["MA20"], x["MA60"], x["MA100"], x["MA20_Vol"] = x["close"].rolling(20).mean(), x["close"].rolling(60).mean(), x["close"].rolling(100).mean(), x["vol"].rolling(20).mean()
    x["Res_20D"], x["std20"] = x["high"].shift(1).rolling(20).max(), x["close"].rolling(20).std()
    delta = x["close"].diff()
    x["RSI14"] = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=13, adjust=False).mean() / delta.clip(upper=0).ewm(com=13, adjust=False).mean().replace(0, -0.00001).abs())))
    x["MACD_HIST"] = (x["close"].ewm(span=12, adjust=False).mean() - x["close"].ewm(span=26, adjust=False).mean()) - (x["close"].ewm(span=12, adjust=False).mean() - x["close"].ewm(span=26, adjust=False).mean()).ewm(span=9, adjust=False).mean()
    l_min, h_max = x["low"].rolling(9).min(), x["high"].rolling(9).max()
    x["RSV"] = 100 * ((x["close"] - l_min) / (h_max - l_min).replace(0, 0.00001))
    k_l, d_l, ck, cd = [], [], 50.0, 50.0
    for rsv in x["RSV"]:
        if pd.isna(rsv): k_l.append(np.nan); d_l.append(np.nan)
        else: ck = (2/3)*ck + (1/3)*rsv; cd = (2/3)*cd + (1/3)*ck; k_l.append(ck); d_l.append(cd)
    x["K9"], x["D9"] = k_l, d_l
    return x.dropna(subset=["ATR14", "MA5", "MA20", "MA60", "Res_20D", "RSI14", "MACD_HIST", "K9", "D9"]).copy()

def unified_institutional_brain(res_dict, df_hist, is_holding=False, entry_cost=0.0, sector_panic=False):
    p, resistance = res_dict["current_price"], res_dict["real_resistance"]
    atr, pnl_pct = res_dict["atr"], res_dict["pnl_pct"]
    trailing_stop = res_dict["trailing_stop_value"]
    quality = res_dict.get("data_quality_score", 0)
    macro_bull = res_dict.get("macro_bull")
    market_vol_healthy = res_dict.get("market_vol_healthy")
    confirmed_breakout = res_dict.get("confirmed_breakout", False)
    attempted_breakout = res_dict.get("attempted_breakout", False)
    chip_desc = f"投信：{res_dict.get('sitc_trend')}；融資：{res_dict.get('margin_trend')}。"

    if quality < 60 or macro_bull is None:
        return {"strategy_name": "⚪ 資料不足", "color": "#64748B", "action_now": "暫不產生方向",
                "signal": "關鍵資料未完整", "blueprint": {"停損防守": "待資料恢復", "移動停利": "不適用", "預期目標": "不提供"},
                "desc": f"本次資料完整度為 {quality:.0f}%。為避免誤導，系統只顯示已取得的資訊，不提供買賣方向。"}
    if sector_panic:
        return {"strategy_name": "🟠 族群風險升高", "color": "#F59E0B", "action_now": "暫停新增部位",
                "signal": "同產業股票集體明顯下跌", "blueprint": {"停損防守": f"觀察 {res_dict['stop_brk']:.2f} 元", "移動停利": "已有部位宜縮小風險", "預期目標": "待族群止穩"},
                "desc": "族群同步下跌時，個股技術訊號容易失效。建議先確認是否為產業性事件，再決定是否增加部位。"}
    if is_holding and entry_cost > 0:
        if p < trailing_stop:
            return {"strategy_name": "🔴 跌破波動防守線", "color": "#EF4444", "action_now": "重新評估持股",
                    "signal": "價格低於 ATR 移動防守線", "blueprint": {"停損防守": f"參考 {trailing_stop:.2f} 元", "移動停利": "防守條件已觸發", "預期目標": "先控制風險"},
                    "desc": f"目前損益約 {pnl_pct:+.1f}%。這不代表一定要全部賣出，但原先的波段條件已轉弱，應依部位大小與個人承受度決定減碼或退出。"}
        return {"strategy_name": "🟢 趨勢尚未破壞", "color": "#10B981", "action_now": "續抱並設定防守",
                "signal": "價格仍在移動防守線之上", "blueprint": {"停損防守": f"參考 {trailing_stop:.2f} 元", "移動停利": "隨價格上移", "預期目標": f"情境價 {res_dict['target_brk']:.2f} 元"},
                "desc": f"目前損益約 {pnl_pct:+.1f}%。{chip_desc}技術趨勢尚未明顯轉弱，但盤中訊號仍可能改變，宜保留風險空間。"}
    if confirmed_breakout:
        if not macro_bull or not market_vol_healthy:
            return {"strategy_name": "🟠 突破但環境不佳", "color": "#F59E0B", "action_now": "等待或小額分批",
                    "signal": "個股突破，惟大盤或量能未配合", "blueprint": {"停損防守": f"參考 {res_dict['stop_brk']:.2f} 元", "移動停利": "站穩後再上移", "預期目標": f"情境價 {res_dict['target_brk']:.2f} 元"},
                    "desc": "個股已越過前20日壓力，但市場環境未完全配合，假突破風險較高。這是觀察情境，不是保證上漲。"}
        return {"strategy_name": "🟢 收盤突破觀察", "color": "#10B981", "action_now": "可評估小額分批",
                "signal": "價格與量能越過前20日壓力", "blueprint": {"停損防守": f"參考 {res_dict['stop_brk']:.2f} 元", "移動停利": "依 ATR 上移", "預期目標": f"情境價 {res_dict['target_brk']:.2f} 元"},
                "desc": f"突破條件成立，{chip_desc}仍應依自身資金與風險承受度分批處理，避免一次押注。"}
    if attempted_breakout:
        return {"strategy_name": "🟡 接近前高", "color": "#F59E0B", "action_now": "等待收盤確認",
                "signal": "盤中接近或越過壓力，但尚未確認", "blueprint": {"停損防守": f"參考 {res_dict['stop_brk']:.2f} 元", "移動停利": "尚未建立", "預期目標": "先確認突破"},
                "desc": "目前只是嘗試突破，盤中價格可能回落。對新手而言，等待收盤與成交量確認通常比追價更容易控制風險。"}
    return {"strategy_name": "⚪ 區間觀察", "color": "#64748B", "action_now": "暫不急著進場",
            "signal": "尚未形成明確突破", "blueprint": {"停損防守": "未進場不需設定", "移動停利": "不適用", "預期目標": "等待條件明確"},
            "desc": f"目前技術、量能與籌碼訊號尚未一致。{chip_desc}可先觀察，而不是把單一指標當成買賣命令。"}

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
    volume_verdict = f"實時 14 日 RSI 相對強度落在 {rsi_now:.1f}。"

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
    if spring_triggered: spring_verdict = f"🟢 成功收復前波低點 {detected_prior_low:.2f} 元，主力洗盤完成，破底翻型態確立！"

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
    
    # 🌟 🌟 🌟 [100% 完整接回原創趨勢線分析算法] 🌟 🌟 🌟
    short_term_trend = f"🚀 股價成功站上5日線，短線強勢攻擊中 (KD狀態: {kd_status})" if current_price >= ma5_val and ma5_val >= ma20_val else f"📉 均線全面蓋頭下壓，短線格局偏弱 (KD狀態: {kd_status})"
    long_term_trend = "🔥 季線全面翻揚向上，中長線多頭基底非常扎實" if current_price >= ma60_val and (df["MA60"].iloc[-1] > df["MA60"].iloc[-5]) else "💤 季線橫向橫躺，屬於中線沉澱整理格局"
    trend_phase = "🔥 均線結構完美咬合，正運行多頭波段主升段" if current_price >= ma20_val and ma20_val >= ma60_val and (df["MA20"].iloc[-1] > df["MA20"].iloc[-5]) else "💤 處於區間震盪洗盤或潛伏築底期"

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
    estimated_stop_fill = floor_to_tick(stop_brk - slippage, t)
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

st.markdown("## 📡 台股多因子資訊與風險提示系統（v49 新手安全版）")
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

        # 🌟 🌟 🌟 2. [成功救回] 趨勢診斷分析面板 🌟 🌟 🌟
        st.markdown("### ⏱️ K線與均線技術趨勢診斷報告 [數據依據：歷史 K 線排列型態解算]")
        st.info(f"**【短期攻擊動能】**：{res['short_term_trend']}\n\n"
                f"**【長期趨勢方向】**：{res['long_term_trend']}\n\n"
                f"**【波動階段定位】**：{res['trend_phase']}")

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
        with bx2: st.metric("估計停損成交價（含滑價）", f"{res['expected_stop_price']:.2f} 元")
        with bx3: st.metric("大波段移動停利線 (ATR)", res["trailing_stop_line"])

if auto_refresh:
    time.sleep(15)
    st.rerun()
