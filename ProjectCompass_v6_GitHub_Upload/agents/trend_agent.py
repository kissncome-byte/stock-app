from __future__ import annotations
import pandas as pd
from analysis.indicators import detect_swings, safe_float, slope_pct
from models.decision import AgentResult, Evidence


def run_trend_agent(df: pd.DataFrame) -> AgentResult:
    last = df.iloc[-1]
    price = safe_float(last["close"])
    ma20, ma60, ma120, ma240 = [safe_float(last.get(f"MA{n}")) for n in (20,60,120,240)]
    s20, s60 = slope_pct(df["MA20"]), slope_pct(df["MA60"])
    swings = detect_swings(df.tail(120).reset_index(drop=True))
    score = 50
    pros, cons = [], []
    if price > ma20 > ma60: score += 18; pros.append("現價站在中期平均成本之上")
    else: score -= 15; cons.append("現價尚未穩定站回中期平均成本")
    if ma60 > ma120 > ma240 and ma240 > 0: score += 18; pros.append("中長期平均成本維持多頭排列")
    elif ma120 and ma240 and ma60 < ma120: score -= 12; cons.append("中期平均成本仍低於長期平均成本")
    if s20 > 0 and s60 > 0: score += 10; pros.append("20日與60日平均成本同步上升")
    elif s20 < 0 and s60 < 0: score -= 10; cons.append("20日與60日平均成本同步下降")
    if swings["higher_high"] and swings["higher_low"]: score += 12; pros.append("最近波段高點與低點同步墊高")
    if swings["lower_high"] and swings["lower_low"]: score -= 16; cons.append("最近反彈高點與下跌低點同步降低")
    score = max(0, min(100, score))
    if score >= 75:
        conclusion, meaning, action = "趨勢健康偏多", "中長期方向仍由買方掌握，短線拉回較可能是整理。", "未持有者等待合理布局區；已持有者續抱並守趨勢失效價。"
    elif score >= 55:
        conclusion, meaning, action = "趨勢偏多但仍需確認", "方向略偏向上，但部分條件尚未完全一致。", "不要追價，等待回測支撐或放量突破。"
    elif score >= 35:
        conclusion, meaning, action = "方向不明、以整理看待", "多空證據互相抵銷，現在沒有明顯優勢。", "先等待，不勉強交易。"
    else:
        conclusion, meaning, action = "趨勢明顯轉弱", "反彈力道與支撐結構都偏弱，低價不代表安全。", "未持有者暫停接刀；已持有者優先控制風險。"
    evidence = [
        Evidence("現價", f"{price:.2f}", "比較股價與平均成本位置"),
        Evidence("MA20 / MA60", f"{ma20:.2f} / {ma60:.2f}", "判斷短中期方向"),
        Evidence("MA120 / MA240", f"{ma120:.2f} / {ma240:.2f}", "判斷中長期方向"),
        Evidence("MA20 / MA60斜率", f"{s20:+.2f}% / {s60:+.2f}%", "斜率向上比單純交叉更可靠"),
        Evidence("最近波段高低點", f"高 {swings['last_high'] or 0:.2f} / 低 {swings['last_low'] or 0:.2f}", "判斷波段結構是否墊高"),
    ]
    return AgentResult("Trend", score, conclusion, meaning, action, min(95, 55 + abs(score-50)), pros, cons, evidence)
