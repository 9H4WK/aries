from types import SimpleNamespace

from aries.models import BinanceState
from aries.probability import estimate_probability


def config():
    return {
        "bot": {"interval_seconds": 300},
        "probability": {
            "noise_band_usd": 0.0,
            "distance_bps_scale": 10.0,
            "velocity_bps_scale": 5.0,
            "slope_bps_scale": 3.0,
            "volume_spike_scale": 0.75,
            "imbalance_scale": 0.35,
            "distance_weight": 1.0,
            "ma_weight": 0.75,
            "velocity_weight": 0.35,
            "slope_weight": 0.50,
            "volume_weight": 0.35,
            "imbalance_weight": 0.45,
            "persistence_weight": 0.25,
            "clamp_min": 0.01,
            "clamp_max": 0.99,
        },
        "technical_indicators": {
            "enabled": True,
            "adx_enabled": True,
            "adx_weak_threshold": 20.0,
            "adx_strong_threshold": 25.0,
            "adx_weak_neutral_pull": 0.50,
            "adx_strong_ma_boost": 0.50,
            "vwap_enabled": True,
            "vwap_distance_bps_scale": 8.0,
            "vwap_overextension_bps": 8.0,
            "vwap_probability_weight": 0.08,
            "macd_enabled": True,
            "macd_histogram_slope_bps_scale": 1.5,
            "macd_probability_weight": 0.20,
            "max_raw_adjustment": 0.50,
        },
    }


def indicator(**overrides):
    values = {
        "pressure": "UP",
        "ma_fast": 101.0,
        "ma_slow": 100.0,
        "ma_fast_slope_bps": 0.5,
        "ma_mid_slope_bps": 0.4,
        "velocity_5s_bps": 1.0,
        "orderbook_imbalance": 0.1,
        "volume_spike": 1.2,
        "persistence_above_target_seconds": 8,
        "persistence_below_target_seconds": 0,
        "adx": 30.0,
        "adx_slope": 1.0,
        "vwap_distance_bps": 2.0,
        "macd_histogram_slope_bps": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_weak_adx_pulls_probability_toward_neutral():
    state = BinanceState(price=101.0, best_bid=None, best_ask=None, best_bid_qty=None, best_ask_qty=None, ts=1.0)
    strong = estimate_probability(config(), state, indicator(adx=30.0, adx_slope=1.0), 100.0, 200)
    weak = estimate_probability(config(), state, indicator(adx=10.0, adx_slope=-1.0), 100.0, 200)

    assert strong.p_up > weak.p_up
    assert weak.confidence < strong.confidence
    assert weak.adx_score == -1.0


def test_macd_and_vwap_scores_are_preserved_in_probability_state():
    state = BinanceState(price=101.0, best_bid=None, best_ask=None, best_bid_qty=None, best_ask_qty=None, ts=1.0)
    probability = estimate_probability(
        config(),
        state,
        indicator(vwap_distance_bps=20.0, macd_histogram_slope_bps=-2.0),
        100.0,
        200,
    )

    assert probability.vwap_score < 0
    assert probability.macd_score < 0
    assert "vwap_overextended" in probability.reason
    assert "macd_hist_falling" in probability.reason
