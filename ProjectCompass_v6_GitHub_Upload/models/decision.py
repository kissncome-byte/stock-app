from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass
class Evidence:
    label: str
    value: str
    rule: str

@dataclass
class AgentResult:
    name: str
    score: int
    conclusion: str
    meaning: str
    action: str
    confidence: int
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

@dataclass
class TradePlan:
    mode: str
    action: str
    buy_zone_low: float | None
    buy_zone_high: float | None
    breakout_price: float | None
    invalidation_price: float | None
    target_1: float | None
    target_2: float | None
    risk_reward: float | None
    current_distance_pct: float | None
    price_sources: list[str] = field(default_factory=list)

@dataclass
class CompassDecision:
    stock_id: str
    stock_name: str
    current_price: float
    recommendation: str
    state: str
    confidence: int
    quality_score: int
    today_focus: str
    explanation: str
    trade_plan: TradePlan
    agents: list[AgentResult]
    support_reasons: list[str]
    opposing_reasons: list[str]
    waiting_conditions: list[str]
    disclaimer: str = "本系統依目前公開資料提供決策參考，不保證獲利，也不取代個人投資判斷。"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
