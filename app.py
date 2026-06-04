import os
import time
import random
import requests
import certifi
import pandas as pd
import numpy as np
import streamlit as st
import altair as alt
from datetime import datetime, timedelta
import pytz
from FinMind.data import DataLoader

# ============ 1. Page Config ============
st.set_page_config(page_title="SOP v16 終極多因子秒級雷達系統", layout="wide")

# ============ 2. Global ============
TZ = pytz.timezone("Asia/Taipei")


# ============ 3. Helper ============
def safe_float(x, default=0.0):
    try:
        if x is None or str(x).strip() in ["-", "", "None", "nan"]:
            return default
        return float(str(x).replace(",", ""))
    except Exception:
        return default


def tick_size(p: float) -> float:
    """符合台灣證券交易所現行法規之升降單位規則"""
    if p >= 1000:
        return 5.0
    if p >= 500:
        return 1.0
    if p >= 100:
        return 0.5
    if p >= 50:
        return 0.1
    if p >= 10:
        return 0.05
    return 0.01


def round_to_tick(x: float, t: float) -> float:
    if x is None or pd.isna(x) or t == 0:
        return 0.0
    return round(x / t) * t


def fmt_space(x) -> str:
    if x is None or pd.isna(x) or np.isinf(x):
        return "無更高壓力位"
    return f"{x:.2f}"


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
        if is_trading_hours:
            return "OPEN", "市場交易中 (即時更新)", "red"
        elif current_time < start_time:
            return "PRE_MARKET", "盤前準備中 (即時連線正常)", "blue"
        else:
            return "POST_MARKET", "今日已收盤 (即時報價)", "green"
    else:
        if is_trading_hours:
            return "API_WAIT", f"連線受限，改用歷史價 | 歷史日期: {last_trade_date_str}", "orange"
        elif current_time < start_time:
            return "PRE_MARKET", f"盤前準備中 | 歷史日期: {last_trade_date_str}", "blue"
        else:
            if current_time > datetime.strptime("10:00", "%H:%M").time() and last_trade_date_str != now.strftime("%Y-%m-%d"):
                return "CLOSED_HOLIDAY", f"市場休市 (國定假日) | 數據日期: {last_trade_date_str}", "gray"
            return "POST_MARKET", f"今日已收盤 | 數據日期: {last_trade_date_str}", "green"


def next_resistance_above(price: float, levels):
    above = [lv for lv in levels if lv > price]
    return min(above) if above else float("inf")


def detect_style(result: dict) -> str:
    brk_score = 0
    pb_score = 0

    if result.get("breakout_setup"): brk_score += 3
    if result.get("pullback_setup"): pb_score += 3
    if result.get("space_ok_brk"): brk_score += 2
    if result.get("space_ok_pb"): pb_score += 2
    if result.get("rr1_brk", 0) >= 2.0: brk_score += 2
    if result.get("rr1_pb", 0) >= 3.0: pb_score += 2
    if result.get("brk_tradeable"): brk_score += 3
    if result.get("pb_tradeable"): pb_score += 3

    if brk_score > pb_score:
        return "突破型"
    if pb_score > brk_score:
        return "拉回型"

    if result.get("current_price", 0) >= result.get("pivot", 0):
        return "突破型"
    return "拉回型"


def analyze_news_sentiment(title: str) -> tuple:
    pos_words = ['創新高', '大賺', '暴增', '飆', '大成長', '利多', '優於預期', '加碼', '看旺', '強勢', '獲利', '突破', '轉盈', '充沛', '加持', '買超', '爆發', '新高']
    neg_words = ['衰退', '虧損', '重挫', '低於預期', '縮水', '跌破', '警告', '利空', '下滑', '疲弱', '裁員', '大跌', '慘', '賣壓', '修正', '賣超', '暴跌', '逆風']
    
    pos_score = sum(1 for w in pos_words if w in title)
    neg_score = sum(1 for w in neg_words if w in title)
    
    if pos_score > neg_score:
        return "🟢 即時利多", "green"
    elif neg_score > pos_score:
        return "🔴 即時利空", "red"
    else:
        return "🟡 中性消息", "gray"


def compute_live_price(stock_id: str, hist_last_close: float, live_price_override: float = None):
    if live_price_override is not None and live_price_override > 0:
        return live_price_override, True, "雷達模擬串流", "realtime"

    rt_price = None
    rt_success = False
    rt_source = "歷史收盤"
    rt_type = "historical"

    try:
        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0"}
        session.get("https://mis.twse.com.tw/stock/index.jsp", headers=headers, timeout=2, verify=certifi.where())
        ts = int(time.time() * 1000)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
        r = session.get(url, headers=headers, timeout=2, verify=certifi.where())

        if r.status_code == 200:
            data = r.json()
            if "msgArray" in data and len(data["msgArray"]) > 0:
                info = data["msgArray"][0]
                z = safe_float(info.get("z"))
                tv = safe_float(info.get("tv"))
                if z > 0 and tv > 0:
                    rt_price = z
                    rt_success = True
                    rt_source = "TWSE 即時成交"
                    rt_type = "realtime"
    except Exception:
        pass

    if not rt_success:
        try:
            for suffix in [".TW", ".TWO"]:
                url = f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}"
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=2, verify=certifi.where())
                if r.status_code == 200:
                    meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                    p = safe_float(meta.get("regularMarketPrice"))
                    if p > 0:
                        rt_price = p
                        rt_success = True
                        rt_source = f"Yahoo 價格 {suffix}"
                        rt_type = "delayed"
                        break
        except Exception:
            pass

    final_price = rt_price if rt_success else hist_last_close
    if not rt_success:
        rt_source = "歷史收盤"
        rt_type = "historical"

    return final_price, rt_success, rt_source, rt_type


# ============ 4. Auth (已移除密碼驗證) ============
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")


# ============ 5. Cached API ============
@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try:
            api.login_by_token(FINMIND_TOKEN)
        except Exception:
            pass
    return api


@st.cache_data(ttl=3600)
def get_stock_info_df():
    api = get_api()
    df = api.taiwan_stock_info()
    if df is None or df.empty:
        return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category"])
    df = df.copy()
    if "stock_id" in df.columns:
        df["stock_id"] = df["stock_id"].astype(str).str.strip()
    return df


@st.cache_data(ttl=900)
def get_daily_df(stock_id: str, days: int = 365):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df_raw = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
    if df_raw is None or df_raw.empty:
        return None

    df = df_raw.copy()
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={
        "Trading_Volume": "vol",
        "Trading_money": "amount",
        "max": "high",
        "min": "low",
    })

    for c in ["close", "high", "low", "vol", "amount"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["close", "high", "low", "vol"]).copy()
    df = df[df["vol"] > 0].copy()
    return df


@st.cache_data(ttl=900)
def get_inst_df(stock_id: str, days: int = 60):
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
    return df if df is not None else pd.DataFrame()


@st.cache_data(ttl=900)
def get_rev_df(stock_id: str, days: int = 220):
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
        if df_raw is None or df_raw.empty:
            return pd.DataFrame()
        df = df_raw.copy()
        
        # 💡 [修正核心] 改為撈取原始財務金額項目，而非比例名稱
        targets = ["EPS", "Revenue", "GrossProfit", "OperatingIncome"]
        df = df[df["type"].isin(targets)]
        if df.empty:
            return pd.DataFrame()
        
        df_pivot = df.pivot_table(index="date", columns="type", values="value", aggfunc="last").reset_index()
        
        for col in targets:
            if col not in df_pivot.columns:
                df_pivot[col] = 0.0
            else:
                df_pivot[col] = pd.to_numeric(df_pivot[col], errors="coerce").fillna(0.0)
                
        return df_pivot
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def get_realtime_news_df(stock_id: str, stock_name: str):
    news_list = []
    try:
        import xml.etree.ElementTree as ET
        query = f"{stock_id} {stock_name}"
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=4)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ""
                link = item.find('link').text if item.find('link') is not None else ""
                pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ""
                source_el = item.find('source')
                source = source_el.text if source_el is not None else "即時財經快訊"
                
                if " - " in title:
                    title = title.rsplit(" - ", 1)[0]
                    
                news_list.append({
                    "date": pub_date,
                    "title": title,
                    "source": source,
                    "link": link
                })
    except Exception:
        pass
        
    if not news_list:
        try:
            api = get_api()
            start_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
            df = api.taiwan_stock_news(stock_id=stock_id, start_date=start_date)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    news_list.append({
                        "date": str(row.get("date", "盤中即時")),
                        "title": str(row.get("news_title", row.get("title", "重大公告"))),
                        "source": str(row.get("news_source", row.get("source", "FinMind"))),
                        "link": str(row.get("news_link", row.get("link", "")))
                    })
        except Exception:
            pass
            
    return pd.DataFrame(news_list)


# ============ 6. Core ============
def prepare_indicator_df(df: pd.DataFrame):
    if df is None or df.empty:
        return None

    x = df.copy()
    x["ATR14"] = (x["high"] - x["low"]).rolling(14).mean()
    x["MA20"] = x["close"].rolling(20).mean()

    if "amount" in x.columns:
        x["MA20_Amount"] = (x["amount"] / 1e8).rolling(20).mean()
    else:
        x["MA20_Amount"] = (x["close"] * x["vol"] / 1e8).rolling(20).mean()

    direction = np.where(x["close"].diff() > 0, 1, np.where(x["close"].diff() < 0, -1, 0))
    x["OBV"] = (direction * x["vol"]).cumsum()
    x["OBV_MA10"] = x["OBV"].rolling(10).mean()

    delta = x["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    avg_loss = np.where(avg_loss == 0, 0.00001, avg_loss)
    rs = avg_gain / avg_loss
    x["RSI14"] = 100 - (100 / (1 + rs))

    x["up_move"] = x["high"].diff()
    x["down_move"] = x["low"].shift(1) - x["low"]
    x["plus_dm"] = np.where((x["up_move"] > x["down_move"]) & (x["up_move"] > 0), x["up_move"], 0)
    x["minus_dm"] = np.where((x["down_move"] > x["up_move"]) & (x["down_move"] > 0), x["down_move"], 0)

    x["tr1"] = x["high"] - x["low"]
    x["tr2"] = (x["high"] - x["close"].shift(1)).abs()
    x["tr3"] = (x["low"] - x["close"].shift(1)).abs()
    x["TR"] = x[["tr1", "tr2", "tr3"]].max(axis=1)

    tr_14 = x["TR"].rolling(14).sum()
    tr_14 = np.where(tr_14 == 0, 0.00001, tr_14)
    plus_dm_14 = x["plus_dm"].rolling(14).sum()
    minus_dm_14 = x["minus_dm"].rolling(14).sum()

    x["PLUS_DI"] = (plus_dm_14 / tr_14) * 100
    x["MINUS_DI"] = (minus_dm_14 / tr_14) * 100

    di_sum = x["PLUS_DI"] + x["MINUS_DI"]
    di_sum = np.where(di_sum == 0, 0.00001, di_sum)
    x["DX"] = ((x["PLUS_DI"] - x["MINUS_DI"]).abs() / di_sum) * 100
    x["ADX14"] = x["DX"].rolling(14).mean()

    x["EMA12"] = x["close"].ewm(span=12, adjust=False).mean()
    x["EMA26"] = x["close"].ewm(span=26, adjust=False).mean()
    x["MACD_DIF"] = x["EMA12"] - x["EMA26"]
    x["MACD_SIGNAL"] = x["MACD_DIF"].ewm(span=9, adjust=False).mean()
    x["MACD_HIST"] = x["MACD_DIF"] - x["MACD_SIGNAL"]

    x = x.dropna(subset=["ATR14", "MA20", "MA20_Amount", "OBV_MA10", "RSI14", "ADX14", "MACD_HIST"]).copy()
    if x.empty:
        return None
    return x


def evaluate_stock(
    stock_id: str,
    total_capital: float,
    risk_per_trade: float,
    liq_gate: float,
    slip_ticks: int,
    space_atr_mult: float,
    space_tick_buffer: int,
    live_price_override: float = None
):
    df_raw = get_daily_df(stock_id, days=365)
    if df_raw is None or df_raw.empty:
        return None

    df = prepare_indicator_df(df_raw)
    if df is None or df.empty:
        return None

    info_df = get_stock_info_df()
    match = info_df[info_df["stock_id"] == stock_id]
    stock_name = match["stock_name"].values[0] if ("stock_name" in match.columns and not match.empty) else "未知"
    industry = match["industry_category"].values[0] if ("industry_category" in match.columns and not match.empty) else "未知產業"

    hist_last = df.iloc[-1]
    last_trade_date_str = str(hist_last["date"])

    current_price, rt_success, rt_source, rt_type = compute_live_price(
        stock_id, float(hist_last["close"]), live_price_override=live_price_override
    )
    rt_y_price = float(hist_last["close"])
    m_code, m_desc, m_color = get_market_status_label(rt_success, last_trade_date_str)

    # 籌碼面
    inst_df = get_inst_df(stock_id, days=15)
    inst_3d_sum = 0.0
    if not inst_df.empty and "buy" in inst_df.columns and "sell" in inst_df.columns:
        inst_df["net_sheets"] = pd.to_numeric(inst_df["buy"], errors="coerce").fillna(0) - pd.to_numeric(inst_df["sell"], errors="coerce").fillna(0)
        daily_inst = inst_df.groupby("date")["net_sheets"].sum().reset_index()
        inst_3d_sum = float(daily_inst.tail(3)["net_sheets"].sum())

    # 營收基本面
    rev_df = get_rev_df(stock_id, days=120)
    latest_yoy = 0.0
    if not rev_df.empty and "revenue_year_growth_rate" in rev_df.columns:
        rev_sorted = rev_df.sort_values("date")
        latest_yoy = safe_float(rev_sorted.iloc[-1]["revenue_year_growth_rate"])

    # 💡 [全新重構] 財報季報數據底層：利用原始金額，就地即時精算毛利率與營益率
    fin_df = get_financial_statement_df(stock_id, years=2)
    eps_now, eps_prev = 0.0, 0.0
    gpm_now, gpm_prev = 0.0, 0.0
    opm_now, opm_prev = 0.0, 0.0
    fin_conclusion = "📋 該標的暫無足夠的季度財報歷史數據可供比對。"
    
    if fin_df is not None and not fin_df.empty:
        fin_df = fin_df.sort_values("date").reset_index(drop=True)
        
        # 盤中即時換算比例因子 (防呆營收為0)
        for idx in range(len(fin_df)):
            rev_amt = safe_float(fin_df.loc[idx, "Revenue"])
            gp_amt = safe_float(fin_df.loc[idx, "GrossProfit"])
            op_amt = safe_float(fin_df.loc[idx, "OperatingIncome"])
            fin_df.loc[idx, "gpm"] = (gp_amt / rev_amt * 100) if rev_amt > 0 else 0.0
            fin_df.loc[idx, "opm"] = (op_amt / rev_amt * 100) if rev_amt > 0 else 0.0

        latest_fin = fin_df.iloc[-1]
        eps_now = safe_float(latest_fin.get("EPS", 0.0))
        gpm_now = safe_float(latest_fin.get("gpm", 0.0))
        opm_now = safe_float(latest_fin.get("opm", 0.0))
        
        if len(fin_df) >= 2:
            prev_fin = fin_df.iloc[-2]
            eps_prev = safe_float(prev_fin.get("EPS", 0.0))
            gpm_prev = safe_float(prev_fin.get("gpm", 0.0))
            opm_prev = safe_float(prev_fin.get("opm", 0.0))
            
            gpm_text = "進步" if gpm_now > gpm_prev else "退步" if gpm_now < gpm_prev else "持平"
            opm_text = "進步" if opm_now > opm_prev else "退步" if opm_now < opm_prev else "持平"
            eps_text = "進步" if eps_now > eps_prev else "退步" if eps_now < eps_prev else "持平"
            eps_lbl = "多賺" if eps_now > eps_prev else "少賺" if eps_now < eps_prev else "持平"
            
            if gpm_text == "進步" and opm_text == "進步" and eps_text == "進步":
                fin_conclusion = f"📈 **【財報全面升級！比以前更好】** 最新一季的三大核心賺錢指標（EPS、毛利率、營業利益率）**全數超越上一季**！這意味著公司產品利潤變高（毛利率漲）、內部管銷風控得當（營益率噴），且幫股東賺到更多真金白銀（EPS增），體質極度強健！"
            elif gpm_text == "退步" and opm_text == "退步" and eps_text == "退步":
                fin_conclusion = f"📉 **【小心金玉其外！獲利能力全面退步】** 公司賺錢本領**出現全面性倒退**！毛利、營益率、EPS 同步低於前一季。如果這檔股票目前盤中炒得正熱，雷達強烈警告：這極有可能是『題材亂炒、基本面跟不上』的虛火盤，主力隨時可能拉高出貨，風控切記要抓得極緊！"
            else:
                fin_conclusion = f"⚖️ **【橫盤拉鋸調整期！表現與前季互有勝負】** 賺錢能力正處於結構調整期。與上一季相比：毛利率『{gpm_text}』、營業利益率『{opm_text}』、每股 EPS『{eps_lbl}』。本業防守力還在，沒有全面惡化，屬一般良性波動。"

    # 技術面指標數據
    ma20_val = float(hist_last["MA20"])
    atr = float(hist_last["ATR14"]) if not np.isnan(hist_last["ATR14"]) else current_price * 0.03
    t = tick_size(current_price)
    slip = float(slip_ticks) * t

    rsi_now = float(hist_last["RSI14"])
    adx_now = float(hist_last["ADX14"])
    plus_di = float(hist_last["PLUS_DI"])
    minus_di = float(hist_last["MINUS_DI"])
    
    macd_dif = float(hist_last["MACD_DIF"])
    macd_sig = float(hist_last["MACD_SIGNAL"])
    macd_hist = float(hist_last["MACD_HIST"])

    tech_conclusion_long = "⚖️ 擺動指標目前處於中性區，大資金尚未表態，短線缺乏爆發性動能。"
    tech_conclusion_short = "中性觀望"

    if adx_now < 20:
        if inst_3d_sum < 0 and latest_yoy < 0:
            tech_conclusion_long = "❌ **【死水無底洞，請直接忽略】** 技術面死氣沉沉完全沒有攻擊動能，而且法人像逃難一樣天天倒貨，月營收也慘不忍睹。這種股票哪怕跌再深，進去也只是浪費資金的潛在時間成本，請直接忽略它！"
            tech_conclusion_short = "❌ 死水忽略"
        else:
            tech_conclusion_long = "💤 **【盤整死水期】** 目前處於毫無波瀾的死水盤整期（ADX低於20）。多空沒有方向，此時任何『突破型策略』失敗率極高，容易買了就被洗盤，建議把資金抽回換去有量的地方。"
            tech_conclusion_short = "💤 盤整死水"
            
    elif rsi_now >= 75:
        if inst_3d_sum > 0:
            tech_conclusion_long = "🔥 **【極度過熱！主力硬幹妖股】** 短線技術指標已經高達 75 以上，追高被埋的風險巨大。但雷達警報發現，外資與投信完全不管指標死活，繼續瘋狂加碼硬幹！這屬於極高風險、高回報的妖股模式，如果想上車，千萬不能重倉，且手速要快、停損要設得極窄！"
            tech_conclusion_short = "🔥 妖股狂飆"
        else:
            tech_conclusion_long = "⚠️ **【短線極度過熱】** 股價買盤短線已經推升到極限（RSI超買）。此時衝動追高的性價比極低，回檔修正隨時會來，強烈建議高位克制雙手，耐心等待拉回均線再找機會。"
            tech_conclusion_short = "⚠️ 短線過熱"
            
    elif rsi_now <= 30:
        tech_conclusion_long = "📉 **【恐慌超賣區】** 股價極度超賣，市場出現恐慌性拋售，空頭宣洩中。雖然價格便宜，但目前尚未見到底部止跌訊號，暫不具備進場做多的攻擊條件，不可盲目伸手接刀。"
        tech_conclusion_short = "📉 恐慌超賣"
        
    elif plus_di > minus_di and adx_now >= 20:
        if inst_3d_sum > 0 and latest_yoy > 20:
            tech_conclusion_long = "🚀 **【黃金進攻訊號】** 這隻股票目前技術面強勢多頭，買盤動能飽滿。最漂亮的是法人在後面用真金白銀幫忙抬轎，基本面又有強勁營收撐腰！不論你想走『突破快市追擊』還是『拉回小波段』，這檔都是今天勝率極高的極品首選！"
            tech_conclusion_short = "🚀 完美多頭"
        elif inst_3d_sum < 0:
            tech_conclusion_long = "⚠️ **【小心假突破！主力在出貨】** 日K線雖然看起來很漂亮、好像要發動大攻擊，但雷達抓到三大法人這幾天一邊拉抬股價、一邊瘋狂倒貨給散戶！這高度懷疑是個美麗的假突破陷阱，盤中千萬別追高，進去極容易接盤！"
            tech_conclusion_short = "⚠️ 假突破嫌疑"
        else:
            tech_conclusion_long = "趨勢多頭成形，買盤動能延續性佳，屬於健康的攻擊型態，適合尋找突破點切入。"
            tech_conclusion_short = "🚀 多頭成形"
            
    elif minus_di > plus_di and adx_now >= 20:
        tech_conclusion_long = "📉 **【強勢空頭成形】** 技術面完全由空方主導（ADX上攻且空頭掌控）。市場賣壓極其沉重，此時盲目做多無異於螳臂擋車，極易逆勢受傷，強烈建議觀望，或尋找融券放空機會。"
        tech_conclusion_short = "📉 空頭成形"

    if tech_conclusion_short not in ["🚀 完美多頭", "⚠️ 假突破嫌疑", "🔥 股狂飆", "❌ 死水忽略"]:
        if current_price >= ma20_val and (current_price - ma20_val) / ma20_val <= 0.04:
            if inst_3d_sum > 0 and latest_yoy > 15:
                tech_conclusion_long = "🛡️ **【高手最愛！拉回安全防守點】** 股價經歷短線修正，目前精準跌到 MA20 均線防守區，過熱指標被洗乾淨了。最棒的是，下跌期間法人在偷偷吃貨，營收也很好！這就是最標準的拉回極品，下檔有肉墊，適合分批佈局。"
                tech_conclusion_short = "🛡️ 精準拉回"

    pivot = ma20_val
    brk_setup = (current_price >= pivot) and (rsi_now < 70)
    pb_setup = (current_price < pivot) and (current_price >= ma20_val * 0.97)

    levels = [ma20_val * 1.1, ma20_val * 1.2, ma20_val * 1.3]
    next_res = next_resistance_above(current_price, levels)

    target_brk = round_to_tick(next_res if next_res != float("inf") else current_price * 1.15, t)
    stop_brk = round_to_tick(current_price - (2 * atr) - slip, t)
    r_brk = target_brk - current_price
    s_brk = current_price - stop_brk
    rr1_brk = r_brk / s_brk if s_brk > 0 else 0

    target_pb = round_to_tick(pivot, t)
    stop_pb = round_to_tick(current_price - atr - slip, t)
    r_pb = target_pb - current_price
    s_pb = current_price - stop_pb
    rr1_pb = r_pb / s_pb if s_pb > 0 else 0

    result = {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "industry": industry,
        "current_price": current_price,
        "hist_last_close": rt_y_price,
        "market_desc": m_desc,
        "market_color": m_color,
        "pivot": pivot,
        "atr": atr,
        "tick_size": t,
        "inst_3d_sheets": inst_3d_sum,
        "latest_revenue_yoy": latest_yoy,
        "breakout_setup": brk_setup,
        "pullback_setup": pb_setup,
        "space_ok_brk": r_brk > (space_atr_mult * atr),
        "space_ok_pb": r_pb > (float(space_tick_buffer) * t),
        "target_brk": target_brk,
        "stop_brk": stop_brk,
        "rr1_brk": rr1_brk,
        "target_pb": target_pb,
        "stop_pb": stop_pb,
        "rr1_pb": rr1_pb,
        "brk_tradeable": brk_setup and rr1_brk >= 1.5,
        "pb_tradeable": pb_setup and rr1_pb >= 2.0,
        "tech_conclusion_long": tech_conclusion_long,
        "tech_conclusion_short": tech_conclusion_short,
        "eps_now": eps_now, "eps_prev": eps_prev,
        "gpm_now": gpm_now, "gpm_prev": gpm_prev,
        "opm_now": opm_now, "opm_prev": opm_prev,
        "fin_conclusion": fin_conclusion,
        "macd_dif": macd_dif,
        "macd_sig": macd_sig,
        "macd_hist": macd_hist,
        "rsi_now": rsi_now,
        "adx_now": adx_now
    }

    result["style"] = detect_style(result)
    return result


# ============ 7. Streamlit UI ============
st.title("SOP v16 終極多因子雷達決策系統")

st.sidebar.header("⚙️ 全局風控參數")
total_cap = st.sidebar.number_input("總本金 (萬元)", value=100.0)
risk_pct = st.sidebar.slider("單筆最大風險 (%)", 0.5, 3.0, 1.0)

tab1, tab2 = st.tabs(["🔍 單股詳細資料深度診斷", "🚀 大盤多股批量雷達"])

with tab1:
    target_stock = st.text_input("輸入股票代碼進行多因子健檢", value="2330").strip()
    if st.button("開始單股雷達掃描"):
        with st.spinner("多因子融合矩陣計算中..."):
            res = evaluate_stock(target_stock, total_cap, risk_pct, 500, 1, 1.5, 3)
            
            if res:
                st.markdown(f"## 🏢 {res['stock_name']} ({res['stock_id']}) · `{res['industry']}`")
                
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("即時股價", f"{res['current_price']} 元", f"狀態: {res['tech_conclusion_short']}")
                col2.metric("近3日法人買賣超", f"{res['inst_3d_sheets']:.0f} 張")
                col3.metric("最新營收年增率", f"{res['latest_revenue_yoy']:.2f} %")
                col4.metric("建議操盤風格", res["style"])
                
                st.subheader("💡 終極雷達白話文操盤建議")
                st.info(res["tech_conclusion_long"])
                
                st.subheader("📈 盤中五大技術分析因子診斷")
                tech_col1, tech_col2, tech_col3, tech_col4, tech_col5 = st.columns(5)
                macd_trend = "🔺 多頭擴張" if res['macd_hist'] > 0 else "🔻 空頭修正"
                tech_col1.metric("MACD 柱狀體 (Hist)", f"{res['macd_hist']:.3f}", macd_trend)
                tech_col2.metric("RSI(14) 強弱度", f"{res['rsi_now']:.2f}", "超買 >70 | 超賣 <30")
                tech_col3.metric("20日均線支撐 (MA20)", f"{res['pivot']:.1f} 元", f"現價乖離: {((res['current_price']-res['pivot'])/res['pivot']*100):.1f}%")
                tech_col4.metric("DMI 趨勢動能 (ADX)", f"{res['adx_now']:.1f}", "有趨勢 >20 | 死水 <20")
                tech_col5.metric("14日真實波動度 (ATR)", f"{res['atr']:.2f} 元", f"Tick大小: {res['tick_size']}")
                
                st.subheader("📊 季度核心基本面體檢 (最新季 vs 前一季)")
                st.success(res["fin_conclusion"])
                f_col1, f_col2, f_col3 = st.columns(3)
                def get_trend_tag(now, prev):
                    return "🔺 進步" if now > prev else "🔻 退步" if now < prev else "➖ 持平"
                f_col1.metric("每股盈餘 (EPS)", f"{res['eps_now']:.2f} 元", f"前季: {res['eps_prev']:.2f} | {get_trend_tag(res['eps_now'], res['eps_prev'])}")
                f_col2.metric("營業毛利率", f"{res['gpm_now']:.2f} %", f"前季: {res['gpm_prev']:.2f} | {get_trend_tag(res['gpm_now'], res['gpm_prev'])}")
                f_col3.metric("營業利益率", f"{res['opm_now']:.2f} %", f"前季: {res['opm_prev']:.2f} | {get_trend_tag(res['opm_now'], res['opm_prev'])}")
                
                st.subheader("🎯 交易藍圖與精算風控價位")
                box_brk, box_pb = st.columns(2)
                with box_brk:
                    st.markdown("### 🏃‍♂️ 【突破型策略】方案藍圖")
                    st.markdown(f"* **現價進場點：** `{res['current_price']}` 元")
                    st.markdown(f"* **停利目標價：** `{res['target_brk']}` 元")
                    st.markdown(f"* **防守停損點：** `{res['stop_brk']}` 元")
                    if res['rr1_brk'] < 1.5:
                        st.error(f"❌ 當前風報比: **{res['rr1_brk']:.2f}** (🔴 空間不足)")
                    elif res['rr1_brk'] < 2.0:
                        st.warning(f"🟡 當前風報比: **{res['rr1_brk']:.2f}** (黃金突破及格線)")
                    else:
                        st.success(f"🚀 當前風報比: **{res['rr1_brk']:.2f}** (🟢 優勢點位)")
                with box_pb:
                    st.markdown("### 🛡️ 【拉回型策略】方案藍圖")
                    st.markdown(f"* **理想買入點：** `{res['current_price']}` 元")
                    st.markdown(f"* **短線停利價：** `{res['target_pb']}` 元")
                    st.markdown(f"* **破位停損點：** `{res['stop_pb']}` 元")
                    if res['rr1_pb'] < 2.0:
                        st.error(f"❌ 當前風報比: **{res['rr1_pb']:.2f}** (🔴 利潤太薄)")
                    else:
                        st.success(f"🚀 當前風報比: **{res['rr1_pb']:.2f}** (🟢 理想低吸點)")

                st.subheader("📰 盤中即時消息面解讀")
                news_df = get_realtime_news_df(res['stock_id'], res['stock_name'])
                if news_df is not None and not news_df.empty:
                    num_pos = 0
                    num_neg = 0
                    num_neu = 0
                    news_head = news_df.head(8)
                    
                    for _, row in news_head.iterrows():
                        _, s_col = analyze_news_sentiment(row['title'])
                        if s_col == "green": num_pos += 1
                        elif s_col == "red": num_neg += 1
                        else: num_neu += 1
                    
                    st.markdown("#### 📋 盤中核心消息重點摘要")
                    st.markdown(f"* ⚡ 雷達偵測到盤中最新 `{len(news_head)}` 則財經媒體要聞，大數據權值分類：🟢 **即時利多 `{num_pos}` 則** | 🔴 **即時利空 `{num_neg}` 則** | 🟡 **中性常規公告 `{num_neu}` 則**。")
                    
                    st.markdown("#### 🎯 消息面多空綜合操盤結論")
                    if num_pos > num_neg and num_pos >= 2:
                        st.success("🚀 **【偏多訊號】消息面利多頻傳，盤中買盤熱度高！** 大量正面報導容易引發游資及散戶在盤中跟風追價。這種局勢高度有利於配合『突破型策略』進行快市順勢追擊，肉厚汁多！")
                    elif num_neg > num_pos and num_neg >= 2:
                        st.error("⚠️ **【偏空警告】利空罩頂！提防盤中爆發拋售潮！** 負面關鍵詞密集跳出，市場恐慌情緒正在凝聚。此時哪怕個股技術線型再漂亮，也要高度提防假突破陷阱，絕對不宜追高，想玩只能克制雙手等回檔！")
                    elif num_pos > 0 and num_neg > 0:
                        st.warning("⚖️ **【震盪洗盤】多空消息劇烈拉鋸，市場分歧巨大！** 好壞題材同時交織（例如外資調降評等但營收創新高）。這種狀態極易導致盤中上下甩尾洗盤，操作難度極高，風控必須卡死！")
                    else:
                        st.info("📋 **【中性平穩】消息面風平浪靜，無重大題材。** 目前均為例行性常規公告。盤中走勢將回歸『技術面（MA20）與籌碼法人進出』的純量化主導，缺乏話題帶動的爆發性動能。")
                    
                    st.markdown("#### 🔍 即時新聞備查列表（點開看詳情）")
                    for _, row in news_head.iterrows():
                        sentiment_text, sentiment_color = analyze_news_sentiment(row['title'])
                        with st.expander(f"⏱️ {row['date']} | 📢 {row['source']} | :{sentiment_color}[**{sentiment_text}**] ── {row['title']}"):
                            st.markdown(f"**📌 新聞原文：** {row['title']}")
                            st.markdown(f"**⏰ 發布時間：** `{row['date']}` (來源媒體: {row['source']})")
                            if row['link'] and str(row['link']).startswith("http"):
                                st.markdown(f"🔗 [點我展開查看媒體詳細原始報導]({row['link']})")
                else:
                    st.info("📋 盤中安全掃描：此標的當下暫無即時媒體快訊發布。")

                st.write("### 🔍 因子診斷後台原始 JSON 數據")
                st.json(res)
            else:
                st.error("找不到該股票歷史資料，請確認代碼。")

# 大盤多股批量雷達
with tab2:
    st.subheader("🛸 大盤多因子全自動選股雷達")
    info_df = get_stock_info_df()
    all_industries = sorted([str(x) for x in info_df["industry_category"].dropna().unique() if str(x).strip() != ""])
    
    scan_scope = st.radio(
        "🎯 請選擇雷達自動掃描範圍：",
        ["🔥 核心權值龍頭股 (自動精選市值前30大標的)", "🏭 依特定產業類股全自動橫掃"],
        key="bulk_scan_scope"
    )
    
    selected_ind = None
    if scan_scope == "🏭 依特定產業類股全自動橫掃":
        selected_ind = st.selectbox("選擇要轟炸的產業板塊：", all_industries, index=all_industries.index("半導體業") if "半導體業" in all_industries else 0)

    if st.button("🚀 啟動全自動雷達大盤掃描"):
        if scan_scope == "🔥 核心權值龍頭股 (自動精選市值前30大標的)":
            stock_list = [
                "2330", "2317", "2454", "2308", "2382", "2324", "2881", "2882", "2603", "2609", 
                "2618", "2357", "4938", "3231", "2301", "2886", "2891", "2884", "2892", "2379", 
                "3008", "3045", "2408", "1301", "1303", "2002", "2912", "9910", "2345", "6505"
            ]
        else:
            stock_list = info_df[info_df["industry_category"] == selected_ind]["stock_id"].tolist()
            if len(stock_list) > 60:
                stock_list = stock_list[:60]
            
        all_results = []
        progress_bar = st.progress(0)
        
        for idx, sid in enumerate(stock_list):
            try:
                res = evaluate_stock(sid, total_cap, risk_pct, 500, 1, 1.5, 3)
                if res:
                    all_results.append({
                        "代碼": res["stock_id"],
                        "名稱": res["stock_name"],
                        "即時現價": res["current_price"],
                        "操作風格": res["style"],
                        "盤中技術體檢": res["tech_conclusion_short"],
                        "法人3日買賣(張)": res["inst_3d_sheets"],
                        "月營收YoY(%)": res["latest_revenue_yoy"],
                        "最新季EPS": res["eps_now"],
                        "前季EPS": res["eps_prev"],
                        "突破型風報比": round(res["rr1_brk"], 2),
                        "拉回型風報比": round(res["rr1_pb"], 2),
                        "突破型建議": "🟢 值得進攻" if res["brk_tradeable"] else "❌ 風報比不及格",
                        "拉回型建議": "🟢 值得低吸" if res["pb_tradeable"] else "❌ 空間不足"
                    })
            except Exception:
                pass
            progress_bar.progress((idx + 1) / len(stock_list))
            
        if all_results:
            scan_df = pd.DataFrame(all_results)
            st.success(f"🎉 大盤全自動掃描完成！成功分析 {len(stock_list)} 檔標的。")
            tradeable_only = st.checkbox("🎯 只顯示風報比過關、符合量化交易策略標準的標的（強力推薦）")
            
            if tradeable_only:
                filtered_df = scan_df[(scan_df["突破型建議"] == "🟢 值得進攻") | (scan_df["拉回型建議"] == "🟢 值得低吸")]
                if not filtered_df.empty:
                    filtered_df = filtered_df.sort_values(by=["突破型風報比", "拉回型風報比"], ascending=False)
                    st.dataframe(filtered_df, use_container_width=True, hide_index=True)
                else:
                    st.warning("😭 盤中因子掃描完畢，目前大盤此範圍內暫時沒有任何標的符合黃金風報比條件。")
            else:
                st.dataframe(scan_df, use_container_width=True, hide_index=True)
        else:
            st.error("未能成功讀取任何標的數據，請確認 API 連線狀態。")
