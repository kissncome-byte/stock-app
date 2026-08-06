"""Price Engine unit tests."""

import pytest

from stockpilot.engines import PriceEngine, PriceEngineError
from stockpilot.models import PriceEngineInput


def test_builds_one_consistent_price_set() -> None:
    levels = PriceEngine().build(
        PriceEngineInput(
            current_price=100.0,
            atr=4.0,
            ma20=103.0,
            ma60=88.0,
            recent_resistance=110.0,
            breakout_level=105.0,
            recent_swing_low=94.0,
            platform_floor=86.0,
            original_target=118.0,
            fair_value=101.0,
            tick_size=0.5,
        )
    )

    assert levels.confirmation.value == 103.0
    assert levels.moving_protection.value == 94.0
    assert levels.structural_exit.value == 88.0
    assert levels.first_target.value == 108.0
    assert levels.structural_exit.value < levels.moving_protection.value
    assert levels.moving_protection.value < 100.0
    assert levels.first_target.value > 100.0
    assert levels.reward_risk_ratio == pytest.approx(8.0 / 6.0)


def test_missing_candidates_are_explicitly_unavailable() -> None:
    levels = PriceEngine().build(
        PriceEngineInput(
            current_price=100.0,
            tick_size=0.5,
        )
    )

    assert levels.confirmation.valid is False
    assert levels.moving_protection.valid is False
    assert levels.structural_exit.valid is False
    assert levels.first_target.valid is False
    assert levels.reward_risk_ratio is None


def test_rejects_invalid_current_price() -> None:
    with pytest.raises(PriceEngineError):
        PriceEngine().build(
            PriceEngineInput(current_price=0.0)
        )


def test_structural_exit_remains_below_moving_protection() -> None:
    levels = PriceEngine().build(
        PriceEngineInput(
            current_price=200.0,
            atr=10.0,
            ma60=190.0,
            recent_swing_low=185.0,
            platform_floor=170.0,
        )
    )

    assert levels.moving_protection.value == 185.0
    assert levels.structural_exit.value == 170.0
