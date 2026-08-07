from __future__ import annotations

import math
from collections.abc import Sequence

from stockpilot.models import (
    ChipEngineInput,
    ChipResult,
    ChipState,
    Evidence,
    EvidenceDirection,
    EvidenceEngine,
    EvidenceSeverity,
)


class ChipEngineError(ValueError):
    pass


class ChipEngine:
    MIN_FLOW_DAYS = 20
    MIN_MARGIN_DAYS = 21

    def evaluate(self, data: ChipEngineInput) -> ChipResult:
        self._validate_lengths(data)

        foreign = self._series(data.institutional_flows, "foreign")
        trust = self._series(data.institutional_flows, "trust")
        dealer = self._series(data.institutional_flows, "dealer")

        foreign_5d = self._sum_last(foreign, 5)
        foreign_10d = self._sum_last(foreign, 10)
        foreign_20d = self._sum_last(foreign, 20)
        trust_5d = self._sum_last(trust, 5)
        trust_10d = self._sum_last(trust, 10)
        trust_20d = self._sum_last(trust, 20)
        dealer_20d = self._sum_last(dealer, 20)

        margin_values = [
            record.margin_balance
            for record in data.margin_records
        ]
        margin_5d = self._pct_change(margin_values, 5)
        margin_10d = self._pct_change(margin_values, 10)
        margin_20d = self._pct_change(margin_values, 20)
        price_20d = self._price_change(data.closes, 20)

        institutional_available = all(
            value is not None
            for value in (
                foreign_5d, foreign_10d, foreign_20d,
                trust_5d, trust_10d, trust_20d,
            )
        )
        margin_available = (
            margin_20d is not None and price_20d is not None
        )

        evidence = []

        if institutional_available:
            evidence.append(
                self._institutional_evidence(
                    foreign_5d, foreign_10d, foreign_20d,
                    trust_5d, trust_10d, trust_20d,
                    dealer_20d,
                )
            )
        else:
            evidence.append(
                Evidence(
                    code="CHIP_INSTITUTIONAL_UNAVAILABLE",
                    engine=EvidenceEngine.CHIP,
                    title="法人資料不足",
                    direction=EvidenceDirection.UNAVAILABLE,
                    severity=EvidenceSeverity.HIGH,
                    explanation="近 20 日外資或投信資料不足，不納入籌碼方向。",
                    value=None,
                    threshold=20,
                    source="institutional daily flows",
                    data_valid=False,
                )
            )

        if margin_available:
            evidence.append(
                self._margin_interaction_evidence(
                    price_20d=price_20d,
                    margin_20d=margin_20d,
                    foreign_20d=foreign_20d,
                    trust_20d=trust_20d,
                )
            )
        else:
            evidence.append(
                Evidence(
                    code="CHIP_MARGIN_UNAVAILABLE",
                    engine=EvidenceEngine.CHIP,
                    title="融資資料不足",
                    direction=EvidenceDirection.UNAVAILABLE,
                    severity=EvidenceSeverity.LOW,
                    explanation="融資資料不足，不以 0 代替，也不納入籌碼方向。",
                    value=None,
                    threshold=20,
                    source="margin daily balance",
                    data_valid=False,
                )
            )

        valid_directions = [
            item.direction
            for item in evidence
            if item.direction is not EvidenceDirection.UNAVAILABLE
        ]

        if not valid_directions:
            state = ChipState.UNAVAILABLE
            data_valid = False
        else:
            support = sum(
                1 for direction in valid_directions
                if direction is EvidenceDirection.SUPPORT
            )
            oppose = sum(
                1 for direction in valid_directions
                if direction is EvidenceDirection.OPPOSE
            )
            if oppose > support:
                state = ChipState.OPPOSE
            elif support > oppose:
                state = ChipState.SUPPORT
            else:
                state = ChipState.NEUTRAL
            data_valid = True

        return ChipResult(
            state=state,
            evidence=tuple(evidence),
            data_valid=data_valid,
            foreign_5d=foreign_5d,
            foreign_10d=foreign_10d,
            foreign_20d=foreign_20d,
            trust_5d=trust_5d,
            trust_10d=trust_10d,
            trust_20d=trust_20d,
            margin_5d_change_pct=margin_5d,
            margin_10d_change_pct=margin_10d,
            margin_20d_change_pct=margin_20d,
        )

    def _validate_lengths(self, data: ChipEngineInput) -> None:
        if len(data.closes) != len(data.dates):
            raise ChipEngineError("closes and dates must have the same length")
        if len(data.closes) < 21:
            raise ChipEngineError("at least 21 closes are required")
        if any(
            close <= 0 or not math.isfinite(float(close))
            for close in data.closes
        ):
            raise ChipEngineError("closes must be finite positive numbers")

    def _series(self, records, field: str) -> list[float | None]:
        ordered = sorted(records, key=lambda item: item.trading_date)
        return [getattr(item, field) for item in ordered]

    def _sum_last(
        self,
        values: Sequence[float | None],
        days: int,
    ) -> float | None:
        if len(values) < days:
            return None
        window = values[-days:]
        if any(value is None for value in window):
            return None
        return float(sum(float(value) for value in window))

    def _pct_change(
        self,
        values: Sequence[float | None],
        days: int,
    ) -> float | None:
        if len(values) < days + 1:
            return None
        latest = values[-1]
        previous = values[-(days + 1)]
        if (
            latest is None or previous is None
            or previous <= 0
            or not math.isfinite(float(latest))
            or not math.isfinite(float(previous))
        ):
            return None
        return (float(latest) / float(previous) - 1.0) * 100.0

    def _price_change(
        self,
        closes: Sequence[float],
        days: int,
    ) -> float | None:
        if len(closes) < days + 1:
            return None
        return (closes[-1] / closes[-(days + 1)] - 1.0) * 100.0

    def _institutional_evidence(
        self,
        foreign_5d, foreign_10d, foreign_20d,
        trust_5d, trust_10d, trust_20d,
        dealer_20d,
    ) -> Evidence:
        support = (
            foreign_5d > 0 and foreign_10d > 0 and foreign_20d > 0
            and trust_5d >= 0 and trust_20d >= 0
        )
        oppose = (
            foreign_5d < 0 and foreign_10d < 0 and foreign_20d < 0
            and trust_5d <= 0 and trust_20d <= 0
        )
        direction = (
            EvidenceDirection.SUPPORT if support
            else EvidenceDirection.OPPOSE if oppose
            else EvidenceDirection.NEUTRAL
        )
        return Evidence(
            code="CHIP_INSTITUTIONAL_TREND",
            engine=EvidenceEngine.CHIP,
            title="外資與投信 5／10／20 日方向",
            direction=direction,
            severity=EvidenceSeverity.HIGH,
            explanation=(
                f"外資 5/10/20 日：{foreign_5d:.0f}/"
                f"{foreign_10d:.0f}/{foreign_20d:.0f}；"
                f"投信 5/10/20 日：{trust_5d:.0f}/"
                f"{trust_10d:.0f}/{trust_20d:.0f}。"
            ),
            value={
                "foreign_5d": foreign_5d,
                "foreign_10d": foreign_10d,
                "foreign_20d": foreign_20d,
                "trust_5d": trust_5d,
                "trust_10d": trust_10d,
                "trust_20d": trust_20d,
                "dealer_20d": dealer_20d,
            },
            threshold=0.0,
            source="institutional daily flows",
        )

    def _margin_interaction_evidence(
        self,
        *,
        price_20d: float,
        margin_20d: float,
        foreign_20d: float | None,
        trust_20d: float | None,
    ) -> Evidence:
        institutional_20d = (
            None
            if foreign_20d is None or trust_20d is None
            else foreign_20d + trust_20d
        )

        if (
            price_20d > 0
            and margin_20d < 0
            and institutional_20d is not None
            and institutional_20d > 0
        ):
            direction = EvidenceDirection.SUPPORT
            interpretation = "股價上漲、融資下降且法人買超，籌碼結構健康。"
        elif (
            price_20d < 0
            and margin_20d > 0
            and institutional_20d is not None
            and institutional_20d < 0
        ):
            direction = EvidenceDirection.OPPOSE
            interpretation = "股價下跌、融資增加且法人賣超，散戶承接法人賣壓。"
        else:
            direction = EvidenceDirection.NEUTRAL
            interpretation = "股價、法人與融資方向未形成明確一致結論。"

        return Evidence(
            code="CHIP_MARGIN_INTERACTION",
            engine=EvidenceEngine.CHIP,
            title="法人與融資互動",
            direction=direction,
            severity=EvidenceSeverity.HIGH,
            explanation=(
                f"{interpretation} 20 日股價 {price_20d:.2f}%，"
                f"融資 {margin_20d:.2f}%。"
            ),
            value={
                "price_20d_pct": price_20d,
                "margin_20d_pct": margin_20d,
                "institutional_20d": institutional_20d,
            },
            threshold=0.0,
            source="institutional + margin + completed closes",
        )
