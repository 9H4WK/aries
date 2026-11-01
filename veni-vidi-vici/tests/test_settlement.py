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


def test_official_winning_side_overrides_price_comparison(tmp_path: Path):
    """Polymarket resolves Up when the Chainlink TWAP is >= the price to beat."""
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
        winning_side="UP",
        notes="settled_from_polymarket_chainlink_twap",
    )
    assert settled is not None
    assert settled["result"]["winning_side"] == "UP"
    assert settled["pnl"]["notes"] == "settled_from_polymarket_chainlink_twap"
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
