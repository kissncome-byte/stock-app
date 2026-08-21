from __future__ import annotations
import math

from stockpilot.models import (
    DailyBar,
    Evidence,
    EvidenceDirection,
    EvidenceEngine,
    EvidenceSeverity,
    TrendEngineInput,
    TrendResult,
    TrendState,
)

class TrendEngineError(ValueError):
    pass

class TrendEngine:
    MINIMUM_BARS = 65

    def evaluate(self, data: TrendEngineInput) -> TrendResult:
        bars = self._validated_bars(data)
        raw_history = []
        formal_history = []
        current = None
        pending = None
        pending_count = 0

        for index in range(64, len(bars)):
            raw = self._classify_raw_state(bars, index)
            raw_history.append(raw)

            if current is None:
                current = raw
                formal_history.append(current)
                continue

            if raw is current:
                pending = None
                pending_count = 0
                formal_history.append(current)
                continue

            if self._is_major_breakdown(bars, index):
                current = TrendState.DOWNTREND
                pending = None
                pending_count = 0
                formal_history.append(current)
                continue

            if raw is pending:
                pending_count += 1
            else:
                pending = raw
                pending_count = 1

            if pending_count >= data.confirmation_days:
                current = raw
                pending = None
                pending_count = 0

            formal_history.append(current)

        if current is None or not raw_history:
            raise TrendEngineError("unable to derive trend state")

        days_in_state = 1
        for state in reversed(formal_history[:-1]):
            if state is current:
                days_in_state += 1
            else:
                break

        return TrendResult(
            formal_state=current,
            raw_state=raw_history[-1],
            days_in_state=days_in_state,
            evidence=tuple(self._build_evidence(bars, len(bars) - 1)),
            state_history=tuple(formal_history),
        )

    def _validated_bars(self, data: TrendEngineInput) -> tuple[DailyBar, ...]:
        if data.confirmation_days < 1:
            raise TrendEngineError("confirmation_days must be at least 1")
        if data.lookback_days < self.MINIMUM_BARS:
            raise TrendEngineError(f"lookback_days must be at least {self.MINIMUM_BARS}")
        if len(data.bars) < self.MINIMUM_BARS:
            raise TrendEngineError(f"at least {self.MINIMUM_BARS} completed daily bars are required")

        bars = tuple(sorted(data.bars, key=lambda x: x.trading_date))[-data.lookback_days:]
        for bar in bars:
            vals = (bar.open, bar.high, bar.low, bar.close)
            if any(v <= 0 or not math.isfinite(float(v)) for v in vals):
                raise TrendEngineError("all OHLC values must be finite and positive")
            if bar.high < max(bar.open, bar.close, bar.low):
                raise TrendEngineError("high price is inconsistent")
            if bar.low > min(bar.open, bar.close, bar.high):
                raise TrendEngineError("low price is inconsistent")
        return bars

    def _classify_raw_state(self, bars: tuple[DailyBar, ...], index: int) -> TrendState:
        close = bars[index].close
        ma20 = self._sma(bars, index, 20)
        ma60 = self._sma(bars, index, 60)
        ma20_prev = self._sma(bars, index - 5, 20)
        ma60_prev = self._sma(bars, index - 5, 60)
        slope20 = ma20 - ma20_prev
        slope60 = ma60 - ma60_prev
        ret20 = close / bars[index - 20].close - 1.0
        ma_gap = abs(ma20 - ma60) / close

        if close > ma20 > ma60 and slope20 > 0 and slope60 > 0 and ret20 > 0.05:
            return TrendState.STRONG_UPTREND
        if close > ma60 and slope60 >= 0 and (close <= ma20 or ret20 >= -0.02):
            return TrendState.UPTREND_PULLBACK
        if ma_gap <= 0.035 and abs(ret20) <= 0.08:
            return TrendState.RANGE
        if close > ma20 and slope20 > 0 and (close < ma60 or slope60 < 0):
            return TrendState.BEAR_RALLY
        return TrendState.DOWNTREND

    def _is_major_breakdown(self, bars: tuple[DailyBar, ...], index: int) -> bool:
        close = bars[index].close
        ma20 = self._sma(bars, index, 20)
        ma60 = self._sma(bars, index, 60)
        ma20_prev = self._sma(bars, index - 5, 20)
        ma60_prev = self._sma(bars, index - 5, 60)
        prior_low = min(bar.low for bar in bars[index - 20:index])
        return (
            close < ma20
            and close < ma60
            and ma20 < ma20_prev
            and ma60 < ma60_prev
            and close < prior_low
        )

    def _build_evidence(self, bars: tuple[DailyBar, ...], index: int) -> list[Evidence]:
        close = bars[index].close
        ma20 = self._sma(bars, index, 20)
        ma60 = self._sma(bars, index, 60)
        ma20_prev = self._sma(bars, index - 5, 20)
        ma60_prev = self._sma(bars, index - 5, 60)
        ret20 = close / bars[index - 20].close - 1.0

        return [
            Evidence(
                code="TREND_CLOSE_VS_MA20",
                engine=EvidenceEngine.TREND,
                title="收盤相對 MA20",
                direction=EvidenceDirection.SUPPORT if close > ma20 else EvidenceDirection.OPPOSE,
                severity=EvidenceSeverity.MEDIUM,
                explanation=f"收盤 {close:.2f}，MA20 {ma20:.2f}。",
                value=close,
                threshold=ma20,
                source="completed daily bars",
            ),
            Evidence(
                code="TREND_MA_ALIGNMENT",
                engine=EvidenceEngine.TREND,
                title="MA20 與 MA60 排列",
                direction=EvidenceDirection.SUPPORT if ma20 > ma60 else EvidenceDirection.OPPOSE,
                severity=EvidenceSeverity.HIGH,
                explanation=f"MA20 {ma20:.2f}，MA60 {ma60:.2f}。",
                value=ma20,
                threshold=ma60,
                source="completed daily bars",
            ),
            Evidence(
                code="TREND_MA_SLOPES",
                engine=EvidenceEngine.TREND,
                title="均線斜率",
                direction=EvidenceDirection.SUPPORT if (ma20 > ma20_prev and ma60 >= ma60_prev) else EvidenceDirection.OPPOSE,
                severity=EvidenceSeverity.MEDIUM,
                explanation=(
                    f"MA20 五日前 {ma20_prev:.2f}，目前 {ma20:.2f}；"
                    f"MA60 五日前 {ma60_prev:.2f}，目前 {ma60:.2f}。"
                ),
                value={"ma20_change": ma20 - ma20_prev, "ma60_change": ma60 - ma60_prev},
                threshold=0.0,
                source="completed daily bars",
            ),
            Evidence(
                code="TREND_20D_RETURN",
                engine=EvidenceEngine.TREND,
                title="20 日價格方向",
                direction=EvidenceDirection.SUPPORT if ret20 > 0 else EvidenceDirection.OPPOSE,
                severity=EvidenceSeverity.LOW,
                explanation=f"近 20 日報酬 {ret20 * 100:.2f}%。",
                value=ret20,
                threshold=0.0,
                source="completed daily bars",
            ),
        ]

    def _sma(self, bars: tuple[DailyBar, ...], index: int, period: int) -> float:
        start = index - period + 1
        if start < 0:
            raise TrendEngineError(f"not enough bars for SMA{period}")
        return sum(bar.close for bar in bars[start:index + 1]) / period
