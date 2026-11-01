import time

import pytest

from aries.models import ProbabilityState
from aries.polymarket_flow import PolymarketFlowEngine, apply_polymarket_flow_bias
from aries.polymarket_ws import PolymarketQuoteStore


def config(**overrides):
    values = {
        "enabled": True,
        "levels": 5,
        "trade_lookback_seconds": 30,
        "fast_ema_seconds": 5,
        "slow_ema_seconds": 20,
        "fast_ema_weight": 0.70,
        "max_age_seconds": 3,
        "min_depth_shares": 1.0,
        "min_trade_volume_shares": 1.0,
        "queue_weight": 0.30,
        "depth_weight": 0.25,
        "microprice_weight": 0.15,
        "trade_weight": 0.30,
        "apply_to_probability": True,
        "probability_weight": 0.06,
        "max_probability_adjustment": 0.04,
    }
    values.update(overrides)
    return {
        "probability": {"clamp_min": 0.01, "clamp_max": 0.99},
        "polymarket_flow": values,
    }


def book(token_id, bid_size, ask_size):
    return {
        "event_type": "book",
        "asset_id": token_id,
        "bids": [
            {"price": "0.49", "size": str(bid_size)},
            {"price": "0.48", "size": str(bid_size / 2)},
        ],
        "asks": [
            {"price": "0.51", "size": str(ask_size)},
            {"price": "0.52", "size": str(ask_size / 2)},
        ],
    }


def trade(token_id, side, size):
    return {
        "event_type": "last_trade_price",
        "asset_id": token_id,
        "price": "0.50",
        "side": side,
        "size": str(size),
        "timestamp": str(int(time.time() * 1000)),
    }


def test_official_price_changes_remove_zero_size_level():
    store = PolymarketQuoteStore()
    store.apply_message(book("UP", 10, 10))

    store.apply_message(
        {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "UP", "side": "BUY", "price": "0.49", "size": "0"}
            ],
        }
    )

    assert store.quote("UP").bid == 0.48
    assert 0.49 not in store.books["UP"]["BUY"]


def test_matched_trade_side_and_size_are_retained():
    store = PolymarketQuoteStore()
    now = time.time()
    store.apply_message(trade("UP", "BUY", 12.5))

    trades = store.recent_trades("UP", now - 1)

    assert len(trades) == 1
    assert trades[0]["side"] == "BUY"
    assert trades[0]["size"] == 12.5


def test_bullish_up_and_down_books_and_trades_create_up_bias():
    store = PolymarketQuoteStore()
    store.apply_message(book("UP", 100, 10))
    store.apply_message(book("DOWN", 10, 100))
    store.apply_message(trade("UP", "BUY", 20))
    store.apply_message(trade("DOWN", "SELL", 10))
    engine = PolymarketFlowEngine(config(), store)

    state = engine.update("UP", "DOWN", time.time())

    assert state.ready is True
    assert state.cross_queue_signal > 0
    assert state.cross_depth_signal > 0
    assert state.cross_microprice_signal > 0
    assert state.trade_imbalance == 1.0
    assert state.bias_side == "UP"
    assert state.p_up_bias > 0.5


def test_flow_probability_adjustment_is_capped():
    store = PolymarketQuoteStore()
    store.apply_message(book("UP", 100, 10))
    store.apply_message(book("DOWN", 10, 100))
    state = PolymarketFlowEngine(config(), store).update("UP", "DOWN", time.time())
    state.signal = 1.0
    probability = ProbabilityState(
        p_up=0.50,
        p_down=0.50,
        confidence=0.0,
        distance_from_target=0.0,
        distance_bps=0.0,
        inside_noise_band=False,
        reason="base",
    )

    adjusted = apply_polymarket_flow_bias(
        config(probability_weight=0.20, max_probability_adjustment=0.03),
        probability,
        state,
    )

    assert adjusted.p_up == pytest.approx(0.53)
    assert adjusted.p_down == pytest.approx(0.47)
    assert state.probability_adjustment == pytest.approx(0.03)
    assert adjusted.reason == "base+pm_flow_up"
