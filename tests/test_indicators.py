from aries.indicators import compute_indicators
from aries.models import BinanceState


def state(prices):
    return BinanceState(
        price=prices[-1],
        best_bid=prices[-1] - 1,
        best_ask=prices[-1] + 1,
        best_bid_qty=2.0,
        best_ask_qty=1.0,
        ts=200.0,
        bars=list(enumerate(prices)),
        volume_bars=list(enumerate([1.0] * len(prices))),
    )


def ohlc_state(prices, volumes=None):
    volumes = volumes or [1.0] * len(prices)
    ohlc = []
    for index, price in enumerate(prices):
        open_price = prices[index - 1] if index else price
        high = max(open_price, price) + 0.25
        low = min(open_price, price) - 0.25
        ohlc.append((index, open_price, high, low, price))
    return BinanceState(
        price=prices[-1],
        best_bid=prices[-1] - 1,
        best_ask=prices[-1] + 1,
        best_bid_qty=2.0,
        best_ask_qty=1.0,
        ts=float(len(prices)),
        bars=list(enumerate(prices)),
        volume_bars=list(enumerate(volumes)),
        ohlc_bars=ohlc,
    )


def test_anchor_stability_scores_aligned_uptrend():
    indicators = compute_indicators(
        state([float(value) for value in range(1, 201)]),
        target_price=100.0,
        ma_fast=7,
        ma_mid=25,
        ma_slow=69,
        ma_anchor=144,
    )

    assert indicators.ma_anchor is not None
    assert indicators.ma_anchor_slope_bps > 0
    assert indicators.up_stability_score == 6
    assert indicators.down_stability_score == 0


def test_anchor_stability_scores_aligned_downtrend():
    indicators = compute_indicators(
        state([float(value) for value in range(300, 100, -1)]),
        target_price=200.0,
        ma_fast=7,
        ma_mid=25,
        ma_slow=69,
        ma_anchor=144,
    )

    assert indicators.ma_anchor_slope_bps < 0
    assert indicators.down_stability_score == 6
    assert indicators.up_stability_score == 0


def test_adx_vwap_and_macd_values_are_calculated():
    prices = [100.0 + (index * 0.3) for index in range(80)]
    indicators = compute_indicators(
        ohlc_state(prices, [1.0 + (index % 3) for index in range(80)]),
        target_price=110.0,
        ma_fast=7,
        ma_mid=25,
        ma_slow=69,
        adx_period=14,
        vwap_lookback_seconds=60,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
    )

    assert indicators.adx is not None
    assert indicators.plus_di is not None
    assert indicators.minus_di is not None
    assert indicators.vwap is not None
    assert indicators.vwap_distance_bps is not None
    assert indicators.macd_histogram is not None
    assert indicators.macd_histogram_slope_bps is not None
