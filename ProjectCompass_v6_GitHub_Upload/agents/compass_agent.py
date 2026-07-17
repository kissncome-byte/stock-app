from __future__ import annotations
from models.decision import CompassDecision, AgentResult, TradePlan


def run_compass(stock_id: str, stock_name: str, current_price: float, agents: list[AgentResult], plan: TradePlan) -> CompassDecision:
    by_name = {a.name: a for a in agents}
    trend = by_name["Trend"]
    money = by_name["Money"]
    risk = by_name["Risk"]
    # 優先規則：趨勢破壞與風險不佳優先於單一偏多訊號。
    if trend.score < 35:
        recommendation, state = "暫停買進；已持有者優先控制風險", "趨勢轉弱"
    elif risk.score < 35:
        recommendation, state = "等待更好的價格，不勉強交易", "風險報酬不足"
    elif trend.score >= 70 and money.score >= 55 and risk.score >= 55:
        recommendation, state = ("續抱並依計畫分批獲利" if plan.mode == "已持有" else "等待合理布局區，可分批進場"), "偏多"
    elif trend.score >= 55 and risk.score >= 45:
        recommendation, state = "等待拉回或放量突破確認", "偏多但未完全確認"
    else:
        recommendation, state = "目前等待，不需要勉強做動作", "證據不一致"
    quality = round(trend.score * .45 + money.score * .25 + risk.score * .30)
    disagreement = max(a.score for a in agents) - min(a.score for a in agents)
    confidence = max(35, min(95, round((sum(a.confidence for a in agents)/len(agents)) - disagreement * .25)))
    supports = [x for a in agents for x in a.pros][:6]
    oppositions = [x for a in agents for x in a.cons][:6]
    if not supports: supports = ["目前沒有足以形成高品質交易的明確優勢"]
    if not oppositions: oppositions = ["仍需留意市場突然放量反轉與整體大盤風險"]
    if plan.current_distance_pct is not None and plan.current_distance_pct > 3:
        today_focus = f"現價高於合理布局區約 {plan.current_distance_pct:.1f}%，今天最重要的是不要追價。"
    elif trend.score < 35:
        today_focus = "波段結構轉弱是今天最大的警訊，低價不代表安全。"
    elif money.score >= 70:
        today_focus = "今天最重要的是上漲獲得成交量支持，但仍需遵守布局與失效價格。"
    else:
        today_focus = "今天沒有足以改變策略的重大訊號，等待比勉強交易更有利。"
    explanation = f"趨勢 {trend.score} 分、價量 {money.score} 分、風險 {risk.score} 分；系統依優先規則整合，而不是把單一指標直接當成買賣答案。"
    waiting = [f"回到布局區 {plan.buy_zone_low:.2f}～{plan.buy_zone_high:.2f}" if plan.buy_zone_low else "回到合理支撐區", f"突破 {plan.breakout_price:.2f} 且成交量至少達20日均量1.2倍" if plan.breakout_price else "放量突破壓力", "趨勢、價量與風險三項至少兩項同步改善"]
    return CompassDecision(stock_id, stock_name, current_price, recommendation, state, confidence, quality, today_focus, explanation, plan, agents, supports, oppositions, waiting)
