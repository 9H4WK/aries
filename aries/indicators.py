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


def _ema_series(values: list[float], period: int) -> list[float]:
    """Return an EMA series seeded from the first value for live robustness."""
    if period <= 0 or not values:
        return []
    alpha = 2.0 / (period + 1.0)
    ema = values[0]
    out = [ema]
    for value in values[1:]:
        ema = (value * alpha) + (ema * (1.0 - alpha))
        out.append(ema)
    return out


def _macd(
    closes: list[float],
    *,
    fast: int,
    slow: int,
    signal: int,
    slope_lookback: int,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Calculate MACD line, signal, histogram, and histogram slope in bps."""
    min_points = max(fast, slow) + signal + max(1, slope_lookback)
    if len(closes) < min_points or closes[-1] == 0:
        return None, None, None, None
    fast_ema = _ema_series(closes, fast)
    slow_ema = _ema_series(closes, slow)
    macd_line = [fast_value - slow_value for fast_value, slow_value in zip(fast_ema, slow_ema)]
    signal_line = _ema_series(macd_line, signal)
    histogram = [line - sig for line, sig in zip(macd_line, signal_line)]
    if len(histogram) <= slope_lookback:
        return macd_line[-1], signal_line[-1], histogram[-1], None
    hist_slope_bps = ((histogram[-1] - histogram[-1 - slope_lookback]) / closes[-1]) * 10_000
    return macd_line[-1], signal_line[-1], histogram[-1], hist_slope_bps


def _adx(
    ohlc_bars: list[tuple[int, float, float, float, float]],
    *,
    period: int,
    slope_lookback: int,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Calculate Wilder ADX/+DI/-DI from OHLC bars."""
    if period <= 1 or len(ohlc_bars) < (period * 2) + max(1, slope_lookback) + 1:
        return None, None, None, None

    true_ranges: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for previous, current in zip(ohlc_bars, ohlc_bars[1:]):
        _, _, prev_high, prev_low, prev_close = previous
        _, _, high, low, _ = current
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    if len(true_ranges) < period * 2:
        return None, None, None, None

    smoothed_tr = sum(true_ranges[:period])
    smoothed_plus = sum(plus_dm[:period])
    smoothed_minus = sum(minus_dm[:period])
    dx_values: list[float] = []
    latest_plus_di: Optional[float] = None
    latest_minus_di: Optional[float] = None

    for index in range(period, len(true_ranges)):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + true_ranges[index]
        smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_dm[index]
        smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm[index]
        if smoothed_tr <= 0:
            continue
        latest_plus_di = 100.0 * smoothed_plus / smoothed_tr
        latest_minus_di = 100.0 * smoothed_minus / smoothed_tr
        di_sum = latest_plus_di + latest_minus_di
        dx_values.append(0.0 if di_sum <= 0 else 100.0 * abs(latest_plus_di - latest_minus_di) / di_sum)

    if len(dx_values) < period:
        return None, None, latest_plus_di, latest_minus_di

    adx_values = [sum(dx_values[:period]) / period]
    for dx in dx_values[period:]:
        adx_values.append(((adx_values[-1] * (period - 1)) + dx) / period)

    adx = adx_values[-1]
    if len(adx_values) > slope_lookback:
        adx_slope = adx - adx_values[-1 - slope_lookback]
    elif len(adx_values) > 1:
        adx_slope = adx - adx_values[-2]
    else:
        adx_slope = None
    return adx, adx_slope, latest_plus_di, latest_minus_di


def _vwap(
    ohlc_bars: list[tuple[int, float, float, float, float]],
    volume_bars: list[tuple[int, float]],
    *,
    lookback_seconds: int,
) -> Optional[float]:
    """Calculate rolling VWAP from typical price and Binance volume bars."""
    if not ohlc_bars or not volume_bars:
        return None
    volume_by_ts = {ts: volume for ts, volume in volume_bars}
    latest_ts = ohlc_bars[-1][0]
    cutoff = latest_ts - max(1, int(lookback_seconds))
    price_volume = 0.0
    total_volume = 0.0
    for ts, _, high, low, close in ohlc_bars:
        if ts < cutoff:
            continue
        volume = max(0.0, float(volume_by_ts.get(ts, 0.0)))
        if volume <= 0:
            continue
        typical_price = (high + low + close) / 3.0
        price_volume += typical_price * volume
        total_volume += volume
    if total_volume <= 0:
        return None
    return price_volume / total_volume


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
    ma_anchor: int = 144,
    volume_ma_fast: int = 5,
    volume_ma_slow: int = 10,
    adx_period: int = 14,
    adx_slope_lookback: int = 3,
    vwap_lookback_seconds: int = 300,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    macd_slope_lookback: int = 3,
) -> IndicatorState:
    closes = [p for _, p in state.bars]
    volumes = [v for _, v in state.volume_bars]
    fast = _ma(closes, ma_fast)
    mid = _ma(closes, ma_mid)
    slow = _ma(closes, ma_slow)
    anchor = _ma(closes, ma_anchor)
    price = state.price
    fast_slope = _ma_slope_bps(closes, ma_fast)
    mid_slope = _ma_slope_bps(closes, ma_mid)
    anchor_slope = _ma_slope_bps(closes, ma_anchor)
    adx, adx_slope, plus_di, minus_di = _adx(
        state.ohlc_bars,
        period=adx_period,
        slope_lookback=adx_slope_lookback,
    )
    vwap = _vwap(
        state.ohlc_bars,
        state.volume_bars,
        lookback_seconds=vwap_lookback_seconds,
    )
    vwap_distance_bps = ((price - vwap) / vwap) * 10_000 if price is not None and vwap not in (None, 0) else None
    macd_line, macd_signal_value, macd_histogram, macd_histogram_slope_bps = _macd(
        closes,
        fast=macd_fast,
        slow=macd_slow,
        signal=macd_signal,
        slope_lookback=macd_slope_lookback,
    )

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

    # Exit stability deliberately requires several aligned conditions so a
    # single fast/mid crossover cannot trigger an inventory sale.
    up_stability = sum(
        (
            fast is not None and mid is not None and fast > mid,
            mid is not None and slow is not None and mid > slow,
            price is not None and anchor is not None and price > anchor,
            fast_slope is not None and fast_slope > 0,
            mid_slope is not None and mid_slope > 0,
            anchor_slope is not None and anchor_slope >= 0,
        )
    )
    down_stability = sum(
        (
            fast is not None and mid is not None and fast < mid,
            mid is not None and slow is not None and mid < slow,
            price is not None and anchor is not None and price < anchor,
            fast_slope is not None and fast_slope < 0,
            mid_slope is not None and mid_slope < 0,
            anchor_slope is not None and anchor_slope <= 0,
        )
    )

    return IndicatorState(
        ma_fast=fast,
        ma_mid=mid,
        ma_slow=slow,
        ma_anchor=anchor,
        pressure=pressure,
        ma_fast_slope_bps=fast_slope,
        ma_mid_slope_bps=mid_slope,
        ma_anchor_slope_bps=anchor_slope,
        up_stability_score=up_stability,
        down_stability_score=down_stability,
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
        adx=adx,
        adx_slope=adx_slope,
        plus_di=plus_di,
        minus_di=minus_di,
        vwap=vwap,
        vwap_distance_bps=vwap_distance_bps,
        macd_line=macd_line,
        macd_signal=macd_signal_value,
        macd_histogram=macd_histogram,
        macd_histogram_slope_bps=macd_histogram_slope_bps,
    )
