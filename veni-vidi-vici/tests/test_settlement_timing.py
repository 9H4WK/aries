import asyncio
import time
from types import SimpleNamespace

from aries.config import Config
from aries.strategy import AriesBot


def make_bot(unresolved, official_snapshot, grace=420.0):
    settled_calls = []

    def settle_window(**kwargs):
        settled_calls.append(kwargs)
        return {"ok": True}

    bot = SimpleNamespace(
        mode="paper",
        config=Config(
            {
                "bot": {"interval_seconds": 300},
                "polymarket": {
                    "settlement_official_grace_seconds": grace,
                    "settlement_poll_seconds": 15,
                },
                "reports": {"enabled": False},
            }
        ),
        db=SimpleNamespace(list_unresolved_markets=lambda now_ts: unresolved),
        paper_broker=SimpleNamespace(settle_window=settle_window),
        resolver=SimpleNamespace(crypto_price_by_window={}),
        pm_api=SimpleNamespace(),
        _official_poll_ts={},
        _final_price_for_window=lambda bars, window_end: 64_999.0,
        settled_calls=settled_calls,
    )

    async def official_window_prices(window_start_ts, allow_poll=True):
        return official_snapshot

    bot._official_window_prices = official_window_prices
    return bot


def make_market(window_start, target=64_000.0):
    return {
        "market_id": "m1",
        "window_start_ts": window_start,
        "window_end_ts": window_start + 300,
        "target_price": target,
    }


def run(bot, now):
    asyncio.run(AriesBot._settle_finished_windows(bot, now, SimpleNamespace(bars=[])))


def test_settlement_waits_for_official_twap_within_grace_period():
    now = time.time()
    window_start = int(now) - 330
    market = make_market(window_start)
    incomplete = {
        "window_start_ts": window_start,
        "open_price": 64_000.0,
        "close_price": None,
        "completed": False,
    }
    bot = make_bot([market], incomplete)
    run(bot, now)
    assert bot.settled_calls == []


def test_settlement_uses_official_twap_within_grace_period():
    now = time.time()
    window_start = int(now) - 330
    market = make_market(window_start, target=64_000.0)
    official = {
        "window_start_ts": window_start,
        "open_price": 64_000.0,
        "close_price": 63_950.0,
        "completed": True,
    }
    bot = make_bot([market], official)
    run(bot, now)
    assert len(bot.settled_calls) == 1
    call = bot.settled_calls[0]
    assert call["winning_side"] == "DOWN"
    assert call["notes"] == "settled_from_polymarket_chainlink_twap"
    assert call["target_price"] == 64_000.0
    assert call["final_btc_price"] == 63_950.0


def test_official_tie_resolves_up():
    now = time.time()
    window_start = int(now) - 330
    market = make_market(window_start)
    official = {
        "window_start_ts": window_start,
        "open_price": 64_000.0,
        "close_price": 64_000.0,
        "completed": True,
    }
    bot = make_bot([market], official)
    run(bot, now)
    assert bot.settled_calls[0]["winning_side"] == "UP"


def test_settlement_falls_back_to_binance_after_grace_period():
    now = time.time()
    window_start = int(now) - 800
    market = make_market(window_start)
    incomplete = {
        "window_start_ts": window_start,
        "open_price": 64_000.0,
        "close_price": None,
        "completed": False,
    }
    bot = make_bot([market], incomplete, grace=420.0)
    run(bot, now)
    assert len(bot.settled_calls) == 1
    call = bot.settled_calls[0]
    assert call["notes"] == "settled_from_binance_close_near_window_end"
    assert call["winning_side"] is None
    assert call["final_btc_price"] == 64_999.0


def test_settlement_skips_when_no_prices_available():
    now = time.time()
    window_start = int(now) - 800
    market = make_market(window_start, target=None)
    bot = make_bot([market], None, grace=420.0)
    bot._final_price_for_window = lambda bars, window_end: None
    run(bot, now)
    assert bot.settled_calls == []


def test_official_window_prices_throttles_polling():
    calls = []

    async def fetch_crypto_price(window_start_ts, symbol=None):
        calls.append(window_start_ts)
        return {
            "window_start_ts": window_start_ts,
            "open_price": 64_000.0,
            "close_price": None,
            "completed": False,
        }

    window_start = (int(time.time()) // 300) * 300 - 600
    bot = SimpleNamespace(
        config=Config({"bot": {"interval_seconds": 300}, "polymarket": {"settlement_poll_seconds": 15}}),
        resolver=SimpleNamespace(crypto_price_by_window={}),
        pm_api=SimpleNamespace(fetch_crypto_price=fetch_crypto_price),
        _official_poll_ts={},
    )

    async def run_twice():
        first = await AriesBot._official_window_prices(bot, window_start)
        second = await AriesBot._official_window_prices(bot, window_start)
        return first, second

    first, second = asyncio.run(run_twice())
    assert calls == [window_start]
    assert first is not None and second is first


def test_official_window_prices_returns_completed_cache_without_polling():
    calls = []

    async def fetch_crypto_price(window_start_ts, symbol=None):
        calls.append(window_start_ts)
        return None

    window_start = (int(time.time()) // 300) * 300 - 600
    completed = {
        "window_start_ts": window_start,
        "open_price": 64_000.0,
        "close_price": 64_100.0,
        "completed": True,
    }
    bot = SimpleNamespace(
        config=Config({"bot": {"interval_seconds": 300}, "polymarket": {}}),
        resolver=SimpleNamespace(crypto_price_by_window={window_start: completed}),
        pm_api=SimpleNamespace(fetch_crypto_price=fetch_crypto_price),
        _official_poll_ts={},
    )
    result = asyncio.run(AriesBot._official_window_prices(bot, window_start))
    assert result is completed
    assert calls == []


def test_official_window_prices_does_not_poll_window_still_running():
    calls = []

    async def fetch_crypto_price(window_start_ts, symbol=None):
        calls.append(window_start_ts)
        return None

    window_start = (int(time.time()) // 300) * 300
    bot = SimpleNamespace(
        config=Config({"bot": {"interval_seconds": 300}, "polymarket": {}}),
        resolver=SimpleNamespace(crypto_price_by_window={}),
        pm_api=SimpleNamespace(fetch_crypto_price=fetch_crypto_price),
        _official_poll_ts={},
    )
    result = asyncio.run(AriesBot._official_window_prices(bot, window_start))
    assert result is None
    assert calls == []


def test_settlement_backlog_polls_once_per_cycle_and_skips_old_windows():
    now = time.time()
    current = int(now) // 300 * 300
    stale = [make_market(current - 90_000 + i * 300) for i in range(20)]
    recent = [make_market(current - 900), make_market(current - 600)]
    bot = make_bot(stale + recent, None)
    calls = []

    async def fetch_crypto_price(window_start_ts):
        calls.append(window_start_ts)
        return None

    bot.pm_api = SimpleNamespace(fetch_crypto_price=fetch_crypto_price)
    bot._official_window_prices = lambda window_start_ts, allow_poll=True: AriesBot._official_window_prices(
        bot, window_start_ts, allow_poll
    )

    run(bot, now)
    run(bot, now)
    run(bot, now)
    assert calls == [current - 900, current - 600]
