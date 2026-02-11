import streamlit as st
from FinMind.data import DataLoader
import pandas as pd
import requests

st.set_page_config(page_title="台股交易決策系統", layout="wide")
st.title("📈 台股自動交易決策系統")

token = st.text_input("輸入 FinMind Token", type="password")
stock_id = st.text_input("輸入股票代號")

def pick_col(df: pd.DataFrame, candidates: list[str]) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    raise KeyError(f"找不到欄位：{candidates}，目前欄位={list(df.columns)}")

if st.button("查詢") and stock_id and token:
    try:
        api = DataLoader()
        api.login_by_token(token)

        df = api.taiwan_stock_daily(stock_id=stock_id, start_date="2023-01-01")

        if df is None or len(df) < 60:
            st.error("歷史資料不足（少於60筆），無法計算 MA/ATR。")
            st.stop()

        close_col = pick_col(df, ["close", "Close"])
        high_col  = pick_col(df, ["max", "high", "High"])
        low_col   = pick_col(df, ["min", "low", "Low"])

        df["MA20"] = df[close_col].rolling(20).mean()
        df["MA50"] = df[close_col].rolling(50).mean()

        df["H-L"]  = df[high_col] - df[low_col]
        df["H-PC"] = (df[high_col] - df[close_col].shift(1)).abs()
        df["L-PC"] = (df[low_col] - df[close_col].shift(1)).abs()
        df["TR"]   = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
        df["ATR14"] = df["TR"].rolling(14).mean()

        latest = df.iloc[-1]
        high_52w = df.tail(252)[high_col].max()

        st.subheader("📊 歷史技術資料")
        col1, col2, col3 = st.columns(3)
        col1.metric("最新收盤", float(latest[close_col]))
        col2.metric("MA20", float(round(latest["MA20"], 2)))
        col3.metric("MA50", float(round(latest["MA50"], 2)))
        st.write("ATR14:", float(round(latest["ATR14"], 2)))
        st.write("52週高:", float(high_52w))

        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0"
        r = requests.get(url, timeout=10)
        data = r.json()

        if "msgArray" in data and len(data["msgArray"]) > 0:
            info = data["msgArray"][0]
            st.subheader("⚡ 即時資料")
            st.write(f"即時價: {info.get('z')} | 成交量: {info.get('v')} | 時間: {info.get('d')} {info.get('t')}")

            price = float(info["z"])
            atr = float(latest["ATR14"])

            pivot = float(high_52w)
            stop = price - atr
            tp1 = price + atr * 2
            tp2 = price + atr * 4

            st.subheader("🎯 自動交易建議")
            st.success(f"Pivot (壓力位): {pivot:.2f}")
            st.warning(f"停損位 (Stop Loss): {stop:.2f}")
            st.info(f"獲利目標 TP1: {tp1:.2f} | TP2: {tp2:.2f}")
        else:
            st.error("找不到該股票的即時資料，請確認代號是否正確。")

    except Exception as e:
        st.error(f"發生錯誤: {type(e).__name__}: {e}")
