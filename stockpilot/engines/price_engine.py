"""Single-source Price Engine.

The engine accepts already-prepared market inputs and returns the only
PriceLevels object allowed to be consumed by the rest of StockPilot.
It does not fetch data, read Streamlit state, or make strategy decisions.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from stockpilot.models import PriceEngineInput, PriceLevel, PriceLevels


class PriceEngineError(ValueError):
    """Raised when valid, internally consistent price levels cannot be built."""


class PriceEngine:
    """Build and validate one canonical set of price levels."""

    def build(self, data: PriceEngineInput) -> PriceLevels:
        self._validate_input(data)

        confirmation_value, confirmation_source = self._confirmation(data)
        moving_value, moving_source = self._moving_protection(data)
        structural_value, structural_source = self._structural_exit(
            data,
            moving_value,
        )
        first_target_value, target_source = self._first_target(data)

        entry_low, entry_high, entry_source = self._entry_zone(data)
        extended_value = self._extended_target(
            data,
            first_target_value,
        )

        risk_pct = self._pct_change(
            data.current_price,
            moving_value,
            downside=True,
        )
        reward_pct = self._pct_change(
            data.current_price,
            first_target_value,
            downside=False,
        )
        ratio = (
            reward_pct / risk_pct
            if reward_pct is not None
            and risk_pct is not None
            and risk_pct > 0
            else None
        )

        levels = PriceLevels(
            entry_zone_low=self._level(
                entry_low,
                entry_source,
                entry_low is not None,
                "support intersection with ATR buffer",
            ),
            entry_zone_high=self._level(
                entry_high,
                entry_source,
                entry_high is not None,
                "support intersection with ATR buffer",
            ),
            confirmation=self._level(
                confirmation_value,
                confirmation_source,
                confirmation_value is not None,
                "nearest valid trend-confirmation event above current price",
            ),
            moving_protection=self._level(
                moving_value,
                moving_source,
                moving_value is not None,
                "nearest valid first-layer protection below current price",
            ),
            structural_exit=self._level(
                structural_value,
                structural_source,
                structural_value is not None,
                "major structural invalidation below moving protection",
            ),
            first_target=self._level(
                first_target_value,
                target_source,
                first_target_value is not None,
                "nearest executable upside objective",
            ),
            extended_target=self._level(
                extended_value,
                "ATR extension",
                extended_value is not None,
                "secondary objective; not the first exit basis",
            ),
            reward_pct=reward_pct,
            risk_pct=risk_pct,
            reward_risk_ratio=ratio,
        )
        self.validate_levels(levels, data.current_price)
        return levels

    def validate_levels(
        self,
        levels: PriceLevels,
        current_price: float,
    ) -> None:
        """Raise PriceEngineError if canonical levels conflict."""

        if current_price <= 0 or not math.isfinite(current_price):
            raise PriceEngineError("current_price must be a finite positive number")

        structural = levels.structural_exit.value
        moving = levels.moving_protection.value
        target = levels.first_target.value
        entry_low = levels.entry_zone_low.value
        entry_high = levels.entry_zone_high.value

        if structural is not None and moving is not None:
            if not structural < moving:
                raise PriceEngineError(
                    "structural_exit must be lower than moving_protection"
                )

        if moving is not None and not moving < current_price:
            raise PriceEngineError(
                "moving_protection must be lower than current_price"
            )

        if target is not None and not target > current_price:
            raise PriceEngineError(
                "first_target must be higher than current_price"
            )

        if entry_low is not None and entry_high is not None:
            if entry_low > entry_high:
                raise PriceEngineError(
                    "entry_zone_low must not exceed entry_zone_high"
                )

    def _validate_input(self, data: PriceEngineInput) -> None:
        if (
            data.current_price <= 0
            or not math.isfinite(data.current_price)
        ):
            raise PriceEngineError(
                "current_price must be a finite positive number"
            )
        if data.tick_size <= 0 or not math.isfinite(data.tick_size):
            raise PriceEngineError(
                "tick_size must be a finite positive number"
            )
        if data.atr is not None and data.atr <= 0:
            raise PriceEngineError("atr must be positive when supplied")

    def _confirmation(
        self,
        data: PriceEngineInput,
    ) -> tuple[float | None, str]:
        candidates = [
            (data.ma20, "MA20"),
            (data.recent_resistance, "recent resistance"),
            (data.breakout_level, "platform breakout"),
        ]
        return self._nearest_above(
            data.current_price,
            candidates,
            data.tick_size,
        )

    def _moving_protection(
        self,
        data: PriceEngineInput,
    ) -> tuple[float | None, str]:
        candidates: list[tuple[float | None, str]] = [
            (data.recent_swing_low, "recent swing low"),
        ]
        if data.atr is not None:
            candidates.append(
                (
                    data.current_price - 1.5 * data.atr,
                    "1.5 ATR trailing protection",
                )
            )
        return self._nearest_below(
            data.current_price,
            candidates,
            data.tick_size,
        )

    def _structural_exit(
        self,
        data: PriceEngineInput,
        moving_protection: float | None,
    ) -> tuple[float | None, str]:
        upper_limit = (
            moving_protection
            if moving_protection is not None
            else data.current_price
        )
        candidates = [
            (data.platform_floor, "platform floor"),
            (data.ma60, "MA60 structural defence"),
        ]
        if data.atr is not None:
            candidates.append(
                (
                    data.current_price - 3.0 * data.atr,
                    "3 ATR structural exit",
                )
            )
        return self._nearest_below(
            upper_limit,
            candidates,
            data.tick_size,
        )

    def _first_target(
        self,
        data: PriceEngineInput,
    ) -> tuple[float | None, str]:
        candidates: list[tuple[float | None, str]] = [
            (data.recent_resistance, "recent resistance"),
            (data.original_target, "legacy target"),
        ]
        if data.atr is not None:
            candidates.append(
                (
                    data.current_price + 2.0 * data.atr,
                    "2 ATR objective",
                )
            )
        return self._nearest_above(
            data.current_price,
            candidates,
            data.tick_size,
        )

    def _entry_zone(
        self,
        data: PriceEngineInput,
    ) -> tuple[float | None, float | None, str]:
        anchors = self._finite_positive(
            [data.ma20, data.fair_value, data.breakout_level]
        )
        if not anchors:
            return None, None, "unavailable"

        anchor_low = min(anchors)
        anchor_high = max(anchors)
        buffer_value = (
            0.5 * data.atr
            if data.atr is not None
            else data.current_price * 0.015
        )
        low = self._round_to_tick(
            max(data.tick_size, anchor_low - buffer_value),
            data.tick_size,
        )
        high = self._round_to_tick(
            anchor_high + buffer_value,
            data.tick_size,
        )
        return low, high, "MA20/fair-value/breakout intersection"

    def _extended_target(
        self,
        data: PriceEngineInput,
        first_target: float | None,
    ) -> float | None:
        if first_target is None or data.atr is None:
            return None
        return self._round_to_tick(
            first_target + 2.0 * data.atr,
            data.tick_size,
        )

    def _nearest_above(
        self,
        boundary: float,
        candidates: Iterable[tuple[float | None, str]],
        tick_size: float,
    ) -> tuple[float | None, str]:
        valid = [
            (float(value), source)
            for value, source in candidates
            if self._is_positive_finite(value)
            and float(value) > boundary
        ]
        if not valid:
            return None, "unavailable"
        value, source = min(valid, key=lambda item: item[0])
        return self._round_to_tick(value, tick_size), source

    def _nearest_below(
        self,
        boundary: float,
        candidates: Iterable[tuple[float | None, str]],
        tick_size: float,
    ) -> tuple[float | None, str]:
        valid = [
            (float(value), source)
            for value, source in candidates
            if self._is_positive_finite(value)
            and float(value) < boundary
        ]
        if not valid:
            return None, "unavailable"
        value, source = max(valid, key=lambda item: item[0])
        return self._round_to_tick(value, tick_size), source

    def _level(
        self,
        value: float | None,
        source: str,
        valid: bool,
        formula: str,
    ) -> PriceLevel:
        return PriceLevel(
            value=value,
            source=source,
            valid=valid,
            formula=formula,
            confidence="rule_based" if valid else None,
        )

    def _pct_change(
        self,
        current: float,
        level: float | None,
        *,
        downside: bool,
    ) -> float | None:
        if level is None:
            return None
        raw = (
            (current - level) / current
            if downside
            else (level - current) / current
        )
        if raw <= 0:
            return None
        return raw * 100.0

    def _round_to_tick(self, value: float, tick_size: float) -> float:
        return round(round(value / tick_size) * tick_size, 10)

    def _finite_positive(
        self,
        values: Iterable[float | None],
    ) -> list[float]:
        return [
            float(value)
            for value in values
            if self._is_positive_finite(value)
        ]

    def _is_positive_finite(self, value: float | None) -> bool:
        return (
            value is not None
            and math.isfinite(float(value))
            and float(value) > 0
        )
