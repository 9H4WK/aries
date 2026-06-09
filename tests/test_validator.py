import time
from pathlib import Path

from aries.config import Config
from aries.database import Database
from aries.logger import JsonlLogger
from aries.paper_broker import PaperBroker
from aries.validator import Validator


def make_stack(tmp_path: Path):
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
                "max_same_side_adds": 0,
            },
            "paper": {"immediate_fill": True},
            "validation": {"fail_fast": False},
        }
    )
    db = Database(config.get_path("database.path"))
    logger = JsonlLogger(
        config.get_path("logging.decisions_log"),
        config.get_path("logging.fills_log"),
        console=False,
    )
    broker = PaperBroker(config, db, logger)
    validator = Validator(config, db, logger)
    return config, db, broker, validator


def test_invalid_fill_order_detected(tmp_path: Path):
    _, db, _, validator = make_stack(tmp_path)
    db.insert_paper_order(
        {
            "created_at": time.time(),
            "decision_id": "",
            "window_start": 100,
            "window_end": 200,
            "market_id": "m1",
            "token_id": "u1",
            "side": "UP",
            "action": "BUY",
            "price": 0.5,
            "notional_usdc": 2.0,
            "shares": 4.0,
            "status": "REJECTED",
            "reason": "",
            "metadata_json": {"quote_age_seconds": 10},
        }
    )
    assert validator.validate_session() is False


def test_valid_session_passes(tmp_path: Path):
    _, db, broker, validator = make_stack(tmp_path)
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
        price=0.5,
        notional_usdc=2.0,
        shares=4.0,
        decision_id="d1",
        quote_ts=now,
        reason="test_order",
    )
    db.insert_decision(
        {
            "decision_id": "d1",
            "timestamp": now,
            "window_start": window_start,
            "market_id": "m1",
            "decision": "BUY_UP",
            "selected_reason": "test_order",
            "position_before": {},
            "candidate_up": {},
            "candidate_down": {},
            "position_after": {},
        }
    )
    broker.settle_window(
        timestamp=now + 5,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        target_price=100000.0,
        final_btc_price=100100.0,
    )
    assert validator.validate_session() is True
