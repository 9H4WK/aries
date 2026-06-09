from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any, Optional

from .database import Database
from .logger import JsonlLogger
from .models import PaperFill, PaperOrder, PositionShape, WindowPnl, WindowResult
from .position_shape import apply_buy, shape_from_fills
from .risk import quote_age_seconds


class PaperBroker:
    def __init__(self, config: dict, db: Database, logger: JsonlLogger):
        self.config = config
        self.db = db
        self.logger = logger

    def submit_order(
        self,
        *,
        timestamp: float,
        window_start: int,
        window_end: int,
        market_id: str,
        token_id: str,
        side: str,
        action: str,
        price: Optional[float],
        notional_usdc: float,
        shares: float,
        decision_id: str,
        quote_ts: Optional[float],
        reason: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[PaperOrder, Optional[PaperFill]]:
        shape_before = self.get_position(market_id, window_start)
        fills_before = self.get_fills(market_id, window_start)
        order_metadata = dict(metadata or {})
        order_metadata["strategy_reason"] = reason
        order_metadata["quote_age_seconds"] = (timestamp - quote_ts) if quote_ts is not None else None
        order_metadata["shape_before"] = asdict(shape_before)
        order_metadata["orders_in_window_before"] = len(fills_before)
        order_metadata["locked_before"] = shape_before.locked

        attempt_payload = {
            "timestamp": timestamp,
            "window_start": window_start,
            "window_end": window_end,
            "market_id": market_id,
            "token_id": token_id,
            "side": side,
            "action": action,
            "price": price,
            "notional_usdc": notional_usdc,
            "shares": shares,
            "decision_id": decision_id,
            "reason": reason,
            "metadata": order_metadata,
        }
        self.logger.paper_order_attempt(attempt_payload)

        reject_reason = self._reject_reason(
            now_ts=timestamp,
            window_end=window_end,
            price=price,
            notional_usdc=notional_usdc,
            shares=shares,
            quote_ts=quote_ts,
            market_id=market_id,
            window_start=window_start,
            side=side,
            shape_before=shape_before,
            fills_before=fills_before,
        )
        status = "REJECTED" if reject_reason else "ACCEPTED"
        order_reason = reject_reason or "accepted_for_paper_fill"
        payload = {
            "created_at": timestamp,
            "decision_id": decision_id,
            "window_start": window_start,
            "window_end": window_end,
            "market_id": market_id,
            "token_id": token_id,
            "side": side,
            "action": action,
            "price": float(price or 0.0),
            "notional_usdc": float(notional_usdc),
            "shares": float(shares),
            "status": status,
            "reason": order_reason,
            "metadata_json": order_metadata,
        }
        order_id = self.db.insert_paper_order(payload)
        order = PaperOrder(
            id=order_id,
            timestamp=timestamp,
            window_start=window_start,
            window_end=window_end,
            market_id=market_id,
            token_id=token_id,
            side=side,
            action=action,
            price=float(price or 0.0),
            notional_usdc=float(notional_usdc),
            shares=float(shares),
            status=status,
            reason=order_reason,
            decision_id=decision_id,
            metadata=order_metadata,
        )

        if reject_reason:
            self.logger.paper_order_rejected(asdict(order))
            return order, None

        self.logger.paper_order_accepted(asdict(order))
        if not bool(self.config.get_path("paper.immediate_fill", True)):
            return order, None

        fill = self.simulate_fill(order)
        refreshed = self.db.get_paper_order(order_id)
        if refreshed:
            order.status = str(refreshed["status"])
            order.reason = str(refreshed["reason"])
            order.metadata = _loads_json(refreshed.get("metadata_json"))
        return order, fill

    def simulate_fill(self, order: PaperOrder) -> Optional[PaperFill]:
        if order.id is None or order.status != "ACCEPTED":
            return None

        shape_before = self.get_position(order.market_id, order.window_start)
        shape_after = apply_buy(shape_before, order.side, order.shares, order.notional_usdc)
        fill_metadata = dict(order.metadata)
        fill_metadata["shape_before"] = asdict(shape_before)
        fill_metadata["shape_after"] = asdict(shape_after)

        fill = PaperFill(
            order_id=order.id,
            created_at=order.timestamp,
            window_start=order.window_start,
            window_end=order.window_end,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            action=order.action,
            price=order.price,
            notional_usdc=order.notional_usdc,
            shares=order.shares,
            metadata=fill_metadata,
        )
        fill_id = self.db.insert_paper_fill(
            {
                "order_id": fill.order_id,
                "created_at": fill.created_at,
                "window_start": fill.window_start,
                "window_end": fill.window_end,
                "market_id": fill.market_id,
                "token_id": fill.token_id,
                "side": fill.side,
                "action": fill.action,
                "price": fill.price,
                "notional_usdc": fill.notional_usdc,
                "shares": fill.shares,
                "metadata_json": fill.metadata,
            }
        )
        fill.id = fill_id

        order.status = "FILLED"
        order.reason = "filled_immediately_at_selected_ask"
        order.metadata["fill_id"] = fill.id
        order.metadata["shape_after"] = asdict(shape_after)
        self.db.update_paper_order(order.id, order.status, order.reason, order.metadata)
        self.logger.fill(asdict(fill))
        return fill

    def get_position(self, market_id: str, window_start: int) -> PositionShape:
        return shape_from_fills(self.get_fills(market_id, window_start))

    def get_fills(self, market_id: str, window_start: int) -> list[PaperFill]:
        rows = self.db.list_paper_fills(market_id, window_start)
        fills: list[PaperFill] = []
        for row in rows:
            fills.append(
                PaperFill(
                    id=int(row["id"]),
                    order_id=int(row["order_id"]),
                    created_at=float(row["created_at"]),
                    window_start=int(row["window_start"]),
                    window_end=int(row["window_end"]),
                    market_id=str(row["market_id"]),
                    token_id=str(row["token_id"]),
                    side=str(row["side"]),
                    action=str(row["action"]),
                    price=float(row["price"]),
                    notional_usdc=float(row["notional_usdc"]),
                    shares=float(row["shares"]),
                    metadata=_loads_json(row.get("metadata_json")),
                )
            )
        return fills

    def get_open_positions(self) -> list[dict[str, Any]]:
        positions: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for row in self.db.list_all_paper_fills():
            key = (str(row["market_id"]), int(row["window_start"]))
            if key in seen:
                continue
            seen.add(key)
            if self.db.get_window_result(key[0], key[1]):
                continue
            positions.append(
                {
                    "market_id": key[0],
                    "window_start": key[1],
                    "shape": asdict(self.get_position(key[0], key[1])),
                }
            )
        return positions

    def settle_window(
        self,
        *,
        timestamp: float,
        window_start: int,
        window_end: int,
        market_id: str,
        target_price: Optional[float],
        final_btc_price: Optional[float],
        notes: str = "",
    ) -> Optional[dict[str, Any]]:
        if target_price is None or final_btc_price is None:
            return None

        winning_side = "UP" if final_btc_price > target_price else "DOWN"
        shape = self.get_position(market_id, window_start)
        realized_pnl = shape.up_shares - shape.total_cost if winning_side == "UP" else shape.down_shares - shape.total_cost

        result = WindowResult(
            window_start=window_start,
            window_end=window_end,
            market_id=market_id,
            target_price=float(target_price),
            final_btc_price=float(final_btc_price),
            winning_side=winning_side,
            resolved=True,
            resolved_at=timestamp,
        )
        result.id = self.db.upsert_window_result(asdict(result))

        pnl = WindowPnl(
            window_start=window_start,
            market_id=market_id,
            up_shares=shape.up_shares,
            down_shares=shape.down_shares,
            total_cost=shape.total_cost,
            pnl_if_up=shape.pnl_if_up,
            pnl_if_down=shape.pnl_if_down,
            realized_pnl=realized_pnl,
            winning_side=winning_side,
            locked=shape.locked,
            notes=notes,
        )
        pnl.id = self.db.upsert_window_pnl(asdict(pnl))

        self.logger.window_resolved(asdict(result))
        self.logger.window_pnl(asdict(pnl))
        return {"result": asdict(result), "pnl": asdict(pnl)}

    def _reject_reason(
        self,
        *,
        now_ts: float,
        window_end: int,
        price: Optional[float],
        notional_usdc: float,
        shares: float,
        quote_ts: Optional[float],
        market_id: str,
        window_start: int,
        side: str,
        shape_before: PositionShape,
        fills_before: list[PaperFill],
    ) -> Optional[str]:
        if price is None:
            return "price_missing"
        if price <= 0 or price >= 1:
            return "price_out_of_bounds"
        if notional_usdc <= 0:
            return "notional_non_positive"
        min_shares = float(self.config.get_path("sizing.min_order_shares", 1.0))
        if shares < min_shares:
            return "shares_below_minimum"
        max_quote_age = float(self.config.get_path("polymarket.max_quote_age_seconds", 3))
        if quote_ts is None or (now_ts - quote_ts) > max_quote_age:
            return "quote_stale"
        if shape_before.locked and bool(self.config.get_path("risk.stop_when_locked", True)):
            return "window_locked"
        if now_ts >= window_end:
            return "window_expired"

        shape_after = apply_buy(shape_before, side, shares, notional_usdc)
        if shape_after.total_cost > float(self.config.get_path("risk.max_window_notional_usdc", 3.0)):
            return "max_window_notional"
        if shape_after.worst_case_pnl < -float(self.config.get_path("risk.max_window_loss_usdc", 1.0)):
            return "max_window_loss"
        max_orders = int(self.config.get_path("risk.max_orders_per_window", 3))
        if len(fills_before) >= max_orders:
            return "max_orders_per_window"

        max_same_side_adds = int(self.config.get_path("risk.max_same_side_adds", 0))
        if max_same_side_adds == 0:
            filled_same_side = sum(1 for fill in fills_before if fill.side == side)
            if filled_same_side > 0:
                return "same_side_add_blocked"
        return None


def _loads_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}
