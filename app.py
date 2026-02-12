import os
import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
import altair as alt
from datetime import datetime, timedelta
import pytz  # 用於精確處理台北時區
from FinMind.data import DataLoader

# ============ 1. Page Config ============
st.set_page_config(page_title="SOP v3.3 全方位自動判斷系統", layout="wide")

# ============ 2. 台北時區與市場狀態判斷 ============
def get_market_status():
    """
    判斷台北市場目前狀態
    回傳: (狀態代碼, 狀態名稱, 提示顏色)
    """
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    weekday = now.weekday()  # 0=Mon, 6=Sun
    current_time = now.time()
    
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("13:35", "%H:%M").time() # 包含最後撮合

    if weekday >= 5:
        return "WEEKEND", "市場休市 (週末)", "gray"
    elif current_time < start_time:
        return "PRE_MARKET", "盤前準備中", "blue"
    elif start_time <= current_time <= end_time:
        return "OPEN", "市場交易中 (即時更新)", "red"
    else:
        return "POST_MARKET", "今日已收盤", "green"

# ============ 3. 輔助函式 ============
def safe_float(x, default=None):
    try:
        if x is None or str(x).strip() in ["-", ""]: return default
        return float(str(x).replace(",", ""))
    except: return default

def tick_size(p: float) -> float:
    if p >= 1000: return 5.0
    if p >= 500:  return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.01
    return 0.001

def round_to_tick(x: float, t: float) -> float:
    return round(x / t) * t

# ============ 4. 權限認證 ============
APP_PASSWORD = os.getenv("APP_PASSWORD", "") or st.secrets.get("APP_PASSWORD", "")
if APP_PASSWORD:
    if "authed" not in st.session_state: st.session_state.authed = False
    if not st.session_state.authed:
        st.title("🔐 系統登入")
        pw = st.text_input("Access Password", type="password")
        if st.button("Login"):
            if pw == APP_PASSWORD:
                st.session_state.authed = True
                st.rerun()
        st.stop()

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

# ============ 5. 主介面 ============
market_code, market_desc, market_color = get_market_status()
st.title("🦅 SOP v3.3 全方位操盤系統")
st.subheader(f"市場狀態：:{market_color}[{market_desc}]")

with st.form("query_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        stock_id = st.text_input("股票代號", value="2330").strip()
    with col2:
        submitted = st.form_submit_button("啟動分析", type="primary")

# ============ 6. 核心邏輯 ============
if submitted:
    if not stock_id.isdigit():
        st.error("❌ 代號格式錯誤")
        st.stop()

    with st.spinner("正在同步全球數據與市場狀態..."):
        try:
            api = DataLoader()
            api.login_by_token(FINMIND_TOKEN)
            
            # 1. 抓取歷史 (個股與大盤)
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            df = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
            df_index = api.taiwan_stock_daily(stock_id='TAIEX', start_date=start_date)
            
            # 2. 抓取籌碼與基本面
            short_start = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
            df_inst = api.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=short_start)
            df_margin = api.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=short_start)
            df_rev = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d'))

            if df is None or len(df) < 60:
                st.error("❌ 無法取得足夠的歷史資料")
                st.stop()

            # --- 數據清洗：過濾掉成交量為 0 的非交易日 ---
            df.columns = [c.strip() for c in df.columns]
            df = df.rename(columns={"Trading_Volume": "vol", "Trading_Money": "amount", "close": "close", "max": "high", "min": "low"})
            df = df[df['vol'] > 0].copy() # 確保歷史計算不被假日干擾
            
            for c in ["close", "high", "low", "vol", "amount"]:
                df[c] = pd.to_numeric(df[c], errors='coerce')

            # --- 基礎指標計算 ---
            df["MA20"] = df["close"].rolling(20).mean()
            df["Amount_Yi"] = df["amount"] / 1e8
            df["MA20_Amount"] = df["Amount_Yi"].rolling(20).mean()
            
            # OBV
            df['change'] = df['close'].diff()
            df['direction'] = np.where(df['change'] > 0, 1, np.where(df['change'] < 0, -1, 0))
            df['OBV'] = (df['direction'] * df['vol']).cumsum()
            df['OBV_MA10'] = df['OBV'].rolling(10).mean()
            
            # ATR
            df["H-L"] = df["high"] - df["low"]
            df["H-PC"] = (df["high"] - df["close"].shift(1)).abs()
            df["L-PC"] = (df["low"] - df["close"].shift(1)).abs()
            df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
            df["ATR14"] = df["TR"].rolling(14).mean()

            # 歷史最後一筆 (作為休市時的參考)
            hist_last = df.iloc[-1]
            
        except Exception as e:
            st.error(f"數據抓取失敗: {e}")
            st.stop()

    # --- Step 7: 自動判斷數據源 (核心大腦) ---
    rt_success = False
    current_price = float(hist_last["close"])
    current_vol = 0
    data_source_label = "歷史收盤數據"

    # 只有在非週末時，才去嘗試 MIS
    if market_code != "WEEKEND":
        try:
            ts = int(time.time() * 1000)
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
            r = requests.get(url, timeout=3)
            info = r.json().get("msgArray", [])[0]
            
            z = safe_float(info.get("z")) # 現價
            v = safe_float(info.get("v")) # 今日量
            y = safe_float(info.get("y")) # 昨收
            
            if z and z > 0:
                current_price = z
                current_vol = v or 0
                rt_success = True
                data_source_label = "即時報價系統"
            elif y:
                current_price = y
                rt_success = True
                data_source_label = "即時系統 (參考昨收)"
        except:
            pass

    # --- Step 8: 最終數據融合 ---
    # 如果是休市或 MIS 失敗，成交量強制使用「最後一個交易日」的量，避免日均量判斷錯誤
    if not rt_success or current_vol == 0:
        final_vol = float(hist_last["vol"])
        final_amount_yi = float(hist_last["Amount_Yi"])
        final_obv = float(hist_last["OBV"])
    else:
        final_vol = current_vol
        final_amount_yi = (current_price * current_vol * 1000) / 1e8
        # 即時 OBV 計算
        if current_price > float(hist_last["close"]):
            final_obv = float(hist_last["OBV"]) + current_vol
        elif current_price < float(hist_last["close"]):
            final_obv = float(hist_last["OBV"]) - current_vol
        else:
            final_obv = float(hist_last["OBV"])

    # --- Step 9: 指標判定與 UI 顯示 ---
    ma20 = float(hist_last["MA20"])
    avg_amount_20 = float(hist_last["MA20_Amount"])
    atr = float(hist_last["ATR14"])
    high_52w = float(df.tail(252)["high"].max())
    bias_20 = ((current_price - ma20) / ma20) * 100
    
    # 策略點位
    t = tick_size(current_price)
    breakout_entry = round_to_tick(high_52w + max(0.2 * atr, t), t)
    pb_low = round_to_tick(max(ma20, current_price - 0.8 * atr), t)
    pb_high = round_to_tick(max(pb_low, current_price - 0.2 * atr), t)

    # UI 呈現
    st.divider()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("目前價格", f"{current_price}", delta=f"{round(current_price - float(hist_last['close']), 2)}")
    c2.metric("數據來源", data_source_label)
    c3.metric("日均成交額 (20D)", f"{avg_amount_20:.2f} 億")
    c4.metric("乖離率 (Bias)", f"{bias_20:.1f}%")

    # 綜合診斷 (自動避開假日誤判)
    is_liquid = avg_amount_20 >= 0.5
    obv_up = final_obv > float(hist_last["OBV_MA10"])
    
    if market_code == "WEEKEND":
        status_msg = "休市中：基於最後交易日分析"
        status_color = "blue"
    elif current_price >= breakout_entry and obv_up:
        status_msg = "🔥 強勢突破中"
        status_color = "red"
    elif pb_low <= current_price <= pb_high:
        status_msg = "🟢 處於 Pullback 買進區"
        status_color = "green"
    else:
        status_msg = "🟡 盤整觀察中"
        status_color = "orange"

    st.info(f"### 系統診斷：:{status_color}[**{status_msg}**]")

    # 圖表與交易計畫 (同前版本...)
    st.markdown("### 📈 走勢與 OBV 觀測")
    chart_df = df.tail(100).copy()
    chart_df["date"] = pd.to_datetime(chart_df["date"])
    base = alt.Chart(chart_df).encode(x='date:T')
    line_p = base.mark_line(color='#2962FF').encode(y=alt.Y('close:Q', scale=alt.Scale(zero=False)))
    line_o = base.mark_line(color='#FF6D00').encode(y=alt.Y('OBV:Q', scale=alt.Scale(zero=False)))
    st.altair_chart(alt.layer(line_p, line_o).resolve_scale(y='independent').interactive(), use_container_width=True)

    t1, t2 = st.tabs(["⚔️ 交易計畫", "📋 籌碼數據"])
    with t1:
        col_a, col_b = st.columns(2)
        with col_a:
            st.success(f"**拉回買進區**: {pb_low} ~ {pb_high}")
        with col_b:
            st.error(f"**突破進場點**: {breakout_entry}")
    with t2:
        if df_inst is not None: st.write("最近法人動態"), st.dataframe(df_inst.tail(5))
