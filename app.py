import os
import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
import altair as alt
from datetime import datetime, timedelta
from FinMind.data import DataLoader

# ============ 1. Page Config & Setup ============
st.set_page_config(
    page_title="SOP v3.0 全方位操盤系統", 
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 自定義 CSS 優化視覺
st.markdown("""
<style>
    .metric-container {
        background-color: #f0f2f6;
        padding: 10px;
        border-radius: 5px;
    }
    .stAlert { padding: 0.5rem; }
</style>
""", unsafe_allow_html=True)

# ============ 2. 輔助函式 ============
def safe_float(x, default=None):
    try:
        if x is None or str(x).strip() in ["-", ""]:
            return default
        return float(x.replace(",", ""))
    except:
        return default

def tick_size(p: float) -> float:
    """台股跳動檔位"""
    if p >= 1000: return 5.0
    if p >= 500:  return 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.01
    return 0.001

def round_to_tick(x: float, t: float) -> float:
    return round(x / t) * t

# ============ 3. 權限認證 ============
APP_PASSWORD = os.getenv("APP_PASSWORD", "") or st.secrets.get("APP_PASSWORD", "")
if APP_PASSWORD:
    if "authed" not in st.session_state:
        st.session_state.authed = False
    if not st.session_state.authed:
        st.title("🔐 系統登入")
        c1, c2 = st.columns([2,1])
        with c1:
            pw = st.text_input("Access Password", type="password")
        if st.button("Login"):
            if pw == APP_PASSWORD:
                st.session_state.authed = True
                st.rerun()
        st.stop()

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")
if not FINMIND_TOKEN:
    st.error("⚠️ 系統缺少 FINMIND_TOKEN，無法獲取歷史數據。")
    st.stop()

# ============ 4. 主介面 ============
st.title("🦅 SOP v3.0 全方位操盤系統")
st.caption("大盤濾網 ｜ 籌碼過濾 ｜ 技術進攻 ｜ 基本面防禦")

with st.form("query_form"):
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        stock_id = st.text_input("股票代號", value="2330").strip()
    with col2:
        lookback_days = st.number_input("分析天數", value=365, min_value=100)
    with col3:
        submitted = st.form_submit_button("🚀 啟動分析", type="primary")

# ============ 5. 核心邏輯 ============
if submitted:
    if not stock_id.isdigit():
        st.error("❌ 代號格式錯誤")
        st.stop()

    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    short_start_date = (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d') # 籌碼抓短一點節省時間

    # --- Step 1: 抓取數據 (FinMind) ---
    with st.spinner("📡 正在建立戰情室數據 (大盤、個股、籌碼、營收)..."):
        try:
            api = DataLoader()
            api.login_by_token(FINMIND_TOKEN)

            # 1.1 大盤指數 (TAIEX) - 用於市場濾網
            df_index = api.taiwan_stock_daily(stock_id='TAIEX', start_date=start_date)
            
            # 1.2 個股價量
            df = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date)
            
            # 1.3 三大法人 (籌碼)
            df_inst = api.taiwan_stock_institutional_investors_buy_sell(stock_id=stock_id, start_date=short_start_date)
            
            # 1.4 融資融券 (籌碼)
            df_margin = api.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=short_start_date)
            
            # 1.5 月營收 (基本面) - 抓最近一年
            df_rev = api.taiwan_stock_month_revenue(stock_id=stock_id, start_date=(datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d'))

            if df is None or len(df) < 60:
                st.error("❌ 個股歷史資料不足，無法分析。")
                st.stop()

        except Exception as e:
            st.error(f"FinMind API 連線失敗: {e}")
            st.stop()

    # --- Step 2: 數據前處理與指標計算 ---
    
    # 2.1 大盤指標
    market_trend = "未知"
    market_ma20 = 0
    index_5d_change = 0
    
    if df_index is not None and not df_index.empty:
        df_index["close"] = pd.to_numeric(df_index["close"])
        df_index["MA20"] = df_index["close"].rolling(20).mean()
        last_idx = df_index.iloc[-1]
        market_ma20 = last_idx["MA20"]
        idx_price = last_idx["close"]
        
        # 判斷多空
        market_trend = "多頭 (Bull)" if idx_price > market_ma20 else "空頭 (Bear)"
        
        # 計算近5日漲幅 (用於 RS 比較)
        if len(df_index) > 5:
            prev_idx = df_index.iloc[-6]["close"]
            index_5d_change = ((idx_price - prev_idx) / prev_idx) * 100

    # 2.2 個股技術指標
    df = df.rename(columns={"Trading_Volume": "vol", "Trading_Money": "amount", "close": "close", "max": "high", "min": "low"})
    cols = ["close", "high", "low", "vol", "amount"]
    for c in cols: df[c] = pd.to_numeric(df[c], errors='coerce')

    df["MA20"] = df["close"].rolling(20).mean()
    df["MA60"] = df["close"].rolling(60).mean()
    
    # ATR
    df["H-L"]  = df["high"] - df["low"]
    df["H-PC"] = (df["high"] - df["close"].shift(1)).abs()
    df["L-PC"] = (df["low"] - df["close"].shift(1)).abs()
    df["TR"]   = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
    df["ATR14"] = df["TR"].rolling(14).mean()

    # OBV
    df['change'] = df['close'].diff()
    df['direction'] = np.where(df['change'] > 0, 1, np.where(df['change'] < 0, -1, 0))
    df['OBV'] = (df['direction'] * df['vol']).cumsum()
    df['OBV_MA10'] = df['OBV'].rolling(10).mean()
    
    # 成交額 (億)
    df["Amount_Yi"] = df["amount"] / 1e8
    df["MA20_Amount"] = df["Amount_Yi"].rolling(20).mean()

    # 歷史參考點
    hist_last = df.iloc[-1]
    ref_price = float(hist_last["close"])
    ref_obv = float(hist_last["OBV"])
    atr = float(hist_last["ATR14"])
    ma20 = float(hist_last["MA20"])
    high_52w = float(df.tail(252)["high"].max())
    avg_amount = float(hist_last["MA20_Amount"])

    # 2.3 籌碼指標 (投信/融資)
    trust_5d_net = 0
    margin_change_1d = 0
    
    if df_inst is not None and not df_inst.empty:
        # 投信
        trust = df_inst[df_inst['name'] == 'Investment_Trust'].copy()
        if not trust.empty:
            trust['net'] = (trust['buy'] - trust['sell']) / 1000
            trust_5d_net = trust.tail(5)['net'].sum()
            
    if df_margin is not None and not df_margin.empty:
        # 融資
        df_margin['MarginPurchaseLimit'] = pd.to_numeric(df_margin['MarginPurchaseLimit'])
        margin_change_1d = df_margin['MarginPurchaseLimit'].diff().iloc[-1]

    # 2.4 基本面指標 (營收)
    rev_yoy = 0
    rev_mom = 0
    if df_rev is not None and not df_rev.empty:
        last_rev = df_rev.iloc[-1]
        rev_yoy = float(last_rev.get('revenue_year_growth_rate', 0))
        rev_mom = float(last_rev.get('revenue_month_growth_rate', 0))

    # --- Step 3: 即時報價 (MIS) 優先策略 ---
    rt_success = False
    current_price = ref_price
    current_vol = 0
    data_source = "FinMind 歷史收盤"
    
    try:
        ts = int(time.time() * 1000)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
        r = requests.get(url, timeout=3)
        data = r.json()
        if "msgArray" in data and len(data["msgArray"]) > 0:
            info = data["msgArray"][0]
            z = safe_float(info.get("z"))
            y = safe_float(info.get("y")) # 昨日收盤
            v = safe_float(info.get("v"))
            
            if z and z > 0:
                current_price = z
                current_vol = v or 0
                rt_success = True
                data_source = "🟢 MIS 盤中即時"
            elif y:
                current_price = y
                rt_success = True
                data_source = "🟡 MIS (未成交/盤前)"
    except:
        pass

    # --- Step 4: 綜合計算與訊號判定 ---
    
    # 4.1 動態 OBV
    if rt_success:
        if current_price > ref_price:
            final_obv = ref_obv + current_vol
        elif current_price < ref_price:
            final_obv = ref_obv - current_vol
        else:
            final_obv = ref_obv
    else:
        final_obv = ref_obv

    # 4.2 乖離率 (Bias)
    bias_20 = ((current_price - ma20) / ma20) * 100
    
    # 4.3 相對強度 (RS) - 比較近5日
    if len(df) > 6:
        prev_stock = float(df.iloc[-6]["close"])
        stock_5d_change = ((current_price - prev_stock) / prev_stock) * 100
    else:
        stock_5d_change = 0
        
    is_stronger = stock_5d_change > index_5d_change

    # 4.4 關鍵價位
    t = tick_size(current_price)
    pivot = high_52w
    breakout_entry = round_to_tick(pivot + max(0.2 * atr, t), t)
    pb_low  = round_to_tick(max(ma20, current_price - 0.8 * atr), t)
    pb_high = round_to_tick(max(pb_low, current_price - 0.2 * atr), t)
    
    # --- Step 5: UI 儀表板 ---
    
    # 5.1 市場雷達
    st.markdown("### 📡 戰場環境 (Market Context)")
    m1, m2, m3, m4 = st.columns(4)
    
    idx_color = "red" if market_trend == "多頭 (Bull)" else "green"
    m1.metric("大盤趨勢", market_trend, delta=f"MA20: {market_ma20:.0f}", delta_color="off")
    
    rs_label = "強於大盤 🔥" if is_stronger else "弱於大盤 ❄️"
    m2.metric("相對強度 (RS)", rs_label, delta=f"個股 {stock_5d_change:.1f}% vs 大盤 {index_5d_change:.1f}%")
    
    bias_alert = "過熱 ⚠️" if bias_20 > 20 else "正常"
    m3.metric("乖離率 (Bias)", f"{bias_20:.1f}%", delta=bias_alert, delta_color="inverse")
    
    liq_alert = "流動性不足 ⚠️" if avg_amount < 0.5 else "充沛"
    m4.metric("日均成交額", f"{avg_amount:.1f} 億", delta=liq_alert)

    st.divider()

    # 5.2 個股戰情
    st.subheader(f"📊 {stock_id} 綜合分析 (現價 {current_price})")
    
    # 訊號燈邏輯
    signals = []
    
    # A. 籌碼面
    chip_score = 0
    if trust_5d_net > 500: 
        signals.append("✅ 投信護盤")
        chip_score += 1
    if margin_change_1d < 0: 
        signals.append("✅ 融資退場 (安定)")
        chip_score += 1
    elif margin_change_1d > 1000:
        signals.append("❌ 融資暴增 (散戶多)")
        chip_score -= 1
        
    # B. 技術面
    tech_score = 0
    obv_up = final_obv > float(hist_last["OBV_MA10"])
    if obv_up: 
        signals.append("✅ OBV 多頭排列")
        tech_score += 1
    else:
        signals.append("⚠️ OBV 背離/轉弱")
        
    if current_price > ma20: tech_score += 1
    
    # C. 基本面
    fund_score = 0
    if rev_yoy > 20: 
        signals.append("✅ 營收高成長 (>20%)")
        fund_score += 1
    elif rev_yoy < -10:
        signals.append("❌ 營收衰退")
        fund_score -= 1

    # 總結建議
    if market_trend == "空頭 (Bear)" and not is_stronger:
        final_action = "空手觀望 (大盤差 + 個股弱)"
        action_color = "gray"
    elif bias_20 > 20:
        final_action = "禁止追價 (乖離過大)"
        action_color = "orange"
    elif current_price >= breakout_entry and chip_score >= 1 and obv_up:
        final_action = "🔥 狙擊進攻 (突破 + 籌碼/量能確認)"
        action_color = "red"
    elif pb_low <= current_price <= pb_high and ma20 < current_price:
        final_action = "🟢 拉回布局 (Pullback + 支撐確認)"
        action_color = "green"
    else:
        final_action = "觀察等待"
        action_color = "blue"

    st.markdown(f"#### 🤖 系統指令：:{action_color}[**{final_action}**]")
    
    # 顯示詳細訊號
    with st.expander("🔍 查看詳細診斷訊號"):
        for s in signals:
            st.write(s)

    # 5.3 關鍵數據列
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("投信近5日", f"{int(trust_5d_net)} 張", delta_color="normal" if trust_5d_net>0 else "inverse")
    k2.metric("融資單日增減", f"{int(margin_change_1d)} 張", delta_color="inverse") # 融資增顯示紅(警告)
    k3.metric("月營收 YoY", f"{rev_yoy:.1f}%", delta="基本面動能")
    k4.metric("OBV 狀態", "多頭" if obv_up else "空頭", delta=f"預估 {int(final_obv):,}")

    # 5.4 圖表 (Price vs OBV)
    chart_df = df.tail(120).copy()
    chart_df["date"] = pd.to_datetime(chart_df["date"])
    
    base = alt.Chart(chart_df).encode(x='date:T')
    
    # 價格線 (藍色)
    line_p = base.mark_line(color='#2962FF').encode(
        y=alt.Y('close:Q', scale=alt.Scale(zero=False), title='股價'),
        tooltip=['date', 'close', 'vol']
    )
    
    # OBV線 (橘色)
    line_o = base.mark_line(color='#FF6D00').encode(
        y=alt.Y('OBV:Q', scale=alt.Scale(zero=False), title='OBV'),
        tooltip=['date', 'OBV']
    )
    
    c_layer = alt.layer(line_p, line_o).resolve_scale(y='independent').properties(height=350)
    st.altair_chart(c_layer, use_container_width=True)

    # 5.5 交易計畫
    tab1, tab2 = st.tabs(["⚔️ 交易計畫書", "📝 原始數據"])
    
    with tab1:
        c_left, c_right = st.columns(2)
        with c_left:
            st.info("### 🟢 Pullback (拉回買進)")
            st.write(f"**進場區間**: `{pb_low}` ~ `{pb_high}`")
            st.write(f"**停損價 (Stop)**: `{round_to_tick(pb_low - 1.2*atr, t)}`")
            st.caption("條件：大盤非空頭，且股價回到 MA20 附近縮量不破。")
            
        with c_right:
            st.error("### 🔴 Breakout (突破買進)")
            st.write(f"**突破觸發價**: `{breakout_entry}`")
            st.write(f"**停損價 (Stop)**: `{round_to_tick(breakout_entry - 1.0*atr, t)}`")
            st.write(f"**目標價 (TP1)**: `{round_to_tick(breakout_entry + 2.0*atr, t)}`")
            st.caption("條件：必須帶量 (OBV創新高) 且籌碼安定。")

    with tab2:
        st.write("最新 5 筆交易數據")
        st.dataframe(df.tail(5)[['date', 'close', 'vol', 'amount', 'MA20', 'OBV', 'ATR14']])
        st.write("最新籌碼數據")
        if df_inst is not None: st.dataframe(df_inst.tail(3))

