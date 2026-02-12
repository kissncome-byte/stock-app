import os
import requests
import pandas as pd
import streamlit as st
from FinMind.data import DataLoader

# ============ 1. Page Config (必須是第一個 st 指令) ============
st.set_page_config(page_title="SOP v1.1（進攻型 2–8 週）", layout="wide")

# ============ 2. 輔助函式 ============
def safe_float(x, default=None):
    """安全轉換浮點數，失敗回傳 default"""
    try:
        return float(x)
    except:
        return default

def estimate_turnover_yi(price: float, vol_lot: float) -> float:
    """估算成交額（億）：價格 * 張數 * 1000 / 1億"""
    return (price * vol_lot * 1000.0) / 1e8

def tick_size(p: float) -> float:
    """台股跳動檔位規則"""
    if p >= 1000: return 5.0  # 註：台股千元以上跳動通常是5元
    if p >= 500:  return 1.0  # 修正：500-1000 跳動為 1.0
    if p >= 100:  return 0.5
    if p >= 50:   return 0.1
    if p >= 10:   return 0.01
    return 0.001

def round_to_tick(x: float, t: float) -> float:
    """將價格四雪五入到最近的檔位"""
    return round(x / t) * t

# ============ 3. 權限認證 (Login) ============
# 從環境變數或 Streamlit Secrets 讀取密碼，若未設定則預設為空（不鎖）
APP_PASSWORD = os.getenv("APP_PASSWORD", "") or st.secrets.get("APP_PASSWORD", "")

if APP_PASSWORD:
    if "authed" not in st.session_state:
        st.session_state.authed = False

    if not st.session_state.authed:
        st.title("🔐 存取保護")
        col1, col2 = st.columns([2, 1])
        with col1:
            pw = st.text_input("請輸入密碼", type="password")
        if st.button("登入"):
            if pw == APP_PASSWORD:
                st.session_state.authed = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        st.stop()

# ============ 4. 設定與 Token 檢查 ============
# 優先讀取環境變數，其次讀取 st.secrets
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "") or st.secrets.get("FINMIND_TOKEN", "")

if not FINMIND_TOKEN:
    st.error("⚠️ 系統缺少 FINMIND_TOKEN。請在環境變數或 .streamlit/secrets.toml 中設定。")
    st.info("申請網址: https://finmind.github.io/")
    st.stop()

# ============ 5. 主介面 UI ============
st.title("📈 SOP v1.1 交易決策（進攻型｜2–8 週）")
st.caption("結合 FinMind 歷史數據與 TWSE 盤中即時資訊")

with st.form("query_form"):
    col_input, col_btn = st.columns([3, 1])
    with col_input:
        stock_id = st.text_input("股票代號", value="2330", placeholder="例如：2330").strip()
    with col_btn:
        submitted = st.form_submit_button("開始分析", type="primary")

# ============ 6. 核心邏輯 ============
if submitted:
    if not stock_id.isdigit():
        st.error("❌ 代號格式不正確（請輸入純數字，如 2330）")
        st.stop()

    with st.spinner(f"正在抓取 {stock_id} 數據..."):
        # --------- A. History (FinMind) ---------
        try:
            api = DataLoader()
            api.login_by_token(FINMIND_TOKEN)
            # 抓取足夠長的時間以計算 MA50 和 52週高點
            df = api.taiwan_stock_daily(stock_id=stock_id, start_date="2023-01-01")
        except Exception as e:
            st.error(f"FinMind API 連線失敗: {str(e)}")
            st.stop()

        if df is None or len(df) < 260:
            st.error(f"❌ 歷史資料不足（目前 {len(df) if df is not None else 0} 筆，需至少 260 筆），無法計算 52W高/MA/ATR。")
            st.stop()

        # 欄位名稱標準化處理
        close_col = "close"
        high_col = "max" if "max" in df.columns else ("high" if "high" in df.columns else None)
        low_col  = "min" if "min" in df.columns else ("low" if "low" in df.columns else None)
        
        if high_col is None or low_col is None:
            st.error(f"資料欄位異常。現有欄位：{list(df.columns)}")
            st.stop()

        # 計算指標
        df["MA20"] = df[close_col].rolling(20).mean()
        df["MA50"] = df[close_col].rolling(50).mean()

        # ATR 計算
        df["H-L"]  = df[high_col] - df[low_col]
        df["H-PC"] = (df[high_col] - df[close_col].shift(1)).abs()
        df["L-PC"] = (df[low_col] - df[close_col].shift(1)).abs()
        df["TR"]   = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
        df["ATR14"] = df["TR"].rolling(14).mean()

        # 取得最新歷史數據
        latest = df.iloc[-1]
        ma20 = safe_float(latest["MA20"])
        ma50 = safe_float(latest["MA50"])
        atr = safe_float(latest["ATR14"])
        last_close = safe_float(latest[close_col])
        
        # 計算 52週高點 (約 252 個交易日)
        high_52w = float(df.tail(252)[high_col].max())

        # --------- B. Realtime (TWSE MIS) ---------
        rt_price = None
        rt_vol = None
        rt_date = rt_time = None
        
        try:
            # 隨機數是為了避免快取
            import time
            ts = int(time.time() * 1000)
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0&_={ts}"
            r = requests.get(url, timeout=5)
            data = r.json()
            if "msgArray" in data and len(data["msgArray"]) > 0:
                info = data["msgArray"][0]
                rt_price = safe_float(info.get("z")) # z: 最近成交價
                if rt_price is None: # 如果沒有成交價，嘗試取收盤價 y
                     rt_price = safe_float(info.get("y"))
                
                rt_vol = safe_float(info.get("v"))  # 累積成交量
                rt_date = info.get("d")
                rt_time = info.get("t")
        except:
            st.warning("⚠️ 無法連線至證交所即時報價，將使用昨日收盤價計算。")

        # --------- C. Decide Price Mode ---------
        if rt_price is not None:
            # 判斷是否為收盤 (13:30 後通常視為收盤，或是看 z 是否等於 y)
            is_close = (rt_time >= "13:30:00")
            used_price = rt_price
            data_time = f"{rt_date} {rt_time}"
            data_type = "盤中即時價" if not is_close else "今日收盤價"
            turnover_yi = estimate_turnover_yi(rt_price, rt_vol or 0.0) if rt_vol is not None else None
        else:
            used_price = last_close
            data_time = f"{latest['date']} (歷史日K)"
            data_type = "昨日收盤價"
            turnover_yi = None

        t = tick_size(used_price)

        # --------- D. Strategy (攻擊型) ---------
        pivot = high_52w

        # Breakout 計算
        breakout_entry = round_to_tick(pivot + max(0.2 * atr, t), t)
        breakout_stop  = round_to_tick(breakout_entry - 1.0 * atr, t)
        tp1 = round_to_tick(breakout_entry + 2.0 * atr, t)
        tp2 = round_to_tick(breakout_entry + 3.0 * atr, t)
        tp3 = round_to_tick(breakout_entry + 4.0 * atr, t)

        # Pullback 計算
        pb_low  = round_to_tick(max(ma20, used_price - 0.8 * atr), t)
        pb_high = round_to_tick(max(pb_low, used_price - 0.2 * atr), t)
        pb_stop = round_to_tick(pb_low - 1.2 * atr, t)
        pb_tp1  = round_to_tick(pivot, t)
        pb_tp2  = tp1
        pb_tp3  = tp2

        # 判斷動作
        action_color = "gray"
        if used_price < pb_low:
            action = "🔵 觀察（低於 Pullback 區下緣，不追）"
            action_color = "blue"
        elif pb_low <= used_price <= pb_high:
            action = "🟢 可小倉 Pullback 試單（在區間內）"
            action_color = "green"
        elif used_price < breakout_entry:
            action = "🟡 等待觸發（不追價；等待 Pullback 或 突破）"
            action_color = "orange"
        else:
            action = "🔴 突破已觸發（依 Breakout 方案執行）"
            action_color = "red"

        # --------- E. UI Output ---------
        st.divider()
        st.subheader(f"📊 分析結果：{stock_id} (現價 {used_price})")
        
        # 狀態指標列
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("ATR14 (波動度)", f"{atr:.2f}")
        c2.metric("52週前高 (Pivot)", f"{high_52w:.2f}")
        c3.metric("MA20", f"{ma20:.2f}", delta=round(used_price-ma20, 2))
        c4.metric("MA50", f"{ma50:.2f}", delta=round(used_price-ma50, 2))

        # 詳細數據與建議
        st.info(f"💡 系統建議：**:{action_color}[{action}]**")
        st.caption(f"數據時間：{data_time} | 資料來源：{data_type}")

        tab1, tab2 = st.tabs(["🚀 進攻計畫", "📋 原始數據"])

        with tab1:
            col_l, col_r = st.columns(2)
            with col_l:
                st.markdown("### ① Pullback (拉回買進)")
                st.markdown(f"""
                - **Entry 區間**: `{pb_low:.2f}` ~ `{pb_high:.2f}`
                - **停損 (Stop)**: `{pb_stop:.2f}`
                - **目標 (TP)**: 
                    1. `{pb_tp1:.2f}`
                    2. `{pb_tp2:.2f}`
                """)
            
            with col_r:
                st.markdown("### ② Breakout (突破買進)")
                st.markdown(f"""
                - **觸發價 (Entry)**: `{breakout_entry:.2f}`
                - **停損 (Stop)**: `{breakout_stop:.2f}`
                - **目標 (TP)**: 
                    1. `{tp1:.2f}`
                    2. `{tp2:.2f}`
                    3. `{tp3:.2f}`
                """)

        with tab2:
            st.json({
                "價格": used_price,
                "MA20": ma20,
                "MA50": ma50,
                "ATR": atr,
                "52W High": high_52w,
                "成交量(張)": rt_vol,
                "估計成交額(億)": turnover_yi
            })
