from __future__ import annotations
import pandas as pd
import streamlit as st
from models.decision import AgentResult, CompassDecision


def metric_card(label: str, value: str, help_text: str = ""):
    st.markdown(f"""<div class='metric-card'><div class='metric-label'>{label}</div><div class='metric-value'>{value}</div><div class='metric-help'>{help_text}</div></div>""", unsafe_allow_html=True)


def render_agent(agent: AgentResult, expanded: bool = False):
    st.markdown(f"### {agent.name}｜{agent.conclusion}")
    st.write(f"**這代表什麼：** {agent.meaning}")
    st.write(f"**所以呢：** {agent.action}")
    cols = st.columns(2)
    with cols[0]:
        st.markdown("**支持目前結論**")
        for item in agent.pros or ["目前沒有明顯加分條件"]: st.write(f"✓ {item}")
    with cols[1]:
        st.markdown("**可能推翻目前結論**")
        for item in agent.cons or ["仍需留意突發市場風險"]: st.write(f"⚠ {item}")
    with st.expander("查看數據依據", expanded=expanded):
        st.dataframe(pd.DataFrame([{"項目":e.label,"數值":e.value,"判斷方式":e.rule} for e in agent.evidence]), use_container_width=True, hide_index=True)


def render_dashboard(decision: CompassDecision, expanded: bool = False):
    st.markdown(f"## {decision.stock_id} {decision.stock_name}")
    st.markdown(f"<div class='hero'><div class='eyebrow'>AI 綜合評估</div><div class='hero-title'>{decision.recommendation}</div><div class='hero-sub'>{decision.explanation}</div></div>", unsafe_allow_html=True)
    c1,c2,c3,c4 = st.columns(4)
    with c1: metric_card("目前狀態", decision.state)
    with c2: metric_card("建議信心", f"{decision.confidence}%", "證據強度，不是上漲機率")
    with c3: metric_card("交易品質", f"{decision.quality_score}/100", "條件完整度，不是獲利保證")
    with c4: metric_card("目前價格", f"{decision.current_price:.2f}")
    st.info(f"**今天最重要的一件事：** {decision.today_focus}")
    plan = decision.trade_plan
    st.markdown("## 交易計畫")
    p1,p2,p3,p4 = st.columns(4)
    with p1: metric_card("最佳布局區", f"{plan.buy_zone_low:.2f}～{plan.buy_zone_high:.2f}" if plan.buy_zone_low else "待確認", "較佳風險報酬區，不保證成交")
    with p2: metric_card("突破方案", f"{plan.breakout_price:.2f}以上", "必須搭配量能確認")
    with p3: metric_card("趨勢失效價", f"{plan.invalidation_price:.2f}", "跌破代表原交易邏輯需重評")
    with p4: metric_card("風險報酬比", f"1 : {plan.risk_reward:.2f}" if plan.risk_reward else "資料不足")
    t1,t2 = st.columns(2)
    with t1: metric_card("第一目標區", f"{plan.target_1:.2f}", "分批獲利參考，不是預測")
    with t2: metric_card("第二目標區", f"{plan.target_2:.2f}", "突破後才重新上修")
    with st.expander("為什麼是這些價格？", expanded=expanded):
        for item in plan.price_sources: st.write(f"✓ {item}")
    st.markdown("## AI 自我檢查")
    a,b = st.columns(2)
    with a:
        st.success("**支持目前建議**\n\n" + "\n\n".join(f"✓ {x}" for x in decision.support_reasons))
    with b:
        st.warning("**反對目前建議**\n\n" + "\n\n".join(f"⚠ {x}" for x in decision.opposing_reasons))
    st.markdown("## 還要等什麼？")
    for item in decision.waiting_conditions: st.write(f"- {item}")
