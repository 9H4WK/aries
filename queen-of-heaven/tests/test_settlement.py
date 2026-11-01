import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from aries.config import Config
from aries.database import Database
from aries.logger import JsonlLogger
from aries.paper_broker import PaperBroker
from aries.strategy import AriesBot


def make_broker(tmp_path: Path) -> PaperBroker:
    config = Config(
        {
            "database": {"path": str(tmp_path / "aries.db")},
            "logging": {
                "decisions_log": str(tmp_path / "logs" / "decisions.jsonl"),
                "fills_log": str(tmp_path / "logs" / "fills.jsonl"),
                "console": False,
            },
            "polymarket": {"max_quote_age_seconds": 3},
            "sizing": {"min_order_shares": 1.0},
            "risk": {
                "stop_when_locked": True,
                "max_window_notional_usdc": 20.0,
                "max_window_loss_usdc": 10.0,
                "max_orders_per_window": 5,
                "max_same_side_adds": 1,
            },
            "paper": {"immediate_fill": True},
        }
    )
    db = Database(config.get_path("database.path"))
    logger = JsonlLogger(
        config.get_path("logging.decisions_log"),
        config.get_path("logging.fills_log"),
        console=False,
    )
    return PaperBroker(config, db, logger)


def seed_fill(broker: PaperBroker, side: str, window_start: int, window_end: int, timestamp: float):
    broker.submit_order(
        timestamp=timestamp,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id=f"{side.lower()}1",
        side=side,
        action="BUY",
        price=0.5,
        notional_usdc=2.0,
        shares=4.0,
        decision_id=f"d_{side.lower()}",
        quote_ts=timestamp,
        reason="seed",
    )


def test_up_win_pnl(tmp_path: Path):
    broker = make_broker(tmp_path)
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    seed_fill(broker, "UP", window_start, window_end, now)
    settled = broker.settle_window(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        target_price=100000.0,
        final_btc_price=100100.0,
    )
    assert settled is not None
    assert settled["result"]["winning_side"] == "UP"
    assert settled["pnl"]["realized_pnl"] == 2.0
    assert broker.db.total_realized_pnl() == 2.0


def test_down_win_pnl(tmp_path: Path):
    broker = make_broker(tmp_path)
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    seed_fill(broker, "DOWN", window_start, window_end, now)
    settled = broker.settle_window(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        target_price=100000.0,
        final_btc_price=99900.0,
    )
    assert settled is not None
    assert settled["result"]["winning_side"] == "DOWN"
    assert settled["pnl"]["realized_pnl"] == 2.0


def test_unresolved_window_ignored(tmp_path: Path):
    broker = make_broker(tmp_path)
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    seed_fill(broker, "UP", window_start, window_end, now)
    settled = broker.settle_window(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        target_price=100000.0,
        final_btc_price=None,
    )
    assert settled is None


def test_settlement_includes_cash_recovered_from_sell(tmp_path: Path):
    broker = make_broker(tmp_path)
    broker.config["intra_window_sells"] = {
        "enabled": True,
        "min_order_shares": 1.0,
        "max_sells_per_window": 3,
        "max_total_actions_per_window": 8,
        "require_worst_case_improvement": True,
        "min_worst_case_improvement_usdc": 0.02,
    }
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    seed_fill(broker, "UP", window_start, window_end, now)
    broker.submit_order(
        timestamp=now + 1,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="up1",
        side="UP",
        action="SELL",
        price=0.75,
        notional_usdc=1.5,
        shares=2.0,
        decision_id="sell1",
        quote_ts=now + 1,
        reason="late_weak_inventory_trim",
    )

    settled = broker.settle_window(
        timestamp=now + 2,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        target_price=100000.0,
        final_btc_price=99900.0,
    )

    assert settled is not None
    assert settled["pnl"]["total_cost"] == 0.5
    assert settled["pnl"]["realized_pnl"] == -0.5


def test_tie_without_official_side_resolves_up(tmp_path: Path):
    """Polymarket rule: a final price equal to the price to beat resolves Up."""
    broker = make_broker(tmp_path)
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    seed_fill(broker, "UP", window_start, window_end, now)
    settled = broker.settle_window(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        target_price=100000.0,
        final_btc_price=100000.0,
    )
    assert settled["result"]["winning_side"] == "UP"


class StubPmApi:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    async def fetch_crypto_price(self, event_start_ts, symbol=None):
        self.calls += 1
        return self.snapshot


class StubDb:
    def __init__(self, markets):
        self.markets = markets

    def list_unresolved_markets(self, now):
        return list(self.markets)


def make_settling_bot(broker: PaperBroker, snapshot, markets):
    bot = SimpleNamespace(
        mode="paper",
        config=Config({"polymarket": {"settlement_official_grace_seconds": 420, "settlement_poll_seconds": 15}}),
        db=StubDb(markets),
        pm_api=StubPmApi(snapshot),
        paper_broker=broker,
        reporter=SimpleNamespace(write_reports=lambda: None),
        _official_prices={},
        _official_poll_ts={},
    )
    bot._official_window_prices = lambda window_start, now, allow_poll=True: AriesBot._official_window_prices(
        bot, window_start, now, allow_poll
    )
    bot._prune_official_cache = lambda oldest: AriesBot._prune_official_cache(bot, oldest)
    bot._final_price_for_window = lambda bars, window_end: AriesBot._final_price_for_window(bot, bars, window_end)
    return bot


def _market(window_start: int, window_end: int, target_price: float) -> dict:
    return {"market_id": "m1", "window_start_ts": window_start, "window_end_ts": window_end, "target_price": target_price}


def test_settlement_prefers_official_chainlink_over_binance(tmp_path: Path):
    broker = make_broker(tmp_path)
    window_start, window_end = 1_000, 1_300
    seed_fill(broker, "UP", window_start, window_end, window_start + 50)
    official = {"window_start_ts": window_start, "open_price": 100.0, "close_price": 99.0, "completed": True}
    bot = make_settling_bot(broker, official, [_market(window_start, window_end, 100.0)])
    binance_says_up = SimpleNamespace(bars=[(window_end, 105.0)])

    asyncio.run(AriesBot._settle_finished_windows(bot, window_end + 30, binance_says_up))

    result = broker.db.get_window_result("m1", window_start)
    assert result["winning_side"] == "DOWN"


def test_settlement_waits_for_official_prices_within_grace(tmp_path: Path):
    broker = make_broker(tmp_path)
    window_start, window_end = 1_000, 1_300
    seed_fill(broker, "UP", window_start, window_end, window_start + 50)
    bot = make_settling_bot(broker, None, [_market(window_start, window_end, 100.0)])
    state = SimpleNamespace(bars=[(window_end, 105.0)])

    asyncio.run(AriesBot._settle_finished_windows(bot, window_end + 30, state))
    assert broker.db.get_window_result("m1", window_start) is None

    asyncio.run(AriesBot._settle_finished_windows(bot, window_end + 421, state))
    result = broker.db.get_window_result("m1", window_start)
    assert result["winning_side"] == "UP"


def test_settlement_backlog_polls_once_per_cycle_and_skips_old_windows(tmp_path: Path):
    broker = make_broker(tmp_path)
    now = 100_000.0
    stale = [_market(int(now) - 90_000 + i * 300, int(now) - 89_700 + i * 300, 100.0) for i in range(20)]
    recent = [_market(int(now) - 900, int(now) - 600, 100.0), _market(int(now) - 600, int(now) - 300, 100.0)]
    bot = make_settling_bot(broker, None, stale + recent)
    state = SimpleNamespace(bars=[])

    asyncio.run(AriesBot._settle_finished_windows(bot, now, state))
    assert bot.pm_api.calls == 1
    asyncio.run(AriesBot._settle_finished_windows(bot, now + 1, state))
    assert bot.pm_api.calls == 2
    asyncio.run(AriesBot._settle_finished_windows(bot, now + 2, state))
    assert bot.pm_api.calls == 2
