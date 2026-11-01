import math

from aries.models import ProbabilityState
from aries.support_resistance import SRCandle, apply_support_resistance_bias, calculate_support_resistance


def _config(**overrides):
    config = {
        "probability": {"clamp_min": 0.01, "clamp_max": 0.99},
        "support_resistance": {
            "enabled": True,
            "candle_seconds": 15,
            "lookback_minutes": [30, 60],
            "include_3h": False,
            "zone_width_usd": 1.0,
            "zone_width_bps": 0.0,
            "swing_left": 2,
            "swing_right": 2,
            "reaction_candles": 4,
            "retest_candles": 6,
            "breakout_buffer_zones": 1.0,
            "apply_to_probability": True,
            "probability_weight": 0.10,
            "max_probability_adjustment": 0.05,
        },
    }
    config["support_resistance"].update(overrides)
    return config


def _wave_candles(count=180):
    candles = []
    previous = 105.0
    for index in range(count):
        close = 105.0 + math.sin(index / 4.0) * 5.0
        high = max(previous, close) + 1.0
        low = min(previous, close) - 1.0
        candles.append(SRCandle(1_700_000_000 + (index * 15), previous, high, low, close))
        previous = close
    return candles


def test_support_resistance_finds_nearest_zones():
    state = calculate_support_resistance(_config(), _wave_candles(), current_price=105.0, now_ts=1.0)

    assert state.enabled is True
    assert state.zones_count > 0
    assert state.nearest_support is not None
    assert state.nearest_resistance is not None
    assert state.nearest_support < 105.0
    assert state.nearest_resistance > 105.0
    assert state.nested_structure in {"SUPPORT_NESTED", "RESISTANCE_NESTED", "BOTH_NESTED", "NONE"}


def test_breakout_uses_recently_broken_resistance():
    candles = _wave_candles()
    last_ts = candles[-1].ts
    candles.extend(
        [
            SRCandle(last_ts + 15, 109.0, 112.0, 108.0, 111.5),
            SRCandle(last_ts + 30, 111.5, 114.0, 111.0, 113.5),
        ]
    )

    state = calculate_support_resistance(_config(zone_width_usd=0.75), candles, current_price=113.5, now_ts=1.0)

    assert state.breakout_state == "BREAKOUT_UP"
    assert state.bias_side == "UP"
    assert "breakout_up" in state.reason


def test_support_resistance_bias_nudges_probability_with_cap():
    state = calculate_support_resistance(_config(), _wave_candles(), current_price=105.0, now_ts=1.0)
    state.bias_score = 1.0
    state.bias_side = "UP"
    probability = ProbabilityState(
        p_up=0.50,
        p_down=0.50,
        confidence=0.0,
        distance_from_target=0.0,
        distance_bps=0.0,
        inside_noise_band=False,
        reason="base",
    )

    adjusted = apply_support_resistance_bias(_config(max_probability_adjustment=0.03), probability, state)

    assert adjusted.p_up == 0.53
    assert adjusted.p_down == 0.47
    assert adjusted.reason == "base+sr_up"
