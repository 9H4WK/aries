from __future__ import annotations

import math
import statistics
from typing import Optional

from .models import BinanceState, IndicatorState


def _ma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def _velocity_bps(values: list[float], lookback: int) -> Optional[float]:
    if len(values) <= lookback or values[-lookback - 1] == 0:
        return None
    return ((values[-1] - values[-lookback - 1]) / values[-lookback - 1]) * 10_000


def _volatility(values: list[float], lookback: int = 60) -> Optional[float]:
    if len(values) < lookback + 1:
        return None
    returns = []
    slice_values = values[-(lookback + 1):]
    for prev, cur in zip(slice_values, slice_values[1:]):
        if prev > 0:
            returns.append((cur - prev) / prev)
    if len(returns) < 2:
        return None
    return statistics.pstdev(returns)


def _persistence(bars: list[tuple[int, float]], target_price: Optional[float]) -> tuple[int, int]:
    if target_price is None:
        return 0, 0
    above = 0
    below = 0
    for _, close in reversed(bars):
        if close > target_price:
            if below:
                break
            above += 1
        elif close < target_price:
            if above:
                break
            below += 1
        else:
            break
    return above, below


def compute_indicators(state: BinanceState, target_price: Optional[float], ma_fast: int, ma_mid: int, ma_slow: int) -> IndicatorState:
    closes = [p for _, p in state.bars]
    fast = _ma(closes, ma_fast)
    mid = _ma(closes, ma_mid)
    slow = _ma(closes, ma_slow)
    price = state.price

    pressure = "UNKNOWN"
    if price is not None and fast is not None and mid is not None and slow is not None:
        if price > fast > mid > slow:
            pressure = "UP"
        elif price < fast < mid < slow:
            pressure = "DOWN"
        else:
            pressure = "CHOP"

    above, below = _persistence(state.bars, target_price)
    return IndicatorState(
        ma_fast=fast,
        ma_mid=mid,
        ma_slow=slow,
        pressure=pressure,
        velocity_5s_bps=_velocity_bps(closes, 5),
        velocity_15s_bps=_velocity_bps(closes, 15),
        volatility_60s=_volatility(closes, 60),
        persistence_above_target_seconds=above,
        persistence_below_target_seconds=below,
    )
