from __future__ import annotations

import math
from typing import Optional

from .models import BinanceState, IndicatorState, ProbabilityState


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def pressure_score(ind: IndicatorState) -> float:
    if ind.pressure == "UP":
        return 1.0
    if ind.pressure == "DOWN":
        return -1.0
    if ind.ma_fast is not None and ind.ma_slow is not None and ind.ma_slow != 0:
        return _clamp(((ind.ma_fast - ind.ma_slow) / ind.ma_slow) * 10_000 / 5.0, -1.0, 1.0)
    return 0.0


def estimate_probability(config: dict, state: BinanceState, ind: IndicatorState, target_price: Optional[float]) -> ProbabilityState:
    if state.price is None:
        return ProbabilityState(None, None, 0.0, None, None, False, "binance_price_missing")
    if target_price is None:
        return ProbabilityState(None, None, 0.0, None, None, False, "target_missing")

    pconf = config["probability"]
    distance = state.price - target_price
    distance_bps = (distance / target_price) * 10_000 if target_price else 0.0
    inside_noise = abs(distance) < float(pconf["noise_band_usd"])

    d_score = _clamp(distance_bps / float(pconf["distance_bps_scale"]), -3.0, 3.0)
    v5 = ind.velocity_5s_bps or 0.0
    v_score = _clamp(v5 / float(pconf["velocity_bps_scale"]), -3.0, 3.0)
    ma_score = pressure_score(ind)

    if ind.persistence_above_target_seconds >= 5:
        pers_score = 1.0
    elif ind.persistence_below_target_seconds >= 5:
        pers_score = -1.0
    else:
        pers_score = 0.0

    raw = (
        float(pconf["distance_weight"]) * d_score
        + float(pconf["velocity_weight"]) * v_score
        + float(pconf["ma_weight"]) * ma_score
        + float(pconf["persistence_weight"]) * pers_score
    )
    p_up = _sigmoid(raw)
    p_up = _clamp(p_up, float(pconf["clamp_min"]), float(pconf["clamp_max"]))
    p_down = 1.0 - p_up

    confidence = min(1.0, abs(p_up - 0.5) * 2.0)
    if inside_noise:
        confidence *= 0.25

    return ProbabilityState(p_up, p_down, confidence, distance, distance_bps, inside_noise, "estimated")
