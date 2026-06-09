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
st.set_page_config(page_title="SOP v18 全串聯多因子量化交易決策系統", layout="wide")

# ============ 2. Global Constants ============
TZ = pytz.timezone("Asia/Taipei")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

# ============ 3. Helper Functions ============
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

@st.cache_resource
def get_requests_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

@st.cache_resource
def get_api():
    api = DataLoader()
    if FINMIND_TOKEN:
        try: api.login_by_token(FINMIND_TOKEN)
        except Exception: pass
    return api

# ============ 4. Advanced Data Layer (Missing Gaps Addressed) ============

@st.cache_data(ttl=3600)
def get_stock_info_df():
    api = get_api()
    df = api.taiwan_stock_info()
    return df.copy() if df is not None else pd.DataFrame()

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
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[df["vol"] > 0].copy()

@st.cache_data(ttl=1800)
def get_market_macro_status():
    """
    【補足缺口一：大盤環境濾網】
    獲取大盤加權指數（TAIEX）判斷市場整體 Beta 風險
    """
    api = get_api()
    start_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    try:
        df = api.taiwan_stock_daily(stock_id="TAIEX", start_date=start_date)
        if df is not None and not df.empty:
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['MA20'] = df['close'].rolling(20).mean()
            last_row = df.iloc[-1]
            is_bull_market = last_row['close'] >= last_row['MA20']
            trend = "🟢 多頭常態" if is_bull_market else "🚨 空頭防禦"
            return is_bull_market, trend, float(last_row['close'])
    except Exception: pass
    return True, "🟢 多頭常態 (無法取得大盤，預設寬鬆)", 20000.0

@st.cache_data(ttl=900)
def get_taiwan_enhanced_chips(stock_id: str, days: int = 30):
    """
    【補足缺口二：台股特色籌碼細分】
    拆解投信（飆股發動機）與融資（散戶浮額過熱指標）
    """
    api = get_api()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    sitc_trend = "中性"
    margin_trend = "中性"
    sitc_3d_sum = 0.0
    
    # 1. 拆解法人中的投信行為
    try:
        inst_df = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=start_date)
        if inst_df is not None and not inst_df.empty:
            sitc_df = inst_df[inst_df['name'] == 'Investment_Trust'].copy()
            if not sitc_df.empty:
                sitc_df['net'] = pd.to_numeric(sitc_df['buy'], errors='coerce') - pd.to_numeric(sitc_df['sell'], errors='coerce')
                sitc_3d_sum = float(sitc_df.tail(3)['net'].sum())
                if sitc_3d_sum > 500: sitc_trend = "🟢 投信大哥鎖碼"
                elif sitc_3d_sum < -500: margin_trend = "🔴 投信棄養"
    except Exception: pass

    # 2. 追蹤融資餘額（散戶浮額）
    try:
        margin_df = api.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start_date)
        if margin_df is not None and not margin_df.empty:
            margin_df['MarginPurchaseTodayBalance'] = pd.to_numeric(margin_df['MarginPurchaseTodayBalance'], errors='coerce')
            margin_diff = margin_df.iloc[-1]['MarginPurchaseTodayBalance'] - margin_df.iloc[-5]['MarginPurchaseTodayBalance']
            if margin_diff > 1000: margin_trend = "🚨 融資散戶強套/浮額凌亂"
            elif margin_diff < -1000: margin_trend = "🟢 融資清洗/籌碼沉澱"
    except Exception: pass

    return sitc_trend, margin_trend, sitc_3d_sum

@st.cache_data(ttl=86400)
def calculate_pe_valuation(stock_id: str, current_price: float):
    """
    【補足缺口三：估值位階與歷史校準】
    計算 Trailing PE（近四季滾動本益比）並評估位階
    """
    api = get_api()
    start_date = (datetime.now() - timedelta(days=365 * 2)).strftime("%Y-%m-%d")
    try:
        df = api.taiwan_stock_financial_statement(stock_id=stock_id, start_date=start_date)
        if df is not None and not df.empty:
            eps_df = df[df['type'] == 'EPS'].sort_values('date')
            if len(eps_df) >= 4:
                sum_eps_4q = pd.to_numeric(eps_df.tail(4)['value'], errors='coerce').sum()
                if sum_eps_4q > 0:
                    current_pe = current_price / sum_eps_4q
                    if current_pe > 35: return current_pe, "🚨 估值瘋狂（高檔吹泡泡）", sum_eps_4q
                    if current_pe < 12: return current_pe, "🟢 價值鐵板（安全邊際高）", sum_eps_4q
                    return current_pe, "⚖️ 估值合理", sum_eps_4q
    except Exception: pass
    return 0.0, "⚪ 數據不足無法計算估值", 0.0

# ============ 5. Technical Processing Engine ============
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

    return x.dropna(subset=["ATR14", "MA20", "MA60", "Res_20D", "BB_bandwidth", "RSI14"]).copy()

# ============ 6. 全串聯核心交叉矩陣引擎 (The Brain) ============
def cross_factor_decoupling_engine(macro_bull, trend_phase, pe_desc, sitc_trend, margin_trend, tech_short, latest_yoy):
    """
    【核心優化：告別分數加總，改用多維矩陣狀態機】
    將宏觀、估值、本業、籌碼與動能縱向串聯，判定最終交易劇本
    """
    # 策略地雷一：覆巢之下無完卵（大盤走空，強勢突破高機率是誘多）
    if not macro_bull and tech_short in ["🚀 準備起漲", "🚀 完美多頭"]:
        return "🚨 大盤空頭陷阱", "red", "技術面雖強，但大盤處於 20MA 下方的修正期，此時假突破機率大於 80%！策略應改為『觀望或極小資金試單』。"

    # 策略地雷二：基本面與籌碼嚴重背離的「高位價值陷阱」
    if "主升段" in trend_phase and pe_desc == "🚨 估值瘋狂（高檔吹泡泡）" and margin_trend == "🚨 融資散戶強套/浮額凌亂":
        return "💥 主力強弩之末（散戶接盤）", "red", "股價處於主升段高位，但估值已達歷史瘋狂區，且近 5 日主力出貨給融資散戶。這是典型利多出尋求抓交替的訊號，絕對不可追高！"

    # 完美風暴一：真·完美風暴（大盤多頭 + 橫盤極致壓縮 + 基本面爆發 + 法人點火）
    if macro_bull and "橫盤蓄勢" in trend_phase and sitc_trend == "🟢 投信大哥鎖碼" and latest_yoy > 25:
        return "🔮 完美風暴：集團/投信作帳主升飆股", "purple", "布林通道長期壓縮後，伴隨月營收年增暴增與投信真槍實彈鎖碼！大盤環境支持，此為勝率極高的起漲風口。"

    # 完美風暴二：精準拉回良性換手（多頭拉回 + 估值合理/便宜 + 融資清洗乾淨）
    if "拉回洗盤期" in trend_phase and pe_desc in ["⚖️ 估值合理", "🟢 價值鐵板（安全邊際高）"] and margin_trend == "🟢 融資清洗/籌碼沉澱":
        return "🛡️ 黃金右腳：良性價值換手點", "green", "中長期結構向上，短線跌破月線但屬於良性洗盤。估值具有高安全邊際，且散戶融資大退，籌碼乾淨，為極佳的『分批低吸潛伏』點。"

    # 垃圾時間：無量死水，沒有主力
    if "橫盤蓄勢" in trend_phase and sitc_trend == "中性" and latest_yoy < 0:
        return "💤 邊緣人時間：基本面失速無量橫盤", "gray", "營收動能退步且法人無心看顧，雖然股價跌不動，但時間成本極高，容易橫盤磨人，建議換股操作。"

    # 預設常態推導
    if "空頭修正" in trend_phase:
        return "❌ 空頭修正：嚴格避開", "red", "結構全面走空，任何技術面反彈皆為诱多，切勿摸底。"
    
    return "⚖️ 標準波段：按常規技術藍圖操作", "blue", "各項因子互有勝負，並未出現極端共振，依據下方交易藍圖精算之價位執行即可。"

# ============ 7. Main Evaluation Executer ============
def evaluate_stock(stock_id: str, total_capital: float, risk_per_trade: float, slip_ticks: int):
    # 基礎資料抓取
    df_raw = get_daily_df(stock_id, days=365)
    if df_raw is None or df_raw.empty: return None
    
    hist_last_raw = df_raw.iloc[-1]
    current_price = float(hist_last_raw["close"]) # 此處可串接即時 API，為精簡架構以最新收盤代入
    current_vol = float(hist_last_raw["vol"])
    
    df = prepare_indicator_df(df_raw)
    if df is None or df.empty: return None
    hist_last = df.iloc[-1]
    
    # 獲取各維度缺失因子 (縱向連動準備)
    macro_bull, macro_desc, _ = get_market_macro_status()
    sitc_trend, margin_trend, sitc_3d_sum = get_taiwan_enhanced_chips(stock_id)
    pe_val, pe_desc, sum_eps_4q = calculate_pe_valuation(stock_id, current_price)
    
    # 判定大象股防禦門檻
    recent_amount_ma = df["amount"].tail(20).mean()
    is_heavyweight = recent_amount_ma > 2000000000  
    vol_multiplier, compress_quantile = (1.25, 0.35) if is_heavyweight else (2.2, 0.18)
    
    # 技術量化物理量
    ma20_val = float(hist_last["MA20"])
    ma60_val = float(hist_last["MA60"])
    vol_ma20_val = float(hist_last["MA20_Vol"])
    real_resistance = float(hist_last["Res_20D"])
    current_bandwidth = float(hist_last["BB_bandwidth"])
    atr = float(hist_last["ATR14"])
    rsi_now = float(hist_last["RSI14"])
    
    vol_spike = current_vol > (vol_ma20_val * vol_multiplier)
    bandwidth_60d = df["BB_bandwidth"].tail(60)
    is_compressed = current_bandwidth < bandwidth_60d.quantile(compress_quantile) if not bandwidth_60d.empty else False

    # 1. 均線架構定性
    ma20_trend_5d = "上升" if df["MA20"].iloc[-1] > df["MA20"].iloc[-5] else "平盤"
    if current_price >= ma20_val and ma20_val >= ma60_val and ma20_trend_5d == "上升":
        trend_phase = "🔥 波段多頭主升段"
    elif current_price < ma20_val and ma20_val >= ma60_val:
        trend_phase = "🛡️ 多頭架換拉回洗盤期"
    elif is_compressed:
        trend_phase = "💤 潛伏築底蓄勢期"
    else:
        trend_phase = "📉 空頭波段修正期"

    # 2. 基本面營收
    api = get_api()
    latest_yoy = 0.0
    try:
        rev_df = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now()-timedelta(days=120)).strftime("%Y-%m-%d"))
        if rev_df is not None and not rev_df.empty:
            latest_yoy = safe_float(rev_df.iloc[-1].get("revenue_year_growth_rate", 0.0))
    except Exception: pass

    # 3. 微觀動能定性
    tech_short = "中性觀望"
    if current_price >= real_resistance * 0.995 and vol_spike and is_compressed:
        tech_short = "🚀 準備起漲"
    elif rsi_now >= (75 if is_heavyweight else 85):
        tech_short = "⚠️ 短線過熱"
    elif float(hist_last["PLUS_DI"]) > float(hist_last["MINUS_DI"]):
        tech_short = "🚀 多頭成形"

    # 4. 執行大腦交叉矩陣決策串聯
    final_decision, final_color, final_desc = cross_factor_decoupling_engine(
        macro_bull, trend_phase, pe_desc, sitc_trend, margin_trend, tech_short, latest_yoy
    )

    # 5. 【補足缺口四：動態出場劇本與量化風控藍圖】
    t = tick_size(current_price)
    slip = float(slip_ticks) * t
    
    # 根據大腦決策調整風險敞口
    adjusted_risk = risk_per_trade
    if final_color == "red": adjusted_risk *= 0.0  # 遇到高風險地雷劇本，直接拒絕交易
    elif final_color == "purple": adjusted_risk *= 1.5 # 完美風暴劇本，允許加碼攻擊
    
    # 進場風控精算 (以防守 MA20 或 前高 為基準)
    stop_loss_price = round_to_tick(ma20_val - (1.5 * atr) - slip, t)
    if stop_loss_price >= current_price: 
        stop_loss_price = round_to_tick(current_price - (2.0 * atr), t)
        
    loss_per_share = current_price - stop_loss_price
    risk_money = total_capital * (adjusted_risk / 100) * 10000
    suggested_lots = int((risk_money / loss_per_share) / 1000) if loss_per_share > 0 else 0
    
    # 動態移動停利線 (Trailing Stop Line)
    trailing_stop_line = round_to_tick(current_price - (2.5 * atr), t)

    return {
        "stock_id": stock_id,
        "current_price": current_price,
        "macro_desc": macro_desc,
        "pe_val": pe_val,
        "pe_desc": pe_desc,
        "sitc_trend": sitc_trend,
        "margin_trend": margin_trend,
        "final_decision": final_decision,
        "final_color": final_color,
        "final_desc": final_desc,
        "suggested_lots": suggested_lots,
        "stop_loss_price": stop_loss_price,
        "trailing_stop_line": trailing_stop_line,
        "eps_4q": sum_eps_4q
    }

# ============ 8. Streamlit UI Presentation ============
st.title("SOP v18 頂級多因子全串聯深度診斷系統")
st.caption("2026 旗艦版 - 已全面整合大盤環境、投信鎖碼、融資洗盤、滾動PE與動態移停利劇本")

with st.sidebar:
    st.header("⚙️ 實戰風控參數配置")
    stock_input = st.text_input("輸入台股代碼", "2330")
    capital = st.number_input("個人交易總資本 (萬新台幣)", value=100.0, step=10.0)
    risk_pct = st.slider("單筆交易最大核心風險承擔 (%)", 0.5, 3.0, 1.0, 0.1)
    slip_input = st.slider("預估防守滑價摩擦 (Ticks)", 0, 5, 1)

if st.button("🔥 啟動跨因子矩陣全方位診斷", use_container_width=True):
    with st.spinner("正在啟動五維度決策大腦，交叉勾稽大盤、籌碼、財報與微觀動能..."):
        res = evaluate_stock(stock_input, capital, risk_pct, slip_input)
        
        if res is None:
            st.error("標的解析失敗，請檢查代碼是否正確或 FinMind API 連線限制。")
        else:
            # 第一層：宏觀與戰略決策（最醒目的位置，點出全串聯結論）
            st.subheader("🎯 頂層戰略串聯裁決")
            
            # 使用醒目的區塊呈現大腦最終結論
            color_hex = {"red": "#FF4B4B", "purple": "#7D3CFF", "green": "#2BD9A1", "blue": "#1C86EE", "gray": "#808080"}[res["final_color"]]
            st.markdown(f"""
            <div style="background-color:{color_hex}22; border-left: 6px solid {color_hex}; padding: 15px; border-radius: 4px; margin-bottom: 20px;">
                <h3 style="margin:0; color:{color_hex};">{res['final_decision']}</h3>
                <p style="margin: 10px 0 0 0; color:#333; font-size:16px; font-weight:500;">{res['final_desc']}</p>
            </div>
            """, unsafe_allow_html=True)
            
            # 第二層：四維度因子現況
            st.subheader("📊 跨因子核心物理量校準")
            c1, c2, c3, c4 = st.columns(4)
            with c1: st.metric("大盤宏觀濾網", res["macro_desc"])
            with c2: st.metric("滾動 PE 位階 (近4季 EPS: " + f"{res['eps_4q']:.2f})", f"{res['pe_val']:.1f} 倍", res["pe_desc"])
            with c3: st.metric("台股投信金流", res["sitc_trend"])
            with c4: st.metric("散戶融資浮額", res["margin_trend"])
            
            st.markdown("---")
            
            # 第三層：風控與動態出場藍圖
            st.subheader("🛡️ 量化風控與動態出場藍圖 (Trading Blueprint)")
            
            if res["suggested_lots"] == 0:
                st.warning("⚠️ 核心大腦目前判定該標的處於『地雷劇本』或『严格避開區』，風控精算給予 0 張買進建議，請保持絕對空倉觀望。")
            
            b1, b2, b3, b4 = st.columns(4)
            with b1: st.metric("目前基準市價", f"{res['current_price']:.2f} 元")
            with b2: st.metric("建議最大進場配置", f"{res['suggested_lots']} 張", "風控自動加減碼後結果")
            with b3: st.metric("鐵板停損價位 (觸價即刻執行)", f"{res['stop_loss_price']:.2f} 元", "防守 MA20 減 ATR")
            with b4: st.metric("動態移動停利線", f"{res['trailing_stop_line']:.2f} 元", "最高價回撤 2.5*ATR")
            
            st.info("💡 **移動停利操作說明**：進場後，若股價持續創新高，請手動將移動停利線上移（新最高價 - 2.5 * ATR）。盤中若『收盤破』該條線，代表波段慣性改變，利多出盡，獲利落袋。")
