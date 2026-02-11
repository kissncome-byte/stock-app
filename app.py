import streamlit as st
from FinMind.data import DataLoader
import pandas as pd
import requests

st.set_page_config(page_title="台股交易決策系統", layout="wide")

st.title("📈 台股自動交易決策系統")

token = st.text_input("輸入 FinMind Token", type="password")
stock_id = st.text_input("輸入股票代號")

if st.button("查詢") and stock_id and token:

    api = DataLoader()

    df = api.taiwan_stock_daily(
        stock_id=stock_id,
        start_date="2023-01-01",
        token=token
    )

    df["MA20"] = df["close"].rolling(20).mean()
    df["MA50"] = df["close"].rolling(50).mean()

    df["H-L"] = df["high"] - df["low"]
    df["H-PC"] = abs(df["high"] - df["close"].shift(1))
    df["L-PC"] = abs(df["low"] - df["close"].shift(1))
    df["TR"] = df[["H-L","H-PC","L-PC"]].max(axis=1)
    df["ATR14"] = df["TR"].rolling(14).mean()

    latest = df.iloc[-1]
    high_52w = df.tail(252)["high"].max()

    st.subheader("📊 歷史技術資料")
    st.write("最新收盤:", latest["close"])
    st.write("MA20:", round(latest["MA20"],2))
    st.write("MA50:", round(latest["MA50"],2))
    st.write("ATR14:", round(latest["ATR14"],2))
    st.write("52週高:", high_52w)

    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw&json=1&delay=0"
        r = requests.get(url)
        data = r.json()
        info = data["msgArray"][0]

        st.subheader("⚡ 即時資料")
        st.write("即時價:", info["z"])
        st.write("成交量:", info["v"])
        st.write("資料時間:", info["d"], info["t"])

        price = float(info["z"])
        atr = latest["ATR14"]

        pivot = high_52w
        stop = price - atr
        tp1 = price + atr * 2
        tp2 = price + atr * 4

        st.subheader("🎯 自動交易建議")
        st.write("Pivot:", pivot)
        st.write("停損:", round(stop,2))
        st.write("TP1:", round(tp1,2))
        st.write("TP2:", round(tp2,2))

    except:
        st.error("即時資料抓取失敗")
