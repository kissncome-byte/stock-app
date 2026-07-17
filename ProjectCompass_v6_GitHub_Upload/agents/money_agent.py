from __future__ import annotations
import pandas as pd
from analysis.indicators import safe_float
from models.decision import AgentResult, Evidence


def run_money_agent(df: pd.DataFrame) -> AgentResult:
    last = df.iloc[-1]
    price_change = safe_float(df["close"].pct_change().iloc[-1] * 100)
    volume = safe_float(last["volume"])
    avg_volume = safe_float(last.get("VOL_MA20"))
    ratio = volume / avg_volume if avg_volume else 1.0
    five_price = safe_float(df["close"].pct_change(5).iloc[-1] * 100)
    five_vol = safe_float(df["volume"].tail(5).mean())
    prev_vol = safe_float(df["volume"].iloc[-25:-5].mean()) if len(df) >= 25 else avg_volume
    score = 50
    pros, cons = [], []
    if price_change > 0 and ratio >= 1.2: score += 20; pros.append("上漲同時有成交量支持")
    elif price_change > 0 and ratio < 0.85: score -= 8; cons.append("上漲但追價量能不足")
    if price_change < 0 and ratio < 0.9: score += 10; pros.append("下跌時成交量縮小，賣壓未明顯增加")
    elif price_change < 0 and ratio >= 1.3: score -= 20; cons.append("下跌時成交量放大，賣壓明顯增加")
    if five_price > 0 and five_vol >= prev_vol: score += 8; pros.append("近五日價格與資金活躍度同步改善")
    if five_price < 0 and five_vol > prev_vol: score -= 8; cons.append("近五日跌勢伴隨資金活躍度提高")
    score = max(0, min(100, score))
    if score >= 70:
        c,m,a = "價量偏多", "買盤有實際成交量支持，訊號可信度較高。", "可搭配趨勢尋找布局或續抱。"
    elif score >= 45:
        c,m,a = "價量中性", "目前成交量沒有明確支持多方或空方。", "不要單靠今天漲跌做決定。"
    else:
        c,m,a = "價量偏空", "賣壓或量價背離提高了失敗風險。", "降低追價與加碼意願，優先觀察止跌。"
    evidence = [Evidence("今日漲跌", f"{price_change:+.2f}%", "辨識價格方向"), Evidence("今日量比", f"{ratio:.2f}倍", "1.2倍以上視為活躍；0.9倍以下視為量縮"), Evidence("近5日漲跌", f"{five_price:+.2f}%", "確認單日訊號是否延續")]
    return AgentResult("Money", score, c, m, a, min(92, 55 + abs(score-50)), pros, cons, evidence)
