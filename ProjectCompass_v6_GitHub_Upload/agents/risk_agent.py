from __future__ import annotations
import pandas as pd
from analysis.indicators import safe_float, tick_size, floor_tick, ceil_tick, detect_swings
from models.decision import AgentResult, Evidence, TradePlan


def run_risk_agent(df: pd.DataFrame, is_holding: bool = False) -> tuple[AgentResult, TradePlan]:
    last = df.iloc[-1]
    price = safe_float(last["close"])
    atr = max(safe_float(last.get("ATR14")), price * 0.015)
    ma20 = safe_float(last.get("MA20"))
    resistance = safe_float(last.get("RES20"), price + 2 * atr)
    support = safe_float(last.get("SUP20"), price - 2 * atr)
    swings = detect_swings(df.tail(120).reset_index(drop=True))
    if swings.get("last_low"):
        support = max(support, float(swings["last_low"])) if support else float(swings["last_low"])
    tick = tick_size(price)
    zone_center = max(0.01, min(ma20 if ma20 > 0 else price, price))
    buy_low = floor_tick(zone_center - 0.45 * atr, tick)
    buy_high = ceil_tick(zone_center + 0.25 * atr, tick)
    invalidation = floor_tick(min(support, buy_low - 0.7 * atr), tick)
    breakout = ceil_tick(max(resistance, price + 0.3 * atr), tick)
    target1 = ceil_tick(max(resistance, price + 1.8 * atr), tick)
    target2 = ceil_tick(max(target1 + atr, price + 3.0 * atr), tick)
    assumed_entry = min(price, buy_high)
    risk = max(assumed_entry - invalidation, tick)
    reward = max(target1 - assumed_entry, 0)
    rr = reward / risk if risk else None
    distance = ((price / buy_high) - 1) * 100 if buy_high else None
    score = 50
    pros, cons = [], []
    if rr and rr >= 2.5: score += 25; pros.append(f"第一目標的風險報酬比約 1:{rr:.1f}")
    elif rr and rr < 1.5: score -= 20; cons.append(f"風險報酬比只有約 1:{rr:.1f}")
    if distance is not None and distance > 5: score -= 15; cons.append("現價距合理布局區過遠，追價風險提高")
    elif distance is not None and -2 <= distance <= 2: score += 12; pros.append("現價接近合理布局區")
    score = max(0, min(100, score))
    conclusion = "風險報酬合理" if score >= 65 else "風險報酬普通" if score >= 45 else "目前交易條件不划算"
    meaning = "停損距離與目標空間仍有合理差距。" if score >= 65 else "現價位置或目標空間不足，交易優勢有限。"
    action = "依計畫分批，不一次買滿。" if score >= 65 else "等待更好的價格或新的突破條件。"
    evidence = [Evidence("ATR14", f"{atr:.2f}", "衡量近期正常波動"), Evidence("布局區", f"{buy_low:.2f}～{buy_high:.2f}", "由MA20、ATR與支撐共同推導"), Evidence("趨勢失效價", f"{invalidation:.2f}", "跌破代表原交易邏輯需重新評估"), Evidence("第一目標 / 第二目標", f"{target1:.2f} / {target2:.2f}", "由壓力與ATR情境推導")]
    plan = TradePlan("已持有" if is_holding else "未持有", "續抱並依失效價防守" if is_holding else "等待布局區或突破條件", buy_low, buy_high, breakout, invalidation, target1, target2, round(rr,2) if rr else None, round(distance,2) if distance is not None else None, ["MA20平均成本", "20日支撐／壓力", "ATR近期波動", "最近波段低點"])
    return AgentResult("Risk", score, conclusion, meaning, action, min(95, 60 + abs(score-50)), pros, cons, evidence), plan
