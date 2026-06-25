from __future__ import annotations

import math
from typing import Optional

from .models import BinanceState, IndicatorState, ProbabilityState


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def pressure_score(ind: IndicatorState) -> float:
    """Convert MA alignment into a normalized bearish/bullish score."""
    if ind.pressure == "UP":
        return 1.0
    if ind.pressure == "DOWN":
        return -1.0
    if ind.ma_fast is not None and ind.ma_slow is not None and ind.ma_slow != 0:
        return _clamp(((ind.ma_fast - ind.ma_slow) / ind.ma_slow) * 10_000 / 5.0, -1.0, 1.0)
    return 0.0


def _slope_score(ind: IndicatorState, scale: float) -> float:
    fast_slope = ind.ma_fast_slope_bps or 0.0
    mid_slope = ind.ma_mid_slope_bps or 0.0
    return _clamp(((fast_slope + mid_slope) / 2.0) / scale, -2.0, 2.0)


def _volume_score(ind: IndicatorState, directional_bias: float, scale: float) -> float:
    if ind.volume_spike is None:
        return 0.0
    spike_strength = max(0.0, ind.volume_spike - 1.0)
    return _clamp(spike_strength / scale, 0.0, 2.0) * directional_bias


def _technical_scores(config: dict, ind: IndicatorState) -> tuple[float, float, float, float, float, str]:
    """Return MA multiplier, raw multiplier, ADX/VWAP/MACD scores, and reason."""
    tech = config.get("technical_indicators", {})
    if not bool(tech.get("enabled", False)):
        return 1.0, 1.0, 0.0, 0.0, 0.0, ""

    ma_multiplier = 1.0
    raw_multiplier = 1.0
    adx_score = 0.0
    reasons: list[str] = []
    if bool(tech.get("adx_enabled", True)) and ind.adx is not None:
        weak = float(tech.get("adx_weak_threshold", 20.0))
        strong = float(tech.get("adx_strong_threshold", 25.0))
        rising_min = float(tech.get("adx_rising_slope_min", 0.0))
        if ind.adx < weak:
            # Weak ADX means trend signals are less reliable, so pull raw score toward 0.
            neutral_pull = _clamp(float(tech.get("adx_weak_neutral_pull", 0.35)), 0.0, 0.95)
            raw_multiplier = 1.0 - neutral_pull
            adx_score = -1.0
            reasons.append("adx_weak")
        elif ind.adx >= strong:
            rising = (ind.adx_slope or 0.0) >= rising_min
            boost = float(tech.get("adx_strong_ma_boost", 0.35)) * (1.0 if rising else 0.50)
            ma_multiplier = 1.0 + max(0.0, boost)
            adx_score = 1.0 if rising else 0.50
            reasons.append("adx_strong_rising" if rising else "adx_strong")

    vwap_score = 0.0
    if bool(tech.get("vwap_enabled", True)) and ind.vwap_distance_bps is not None:
        distance = float(ind.vwap_distance_bps)
        overextension = abs(float(tech.get("vwap_overextension_bps", 8.0)))
        scale = max(0.1, float(tech.get("vwap_distance_bps_scale", 8.0)))
        sign = 1.0 if distance > 0 else -1.0 if distance < 0 else 0.0
        if abs(distance) >= overextension:
            # Far from VWAP is a chase warning, so the nudge leans mildly back toward VWAP.
            vwap_score = -sign * _clamp((abs(distance) - overextension) / scale, 0.0, 1.0)
            reasons.append("vwap_overextended")
        else:
            # Near VWAP, price above/below is mild directional confirmation.
            vwap_score = sign * _clamp(abs(distance) / max(overextension, 0.1), 0.0, 1.0)

    macd_score = 0.0
    if bool(tech.get("macd_enabled", True)) and ind.macd_histogram_slope_bps is not None:
        scale = max(0.1, float(tech.get("macd_histogram_slope_bps_scale", 1.5)))
        macd_score = _clamp(float(ind.macd_histogram_slope_bps) / scale, -2.0, 2.0)
        if macd_score > 0:
            reasons.append("macd_hist_rising")
        elif macd_score < 0:
            reasons.append("macd_hist_falling")

    return ma_multiplier, raw_multiplier, adx_score, vwap_score, macd_score, "+".join(reasons)


def estimate_probability(
    config: dict,
    state: BinanceState,
    ind: IndicatorState,
    target_price: Optional[float],
    seconds_remaining: Optional[int],
) -> ProbabilityState:
    """Combine Binance/target signals into Aries' estimated UP/DOWN probability.

    The feature weights and scales live under `probability` in config.yaml.
    This is the primary function to change when tuning the directional model.
    """
    if state.price is None:
        return ProbabilityState(None, None, 0.0, None, None, False, "binance_price_missing")
    if target_price is None:
        return ProbabilityState(None, None, 0.0, None, None, False, "target_missing")

    pconf = config["probability"]
    distance = state.price - target_price
    distance_bps = (distance / target_price) * 10_000 if target_price else 0.0
    inside_noise = abs(distance) < float(pconf["noise_band_usd"])

    # Each input is normalized before weighting so one raw unit cannot dominate.
    d_score = _clamp(distance_bps / float(pconf["distance_bps_scale"]), -3.0, 3.0)
    v5 = ind.velocity_5s_bps or 0.0
    v_score = _clamp(v5 / float(pconf["velocity_bps_scale"]), -3.0, 3.0)
    ma_score = pressure_score(ind)
    slope_score = _slope_score(ind, float(pconf.get("slope_bps_scale", 3.0)))
    imbalance_score = _clamp((ind.orderbook_imbalance or 0.0) / float(pconf.get("imbalance_scale", 0.35)), -2.0, 2.0)

    if ind.persistence_above_target_seconds >= 5:
        pers_score = 1.0
    elif ind.persistence_below_target_seconds >= 5:
        pers_score = -1.0
    else:
        pers_score = 0.0

    directional_bias = _clamp((d_score + ma_score + slope_score) / 3.0, -1.0, 1.0)
    volume_score = _volume_score(ind, directional_bias, float(pconf.get("volume_spike_scale", 0.75)))
    interval_seconds = int(config.get("bot", {}).get("interval_seconds", 300))
    if seconds_remaining is None:
        time_factor = 1.0
    else:
        elapsed_fraction = 1.0 - _clamp(seconds_remaining / max(1.0, float(interval_seconds)), 0.0, 1.0)
        time_factor = 0.35 + (0.65 * elapsed_fraction)

    ma_multiplier, raw_multiplier, adx_score, vwap_score, macd_score, technical_reason = _technical_scores(config, ind)
    technical = config.get("technical_indicators", {})
    base_ma_weight = float(pconf["ma_weight"])
    ma_weight = base_ma_weight * ma_multiplier
    vwap_weight = float(technical.get("vwap_probability_weight", 0.08)) if bool(technical.get("enabled", False)) else 0.0
    macd_weight = float(technical.get("macd_probability_weight", 0.20)) if bool(technical.get("enabled", False)) else 0.0

    # These normalized microstructure features are research-supported inputs, but
    # the weights are Aries defaults, not statistically calibrated probabilities.
    # Validate and tune them only from settled out-of-sample paper windows.
    base_raw = (
        float(pconf["distance_weight"]) * d_score
        + float(pconf["velocity_weight"]) * v_score
        + base_ma_weight * ma_score
        + float(pconf.get("slope_weight", 0.50)) * slope_score
        + float(pconf.get("volume_weight", 0.35)) * volume_score
        + float(pconf.get("imbalance_weight", 0.45)) * imbalance_score
        + float(pconf["persistence_weight"]) * pers_score
    )
    technical_raw = ((ma_weight - base_ma_weight) * ma_score) + (vwap_weight * vwap_score) + (macd_weight * macd_score)
    max_technical_adjustment = abs(float(technical.get("max_raw_adjustment", 0.50)))
    if max_technical_adjustment > 0:
        technical_raw = _clamp(technical_raw, -max_technical_adjustment, max_technical_adjustment)
    raw = (base_raw + technical_raw) * raw_multiplier
    # Later in the window, target distance and trend receive more influence.
    raw *= time_factor
    p_up = _sigmoid(raw)
    p_up = _clamp(p_up, float(pconf["clamp_min"]), float(pconf["clamp_max"]))
    p_down = 1.0 - p_up

    confidence = min(1.0, abs(p_up - 0.5) * 2.0)
    if inside_noise:
        confidence *= 0.25

    return ProbabilityState(
        p_up,
        p_down,
        confidence,
        distance,
        distance_bps,
        inside_noise,
        "estimated" + (f"+{technical_reason}" if technical_reason else ""),
        raw_score=raw,
        distance_score=d_score,
        ma_score=ma_score,
        slope_score=slope_score,
        volume_score=volume_score,
        imbalance_score=imbalance_score,
        time_factor=time_factor,
        adx_score=adx_score,
        vwap_score=vwap_score,
        macd_score=macd_score,
    )
