from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .database import Database
from .logger import JsonlLogger
from .models import PositionShape
from .position_shape import apply_buy


class Validator:
    def __init__(self, config: dict, db: Database, logger: JsonlLogger):
        self.config = config
        self.db = db
        self.logger = logger

    def validate_session(self) -> bool:
        failures: list[str] = []
        orders = self.db.list_all_paper_orders()
        fills = self.db.list_all_paper_fills()
        decisions = self.db.list_decisions()
        max_quote_age = float(self.config.get_path("polymarket.max_quote_age_seconds", 3))
        min_shares = float(self.config.get_path("sizing.min_order_shares", 1.0))

        order_ids = {int(order["id"]) for order in orders}
        for order in orders:
            metadata = _loads_json(order.get("metadata_json"))
            if not str(order.get("decision_id") or "").strip():
                failures.append(f"paper_order_missing_decision_id:{order['id']}")
            if float(order["shares"]) < min_shares:
                failures.append(f"paper_order_below_min_shares:{order['id']}")
            quote_age = metadata.get("quote_age_seconds")
            if quote_age is None or float(quote_age) > max_quote_age:
                failures.append(f"paper_order_stale_quote:{order['id']}")
            if float(order["created_at"]) >= int(order["window_end"]):
                failures.append(f"paper_order_after_window_end:{order['id']}")
            if bool(metadata.get("locked_before")) and bool(self.config.get_path("risk.stop_when_locked", True)):
                failures.append(f"paper_order_while_locked:{order['id']}")
            if str(order.get("status")) == "REJECTED" and not str(order.get("reason") or "").strip():
                failures.append(f"paper_order_rejected_without_reason:{order['id']}")

        for fill in fills:
            if fill.get("order_id") is None:
                failures.append(f"paper_fill_missing_order_id:{fill['id']}")
            elif int(fill["order_id"]) not in order_ids:
                failures.append(f"paper_fill_orphan_order:{fill['id']}")

        if int(self.config.get_path("risk.max_same_side_adds", 0)) == 0:
            grouped_orders: dict[tuple[str, int], dict[str, int]] = {}
            for order in orders:
                if str(order["status"]) != "FILLED":
                    continue
                key = (str(order["market_id"]), int(order["window_start"]))
                side_counts = grouped_orders.setdefault(key, {"UP": 0, "DOWN": 0})
                side_counts[str(order["side"])] += 1
            for key, counts in grouped_orders.items():
                if counts["UP"] > 1:
                    failures.append(f"same_side_up_repeat_blocked:{key[0]}:{key[1]}")
                if counts["DOWN"] > 1:
                    failures.append(f"same_side_down_repeat_blocked:{key[0]}:{key[1]}")

        for decision in decisions:
            payload = decision.get("payload") or {}
            if not str(payload.get("selected_reason") or "").strip():
                failures.append(f"decision_missing_reason:{decision['id']}")
            if str(payload.get("decision")) in {"SKIP", "WATCH"} and not str(payload.get("selected_reason") or "").strip():
                failures.append(f"skip_missing_reason:{decision['id']}")

        self._validate_fill_shape_updates(fills, failures)
        self._validate_realized_pnl(failures)

        if failures:
            self.logger.validation_failed({"failures": failures, "failure_count": len(failures)})
            if bool(self.config.get_path("validation.fail_fast", False)):
                raise RuntimeError("Validation failed")
            return False

        self.logger.validation_passed(
            {
                "orders_checked": len(orders),
                "fills_checked": len(fills),
                "decisions_checked": len(decisions),
            }
        )
        return True

    def _validate_fill_shape_updates(self, fills: list[dict[str, Any]], failures: list[str]) -> None:
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for fill in fills:
            key = (str(fill["market_id"]), int(fill["window_start"]))
            grouped.setdefault(key, []).append(fill)

        for key, rows in grouped.items():
            shape = PositionShape()
            for row in rows:
                metadata = _loads_json(row.get("metadata_json"))
                expected_before = metadata.get("shape_before")
                expected_after = metadata.get("shape_after")
                if expected_before and not _shape_matches(shape, expected_before):
                    failures.append(f"fill_shape_before_mismatch:{row['id']}")
                shape = apply_buy(shape, str(row["side"]), float(row["shares"]), float(row["notional_usdc"]))
                if expected_after and not _shape_matches(shape, expected_after):
                    failures.append(f"fill_shape_after_mismatch:{row['id']}")

    def _validate_realized_pnl(self, failures: list[str]) -> None:
        for row in self.db.list_window_report_rows():
            winning_side = str(row.get("winning_side") or "")
            pnl_if_up = float(row.get("pnl_if_up", 0.0) or 0.0)
            pnl_if_down = float(row.get("pnl_if_down", 0.0) or 0.0)
            realized_pnl = float(row.get("realized_pnl", 0.0) or 0.0)
            expected = pnl_if_up if winning_side == "UP" else pnl_if_down
            if abs(realized_pnl - expected) > 1e-9:
                failures.append(f"realized_pnl_mismatch:{row['market_id']}:{row['window_start']}")


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


def _shape_matches(shape: PositionShape, payload: dict[str, Any]) -> bool:
    current = asdict(shape)
    for key, value in current.items():
        if abs(float(value) - float(payload.get(key, 0.0))) > 1e-9:
            return False
    return True
