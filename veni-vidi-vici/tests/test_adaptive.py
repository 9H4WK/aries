import math

from aries.adaptive import (
    AdaptiveEngine,
    AdaptiveState,
    OnlineCalibrator,
    blend_logit,
    brownian_bridge_p_up,
)
from aries.models import IndicatorState, ProbabilityState


def _indicator(vol_60s=0.0004):
    return IndicatorState(
        ma_fast=None,
        ma_mid=None,
        ma_slow=None,
        ma_anchor=None,
        pressure="CHOP",
        ma_fast_slope_bps=None,
        ma_mid_slope_bps=None,
        ma_anchor_slope_bps=None,
        up_stability_score=0,
        down_stability_score=0,
        velocity_5s_bps=None,
        velocity_15s_bps=None,
        volatility_60s=vol_60s,
        persistence_above_target_seconds=0,
        persistence_below_target_seconds=0,
        current_volume=None,
        volume_ma_5=None,
        volume_ma_10=None,
        volume_spike=None,
        orderbook_imbalance=None,
    )


class _Quote:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask

    @property
    def mid(self):
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0


class _Getter:
    """Minimal config.get_path stand-in backed by a dict."""

    def __init__(self, values=None):
        self.values = values or {}

    def __call__(self, path, default=None):
        return self.values.get(path, default)


def test_bridge_p_up_monotone_in_distance():
    base = brownian_bridge_p_up(0.0, 4.0, 120.0)
    up = brownian_bridge_p_up(20.0, 4.0, 120.0)
    down = brownian_bridge_p_up(-20.0, 4.0, 120.0)
    assert base == 0.5
    assert up > base > down


def test_bridge_p_up_time_decay_toward_coinflip():
    early = brownian_bridge_p_up(10.0, 4.0, 280.0)
    late = brownian_bridge_p_up(10.0, 4.0, 20.0)
    assert late > early > 0.5


def test_bridge_p_up_handles_missing_inputs():
    assert brownian_bridge_p_up(None, 4.0, 100.0) is None
    assert brownian_bridge_p_up(10.0, None, 100.0) is None
    assert brownian_bridge_p_up(10.0, 0.0, 100.0) is None
    assert brownian_bridge_p_up(10.0, 4.0, None) is None


def test_blend_logit_renormalizes_missing_components():
    blended = blend_logit([(0.7, 0.55), (None, 0.30), (0.9, 0.15)])
    expected = (0.55 * math.log(0.7 / 0.3) + 0.15 * math.log(0.9 / 0.1)) / 0.70
    assert abs(blended - expected) < 1e-9
    assert blend_logit([(None, 0.5)]) is None


def test_calibrator_converges_on_synthetic_signal():
    import random

    rng = random.Random(11)
    samples = []
    for _ in range(300):
        z = rng.uniform(-3, 3)
        p_raw = 1 / (1 + math.exp(-z))
        y = rng.random() < 1 / (1 + math.exp(-(1.2 * z - 0.2)))
        samples.append((p_raw, y))
    state = AdaptiveState()
    calibrator = OnlineCalibrator()
    fitted = calibrator.batch_fit(samples, state)
    assert fitted == len(samples)
    assert 0.5 < state.a < 3.0
    # A high raw probability must map above a low one.
    assert calibrator.apply(0.9, state) > calibrator.apply(0.2, state)


def test_online_observe_updates_state_and_brier():
    getter = _Getter({"adaptive.calibration_enabled": True})
    engine = AdaptiveEngine(getter)
    before = engine.state.samples
    engine.observe_outcome(0.8, True)
    engine.observe_outcome(0.8, False)
    assert engine.state.samples == before + 2
    assert 0.0 < engine.state.brier_ema < 1.0


def test_loss_streak_throttle_reduces_kelly_then_recovers():
    getter = _Getter(
        {
            "adaptive.drawdown_throttle_enabled": True,
            "adaptive.loss_streak_decay": 0.8,
            "adaptive.loss_streak_floor": 0.4,
            "adaptive.win_recovery": 1.1,
            "adaptive.kelly_calibration_enabled": False,
        }
    )
    engine = AdaptiveEngine(getter)
    start = engine.kelly_multiplier()
    for _ in range(6):
        engine.observe_outcome(0.7, False)
    throttled = engine.kelly_multiplier()
    assert throttled < start
    assert throttled >= 0.4 * 0.95
    for _ in range(12):
        engine.observe_outcome(0.7, True)
    assert engine.kelly_multiplier() > throttled


def test_kelly_calibration_scales_with_brier_skill():
    getter = _Getter(
        {
            "adaptive.kelly_calibration_enabled": True,
            "adaptive.min_calibration_samples": 10,
            "adaptive.brier_baseline": 0.25,
            "adaptive.kelly_min_multiplier": 0.6,
            "adaptive.kelly_max_multiplier": 1.25,
            "adaptive.drawdown_throttle_enabled": False,
        }
    )
    engine = AdaptiveEngine(getter)
    engine.state.samples = 50
    engine.state.brier_ema = 0.10  # strong skill -> ratio 2.5 -> capped 1.25
    assert engine.kelly_multiplier() == 1.25
    engine.state.brier_ema = 0.40  # worse than coinflip -> ratio 0.625
    assert abs(engine.kelly_multiplier() - 0.625) < 1e-9


def test_augment_probability_blends_and_calibrates():
    getter = _Getter(
        {
            "adaptive.enabled": True,
            "adaptive.calibration_enabled": True,
            "adaptive.model_weight": 0.55,
            "adaptive.bridge_weight": 0.30,
            "adaptive.market_mid_weight": 0.15,
            "adaptive.min_calibration_samples": 5,
        }
    )
    engine = AdaptiveEngine(getter)
    engine.state.samples = 100
    prob = ProbabilityState(
        p_up=0.70,
        p_down=0.30,
        confidence=0.4,
        distance_from_target=15.0,
        distance_bps=2.0,
        inside_noise_band=False,
        reason="estimated",
    )
    out, comps = engine.augment_probability(
        prob,
        indicators=_indicator(),
        up_quote=_Quote(0.66, 0.70),
        down_quote=_Quote(0.30, 0.34),
        seconds_remaining=120,
    )
    assert comps.p_bridge is not None
    assert comps.p_market_mid is not None
    assert comps.blended is not None
    assert out.p_down == 1.0 - out.p_up
    assert 0.01 <= out.p_up <= 0.99
    assert out.confidence == min(1.0, abs(out.p_up - 0.5) * 2.0)


def test_augment_probability_passthrough_when_disabled():
    getter = _Getter({"adaptive.enabled": False})
    engine = AdaptiveEngine(getter)
    prob = ProbabilityState(
        p_up=0.70,
        p_down=0.30,
        confidence=0.4,
        distance_from_target=None,
        distance_bps=None,
        inside_noise_band=False,
        reason="estimated",
    )
    out, comps = engine.augment_probability(prob)
    assert out.p_up == 0.70
    assert comps.blended is None


def test_snapshot_roundtrip_through_dict():
    payload = {
        "a": 1.23,
        "b": -0.1,
        "samples": 42,
        "brier_ema": 0.12,
        "loss_streak_mult": 0.8,
        "wins": 30,
        "losses": 12,
    }
    restored = AdaptiveState(**payload)
    assert restored.samples == 42
    assert restored.loss_streak_mult == 0.8
