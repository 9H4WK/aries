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
