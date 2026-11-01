from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

# Polymarket BTC 5m markets resolve on a Chainlink TWAP: the price to beat is
# the TWAP at window open and the final price is the TWAP over the last
# `lookback` seconds. This module estimates P(final >= open) from Binance 1s
# closes with a Brownian model of that TWAP, calibrated on official outcomes
# (fat-tail volatility multiplier plus a small resolution-mismatch flip rate).
# Binance 1s closes trend: about a quarter of the last 2s change continues over
# the next 15s, so the expected path adds that drift to every remaining second.

MOMENTUM_LEGS = ((0, 2), (2, 5), (5, 10))  # seconds back for each price change


@dataclass
class TwapEstimate:
    p_up: float
    z_score: float
    sigma_per_second: float
    expected_final: float
    open_twap: float


def _get(config: Any, path: str, default: Any = None) -> Any:
    if hasattr(config, "get_path"):
        return config.get_path(path, default)
    value: Any = config
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _per_second_prices(bars: list[tuple[int, float]], first: int, last: int) -> Optional[list[float]]:
    """Return forward-filled 1s closes for seconds first..last, or None if history is missing."""
    by_second = {int(second): float(price) for second, price in bars}
    seed = None
    for second, price in bars:
        if int(second) < first:
            seed = float(price)
        else:
            break
    if seed is None:
        seed = by_second.get(first)
    if seed is None:
        return None
    prices, current = [], seed
    for second in range(first, last + 1):
        current = by_second.get(second, current)
        prices.append(current)
    return prices


def estimate(
    config: Any,
    bars: list[tuple[int, float]],
    window_start: int,
    now_second: int,
) -> Optional[TwapEstimate]:
    """Calibrated probability that the window resolves UP, using data up to `now_second`."""
    interval = int(_get(config, "bot.interval_seconds", 300))
    lookback = int(_get(config, "queen_of_heaven.twap_lookback_seconds", 60))
    vol_window = int(_get(config, "queen_of_heaven.twap_vol_window_seconds", 900))
    vol_horizon = max(1, int(_get(config, "queen_of_heaven.twap_vol_horizon_seconds", 1)))
    vol_multiplier = float(_get(config, "queen_of_heaven.twap_vol_multiplier", 1.25))
    flip_rate = float(_get(config, "queen_of_heaven.twap_flip_rate", 0.01))
    momentum = [float(b) for b in _get(config, "queen_of_heaven.twap_momentum", None) or []]

    elapsed = int(now_second) - int(window_start)
    if not bars or elapsed < 0 or elapsed > interval:
        return None
    first = min(int(window_start) - lookback + 1, int(now_second) - vol_window - 1)
    if int(bars[0][0]) > first:
        return None
    prices = _per_second_prices(bars, first, int(now_second))
    if prices is None:
        return None

    def at(second: int) -> float:
        return prices[second - first]

    open_twap = sum(at(s) for s in range(window_start - lookback + 1, window_start + 1)) / lookback
    # Per-second variance from overlapping `vol_horizon`-second changes: with a horizon above 1s
    # the estimate includes the short-term trending of 1s closes instead of leaving it to the multiplier.
    diffs = [at(s) - at(s - vol_horizon) for s in range(now_second - vol_window + vol_horizon, now_second + 1)]
    mean = sum(diffs) / len(diffs)
    sigma = math.sqrt(max(sum((d - mean) ** 2 for d in diffs) / len(diffs), 1e-12) / vol_horizon)

    spot = at(now_second)
    path = spot + sum(
        beta * (at(now_second - recent) - at(now_second - older))
        for beta, (recent, older) in zip(momentum, MOMENTUM_LEGS)
    )
    twap_start = interval - lookback
    if elapsed < twap_start:
        expected_final = path
        variance = sigma ** 2 * ((twap_start - elapsed) + lookback / 3.0)
    else:
        locked = elapsed - twap_start
        accumulated = (
            sum(at(s) for s in range(window_start + twap_start + 1, now_second + 1)) / locked if locked > 0 else spot
        )
        remaining = lookback - locked
        expected_final = (locked * accumulated + remaining * path) / lookback
        variance = sigma ** 2 * remaining ** 3 / (3.0 * lookback ** 2)

    z_score = (expected_final - open_twap) / math.sqrt(max(variance, 1e-12))
    p_up = flip_rate + (1.0 - 2.0 * flip_rate) * _normal_cdf(z_score / vol_multiplier)
    return TwapEstimate(p_up, z_score, sigma, expected_final, open_twap)
