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


def _ma_slope_bps(values: list[float], n: int, lookback: int = 3) -> Optional[float]:
    if n <= 0 or len(values) < n + lookback:
        return None
    current = _ma(values, n)
    previous = _ma(values[: len(values) - lookback], n)
    if current is None or previous is None or previous == 0:
        return None
    return ((current - previous) / previous) * 10_000


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


def _orderbook_imbalance(best_bid_qty: Optional[float], best_ask_qty: Optional[float]) -> Optional[float]:
    if best_bid_qty is None or best_ask_qty is None:
        return None
    total = best_bid_qty + best_ask_qty
    if total <= 0:
        return None
    return (best_bid_qty - best_ask_qty) / total


def compute_indicators(
    state: BinanceState,
    target_price: Optional[float],
    ma_fast: int,
    ma_mid: int,
    ma_slow: int,
    volume_ma_fast: int = 5,
    volume_ma_slow: int = 10,
) -> IndicatorState:
    closes = [p for _, p in state.bars]
    volumes = [v for _, v in state.volume_bars]
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
    current_volume = volumes[-1] if volumes else None
    volume_ma_5 = _ma(volumes, volume_ma_fast)
    volume_ma_10 = _ma(volumes, volume_ma_slow)
    volume_spike = None
    if current_volume is not None and volume_ma_10 not in (None, 0):
        volume_spike = current_volume / volume_ma_10

    return IndicatorState(
        ma_fast=fast,
        ma_mid=mid,
        ma_slow=slow,
        pressure=pressure,
        ma_fast_slope_bps=_ma_slope_bps(closes, ma_fast),
        ma_mid_slope_bps=_ma_slope_bps(closes, ma_mid),
        velocity_5s_bps=_velocity_bps(closes, 5),
        velocity_15s_bps=_velocity_bps(closes, 15),
        volatility_60s=_volatility(closes, 60),
        persistence_above_target_seconds=above,
        persistence_below_target_seconds=below,
        current_volume=current_volume,
        volume_ma_5=volume_ma_5,
        volume_ma_10=volume_ma_10,
        volume_spike=volume_spike,
        orderbook_imbalance=_orderbook_imbalance(state.best_bid_qty, state.best_ask_qty),
    )
