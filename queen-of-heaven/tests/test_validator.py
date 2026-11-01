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


def test_valid_session_with_inventory_sell_passes(tmp_path: Path):
    config, db, broker, validator = make_stack(tmp_path)
    config["intra_window_sells"] = {
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
    broker.submit_order(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="BUY",
        price=0.5,
        notional_usdc=5.0,
        shares=10.0,
        decision_id="buy1",
        quote_ts=now,
        reason="seed",
    )
    broker.submit_order(
        timestamp=now + 1,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="SELL",
        price=0.6,
        notional_usdc=2.4,
        shares=4.0,
        decision_id="sell1",
        quote_ts=now + 1,
        reason="late_weak_inventory_trim",
    )
    broker.settle_window(
        timestamp=now + 2,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        target_price=100000.0,
        final_btc_price=99900.0,
    )

    assert db.list_all_paper_fills()[-1]["action"] == "SELL"
    assert validator.validate_session() is True


def _insert_filled_buy(db, created_at, side, decision_id, role):
    db.insert_paper_order(
        {
            "created_at": created_at,
            "decision_id": decision_id,
            "window_start": 100,
            "window_end": 400,
            "market_id": "m1",
            "token_id": "u1" if side == "UP" else "d1",
            "side": side,
            "action": "BUY",
            "price": 0.5,
            "notional_usdc": 2.0,
            "shares": 4.0,
            "status": "FILLED",
            "reason": "filled_immediately_at_selected_ask",
            "metadata_json": {"quote_age_seconds": 0.1, "candidate": {"role": role}},
        }
    )


def test_sold_side_recovery_rebuy_is_not_a_same_side_violation(tmp_path: Path):
    _, db, _, validator = make_stack(tmp_path)
    _insert_filled_buy(db, 110, "UP", "buy1", "FIRST_ENTRY")
    _insert_filled_buy(db, 150, "UP", "buy2", "SOLD_SIDE_RECOVERY")

    assert validator.validate_session() is True


def test_plain_same_side_repeat_is_still_flagged(tmp_path: Path):
    _, db, _, validator = make_stack(tmp_path)
    _insert_filled_buy(db, 110, "UP", "buy1", "FIRST_ENTRY")
    _insert_filled_buy(db, 150, "UP", "buy2", "REPAIR")

    assert validator.validate_session() is False


def test_high_conviction_add_allowed_only_when_profile_enabled(tmp_path: Path):
    config, db, _, validator = make_stack(tmp_path)
    _insert_filled_buy(db, 110, "UP", "buy1", "FIRST_ENTRY")
    _insert_filled_buy(db, 150, "UP", "buy2", "SAME_SIDE_ADD")

    assert validator.validate_session() is False
    config["high_conviction_same_side"] = {"enabled": True}
    assert validator.validate_session() is True
