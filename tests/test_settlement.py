import time
from pathlib import Path

from aries.config import Config
from aries.database import Database
from aries.logger import JsonlLogger
from aries.paper_broker import PaperBroker


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
