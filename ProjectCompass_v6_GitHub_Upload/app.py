from __future__ import annotations
import pandas as pd
import streamlit as st
from data.twse import fetch_daily_prices, read_price_csv, DataError
from analysis.indicators import prepare_indicators
from agents.trend_agent import run_trend_agent
from agents.money_agent import run_money_agent
from agents.risk_agent import run_risk_agent
from agents.compass_agent import run_compass
from ui.components import render_dashboard, render_agent

st.set_page_config(page_title="Project Compass v6.0", layout="wide")
st.markdown("""
<style>
.block-container{max-width:1250px;padding-top:2rem}.hero{background:#0f172a;color:white;padding:28px;border-radius:18px;margin:10px 0 22px}.eyebrow{font-size:13px;opacity:.7;font-weight:700}.hero-title{font-size:30px;font-weight:900;margin:8px 0}.hero-sub{font-size:15px;line-height:1.7;opacity:.9}.metric-card{background:white;border:1px solid #e2e8f0;border-radius:14px;padding:18px;min-height:130px;box-shadow:0 2px 10px rgba(15,23,42,.04)}.metric-label{font-size:13px;color:#64748b;font-weight:700}.metric-value{font-size:22px;color:#0f172a;font-weight:900;margin-top:8px}.metric-help{font-size:12px;color:#64748b;margin-top:7px;line-height:1.4}
</style>""", unsafe_allow_html=True)

with st.sidebar:
    st.header("Project Compass")
    stock_id = st.text_input("股票代碼", "2330")
    stock_name = st.text_input("股票名稱", "")
    is_holding = st.checkbox("我已持有這檔股票")
    expanded = st.checkbox("預設展開數據依據", False)
    source = st.radio("資料來源", ["證交所自動下載", "上傳CSV"])
    uploaded = st.file_uploader("CSV需包含 date/open/high/low/close/volume", type=["csv"]) if source == "上傳CSV" else None
    run = st.button("開始分析", type="primary", use_container_width=True)

st.title("Project Compass v6.0")
st.caption("先回答、再解釋；每個結論都可以展開查看數據。")

if run:
    try:
        with st.spinner("正在整理資料與建立交易計畫…"):
            raw = read_price_csv(uploaded) if source == "上傳CSV" and uploaded else fetch_daily_prices(stock_id)
            if len(raw) < 80:
                st.warning("資料少於80個交易日，長期均線與波段判斷可信度會下降。")
            df = prepare_indicators(raw)
            trend = run_trend_agent(df)
            money = run_money_agent(df)
            risk, plan = run_risk_agent(df, is_holding=is_holding)
            decision = run_compass(stock_id, stock_name or "", float(df.iloc[-1]["close"]), [trend, money, risk], plan)
        render_dashboard(decision, expanded)
        st.markdown("## 為什麼？")
        for agent in decision.agents:
            render_agent(agent, expanded)
        st.caption(decision.disclaimer)
    except (DataError, ValueError) as exc:
        st.error(str(exc))
    except Exception as exc:
        st.exception(exc)
else:
    st.info("輸入股票代碼後按「開始分析」。上櫃股票或自動下載失敗時，可改用CSV匯入。")
