from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from .models import Fill, MarketInfo, PositionShape
from .position_shape import recompute
from .risk import scaled_risk_cap


LIVE_CONFIRMATION = "I_UNDERSTAND_REAL_ORDERS"


@dataclass
class LiveOrderResult:
    audit_id: int
    status: str
    reason: str
    external_order_id: Optional[str] = None
    response: Optional[dict[str, Any]] = None


@dataclass
class LiveAccountSnapshot:
    enabled: bool = False
    connected: bool = False
    wallet: Optional[str] = None
    market_id: Optional[str] = None
    open_orders_count: int = 0
    recent_trades_count: int = 0
    open_positions_count: int = 0
    closed_positions_count: int = 0
    collateral_balance_usdc: Optional[float] = None
    latest_event: Optional[str] = None
    latest_error: Optional[str] = None
    refreshed_at: Optional[float] = None


class LiveExecutor:
    """Authenticated Polymarket CLOB V2 monitoring and guarded BUY execution.

    The strategy supplies the same approved candidate used by paper trading.
    This class independently repeats the important order/risk checks before it
    signs anything. It never reads secrets from config values, only from the
    configured environment-variable names.
    """

    def __init__(self, config: dict, db: Any, logger: Any, client: Any = None):
        self.config = config
        self.db = db
        self.logger = logger
        self.client = client
        self.snapshot = LiveAccountSnapshot(enabled=self.monitoring_enabled)
        self.positions_by_market: dict[str, PositionShape] = {}
        self.fills_by_market: dict[str, list[Fill]] = {}
        self._last_refresh = 0.0
        self._last_order_at = 0.0
        self._user_stream_task: Optional[asyncio.Task[Any]] = None
        self._stream_market_id: Optional[str] = None

    @property
    def mode(self) -> str:
        return str(self._get("mode", "paper"))

    @property
    def monitoring_enabled(self) -> bool:
        return self.mode in {"live_monitor", "live"} or bool(self._get("live.monitor_enabled", False))

    @property
    def orders_armed(self) -> bool:
        return (
            self.mode == "live"
            and bool(self._get("live.enabled", False))
            and bool(self._get("live.allow_real_orders", False))
            and str(self._get("live.confirm_real_orders", "")) == LIVE_CONFIRMATION
        )

    async def start(self) -> None:
        if not self.monitoring_enabled:
            self.logger.set_live_account_snapshot(asdict(self.snapshot))
            return

        if self.mode == "live" and not self.orders_armed:
            raise RuntimeError(
                "Live mode is not armed. Set live.enabled, live.allow_real_orders, "
                f"and live.confirm_real_orders={LIVE_CONFIRMATION!r}."
            )

        try:
            if self.client is None:
                self.client = await self._create_client()
            self.snapshot.connected = True
            self.snapshot.wallet = self._env_optional("live.wallet_env")
            self.snapshot.latest_error = None
            self.logger.live_monitor_connected(asdict(self.snapshot))
            await self.refresh_account(force=True)
        except Exception as error:
            self.snapshot.latest_error = self._safe_error(error)
            self.logger.live_monitor_failed(asdict(self.snapshot))
            if self.mode in {"live_monitor", "live"}:
                raise
        finally:
            self.logger.set_live_account_snapshot(asdict(self.snapshot))

    async def stop(self) -> None:
        if self._user_stream_task:
            self._user_stream_task.cancel()
            await asyncio.gather(self._user_stream_task, return_exceptions=True)
            self._user_stream_task = None
        if self.client is not None:
            close = getattr(self.client, "close", None)
            if close is not None:
                await close()
            self.client = None
        self.snapshot.connected = False
        self.logger.set_live_account_snapshot(asdict(self.snapshot))

    async def refresh_account(self, market: Optional[MarketInfo] = None, force: bool = False) -> LiveAccountSnapshot:
        if not self.monitoring_enabled or self.client is None:
            return self.snapshot

        now = time.time()
        interval = max(1.0, float(self._get("live.account_refresh_seconds", 5)))
        if not force and self.snapshot.market_id == market_id and now - self._last_refresh < interval:
            return self.snapshot

        market_id = market.market_id if market else None
        try:
            market_filter = [market_id] if market_id else None
            open_orders_page, trades_page, positions_page, closed_page, balance = await asyncio.gather(
                self.client.list_open_orders(market=market_id).first_page(),
                self.client.list_account_trades(market=market_id).first_page(),
                self.client.list_positions(market=market_filter, size_threshold=0.0).first_page(),
                self.client.list_closed_positions(market=market_filter).first_page(),
                self.client.get_balance_allowance(asset_type="COLLATERAL"),
            )
            open_orders = list(open_orders_page.items)
            trades = list(trades_page.items)
            positions = list(positions_page.items)
            closed_positions = list(closed_page.items)

            self.snapshot.connected = True
            self.snapshot.market_id = market_id
            self.snapshot.open_orders_count = len(open_orders)
            self.snapshot.recent_trades_count = len(trades)
            self.snapshot.open_positions_count = len(positions)
            self.snapshot.closed_positions_count = len(closed_positions)
            collateral_decimals = max(0, int(self._get("live.collateral_decimals", 6)))
            self.snapshot.collateral_balance_usdc = float(balance.balance) / (10 ** collateral_decimals)
            self.snapshot.refreshed_at = now
            self.snapshot.latest_error = None
            if market:
                self.positions_by_market[market.market_id] = self._shape_from_positions(market, positions)
                self.fills_by_market[market.market_id] = self._fills_from_trades(market, trades)
            self._last_refresh = now
            self.logger.live_account_snapshot(asdict(self.snapshot))
        except Exception as error:
            self.snapshot.connected = False
            self.snapshot.latest_error = self._safe_error(error)
            self.snapshot.refreshed_at = now
            self.logger.live_account_snapshot(asdict(self.snapshot))

        self.logger.set_live_account_snapshot(asdict(self.snapshot))
        return self.snapshot

    async def subscribe_market(self, market_id: str) -> None:
        if not self.monitoring_enabled or self.client is None or self._stream_market_id == market_id:
            return
        if self._user_stream_task:
            self._user_stream_task.cancel()
            await asyncio.gather(self._user_stream_task, return_exceptions=True)
        self._stream_market_id = market_id
        self._user_stream_task = asyncio.create_task(self._consume_user_stream(market_id))

    def get_position(self, market_id: str) -> PositionShape:
        return self.positions_by_market.get(market_id, PositionShape())

    def get_fills(self, market_id: str) -> list[Fill]:
        return self.fills_by_market.get(market_id, [])

    async def place_order(
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
        shape_before: PositionShape,
        fills_before: list[Fill],
    ) -> LiveOrderResult:
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
            "status": "ATTEMPT",
            "reason": reason,
            "external_order_id": None,
            "response_json": {
                "quote_age_seconds": timestamp - quote_ts if quote_ts is not None else None,
                "shape_before": asdict(shape_before),
            },
        }
        self.logger.live_order_attempt(payload)

        reject_reason = self._reject_reason(
            timestamp=timestamp,
            window_end=window_end,
            price=price,
            notional_usdc=notional_usdc,
            shares=shares,
            decision_id=decision_id,
            quote_ts=quote_ts,
            market_id=market_id,
            side=side,
            action=action,
            shape_before=shape_before,
            fills_before=fills_before,
        )
        if reject_reason:
            payload["status"] = "REJECTED"
            payload["reason"] = reject_reason
            audit_id = self.db.insert_live_order_audit(payload)
            payload["id"] = audit_id
            self.logger.live_order_rejected(payload)
            return LiveOrderResult(audit_id=audit_id, status="REJECTED", reason=reject_reason)

        try:
            response = await self.client.place_limit_order(
                token_id=token_id,
                price=float(price),
                size=float(shares),
                side="BUY",
                post_only=bool(self._get("live.post_only", False)),
            )
            response_payload = self._as_payload(response)
            accepted = bool(response_payload.get("ok", False))
            status = str(response_payload.get("status", "REJECTED")).upper() if accepted else "REJECTED"
            order_reason = "clob_order_accepted" if accepted else str(
                response_payload.get("code") or response_payload.get("message") or "clob_order_rejected"
            )
            external_id = response_payload.get("order_id")
            if (
                accepted
                and status == "LIVE"
                and external_id
                and bool(self._get("live.cancel_unmatched_orders", True))
            ):
                try:
                    cancel_response = await self.client.cancel_order(order_id=str(external_id))
                    response_payload["cancel_response"] = self._as_payload(cancel_response)
                    status = "CANCELED"
                    order_reason = "unmatched_marketable_limit_canceled"
                except Exception as cancel_error:
                    response_payload["cancel_error"] = self._safe_error(cancel_error)
                    order_reason = "clob_order_live_cancel_failed"
            payload.update(
                status=status,
                reason=order_reason,
                external_order_id=external_id,
                response_json=response_payload,
            )
            audit_id = self.db.insert_live_order_audit(payload)
            payload["id"] = audit_id
            self._last_order_at = time.time()
            if accepted:
                self.logger.live_order_accepted(payload)
            else:
                self.logger.live_order_rejected(payload)
            return LiveOrderResult(
                audit_id=audit_id,
                status=status,
                reason=order_reason,
                external_order_id=str(external_id) if external_id else None,
                response=response_payload,
            )
        except Exception as error:
            payload["status"] = "REJECTED"
            payload["reason"] = self._safe_error(error)
            audit_id = self.db.insert_live_order_audit(payload)
            payload["id"] = audit_id
            self.logger.live_order_rejected(payload)
            return LiveOrderResult(audit_id=audit_id, status="REJECTED", reason=payload["reason"])

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        self._guard_orders()
        response = await self.client.cancel_order(order_id=order_id)
        return self._as_payload(response)

    async def redeem(self, *args: Any, **kwargs: Any) -> None:
        # TODO: Add a separately guarded redeem workflow after live BUY execution is validated.
        raise RuntimeError("Live redeem is not implemented.")

    async def _create_client(self) -> Any:
        try:
            from polymarket import ApiKeyCreds, AsyncSecureClient
        except ImportError as error:
            raise RuntimeError(
                "Live monitoring requires the official CLOB V2 SDK. "
                "Install requirements with: pip install -r requirements.txt"
            ) from error

        private_key = self._env_required("live.private_key_env")
        wallet = self._env_optional("live.wallet_env")
        api_key = self._env_optional("live.api_key_env")
        api_secret = self._env_optional("live.api_secret_env")
        api_passphrase = self._env_optional("live.api_passphrase_env")
        credentials = None
        if any((api_key, api_secret, api_passphrase)) and not all((api_key, api_secret, api_passphrase)):
            raise RuntimeError(
                "Set POLYMARKET_API_KEY, POLYMARKET_API_SECRET, and "
                "POLYMARKET_API_PASSPHRASE together, or leave all three unset."
            )
        if api_key and api_secret and api_passphrase:
            credentials = ApiKeyCreds(key=api_key, secret=api_secret, passphrase=api_passphrase)
        return await AsyncSecureClient.create(
            private_key=private_key,
            wallet=wallet,
            credentials=credentials,
        )

    async def _consume_user_stream(self, market_id: str) -> None:
        try:
            from polymarket.streams import UserSpec

            handle = await self.client.subscribe(UserSpec(markets=[market_id]))
            async with handle:
                async for event in handle:
                    payload = self._as_payload(event)
                    event_type = str(payload.get("type", "user"))
                    self.snapshot.latest_event = event_type
                    self.snapshot.latest_error = None
                    self.db.insert_live_account_event(
                        {
                            "created_at": time.time(),
                            "market_id": market_id,
                            "event_type": event_type,
                            "external_id": self._event_id(payload),
                            "payload_json": payload,
                        }
                    )
                    self.logger.live_user_event({"market_id": market_id, **payload})
                    self.logger.set_live_account_snapshot(asdict(self.snapshot))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.snapshot.latest_error = self._safe_error(error)
            self.logger.set_live_account_snapshot(asdict(self.snapshot))

    def _reject_reason(
        self,
        *,
        timestamp: float,
        window_end: int,
        price: Optional[float],
        notional_usdc: float,
        shares: float,
        decision_id: str,
        quote_ts: Optional[float],
        market_id: str,
        side: str,
        action: str,
        shape_before: PositionShape,
        fills_before: list[Fill],
    ) -> Optional[str]:
        if not self.orders_armed:
            return "live_orders_not_armed"
        if self.client is None:
            return "live_client_not_connected"
        max_state_age = max(1.0, float(self._get("live.max_account_state_age_seconds", 10)))
        if (
            not self.snapshot.connected
            or self.snapshot.refreshed_at is None
            or time.time() - self.snapshot.refreshed_at > max_state_age
        ):
            return "live_account_state_stale"
        if self.snapshot.market_id == market_id and self.snapshot.open_orders_count > 0:
            return "live_open_order_pending"
        if not decision_id:
            return "decision_id_missing"
        if action != "BUY" or side not in {"UP", "DOWN"}:
            return "only_up_down_buy_orders_supported"
        if price is None:
            return "price_missing"
        if price <= 0 or price >= 1:
            return "price_out_of_bounds"
        if notional_usdc <= 0:
            return "notional_non_positive"
        if shares < float(self._get("sizing.min_order_shares", 1.0)):
            return "shares_below_minimum"
        max_quote_age = float(self._get("polymarket.max_quote_age_seconds", 3))
        if quote_ts is None or timestamp - quote_ts > max_quote_age:
            return "quote_stale"
        if timestamp >= window_end:
            return "window_expired"
        if shape_before.locked:
            if bool(self._get("risk.stop_when_locked", True)):
                return "window_locked"
            if not bool(self._get("risk.allow_post_lock_adds", False)):
                return "post_lock_adds_disabled"
        cooldown = max(0.0, float(self._get("live.order_cooldown_seconds", 3)))
        if time.time() - self._last_order_at < cooldown:
            return "live_order_cooldown"

        from .position_shape import apply_buy

        shape_after = apply_buy(shape_before, side, shares, notional_usdc)
        balance = float(self._get("account.balance_usdc", 0.0)) + self.db.total_realized_pnl()
        max_notional = scaled_risk_cap(
            self.config, float(self._get("risk.max_window_notional_usdc", 3.0)), balance
        )
        max_loss = scaled_risk_cap(
            self.config, float(self._get("risk.max_window_loss_usdc", 1.0)), balance
        )
        if shape_after.total_cost > max_notional:
            return "max_window_notional"
        if shape_after.worst_case_pnl < -max_loss:
            return "max_window_loss"
        if len(fills_before) >= int(self._get("risk.max_orders_per_window", 3)):
            return "max_orders_per_window"
        allow_post_lock_add = shape_before.locked and bool(self._get("risk.allow_post_lock_adds", False))
        if (
            int(self._get("risk.max_same_side_adds", 0)) == 0
            and not allow_post_lock_add
            and any(fill.side == side for fill in fills_before)
        ):
            return "same_side_add_blocked"
        return None

    def _guard_orders(self) -> None:
        if not self.orders_armed or self.client is None:
            raise RuntimeError("Live execution is not armed.")

    def _shape_from_positions(self, market: MarketInfo, positions: list[Any]) -> PositionShape:
        shape = PositionShape()
        for position in positions:
            token_id = str(getattr(position, "token_id", "") or "")
            size = float(getattr(position, "size", 0.0) or 0.0)
            initial_value = getattr(position, "initial_value", None)
            avg_price = float(getattr(position, "avg_price", 0.0) or 0.0)
            cost = float(initial_value) if initial_value is not None else size * avg_price
            if token_id == market.up_token_id:
                shape.up_shares += size
                shape.total_cost += cost
            elif token_id == market.down_token_id:
                shape.down_shares += size
                shape.total_cost += cost
        return recompute(shape)

    def _fills_from_trades(self, market: MarketInfo, trades: list[Any]) -> list[Fill]:
        fills: list[Fill] = []
        for trade in trades:
            if str(getattr(trade, "side", "")).upper() != "BUY":
                continue
            token_id = str(getattr(trade, "token_id", "") or "")
            side = "UP" if token_id == market.up_token_id else "DOWN" if token_id == market.down_token_id else None
            if side is None:
                continue
            shares = float(getattr(trade, "size", 0.0) or 0.0)
            price = float(getattr(trade, "price", 0.0) or 0.0)
            matched_at = getattr(trade, "matched_at", None)
            ts = matched_at.timestamp() if hasattr(matched_at, "timestamp") else time.time()
            fills.append(
                Fill(
                    ts=ts,
                    market_id=market.market_id,
                    window_start_ts=market.window_start_ts,
                    side=side,
                    price=price,
                    notional_usdc=shares * price,
                    shares=shares,
                    reason="live_account_trade",
                )
            )
        return fills

    def _get(self, path: str, default: Any = None) -> Any:
        getter = getattr(self.config, "get_path", None)
        if getter:
            return getter(path, default)
        current: Any = self.config
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def _env_required(self, config_path: str) -> str:
        name = str(self._get(config_path, ""))
        value = os.environ.get(name, "")
        if not value:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return value

    def _env_optional(self, config_path: str) -> Optional[str]:
        name = str(self._get(config_path, ""))
        return os.environ.get(name) or None if name else None

    def _as_payload(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if hasattr(value, "__dataclass_fields__"):
            return asdict(value)
        return {"value": str(value)}

    def _event_id(self, payload: dict[str, Any]) -> Optional[str]:
        inner = payload.get("payload")
        if isinstance(inner, dict) and inner.get("id"):
            return str(inner["id"])
        return None

    def _safe_error(self, error: Exception) -> str:
        # SDK errors are useful, but key material must never be serialized.
        text = f"{type(error).__name__}: {error}"
        for config_path in (
            "live.private_key_env",
            "live.api_key_env",
            "live.api_secret_env",
            "live.api_passphrase_env",
        ):
            secret = self._env_optional(config_path)
            if secret:
                text = text.replace(secret, "<redacted>")
        return text
