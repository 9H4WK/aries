import json

from aries.binance_flow import BinanceFlowEngine, apply_binance_flow_bias
from aries import clock
from aries.binance_ws import BinanceStream
from aries.models import BinanceState, ProbabilityState


def _config(**overrides):
    base = {
        "bot": {"symbol": "BTCUSDT"},
        "binance": {
            "websocket_url": "wss://example.invalid",
            "max_bars": 100,
        },
        "binance_flow": {
            "enabled": True,
            "lookback_seconds": 20,
            "max_age_seconds": 5,
            "min_book_events": 2,
            "min_trade_volume_btc": 0.001,
            "ofi_weight": 0.50,
            "cvd_weight": 0.50,
            "fast_ema_seconds": 3,
            "slow_ema_seconds": 15,
            "fast_ema_weight": 0.70,
            "apply_to_probability": True,
            "probability_weight": 0.08,
            "max_probability_adjustment": 0.06,
        },
        "probability": {
            "clamp_min": 0.01,
            "clamp_max": 0.99,
        },
    }
    base["binance_flow"].update(overrides)
    return base


def _prob(p_up=0.60):
    return ProbabilityState(
        p_up=p_up,
        p_down=1.0 - p_up,
        confidence=abs(p_up - 0.5) * 2.0,
        distance_from_target=1.0,
        distance_bps=1.0,
        inside_noise_band=False,
        reason="estimated",
    )


def test_binance_stream_records_ofi_and_cvd():
    stream = BinanceStream(_config())
    stream._handle_message(json.dumps({"data": {"e": "bookTicker", "E": 1_000, "b": "100", "B": "2", "a": "101", "A": "3"}}))
    stream._handle_message(json.dumps({"data": {"e": "bookTicker", "E": 2_000, "b": "101", "B": "4", "a": "102", "A": "1"}}))
    stream._handle_message(json.dumps({"data": {"e": "trade", "T": 2_500, "p": "101", "q": "0.010", "m": False}}))
    stream._handle_message(json.dumps({"data": {"e": "trade", "T": 2_700, "p": "101", "q": "0.004", "m": True}}))

    state = stream.snapshot()

    assert state.ofi_events[-1] == (2.0, 7.0)
    assert state.cvd_events[-2:] == [(2.5, 0.010), (2.7, -0.004)]


def test_binance_stream_freshness_follows_trade_time_not_book_updates():
    stream = BinanceStream(_config())
    stream._handle_message(json.dumps({"data": {"e": "aggTrade", "T": 5_000, "p": "80178.97", "q": "0.1", "m": False}}))
    # A backlogged stream keeps delivering book updates; they must not make the old price look fresh.
    stream._handle_message(json.dumps({"data": {"e": "bookTicker", "b": "80165", "B": "1", "a": "80166", "A": "1"}}))

    state = stream.snapshot()

    assert state.price == 80178.97
    assert state.ts == 5.0
    assert state.bars[-1] == (5, 80178.97)


def test_binance_stream_answered_ping_keeps_a_quiet_stream_fresh(monkeypatch):
    stream = BinanceStream(_config())
    monkeypatch.setattr(clock, "now", lambda: 5.3)
    stream._handle_message(json.dumps({"data": {"e": "aggTrade", "T": 5_000, "p": "80178.97", "q": "0.1", "m": False}}))
    stream._alive_ts = 12.0  # no trades since, but Binance answered a ping sent at 12.0

    assert stream.snapshot().ts == 12.0


def test_binance_stream_backlogged_trades_ignore_the_ping(monkeypatch):
    stream = BinanceStream(_config())
    monkeypatch.setattr(clock, "now", lambda: 41.0)
    stream._handle_message(json.dumps({"data": {"e": "aggTrade", "T": 5_000, "p": "80178.97", "q": "0.1", "m": False}}))
    stream._alive_ts = 40.0  # a trade arriving 36s late means the stream is behind, whatever the ping says

    assert stream.snapshot().ts == 5.0


def test_binance_flow_combines_ofi_and_cvd():
    engine = BinanceFlowEngine(_config())
    state = BinanceState(
        price=100.0,
        best_bid=99.0,
        best_ask=101.0,
        best_bid_qty=1.0,
        best_ask_qty=1.0,
        ts=10.0,
        ofi_events=[(9.0, 4.0), (9.5, 2.0)],
        cvd_events=[(9.0, 0.012), (9.2, -0.002)],
    )

    flow = engine.update(state, now_ts=10.0)

    assert flow.ready is True
    assert flow.ofi_signal == 1.0
    assert round(flow.cvd_signal, 6) == round((0.012 - 0.002) / (0.012 + 0.002), 6)
    assert flow.bias_side == "UP"


def test_binance_flow_positive_nudges_up_probability():
    engine = BinanceFlowEngine(_config())
    state = BinanceState(
        price=100.0,
        best_bid=99.0,
        best_ask=101.0,
        best_bid_qty=1.0,
        best_ask_qty=1.0,
        ts=10.0,
        ofi_events=[(9.0, 4.0), (9.5, 2.0)],
        cvd_events=[(9.0, 0.012), (9.2, -0.002)],
    )
    flow = engine.update(state, now_ts=10.0)

    adjusted = apply_binance_flow_bias(_config(), _prob(0.60), flow)

    assert adjusted.p_up > 0.60
    assert adjusted.p_down < 0.40
    assert flow.probability_adjustment > 0
    assert "binance_flow_up" in adjusted.reason


def test_binance_flow_negative_reduces_up_probability():
    engine = BinanceFlowEngine(_config())
    state = BinanceState(
        price=100.0,
        best_bid=99.0,
        best_ask=101.0,
        best_bid_qty=1.0,
        best_ask_qty=1.0,
        ts=10.0,
        ofi_events=[(9.0, -4.0), (9.5, -2.0)],
        cvd_events=[(9.0, -0.012), (9.2, 0.002)],
    )
    flow = engine.update(state, now_ts=10.0)

    adjusted = apply_binance_flow_bias(_config(), _prob(0.60), flow)

    assert adjusted.p_up < 0.60
    assert adjusted.p_down > 0.40
    assert flow.probability_adjustment < 0
    assert "binance_flow_down" in adjusted.reason
