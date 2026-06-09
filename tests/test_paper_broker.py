import time
from pathlib import Path

from aries.config import Config
from aries.database import Database
from aries.logger import JsonlLogger
from aries.paper_broker import PaperBroker


def make_config(tmp_path: Path) -> Config:
    return Config(
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
                "max_same_side_adds": 0,
            },
            "paper": {"immediate_fill": True},
        }
    )


def make_broker(tmp_path: Path) -> tuple[PaperBroker, Database]:
    config = make_config(tmp_path)
    db = Database(config.get_path("database.path"))
    logger = JsonlLogger(
        config.get_path("logging.decisions_log"),
        config.get_path("logging.fills_log"),
        console=False,
    )
    return PaperBroker(config, db, logger), db


def test_accepted_order_creates_fill(tmp_path: Path):
    broker, db = make_broker(tmp_path)
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    order, fill = broker.submit_order(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="BUY",
        price=0.5,
        notional_usdc=2.0,
        shares=4.0,
        decision_id="d1",
        quote_ts=now,
        reason="test_order",
    )
    assert order.status == "FILLED"
    assert fill is not None
    assert db.list_paper_fills("m1", window_start)


def test_stale_quote_rejects(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    order, fill = broker.submit_order(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="BUY",
        price=0.5,
        notional_usdc=2.0,
        shares=4.0,
        decision_id="d1",
        quote_ts=now - 10,
        reason="test_order",
    )
    assert order.status == "REJECTED"
    assert order.reason == "quote_stale"
    assert fill is None


def test_min_shares_rejects(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    order, fill = broker.submit_order(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="BUY",
        price=0.5,
        notional_usdc=0.2,
        shares=0.4,
        decision_id="d1",
        quote_ts=now,
        reason="test_order",
    )
    assert order.status == "REJECTED"
    assert order.reason == "shares_below_minimum"
    assert fill is None


def test_locked_window_rejects(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    broker.submit_order(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="BUY",
        price=0.25,
        notional_usdc=5.0,
        shares=20.0,
        decision_id="d1",
        quote_ts=now,
        reason="seed_up",
    )
    broker.submit_order(
        timestamp=now + 1,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="d1",
        side="DOWN",
        action="BUY",
        price=0.25,
        notional_usdc=5.0,
        shares=20.0,
        decision_id="d2",
        quote_ts=now + 1,
        reason="seed_down",
    )
    order, fill = broker.submit_order(
        timestamp=now + 2,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="BUY",
        price=0.25,
        notional_usdc=1.0,
        shares=4.0,
        decision_id="d3",
        quote_ts=now + 2,
        reason="locked_reject",
    )
    assert order.status == "REJECTED"
    assert order.reason == "window_locked"
    assert fill is None
