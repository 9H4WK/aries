from types import SimpleNamespace

from aries.market_resolver import MarketResolver


def make_resolver(max_delay=3):
    config = {
        "bot": {"interval_seconds": 300},
        "polymarket": {"allow_target_from_first_seen_after_open": True, "max_target_set_delay_seconds": max_delay},
    }
    return MarketResolver(config, api=None)


def make_market(window_start=1_000):
    return SimpleNamespace(window_start_ts=window_start, target_price=None, target_source=None)


def test_target_uses_live_price_when_seen_on_time():
    market = make_market()
    make_resolver()._maybe_set_target(market, SimpleNamespace(price=101.0, bars=[(1_000, 100.0)]), 1_002)
    assert market.target_price == 101.0
    assert market.target_source == "binance_first_seen_after_window_open"


def test_target_recovered_from_bars_after_slow_open_cycle():
    market = make_market()
    bars = [(998, 99.0), (999, 99.5), (1_001, 100.25), (1_002, 100.5), (1_020, 103.0)]
    make_resolver()._maybe_set_target(market, SimpleNamespace(price=103.0, bars=bars), 1_020)
    assert market.target_price == 100.25
    assert market.target_source == "binance_bar_after_window_open"


def test_target_stays_missing_without_bars_near_open():
    market = make_market()
    bars = [(998, 99.0), (1_010, 101.0)]
    make_resolver()._maybe_set_target(market, SimpleNamespace(price=101.0, bars=bars), 1_020)
    assert market.target_price is None
