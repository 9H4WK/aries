import asyncio
import time

from aries.market_resolver import MarketResolver
from aries.models import BinanceState, MarketInfo
from aries.polymarket_api import parse_crypto_price


class StubApi:
    def __init__(self, market=None, crypto_price=None, price_history=None):
        self.market = market
        self.crypto_price = crypto_price
        self.price_history = price_history or []
        self.crypto_price_calls = []

    async def resolve_current_market(self, now_ts=None):
        return self.market

    async def fetch_crypto_price(self, event_start_ts, symbol=None):
        self.crypto_price_calls.append(event_start_ts)
        return self.crypto_price

    async def fetch_price_history(self, event_start_ts, symbol=None):
        return self.price_history


def make_config(**polymarket_overrides):
    polymarket = {"market_refresh_seconds": 0}
    polymarket.update(polymarket_overrides)
    return {"bot": {"interval_seconds": 300}, "polymarket": polymarket}


def make_market(window_start_ts):
    return MarketInfo(
        slug=f"btc-updown-5m-{window_start_ts}",
        market_id="m1",
        up_token_id="up1",
        down_token_id="down1",
        window_start_ts=window_start_ts,
        window_end_ts=window_start_ts + 300,
    )


def make_binance(price=64_000.0):
    return BinanceState(
        price=price,
        best_bid=price,
        best_ask=price,
        best_bid_qty=1.0,
        best_ask_qty=1.0,
        ts=time.time(),
    )


def test_resolver_uses_polymarket_official_open_price():
    window_start = (int(time.time()) // 300) * 300
    api = StubApi(
        market=make_market(window_start),
        crypto_price={
            "window_start_ts": window_start,
            "open_price": 64_351.18,
            "close_price": None,
            "completed": False,
        },
    )
    resolver = MarketResolver(make_config(), api)
    market = asyncio.run(resolver.resolve(make_binance(price=64_300.0)))
    assert market is not None
    assert market.target_price == 64_351.18
    assert market.target_source == "polymarket_chainlink_open"


def test_resolver_uses_price_history_open_sample_when_open_price_lags():
    window_start = (int(time.time()) // 300) * 300
    api = StubApi(
        market=make_market(window_start),
        crypto_price={
            "window_start_ts": window_start,
            "open_price": None,
            "close_price": None,
            "completed": False,
        },
        price_history=[(float(window_start), 64_238.78), (float(window_start + 4), 64_238.81)],
    )
    resolver = MarketResolver(make_config(), api)
    market = asyncio.run(resolver.resolve(make_binance(price=64_300.0)))
    assert market is not None
    assert market.target_price == 64_238.78
    assert market.target_source == "polymarket_chainlink_open_sample"


def test_resolver_does_not_fall_back_to_binance_by_default():
    window_start = (int(time.time()) // 300) * 300
    api = StubApi(market=make_market(window_start), crypto_price=None)
    resolver = MarketResolver(make_config(), api)
    market = asyncio.run(resolver.resolve(make_binance(price=64_300.0)))
    assert market is not None
    assert market.target_price is None
    assert market.target_source == "unknown"


def test_resolver_binance_fallback_only_when_explicitly_enabled(monkeypatch):
    window_start = 1_787_054_700
    fake_now = window_start + 2.0
    monkeypatch.setattr("aries.market_resolver.time.time", lambda: fake_now)
    api = StubApi(market=make_market(window_start), crypto_price=None)
    config = make_config(allow_target_from_first_seen_after_open=True, max_target_set_delay_seconds=3)
    resolver = MarketResolver(config, api)
    market = asyncio.run(resolver.resolve(make_binance(price=64_300.0)))
    assert market is not None
    assert market.target_price == 64_300.0
    assert market.target_source == "binance_first_seen_after_window_open"


def test_resolver_keeps_official_target_and_caches_snapshot():
    window_start = (int(time.time()) // 300) * 300
    snapshot = {
        "window_start_ts": window_start,
        "open_price": 64_351.18,
        "close_price": 64_307.83,
        "completed": False,
    }
    api = StubApi(market=make_market(window_start), crypto_price=snapshot)
    resolver = MarketResolver(make_config(), api)

    async def run():
        first = await resolver.resolve(make_binance())
        second = await resolver.resolve(make_binance())
        return first, second

    market, again = asyncio.run(run())
    assert market.target_price == 64_351.18
    assert again is market
    assert resolver.crypto_price_by_window[window_start] == snapshot


def test_parse_crypto_price_normalizes_official_response():
    window_start = 1_787_054_100
    parsed = parse_crypto_price(
        {
            "openPrice": 64_202.81,
            "closePrice": 64_351.19,
            "timestamp": 1_787_054_674_663,
            "completed": True,
            "incomplete": False,
            "cached": False,
        },
        window_start,
    )
    assert parsed == {
        "window_start_ts": window_start,
        "open_price": 64_202.81,
        "close_price": 64_351.19,
        "completed": True,
    }


def test_parse_crypto_price_handles_missing_open_price():
    parsed = parse_crypto_price(
        {"openPrice": None, "closePrice": None, "completed": False, "incomplete": True},
        1_787_054_400,
    )
    assert parsed is not None
    assert parsed["open_price"] is None
    assert parsed["close_price"] is None
    assert parsed["completed"] is False
    assert parse_crypto_price("not-a-dict", 1) is None
