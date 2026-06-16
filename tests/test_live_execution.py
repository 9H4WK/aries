import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from aries.config import Config
from aries.database import Database
from aries.live_execution import LIVE_CONFIRMATION, LiveExecutor
from aries.logger import JsonlLogger
from aries.models import Fill, MarketInfo, PositionShape


class FakePage:
    def __init__(self, items=()):
        self.items = tuple(items)


class FakePaginator:
    def __init__(self, items=()):
        self.items = items

    async def first_page(self):
        return FakePage(self.items)


class FakeClient:
    def __init__(self):
        self.placed = []
        self.canceled = []
        self.redeemed = []
        self.positions = []
        self.trades = []
        self.conditional_balance = 100_000_000
        self.balance_calls = []
        self.order_details = {}
        self.order_response = {
            "ok": True,
            "order_id": "clob-order-1",
            "status": "matched",
            "making_amount": "1",
            "taking_amount": "2",
        }
        self.redeem_response = {"ok": True, "redemption_id": "redeem-1"}

    def list_open_orders(self, **kwargs):
        return FakePaginator()

    def list_account_trades(self, **kwargs):
        return FakePaginator(self.trades)

    def list_positions(self, **kwargs):
        return FakePaginator(self.positions)

    def list_closed_positions(self, **kwargs):
        return FakePaginator()

    async def get_balance_allowance(self, **kwargs):
        self.balance_calls.append(kwargs)
        if kwargs.get("asset_type") == "CONDITIONAL":
            return SimpleNamespace(balance=self.conditional_balance, allowances={"exchange": 2**256 - 1})
        return SimpleNamespace(balance=100_000_000, allowances={})

    async def place_limit_order(self, **kwargs):
        self.placed.append(kwargs)
        return self.order_response

    async def cancel_order(self, **kwargs):
        self.canceled.append(kwargs["order_id"])
        return {"canceled": [kwargs["order_id"]], "not_canceled": {}}

    async def redeem(self, *args, **kwargs):
        self.redeemed.append({"args": args, "kwargs": kwargs})
        return self.redeem_response

    async def get_order(self, **kwargs):
        return self.order_details.get(kwargs["order_id"], {"size_matched": "0"})

    async def close(self):
        return None


def make_executor(tmp_path, mode="live", **live_overrides):
    live = {
        "monitor_enabled": True,
        "enabled": mode == "live",
        "allow_real_orders": mode == "live",
        "allow_sell_orders": False,
        "confirm_real_orders": LIVE_CONFIRMATION if mode == "live" else "",
        "account_refresh_seconds": 1,
        "order_cooldown_seconds": 0,
        "rejection_backoff_seconds": 0,
        "min_order_shares": 1.0,
        "minimum_collateral_balance_usdc": 0.0,
        "max_session_drawdown_fraction": 1.0,
        "max_orders_per_window": 3,
        "max_window_notional_usdc": 7.0,
        **live_overrides,
    }
    config = Config(
        {
            "mode": mode,
            "account": {"balance_usdc": 100.0},
            "live": live,
            "sizing": {"min_order_shares": 1.0},
            "polymarket": {"max_quote_age_seconds": 3},
            "risk": {
                "max_window_notional_usdc": 3.0,
                "max_window_loss_usdc": 1.0,
                "max_orders_per_window": 10,
                "max_same_side_adds": 0,
            },
            "intra_window_sells": {
                "enabled": True,
                "modes": ["paper", "live"],
                "min_order_shares": 1.0,
                "min_worst_case_improvement_usdc": 0.02,
                "directional_weak_side_exit_enabled": True,
                "directional_min_model_exit_edge": 0.01,
                "directional_max_worst_case_deterioration_usdc": 1.0,
                "max_sells_per_window": 3,
            },
        }
    )
    db = Database(str(tmp_path / "aries.db"))
    logger = JsonlLogger(str(tmp_path / "decisions.jsonl"), str(tmp_path / "fills.jsonl"), console=False)
    client = FakeClient()
    executor = LiveExecutor(config, db, logger, client=client)
    executor.snapshot.connected = True
    executor.snapshot.refreshed_at = time.time()
    return executor, db, client


def submit(executor, now=100.0):
    return asyncio.run(
        executor.place_order(
            timestamp=now,
            window_start=0,
            window_end=300,
            market_id="market-1",
            token_id="up-token",
            side="UP",
            action="BUY",
            price=0.5,
            notional_usdc=1.0,
            shares=2.0,
            decision_id="decision-1",
            quote_ts=now,
            reason="first_directional_entry",
            shape_before=PositionShape(),
            fills_before=[],
        )
    )


def test_live_order_requires_explicit_arming(tmp_path):
    executor, db, client = make_executor(tmp_path, mode="live_monitor")

    result = submit(executor)

    assert result.status == "REJECTED"
    assert result.reason == "live_orders_not_armed"
    assert client.placed == []
    assert db.list_live_order_audits()[0]["reason"] == "live_orders_not_armed"


def test_armed_live_order_uses_selected_ask_and_size(tmp_path):
    executor, db, client = make_executor(tmp_path)

    result = submit(executor)

    assert result.status == "MATCHED"
    assert result.external_order_id == "clob-order-1"
    assert client.placed == [
        {
            "token_id": "up-token",
            "price": 0.5,
            "size": 2.0,
            "side": "BUY",
            "post_only": False,
        }
    ]
    assert db.list_live_order_audits()[0]["status"] == "MATCHED"


def test_live_refresh_reconstructs_current_market_shape(tmp_path):
    executor, _, client = make_executor(tmp_path, mode="live_monitor")
    market = MarketInfo(
        slug="btc-updown-5m-0",
        market_id="market-1",
        up_token_id="up-token",
        down_token_id="down-token",
        window_start_ts=0,
        window_end_ts=300,
    )
    client.positions = [
        SimpleNamespace(token_id="up-token", size=2.0, initial_value=1.0, avg_price=0.5),
        SimpleNamespace(token_id="down-token", size=3.0, initial_value=1.5, avg_price=0.5),
    ]
    client.trades = [
        SimpleNamespace(
            side="BUY",
            token_id="up-token",
            size=2.0,
            price=0.5,
            matched_at=datetime.fromtimestamp(10, tz=UTC),
        )
    ]

    asyncio.run(executor.refresh_account(market))

    shape = executor.get_position("market-1")
    assert shape.up_shares == 2.0
    assert shape.down_shares == 3.0
    assert shape.total_cost == 2.5
    assert shape.worst_case_pnl == -0.5
    assert executor.snapshot.collateral_balance_usdc == 100.0
    assert executor.get_fills("market-1") == [
        Fill(
            ts=10.0,
            market_id="market-1",
            window_start_ts=0,
            side="UP",
            price=0.5,
            notional_usdc=1.0,
            shares=2.0,
            reason="live_account_trade",
        )
    ]


def test_unmatched_marketable_limit_is_canceled(tmp_path):
    executor, db, client = make_executor(tmp_path)
    client.order_response = {"ok": True, "order_id": "resting-1", "status": "live"}

    result = submit(executor)

    assert result.status == "CANCELED"
    assert result.reason == "unmatched_marketable_limit_canceled"
    assert client.canceled == ["resting-1"]
    assert db.list_live_order_audits()[0]["status"] == "CANCELED"


def test_live_order_rejects_stale_account_state(tmp_path):
    executor, _, client = make_executor(tmp_path)
    executor.snapshot.refreshed_at = 0.0

    result = submit(executor)

    assert result.status == "REJECTED"
    assert result.reason == "live_account_state_stale"
    assert client.placed == []


def test_live_order_rejects_when_current_market_has_open_order(tmp_path):
    executor, _, client = make_executor(tmp_path)
    executor.snapshot.market_id = "market-1"
    executor.snapshot.open_orders_count = 1

    result = submit(executor)

    assert result.status == "REJECTED"
    assert result.reason == "live_open_order_pending"
    assert client.placed == []


def test_matched_audit_immediately_reconstructs_live_position_and_blocks_repeat(tmp_path):
    executor, db, client = make_executor(tmp_path)

    first = submit(executor)
    second = submit(executor)

    assert first.status == "MATCHED"
    assert second.status == "REJECTED"
    assert second.reason in {"same_side_add_blocked", "max_window_loss"}
    assert len(client.placed) == 1
    assert executor.get_position("market-1").total_cost == 1.0
    assert len(executor.get_fills("market-1")) == 1
    assert len(db.list_matched_live_orders("market-1")) == 1


def test_empty_account_refresh_cannot_erase_matched_audit_position(tmp_path):
    executor, _, client = make_executor(tmp_path)
    market = MarketInfo(
        slug="btc-updown-5m-0",
        market_id="market-1",
        up_token_id="up-token",
        down_token_id="down-token",
        window_start_ts=0,
        window_end_ts=300,
    )

    assert submit(executor).status == "MATCHED"
    asyncio.run(executor.refresh_account(market, force=True))

    assert client.positions == []
    assert client.trades == []
    assert executor.get_position("market-1").total_cost == 1.0


def test_live_minimum_shares_is_enforced_before_clob_submission(tmp_path):
    executor, _, client = make_executor(tmp_path, min_order_shares=5.0)

    result = submit(executor)

    assert result.status == "REJECTED"
    assert result.reason == "shares_below_live_minimum"
    assert client.placed == []


def test_live_account_equity_floor_rejects_new_order(tmp_path):
    executor, _, client = make_executor(
        tmp_path,
        max_window_notional_usdc=100.0,
    )
    executor.config["risk"]["max_window_notional_usdc"] = 100.0
    executor.config["risk"]["max_window_loss_usdc"] = 100.0
    executor.config["risk"]["min_account_equity_fraction"] = 0.50
    executor.snapshot.session_start_balance_usdc = 100.0
    executor.snapshot.collateral_balance_usdc = 50.5

    result = submit(executor)

    assert result.status == "REJECTED"
    assert result.reason == "account_equity_floor"
    assert client.placed == []


def test_live_sell_orders_require_separate_arming(tmp_path):
    executor, _, client = make_executor(tmp_path)

    result = asyncio.run(
        executor.place_order(
            timestamp=100.0,
            window_start=0,
            window_end=300,
            market_id="market-1",
            token_id="up-token",
            side="UP",
            action="SELL",
            price=0.5,
            notional_usdc=1.0,
            shares=2.0,
            decision_id="decision-sell",
            quote_ts=100.0,
            reason="test_sell",
            shape_before=PositionShape(up_shares=2.0, total_cost=1.0),
            fills_before=[],
        )
    )

    assert result.status == "REJECTED"
    assert result.reason == "live_sell_orders_not_armed"
    assert client.placed == []


def test_live_redeem_requires_separate_arming(tmp_path):
    executor, _, client = make_executor(tmp_path)

    try:
        asyncio.run(executor.redeem(token_id="up-token"))
    except RuntimeError as error:
        assert str(error) == "Live redeem is not armed."
    else:
        raise AssertionError("expected redeem arming guard")

    assert client.redeemed == []


def test_armed_live_redeem_calls_client(tmp_path):
    executor, _, client = make_executor(tmp_path, allow_redeem_orders=True)

    result = asyncio.run(
        executor.redeem(
            validated=True,
            token_id="up-token",
            market_id="market-1",
            shape_after=PositionShape(
                up_shares=1.0,
                down_shares=1.0,
                total_cost=0.5,
                pnl_if_up=0.5,
                pnl_if_down=0.5,
                worst_case_pnl=0.5,
                locked=True,
            ),
        )
    )

    assert result == {"ok": True, "redemption_id": "redeem-1"}
    assert client.redeemed == [
        {
            "args": (),
            "kwargs": {
                "token_id": "up-token",
                "market_id": "market-1",
                "shares": 1.0,
                "size": 1.0,
                "amount": 1.0,
                "quantity": 1.0,
                "market": "market-1",
                "asset_id": "up-token",
                "condition_id": "market-1",
            },
        }
    ]


def test_armed_live_inventory_trim_sells_at_selected_bid(tmp_path):
    executor, db, client = make_executor(tmp_path, allow_sell_orders=True)
    client.order_response = {
        "ok": True,
        "order_id": "sell-1",
        "status": "matched",
        "making_amount": "1",
        "taking_amount": "0.5",
    }

    result = asyncio.run(
        executor.place_order(
            timestamp=100.0,
            window_start=0,
            window_end=300,
            market_id="market-1",
            token_id="up-token",
            side="UP",
            action="SELL",
            price=0.5,
            notional_usdc=0.5,
            shares=1.0,
            decision_id="decision-sell",
            quote_ts=100.0,
            reason="late_weak_inventory_trim",
            shape_before=PositionShape(
                up_shares=4.0,
                down_shares=2.0,
                total_cost=2.0,
                pnl_if_up=2.0,
                pnl_if_down=0.0,
                worst_case_pnl=0.0,
            ),
            fills_before=[],
            metadata={"role": "INVENTORY_TRIM"},
        )
    )

    assert result.status == "MATCHED"
    assert client.placed[-1]["side"] == "SELL"
    assert client.placed[-1]["price"] == 0.5
    assert client.placed[-1]["size"] == 1.0
    assert client.balance_calls[-1] == {"asset_type": "CONDITIONAL", "token_id": "up-token"}
    audit = db.list_matched_live_orders("market-1")[-1]
    assert audit["action"] == "SELL"
    assert audit["shares"] == 1.0
    assert audit["notional_usdc"] == 0.5


def test_armed_live_directional_exit_can_sell_smaller_side(tmp_path):
    executor, _, client = make_executor(tmp_path, allow_sell_orders=True)
    client.order_response = {
        "ok": True,
        "order_id": "sell-down-1",
        "status": "matched",
        "making_amount": "1",
        "taking_amount": "0.5",
    }

    result = asyncio.run(
        executor.place_order(
            timestamp=100.0,
            window_start=0,
            window_end=300,
            market_id="market-1",
            token_id="down-token",
            side="DOWN",
            action="SELL",
            price=0.5,
            notional_usdc=0.5,
            shares=1.0,
            decision_id="decision-directional-sell",
            quote_ts=100.0,
            reason="directional_weak_side_exit",
            shape_before=PositionShape(
                up_shares=4.0,
                down_shares=2.0,
                total_cost=2.0,
                pnl_if_up=2.0,
                pnl_if_down=0.0,
                worst_case_pnl=0.0,
            ),
            fills_before=[],
            metadata={
                "role": "DIRECTIONAL_WEAK_SIDE_EXIT",
                "model_exit_edge": 0.10,
            },
        )
    )

    assert result.status == "MATCHED"
    assert client.placed[-1]["side"] == "SELL"
    assert client.placed[-1]["token_id"] == "down-token"


def test_live_sell_rejects_when_conditional_balance_is_too_small(tmp_path):
    executor, _, client = make_executor(tmp_path, allow_sell_orders=True)
    client.conditional_balance = 500_000

    result = asyncio.run(
        executor.place_order(
            timestamp=100.0,
            window_start=0,
            window_end=300,
            market_id="market-1",
            token_id="up-token",
            side="UP",
            action="SELL",
            price=0.5,
            notional_usdc=0.5,
            shares=1.0,
            decision_id="decision-sell",
            quote_ts=100.0,
            reason="late_weak_inventory_trim",
            shape_before=PositionShape(
                up_shares=4.0,
                down_shares=2.0,
                total_cost=2.0,
                pnl_if_up=2.0,
                pnl_if_down=0.0,
                worst_case_pnl=0.0,
            ),
            fills_before=[],
            metadata={"role": "INVENTORY_TRIM"},
        )
    )

    assert result.status == "REJECTED"
    assert result.reason == "live_conditional_balance_insufficient"
    assert client.placed == []


def test_live_account_trades_reconstruct_sell_proceeds(tmp_path):
    executor, _, client = make_executor(tmp_path, mode="live_monitor")
    market = MarketInfo(
        slug="btc-updown-5m-0",
        market_id="market-1",
        up_token_id="up-token",
        down_token_id="down-token",
        window_start_ts=0,
        window_end_ts=300,
    )
    client.positions = [
        SimpleNamespace(token_id="up-token", size=2.0, initial_value=1.0, avg_price=0.5),
    ]
    client.trades = [
        SimpleNamespace(side="SELL", token_id="up-token", size=2.0, price=0.5, matched_at=datetime.fromtimestamp(20, tz=UTC)),
        SimpleNamespace(side="BUY", token_id="up-token", size=4.0, price=0.5, matched_at=datetime.fromtimestamp(10, tz=UTC)),
    ]

    asyncio.run(executor.refresh_account(market))

    shape = executor.get_position("market-1")
    assert shape.up_shares == 2.0
    assert shape.total_cost == 1.0
    assert [fill.action for fill in executor.get_fills("market-1")] == ["BUY", "SELL"]


def test_new_sell_audit_wins_while_account_positions_are_stale(tmp_path):
    executor, db, client = make_executor(tmp_path, mode="live_monitor")
    market = MarketInfo(
        slug="btc-updown-5m-0",
        market_id="market-1",
        up_token_id="up-token",
        down_token_id="down-token",
        window_start_ts=0,
        window_end_ts=300,
    )
    for created_at, action, shares, notional in (
        (10.0, "BUY", 4.0, 2.0),
        (20.0, "SELL", 2.0, 1.0),
    ):
        db.insert_live_order_audit(
            {
                "created_at": created_at,
                "decision_id": f"d-{action}",
                "window_start": 0,
                "window_end": 300,
                "market_id": "market-1",
                "token_id": "up-token",
                "side": "UP",
                "action": action,
                "price": 0.5,
                "notional_usdc": notional,
                "shares": shares,
                "status": "MATCHED",
                "external_order_id": f"o-{action}",
                "reason": "matched",
                "response_json": {},
            }
        )
    client.positions = [
        SimpleNamespace(token_id="up-token", size=4.0, initial_value=2.0, avg_price=0.5),
    ]
    client.trades = [
        SimpleNamespace(side="BUY", token_id="up-token", size=4.0, price=0.5, matched_at=datetime.fromtimestamp(10, tz=UTC)),
    ]

    asyncio.run(executor.refresh_account(market))

    shape = executor.get_position("market-1")
    assert shape.up_shares == 2.0
    assert shape.total_cost == 1.0


def test_partially_matched_live_sell_records_only_matched_shares(tmp_path):
    executor, db, client = make_executor(tmp_path, allow_sell_orders=True)
    client.order_response = {"ok": True, "order_id": "partial-sell", "status": "live"}
    client.order_details["partial-sell"] = {"size_matched": "0.4"}

    result = asyncio.run(
        executor.place_order(
            timestamp=100.0,
            window_start=0,
            window_end=300,
            market_id="market-1",
            token_id="up-token",
            side="UP",
            action="SELL",
            price=0.5,
            notional_usdc=0.5,
            shares=1.0,
            decision_id="decision-partial-sell",
            quote_ts=100.0,
            reason="late_weak_inventory_trim",
            shape_before=PositionShape(
                up_shares=4.0,
                down_shares=2.0,
                total_cost=2.0,
                pnl_if_up=2.0,
                pnl_if_down=0.0,
                worst_case_pnl=0.0,
            ),
            fills_before=[],
            metadata={"role": "INVENTORY_TRIM"},
        )
    )

    audit = db.list_matched_live_orders("market-1")[-1]
    assert result.status == "MATCHED"
    assert result.reason == "partially_matched_then_canceled"
    assert audit["shares"] == 0.4
    assert audit["notional_usdc"] == 0.2


def test_risk_reducing_live_sell_is_allowed_below_buy_balance_floor(tmp_path):
    executor, _, client = make_executor(tmp_path, allow_sell_orders=True)
    executor.snapshot.collateral_balance_usdc = 0.0
    executor.snapshot.session_start_balance_usdc = 100.0
    client.order_response = {
        "ok": True,
        "order_id": "sell-low-cash",
        "status": "matched",
        "making_amount": "1",
        "taking_amount": "0.5",
    }

    result = asyncio.run(
        executor.place_order(
            timestamp=100.0,
            window_start=0,
            window_end=300,
            market_id="market-1",
            token_id="up-token",
            side="UP",
            action="SELL",
            price=0.5,
            notional_usdc=0.5,
            shares=1.0,
            decision_id="decision-sell-low-cash",
            quote_ts=100.0,
            reason="late_weak_inventory_trim",
            shape_before=PositionShape(
                up_shares=4.0,
                down_shares=2.0,
                total_cost=2.0,
                pnl_if_up=2.0,
                pnl_if_down=0.0,
                worst_case_pnl=0.0,
            ),
            fills_before=[],
            metadata={"role": "INVENTORY_TRIM"},
        )
    )

    assert result.status == "MATCHED"


def test_locked_live_buy_triggers_separately_armed_redeem(tmp_path):
    executor, _, client = make_executor(tmp_path, allow_redeem_orders=True)
    client.order_response = {
        "ok": True,
        "order_id": "buy-lock-1",
        "status": "matched",
        "making_amount": "0.2",
        "taking_amount": "2",
    }

    result = asyncio.run(
        executor.place_order(
            timestamp=100.0,
            window_start=0,
            window_end=300,
            market_id="market-1",
            token_id="up-token",
            side="UP",
            action="BUY",
            price=0.1,
            notional_usdc=0.2,
            shares=2.0,
            decision_id="decision-buy-lock",
            quote_ts=100.0,
            reason="second_side_lock",
            shape_before=PositionShape(
                down_shares=2.0,
                total_cost=0.4,
                pnl_if_up=-0.4,
                pnl_if_down=1.6,
                worst_case_pnl=-0.4,
            ),
            fills_before=[],
        )
    )

    assert result.status == "MATCHED"
    assert client.redeemed == [
        {
            "args": (),
            "kwargs": {
                "market_id": "market-1",
                "token_id": "up-token",
                "side": "UP",
                "shares": 2.0,
                "size": 2.0,
                "amount": 2.0,
                "quantity": 2.0,
                "market": "market-1",
                "asset_id": "up-token",
                "condition_id": "market-1",
            },
        }
    ]
