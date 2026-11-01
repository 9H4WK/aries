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


def test_sold_side_recovery_can_rebuy_side_when_same_side_adds_are_blocked(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    broker.submit_order(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="d1",
        side="DOWN",
        action="BUY",
        price=0.5,
        notional_usdc=1.0,
        shares=2.0,
        decision_id="first",
        quote_ts=now,
        reason="first_directional_entry",
    )
    order, fill = broker.submit_order(
        timestamp=now + 10,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="d1",
        side="DOWN",
        action="BUY",
        price=0.4,
        notional_usdc=1.0,
        shares=2.5,
        decision_id="recovery",
        quote_ts=now + 10,
        reason="sold_side_recovery_after_weak_sell",
    )

    assert order.status == "FILLED"
    assert fill is not None


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


def test_account_equity_floor_rejects_order_that_can_cross_half_starting_balance(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    broker.config["account"] = {"balance_usdc": 100.0}
    broker.config["risk"]["min_account_equity_fraction"] = 0.50
    broker.config["risk"]["max_window_notional_usdc"] = 100.0
    broker.config["risk"]["max_window_loss_usdc"] = 100.0
    now = time.time()

    order, fill = broker.submit_order(
        timestamp=now,
        window_start=int(now) - 10,
        window_end=int(now) + 100,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="BUY",
        price=0.5,
        notional_usdc=51.0,
        shares=102.0,
        decision_id="d1",
        quote_ts=now,
        reason="test_account_floor",
    )

    assert order.status == "REJECTED"
    assert order.reason == "account_equity_floor"
    assert fill is None


def test_sell_fill_uses_bid_proceeds_and_reduces_inventory(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    broker.config["intra_window_sells"] = {
        "enabled": True,
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
        decision_id="d1",
        quote_ts=now,
        reason="seed",
    )

    order, fill = broker.submit_order(
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
        decision_id="d2",
        quote_ts=now + 1,
        reason="late_weak_inventory_trim",
    )

    shape = broker.get_position("m1", window_start)
    assert order.status == "FILLED"
    assert fill is not None
    assert order.reason == "filled_immediately_at_selected_bid"
    assert shape.up_shares == 6.0
    assert shape.total_cost == 2.6
    assert shape.worst_case_pnl == -2.6


def test_sell_cannot_exceed_paper_inventory(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    broker.config["intra_window_sells"] = {"enabled": True}
    now = time.time()

    order, fill = broker.submit_order(
        timestamp=now,
        window_start=int(now) - 10,
        window_end=int(now) + 100,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="SELL",
        price=0.5,
        notional_usdc=1.0,
        shares=2.0,
        decision_id="d1",
        quote_ts=now,
        reason="invalid_sell",
    )

    assert order.status == "REJECTED"
    assert order.reason == "sell_exceeds_held_shares"
    assert fill is None


def test_directional_weak_side_sell_can_accept_capped_worst_case_deterioration(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    broker.config["risk"]["max_window_loss_usdc"] = 5.0
    broker.config["intra_window_sells"] = {
        "enabled": True,
        "directional_weak_side_exit_enabled": True,
        "directional_min_model_exit_edge": 0.01,
        "directional_max_worst_case_deterioration_usdc": 1.0,
        "directional_max_post_sell_worst_case_loss_usdc": 5.0,
        "max_sells_per_window": 3,
        "max_total_actions_per_window": 8,
        "require_worst_case_improvement": True,
    }
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100
    for index, (side, shares, cost) in enumerate((("UP", 10.0, 4.0), ("DOWN", 4.0, 1.0))):
        broker.submit_order(
            timestamp=now + index,
            window_start=window_start,
            window_end=window_end,
            market_id="m1",
            token_id=side.lower(),
            side=side,
            action="BUY",
            price=cost / shares,
            notional_usdc=cost,
            shares=shares,
            decision_id=f"d{index}",
            quote_ts=now + index,
            reason="seed",
        )

    order, fill = broker.submit_order(
        timestamp=now + 2,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="down",
        side="DOWN",
        action="SELL",
        price=0.50,
        notional_usdc=1.0,
        shares=2.0,
        decision_id="d2",
        quote_ts=now + 2,
        reason="directional_weak_side_exit",
        metadata={
            "candidate": {
                "role": "DIRECTIONAL_WEAK_SIDE_EXIT",
                "model_exit_edge": 0.20,
            }
        },
    )

    assert order.status == "FILLED"
    assert fill is not None
    assert broker.get_position("m1", window_start).worst_case_pnl == -2.0


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


def test_high_conviction_add_uses_separate_cap_but_never_exceeds_seven(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    broker.config["risk"]["max_window_notional_usdc"] = 3.0
    broker.config["risk"]["max_window_loss_usdc"] = 3.0
    broker.config["high_conviction_same_side"] = {
        "enabled": True,
        "max_window_notional_usdc": 7.0,
        "max_window_loss_usdc": 7.0,
        "max_adds_per_window": 2,
    }
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100

    first, _ = broker.submit_order(
        timestamp=now, window_start=window_start, window_end=window_end,
        market_id="m1", token_id="u1", side="UP", action="BUY", price=0.5,
        notional_usdc=1.0, shares=2.0, decision_id="d1", quote_ts=now,
        reason="first_directional_entry",
    )
    conviction, _ = broker.submit_order(
        timestamp=now + 1, window_start=window_start, window_end=window_end,
        market_id="m1", token_id="u1", side="UP", action="BUY", price=0.5,
        notional_usdc=5.0, shares=10.0, decision_id="d2", quote_ts=now + 1,
        reason="high_conviction_same_side_add",
    )
    over_cap, fill = broker.submit_order(
        timestamp=now + 2, window_start=window_start, window_end=window_end,
        market_id="m1", token_id="u1", side="UP", action="BUY", price=0.5,
        notional_usdc=2.0, shares=4.0, decision_id="d3", quote_ts=now + 2,
        reason="high_conviction_same_side_add",
    )

    assert first.status == "FILLED"
    assert conviction.status == "FILLED"
    assert over_cap.status == "REJECTED"
    assert over_cap.reason == "max_window_notional"
    assert fill is None


def test_queen_profile_rejects_bypass_order_below_price_floor(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    broker.config["mode"] = "paper"
    broker.config["queen_of_heaven"] = {
        "enabled": True,
        "modes": ["paper", "live"],
        "min_pm_ask": 0.85,
        "max_pm_ask": 0.90,
        "min_model_probability": 0.92,
        "min_edge": 0.025,
        "min_net_edge": 0.015,
        "min_win_profit_fraction": 0.10,
        "max_win_profit_fraction": 0.18,
        "min_stake_usdc": 1.0,
        "max_stake_usdc": 5.0,
        "max_window_notional_usdc": 5.0,
        "max_window_loss_usdc": 5.0,
    }
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
        price=0.80,
        notional_usdc=1.0,
        shares=1.25,
        decision_id="queen-bypass",
        quote_ts=now,
        reason="queen_of_heaven_entry",
        metadata={
            "candidate": {
                "model_probability": 0.95,
                "edge": 0.15,
                "net_edge": 0.14,
            }
        },
    )

    assert order.status == "REJECTED"
    assert order.reason == "queen_ask_below_min"
    assert fill is None


def test_post_lock_add_allowed_when_enabled(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    broker.config["risk"]["stop_when_locked"] = False
    broker.config["risk"]["allow_post_lock_adds"] = True
    broker.config["risk"]["max_window_notional_usdc"] = 20.0
    now = time.time()
    window_start = int(now) - 10
    window_end = int(now) + 100

    for index, side in enumerate(("UP", "DOWN")):
        broker.submit_order(
            timestamp=now + index,
            window_start=window_start,
            window_end=window_end,
            market_id="m1",
            token_id=side.lower(),
            side=side,
            action="BUY",
            price=0.25,
            notional_usdc=2.0,
            shares=8.0,
            decision_id=f"d{index}",
            quote_ts=now + index,
            reason="seed_lock",
        )

    order, fill = broker.submit_order(
        timestamp=now + 2,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="up",
        side="UP",
        action="BUY",
        price=0.25,
        notional_usdc=1.0,
        shares=4.0,
        decision_id="d2",
        quote_ts=now + 2,
        reason="post_lock_add",
    )

    assert order.status == "FILLED"
    assert fill is not None


def test_polymarket_taker_fee_matches_fee_schedule():
    from aries.paper_broker import polymarket_taker_fee

    assert abs(polymarket_taker_fee(100.0, 0.5, 0.07) - 1.75) < 1e-9
    assert abs(polymarket_taker_fee(10.0, 0.88, 0.07) - 10 * 0.07 * 0.88 * 0.12) < 1e-9
    assert polymarket_taker_fee(10.0, 0.88, 0.0) == 0.0


def test_paper_fill_and_settlement_include_taker_fee(tmp_path: Path):
    broker, db = make_broker(tmp_path)
    broker.config["polymarket"]["taker_fee_rate"] = 0.07
    now = time.time()
    window_start, window_end = int(now) - 10, int(now) + 100
    _, fill = broker.submit_order(
        timestamp=now,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="BUY",
        price=0.88,
        notional_usdc=8.8,
        shares=10.0,
        decision_id="d1",
        quote_ts=now,
        reason="seed",
    )
    fee = 10 * 0.07 * 0.88 * 0.12
    assert abs(fill.notional_usdc - (8.8 + fee)) < 1e-9
    assert abs(fill.metadata["fee_usdc"] - fee) < 1e-9
    settled = broker.settle_window(
        timestamp=now + 1,
        window_start=window_start,
        window_end=window_end,
        market_id="m1",
        target_price=100.0,
        final_btc_price=101.0,
    )
    assert abs(settled["pnl"]["realized_pnl"] - (10 - 8.8 - fee)) < 1e-9


def submit_delayed(broker, book_ask, price=0.85, limit_price=None):
    now = time.time()
    return broker.submit_order(
        timestamp=now,
        window_start=int(now) - 10,
        window_end=int(now) + 100,
        market_id="m1",
        token_id="u1",
        side="UP",
        action="BUY",
        price=price,
        notional_usdc=10 * price,
        shares=10.0,
        decision_id="d1",
        quote_ts=now,
        reason="seed",
        delayed_fill=True,
        book_ask=book_ask,
        limit_price=limit_price,
    )


def test_delayed_fill_uses_later_ask_within_limit(tmp_path: Path):
    broker, db = make_broker(tmp_path)
    order, fill = submit_delayed(broker, book_ask=0.84)
    assert order.status == "FILLED"
    assert order.reason == "filled_after_delay_at_book_ask"
    assert fill.price == 0.84
    assert abs(fill.notional_usdc - 8.4) < 1e-9
    assert fill.shares == 10.0


def test_delayed_fill_cancels_when_ask_moved_above_limit(tmp_path: Path):
    broker, db = make_broker(tmp_path)
    order, fill = submit_delayed(broker, book_ask=0.86)
    assert fill is None
    assert order.status == "CANCELED"
    assert db.list_all_paper_fills() == []
    assert db.get_paper_order(order.id)["reason"] == "paper_unmatched_after_delay"

    broker.config["paper"]["limit_tolerance"] = 0.01
    order, fill = submit_delayed(broker, book_ask=0.86)
    assert order.status == "FILLED" and fill.price == 0.86


def test_delayed_fill_uses_an_explicit_limit_price(tmp_path: Path):
    broker, db = make_broker(tmp_path)
    order, fill = submit_delayed(broker, book_ask=0.96, price=0.90, limit_price=0.97)
    assert order.status == "FILLED" and fill.price == 0.96
    assert order.metadata["limit_price"] == 0.97
    (tmp_path / "other").mkdir()
    other, _ = make_broker(tmp_path / "other")
    order, fill = submit_delayed(other, book_ask=0.98, price=0.90, limit_price=0.97)
    assert fill is None and order.status == "CANCELED"


def test_delayed_fill_cancels_on_empty_book(tmp_path: Path):
    broker, _ = make_broker(tmp_path)
    order, fill = submit_delayed(broker, book_ask=None)
    assert fill is None and order.status == "CANCELED"
