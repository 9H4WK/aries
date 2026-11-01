import asyncio

from aries.chainlink_feed import ChainlinkPriceFeed
from aries.config import Config
from aries.models import BinanceState
from aries.polymarket_api import parse_price_history


class StubApi:
    def __init__(self, snapshots=None, histories=None):
        self.snapshots = list(snapshots or [])
        self.histories = list(histories or [])

    async def fetch_crypto_price(self, event_start_ts, symbol=None):
        if self.snapshots:
            return self.snapshots.pop(0)
        return None

    async def fetch_price_history(self, event_start_ts, symbol=None):
        if self.histories:
            return self.histories.pop(0)
        return []


def make_config(**overrides):
    chainlink = {
        "enabled": True,
        "refresh_seconds": 1,
        "max_price_age_seconds": 15,
        "require_for_entry": True,
        "basis_ema_alpha": 0.5,
        "max_abs_basis_bps": 50,
    }
    chainlink.update(overrides)
    return Config(
        {
            "bot": {"interval_seconds": 300},
            "polymarket": {"crypto_price_twap_lookback_seconds": 60},
            "chainlink_reference": chainlink,
        }
    )


class FakeMarket:
    def __init__(self, window_start_ts=1_000):
        self.window_start_ts = window_start_ts
        self.window_end_ts = window_start_ts + 300


def make_binance(price=64_000.0):
    return BinanceState(
        price=price,
        best_bid=price - 0.5,
        best_ask=price + 0.5,
        best_bid_qty=2.0,
        best_ask_qty=3.0,
        ts=123.0,
        bars=[(1, price - 2.0), (2, price - 1.0), (3, price)],
        volume_bars=[(1, 0.1), (2, 0.2), (3, 0.3)],
        ohlc_bars=[(1, price - 2.0, price + 1.0, price - 3.0, price)],
        ofi_events=[(1.0, 0.5)],
        cvd_events=[(1.0, 0.25)],
    )


def test_update_tracks_official_open_and_live_sample_price():
    api = StubApi(
        snapshots=[{"window_start_ts": 1_000, "open_price": 64_258.9, "close_price": None, "completed": False}],
        histories=[[(1_000.0, 64_260.1)]],
    )
    feed = ChainlinkPriceFeed(make_config(), api)
    asyncio.run(feed.update(FakeMarket(), 64_255.0, now=100.0))
    assert feed.open_price == 64_258.9
    assert feed.current_price(now=100.0) == 64_260.1
    assert feed.age_seconds(now=101.5) == 1.5


def test_basis_ema_updates_and_rejects_outliers():
    api = StubApi(
        snapshots=[{"window_start_ts": 1_000, "open_price": 64_000.0, "close_price": None, "completed": False}],
        histories=[
            [(1_000.0, 64_005.0)],
            [(1_000.0, 64_005.0), (1_004.0, 64_007.0)],
            [(1_000.0, 64_005.0), (1_004.0, 64_007.0), (1_008.0, 65_000.0)],
        ],
    )
    feed = ChainlinkPriceFeed(make_config(), api)
    asyncio.run(feed.update(FakeMarket(), 64_000.0, now=10.0))
    assert feed.basis == 5.0
    asyncio.run(feed.update(FakeMarket(), 64_000.0, now=12.0))
    assert abs(feed.basis - 6.0) < 1e-9
    asyncio.run(feed.update(FakeMarket(), 64_000.0, now=14.0))
    assert abs(feed.basis - 6.0) < 1e-9


def test_anchor_state_shifts_price_levels_only():
    api = StubApi(
        snapshots=[{"window_start_ts": 1_000, "open_price": 100.0, "close_price": None, "completed": False}],
        histories=[[(1_000.0, 100.5)]],
    )
    feed = ChainlinkPriceFeed(make_config(), api)
    state = make_binance(price=100.0)
    asyncio.run(feed.update(FakeMarket(), 100.0, now=10.0))
    anchored = feed.anchor_state(state)
    assert anchored.price == 100.5
    assert anchored.best_bid == 100.0
    assert anchored.best_ask == 101.0
    assert anchored.bars == [(1, 98.5), (2, 99.5), (3, 100.5)]
    assert anchored.ohlc_bars == [(1, 98.5, 101.5, 97.5, 100.5)]
    assert anchored.best_bid_qty == 2.0
    assert anchored.best_ask_qty == 3.0
    assert anchored.volume_bars == state.volume_bars
    assert anchored.ofi_events == state.ofi_events
    assert anchored.cvd_events == state.cvd_events


def test_reference_hierarchy_and_staleness():
    api = StubApi(
        snapshots=[{"window_start_ts": 1_000, "open_price": 100.0, "close_price": None, "completed": False}],
        histories=[[(1_000.0, 100.5)]],
    )
    feed = ChainlinkPriceFeed(make_config(max_price_age_seconds=5), api)
    asyncio.run(feed.update(FakeMarket(), 100.0, now=10.0))
    price, source = feed.reference(anchored_price=100.5, now=11.0)
    assert (price, source) == (100.5, "chainlink")
    price, source = feed.reference(anchored_price=100.6, now=20.0)
    assert (price, source) == (100.6, "binance_anchored")
    feed._basis = None
    price, source = feed.reference(anchored_price=100.6, now=20.0)
    assert (price, source) == (100.6, "binance_raw")


def test_entry_gate_requires_chainlink_grade_reference():
    feed = ChainlinkPriceFeed(make_config(), StubApi())
    allowed, reason = feed.entry_allowed(anchored_price=100.0, now=10.0)
    assert not allowed
    assert reason == "chainlink_reference_unavailable"
    relaxed = ChainlinkPriceFeed(make_config(require_for_entry=False), StubApi())
    allowed, reason = relaxed.entry_allowed(anchored_price=100.0, now=10.0)
    assert allowed


def test_window_transition_resets_state():
    api = StubApi(
        snapshots=[
            {"window_start_ts": 1_000, "open_price": 100.0, "close_price": None, "completed": False},
            {"window_start_ts": 1_300, "open_price": 101.0, "close_price": None, "completed": False},
        ],
        histories=[[(1_000.0, 100.5)], [(1_300.0, 101.2)]],
    )
    feed = ChainlinkPriceFeed(make_config(), api)
    asyncio.run(feed.update(FakeMarket(1_000), 100.0, now=10.0))
    assert feed.basis == 0.5
    asyncio.run(feed.update(FakeMarket(1_300), 101.0, now=310.0))
    assert feed.open_price == 101.0
    assert feed.current_price(now=310.0) == 101.2
    assert abs(feed.basis - 0.2) < 1e-9



def test_new_window_keeps_previous_basis_until_first_sample():
    api = StubApi(
        snapshots=[
            {"window_start_ts": 1_000, "open_price": 100.0, "close_price": None, "completed": False},
            {"window_start_ts": 1_300, "open_price": 101.0, "close_price": None, "completed": False},
        ],
        histories=[[(1_000.0, 100.5)], [], [(1_300.0, 101.2)]],
    )
    feed = ChainlinkPriceFeed(make_config(), api)
    asyncio.run(feed.update(FakeMarket(1_000), 100.0, now=1_010.0))
    # New window, no official sample published yet: stay entry-eligible.
    asyncio.run(feed.update(FakeMarket(1_300), 101.0, now=1_301.0))
    assert feed.basis == 0.5
    assert feed.entry_allowed(anchored_price=101.5, now=1_301.0) == (True, "binance_anchored")
    # The first sample of the new window replaces the carried basis outright.
    asyncio.run(feed.update(FakeMarket(1_300), 101.0, now=1_303.0))
    assert abs(feed.basis - 0.2) < 1e-9


def test_new_window_drops_basis_older_than_carry_limit():
    api = StubApi(
        snapshots=[{"window_start_ts": 1_000, "open_price": 100.0, "close_price": None, "completed": False}],
        histories=[[(1_000.0, 100.5)]],
    )
    feed = ChainlinkPriceFeed(make_config(max_carried_basis_age_seconds=600), api)
    asyncio.run(feed.update(FakeMarket(1_000), 100.0, now=1_010.0))
    asyncio.run(feed.update(FakeMarket(1_900), 101.0, now=1_901.0))
    assert feed.basis is None
    assert feed.entry_allowed(anchored_price=101.0, now=1_901.0) == (False, "chainlink_reference_unavailable")

def test_twap_state_accumulates_final_lookback():
    feed = ChainlinkPriceFeed(make_config(), StubApi())
    window_end = 1_300
    feed._samples.extend(
        [
            (window_end - 90, 100.0),
            (window_end - 50, 102.0),
            (window_end - 20, 104.0),
        ]
    )
    state = feed.twap_state(window_end, now=window_end - 10)
    assert state["lookback_seconds"] == 60
    assert state["accumulated_seconds"] == 50.0
    assert abs(state["locked_fraction"] - 50.0 / 60.0) < 1e-3
    # Step-function TWAP: the pre-range sample holds [end-60, end-50].
    expected = (100.0 * 10.0 + 102.0 * 30.0 + 104.0 * 10.0) / 50.0
    assert abs(state["twap_so_far"] - expected) < 1e-9
    early = feed.twap_state(window_end, now=window_end - 120)
    assert early["accumulated_seconds"] == 0.0
    assert early["twap_so_far"] is None


def test_parse_price_history_normalizes_and_filters():
    window_start = 1_787_056_200
    data = [
        {"timestamp": window_start * 1000, "value": 100.0},
        {"timestamp": (window_start + 4) * 1000, "value": 100.5},
        {"timestamp": (window_start - 100) * 1000, "value": 99.0},
        {"timestamp": (window_start + 300) * 1000, "value": 101.0},
        {"timestamp": (window_start + 400) * 1000, "value": 102.0},
        {"timestamp": None, "value": 1.0},
        "bad-row",
    ]
    parsed = parse_price_history(data, window_start, 300)
    assert parsed == [
        (float(window_start), 100.0),
        (float(window_start + 4), 100.5),
        (float(window_start + 300), 101.0),
    ]
    assert parse_price_history({"not": "a list"}, window_start, 300) == []
