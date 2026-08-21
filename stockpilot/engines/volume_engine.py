from __future__ import annotations

import math

from stockpilot.models import (
    Evidence,
    EvidenceDirection,
    EvidenceEngine,
    EvidenceSeverity,
    VolumeEngineInput,
    VolumeResult,
    VolumeState,
)


class VolumeEngineError(ValueError):
    pass


class VolumeEngine:
    MINIMUM_BARS = 21

    def evaluate(self, data: VolumeEngineInput) -> VolumeResult:
        bars = self._validated_bars(data)
        latest = bars[-1]
        previous = bars[-2]

        volumes = [bar.volume for bar in bars[-20:]]
        if any(volume is None or volume <= 0 for volume in volumes):
            evidence = Evidence(
                code="VOLUME_DATA_UNAVAILABLE",
                engine=EvidenceEngine.VOLUME,
                title="成交量資料不足",
                direction=EvidenceDirection.UNAVAILABLE,
                severity=EvidenceSeverity.HIGH,
                explanation="近 20 日成交量有缺值或無效值，不納入量價方向。",
                value=None,
                threshold=20,
                source="completed daily bars",
                data_valid=False,
            )
            return VolumeResult(
                state=VolumeState.UNAVAILABLE,
                pattern="資料不足",
                evidence=(evidence,),
                data_valid=False,
            )

        average20 = sum(float(volume) for volume in volumes) / 20.0
        volume_ratio = float(latest.volume) / average20 if average20 > 0 else None
        price_change = latest.close / previous.close - 1.0

        up_volumes = [
            float(bar.volume)
            for idx, bar in enumerate(bars[-20:], start=len(bars) - 20)
            if idx > 0 and bar.close >= bars[idx - 1].close
        ]
        down_volumes = [
            float(bar.volume)
            for idx, bar in enumerate(bars[-20:], start=len(bars) - 20)
            if idx > 0 and bar.close < bars[idx - 1].close
        ]
        up_day_ratio = (
            (sum(up_volumes) / len(up_volumes))
            / (sum(down_volumes) / len(down_volumes))
            if up_volumes and down_volumes
            else None
        )

        state, pattern, direction, severity = self._classify(
            price_change=price_change,
            volume_ratio=volume_ratio,
        )

        evidence = Evidence(
            code="VOLUME_PRICE_QUALITY",
            engine=EvidenceEngine.VOLUME,
            title="量價品質",
            direction=direction,
            severity=severity,
            explanation=(
                f"{pattern}；單日價格變化 {price_change * 100:.2f}%，"
                f"成交量為 20 日均量的 {volume_ratio:.2f} 倍。"
            ),
            value={
                "price_change_pct": price_change * 100.0,
                "volume_ratio_20d": volume_ratio,
                "up_day_volume_ratio": up_day_ratio,
            },
            threshold={"high_volume": 1.25, "low_volume": 0.75},
            source="completed daily bars",
        )

        return VolumeResult(
            state=state,
            pattern=pattern,
            evidence=(evidence,),
            data_valid=True,
            volume_ratio_20d=volume_ratio,
            up_day_volume_ratio=up_day_ratio,
        )

    def _validated_bars(self, data: VolumeEngineInput):
        if data.lookback_days < self.MINIMUM_BARS:
            raise VolumeEngineError(
                f"lookback_days must be at least {self.MINIMUM_BARS}"
            )
        if len(data.bars) < self.MINIMUM_BARS:
            raise VolumeEngineError(
                f"at least {self.MINIMUM_BARS} completed bars are required"
            )
        bars = tuple(
            sorted(data.bars, key=lambda item: item.trading_date)
        )[-data.lookback_days:]
        for bar in bars:
            if (
                bar.close <= 0
                or not math.isfinite(float(bar.close))
            ):
                raise VolumeEngineError("close must be finite and positive")
        return bars

    def _classify(self, *, price_change, volume_ratio):
        if price_change > 0.01 and volume_ratio >= 1.25:
            return (
                VolumeState.CONFIRM,
                "放量上漲",
                EvidenceDirection.SUPPORT,
                EvidenceSeverity.HIGH,
            )
        if price_change < -0.01 and volume_ratio >= 1.25:
            return (
                VolumeState.WARNING,
                "放量下跌",
                EvidenceDirection.OPPOSE,
                EvidenceSeverity.HIGH,
            )
        if abs(price_change) <= 0.01 and volume_ratio < 0.75:
            return (
                VolumeState.NEUTRAL,
                "量縮整理",
                EvidenceDirection.NEUTRAL,
                EvidenceSeverity.LOW,
            )
        if price_change > 0 and volume_ratio < 0.75:
            return (
                VolumeState.WARNING,
                "無量反彈",
                EvidenceDirection.OPPOSE,
                EvidenceSeverity.MEDIUM,
            )
        if price_change < 0 and volume_ratio < 0.75:
            return (
                VolumeState.NEUTRAL,
                "量縮下跌",
                EvidenceDirection.NEUTRAL,
                EvidenceSeverity.LOW,
            )
        return (
            VolumeState.NEUTRAL,
            "一般量價",
            EvidenceDirection.NEUTRAL,
            EvidenceSeverity.LOW,
        )
