from __future__ import annotations
import math

from stockpilot.models import (
    DailyBar,
    Evidence,
    EvidenceDirection,
    EvidenceEngine,
    EvidenceSeverity,
    ListingMarket,
    MarketEngineInput,
    MarketResult,
    MarketState,
)

class MarketEngineError(ValueError):
    pass

class MarketEngine:
    MINIMUM_BARS = 65

    def evaluate(self, data: MarketEngineInput) -> MarketResult:
        expected = self._expected_index(data.listing_market)

        if data.expected_reference_index != expected:
            raise MarketEngineError(
                f"expected_reference_index must be {expected}"
            )

        if data.reference_index != expected:
            return MarketResult(
                state=MarketState.UNAVAILABLE,
                reference_index=data.reference_index,
                evidence=(
                    Evidence(
                        code="MARKET_REFERENCE_MISMATCH",
                        engine=EvidenceEngine.MARKET,
                        title="大盤參考指數不符",
                        direction=EvidenceDirection.UNAVAILABLE,
                        severity=EvidenceSeverity.HIGH,
                        explanation=(
                            f"{data.listing_market.value} 股票應使用 {expected}，"
                            f"目前收到 {data.reference_index}。"
                        ),
                        value=data.reference_index,
                        threshold=expected,
                        source="market mapping",
                        data_valid=False,
                    ),
                ),
                data_valid=False,
                warning="參考指數不符，不進行大盤判斷。",
            )

        if len(data.index_bars) < self.MINIMUM_BARS:
            return MarketResult(
                state=MarketState.UNAVAILABLE,
                reference_index=expected,
                evidence=(
                    Evidence(
                        code="MARKET_DATA_INSUFFICIENT",
                        engine=EvidenceEngine.MARKET,
                        title="大盤資料不足",
                        direction=EvidenceDirection.UNAVAILABLE,
                        severity=EvidenceSeverity.HIGH,
                        explanation=(
                            f"至少需要 {self.MINIMUM_BARS} 個完成交易日，"
                            f"目前只有 {len(data.index_bars)} 個。"
                        ),
                        value=len(data.index_bars),
                        threshold=self.MINIMUM_BARS,
                        source=expected,
                        data_valid=False,
                    ),
                ),
                data_valid=False,
                warning="大盤資料不足，採保守模式。",
            )

        bars = self._validated_bars(data)
        index = len(bars) - 1
        close = bars[index].close
        ma20 = self._sma(bars, index, 20)
        ma60 = self._sma(bars, index, 60)
        ma20_prev = self._sma(bars, index - 5, 20)
        ma60_prev = self._sma(bars, index - 5, 60)
        ret5 = close / bars[index - 5].close - 1.0
        ret20 = close / bars[index - 20].close - 1.0
        atr_pct = self._atr_pct(bars, index, 14)
        volume_ratio = self._volume_ratio(bars, index, 20)

        state = self._classify(
            close, ma20, ma60, ma20_prev, ma60_prev,
            ret5, ret20, atr_pct, data.international_risk_flag,
        )

        evidence = (
            self._trend_evidence(close, ma20, ma60, ma20_prev, ma60_prev),
            self._momentum_evidence(ret5, ret20),
            self._volume_evidence(volume_ratio),
            self._risk_evidence(atr_pct, data.international_risk_flag),
        )

        return MarketResult(
            state=state,
            reference_index=expected,
            evidence=evidence,
            data_valid=True,
        )

    def _expected_index(self, listing_market: ListingMarket) -> str:
        return "TAIEX" if listing_market is ListingMarket.LISTED else "TPEx"

    def _validated_bars(self, data: MarketEngineInput) -> tuple[DailyBar, ...]:
        if data.lookback_days < self.MINIMUM_BARS:
            raise MarketEngineError(
                f"lookback_days must be at least {self.MINIMUM_BARS}"
            )
        bars = tuple(
            sorted(data.index_bars, key=lambda x: x.trading_date)
        )[-data.lookback_days:]
        for bar in bars:
            values = (bar.open, bar.high, bar.low, bar.close)
            if any(v <= 0 or not math.isfinite(float(v)) for v in values):
                raise MarketEngineError(
                    "all index OHLC values must be finite and positive"
                )
            if bar.volume is not None and (
                bar.volume < 0 or not math.isfinite(float(bar.volume))
            ):
                raise MarketEngineError(
                    "index volume must be finite and non-negative"
                )
        return bars

    def _classify(
        self, close, ma20, ma60, ma20_prev, ma60_prev,
        ret5, ret20, atr_pct, international_risk_flag,
    ) -> MarketState:
        slopes_up = ma20 > ma20_prev and ma60 >= ma60_prev
        slopes_down = ma20 < ma20_prev and ma60 < ma60_prev

        if (
            close < ma20 and close < ma60 and slopes_down
            and ret5 < -0.05
            and (atr_pct > 0.03 or international_risk_flag)
        ):
            return MarketState.RISK_OFF_HARD

        if close < ma20 and (close < ma60 or slopes_down) and ret20 < 0:
            return MarketState.RISK_OFF

        if (
            close > ma20 > ma60 and slopes_up
            and ret20 > 0 and not international_risk_flag
        ):
            return MarketState.RISK_ON

        return MarketState.NEUTRAL

    def _trend_evidence(self, close, ma20, ma60, ma20_prev, ma60_prev):
        support = close > ma20 > ma60 and ma20 > ma20_prev and ma60 >= ma60_prev
        oppose = close < ma20 and close < ma60 and ma20 < ma20_prev and ma60 < ma60_prev
        direction = (
            EvidenceDirection.SUPPORT if support
            else EvidenceDirection.OPPOSE if oppose
            else EvidenceDirection.NEUTRAL
        )
        return Evidence(
            code="MARKET_TREND_STRUCTURE",
            engine=EvidenceEngine.MARKET,
            title="大盤趨勢結構",
            direction=direction,
            severity=EvidenceSeverity.HIGH,
            explanation=f"收盤 {close:.2f}，MA20 {ma20:.2f}，MA60 {ma60:.2f}。",
            value={
                "close": close, "ma20": ma20, "ma60": ma60,
                "ma20_change_5d": ma20 - ma20_prev,
                "ma60_change_5d": ma60 - ma60_prev,
            },
            source="completed index daily bars",
        )

    def _momentum_evidence(self, ret5, ret20):
        direction = (
            EvidenceDirection.SUPPORT if ret5 > 0 and ret20 > 0
            else EvidenceDirection.OPPOSE if ret5 < 0 and ret20 < 0
            else EvidenceDirection.NEUTRAL
        )
        return Evidence(
            code="MARKET_MOMENTUM",
            engine=EvidenceEngine.MARKET,
            title="大盤動能",
            direction=direction,
            severity=EvidenceSeverity.MEDIUM,
            explanation=f"近 5 日 {ret5*100:.2f}%，近 20 日 {ret20*100:.2f}%。",
            value={"return_5d": ret5, "return_20d": ret20},
            threshold=0.0,
            source="completed index daily bars",
        )

    def _volume_evidence(self, volume_ratio):
        if volume_ratio is None:
            return Evidence(
                code="MARKET_VOLUME_UNAVAILABLE",
                engine=EvidenceEngine.MARKET,
                title="大盤量能",
                direction=EvidenceDirection.UNAVAILABLE,
                severity=EvidenceSeverity.LOW,
                explanation="大盤量能資料不足，不納入判斷。",
                value=None,
                threshold=1.0,
                source="completed index daily bars",
                data_valid=False,
            )
        direction = (
            EvidenceDirection.SUPPORT if volume_ratio >= 1.05
            else EvidenceDirection.OPPOSE if volume_ratio < 0.75
            else EvidenceDirection.NEUTRAL
        )
        return Evidence(
            code="MARKET_VOLUME_RATIO",
            engine=EvidenceEngine.MARKET,
            title="大盤量能",
            direction=direction,
            severity=EvidenceSeverity.LOW,
            explanation=f"最新量能為 20 日均量的 {volume_ratio:.2f} 倍。",
            value=volume_ratio,
            threshold=1.0,
            source="completed index daily bars",
        )

    def _risk_evidence(self, atr_pct, international_risk_flag):
        direction = (
            EvidenceDirection.OPPOSE
            if atr_pct > 0.03 or international_risk_flag
            else EvidenceDirection.NEUTRAL
        )
        return Evidence(
            code="MARKET_RISK",
            engine=EvidenceEngine.MARKET,
            title="大盤波動與外部風險",
            direction=direction,
            severity=EvidenceSeverity.HIGH if international_risk_flag else EvidenceSeverity.MEDIUM,
            explanation=(
                f"ATR14/收盤為 {atr_pct*100:.2f}%；"
                f"國際市場急跌警示={'是' if international_risk_flag else '否'}。"
            ),
            value={
                "atr_pct": atr_pct,
                "international_risk_flag": international_risk_flag,
            },
            threshold={"atr_pct": 0.03},
            source="completed index daily bars + external risk flag",
        )

    def _sma(self, bars, index, period):
        return sum(bar.close for bar in bars[index-period+1:index+1]) / period

    def _atr_pct(self, bars, index, period):
        start = index - period + 1
        true_ranges = []
        for idx in range(start, index + 1):
            bar = bars[idx]
            previous_close = bars[idx - 1].close
            true_ranges.append(max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            ))
        return (sum(true_ranges) / period) / bars[index].close

    def _volume_ratio(self, bars, index, period):
        window = bars[index-period+1:index+1]
        if any(bar.volume is None or bar.volume <= 0 for bar in window):
            return None
        average = sum(bar.volume for bar in window) / period
        return bars[index].volume / average if average > 0 else None
