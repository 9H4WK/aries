from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from .models import Fill, MarketInfo, PositionShape
from .position_shape import apply_trade, recompute, shape_from_fills
from .risk import account_equity_floor_passes, window_risk_caps


LIVE_CONFIRMATION = "I_UNDERSTAND_REAL_ORDERS"


def _loads_json(value: Any) -> dict[str, Any]:
    """Decode a SQLite JSON string into a dict."""
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


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
    session_start_balance_usdc: Optional[float] = None
    session_drawdown_percent: float = 0.0
    orders_halted: bool = False
    halt_reason: Optional[str] = None
    latest_event: Optional[str] = None
    latest_error: Optional[str] = None
    refreshed_at: Optional[float] = None


class LiveExecutor:
    """Authenticated Polymarket CLOB V2 monitoring and guarded execution.

    The strategy supplies the same approved candidate used by paper trading.
    This class independently repeats the important order/risk checks before it
    signs anything. It never reads secrets from config values, only from the
    configured environment-variable names.
    """

    def __init__(self, config: dict, db: Any, logger: Any, client: Any = None):
        """Keep live execution state, injected SDK client, and audit caches."""
        self.config = config
        self.db = db
        self.logger = logger
        self.client = client
        self.snapshot = LiveAccountSnapshot(enabled=self.monitoring_enabled)
        self.positions_by_market: dict[str, PositionShape] = {}
        self.fills_by_market: dict[str, list[Fill]] = {}
        self._last_refresh = 0.0
        self._last_order_at = 0.0
        self._last_rejection_at = 0.0
        self._orders_halted_reason: Optional[str] = None
        self._user_stream_task: Optional[asyncio.Task[Any]] = None
        self._stream_market_id: Optional[str] = None

    @property
    def mode(self) -> str:
        """Return the configured runtime mode."""
        return str(self._get("mode", "paper"))

    @property
    def monitoring_enabled(self) -> bool:
        """True when authenticated account data should be fetched/displayed."""
        return self.mode in {"live_monitor", "live"} or bool(self._get("live.monitor_enabled", False))

    @property
    def orders_armed(self) -> bool:
        """True only when live mode has every real-order safety switch enabled."""
        return (
            self.mode == "live"
            and bool(self._get("live.enabled", False))
            and bool(self._get("live.allow_real_orders", False))
            and str(self._get("live.confirm_real_orders", "")) == LIVE_CONFIRMATION
        )

    @property
    def sell_orders_armed(self) -> bool:
        """Require an additional explicit switch before any real SELL."""
        return self.orders_armed and bool(self._get("live.allow_sell_orders", False))

    @property
    def redeem_orders_armed(self) -> bool:
        """Require an additional explicit switch before any real redeem."""
        return self.orders_armed and bool(self._get("live.allow_redeem_orders", False))

    async def start(self) -> None:
        """Create the SDK client and fetch the initial live account snapshot."""
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
        """Cancel user-stream tasks and close the SDK client."""
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
        """Refresh open orders, trades, positions, balances, and live shape.

        Recent bot-owned matched audits are trusted immediately because the
        Polymarket positions endpoint can lag right after a fill.
        """
        if not self.monitoring_enabled or self.client is None:
            return self.snapshot

        now = time.time()
        market_id = market.market_id if market else None
        interval = max(1.0, float(self._get("live.account_refresh_seconds", 5)))
        if not force and self.snapshot.market_id == market_id and now - self._last_refresh < interval:
            return self.snapshot

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
            if self.snapshot.session_start_balance_usdc is None:
                self.snapshot.session_start_balance_usdc = self.snapshot.collateral_balance_usdc
            start_balance = self.snapshot.session_start_balance_usdc or 0.0
            if start_balance > 0:
                drawdown = max(0.0, start_balance - self.snapshot.collateral_balance_usdc)
                self.snapshot.session_drawdown_percent = (drawdown / start_balance) * 100.0
            self.snapshot.orders_halted = self._orders_halted_reason is not None
            self.snapshot.halt_reason = self._orders_halted_reason
            self.snapshot.refreshed_at = now
            self.snapshot.latest_error = None
            if market:
                account_shape = self._shape_from_positions(market, positions)
                account_fills = self._fills_from_trades(market, trades)
                audit_fills = self._audit_fills(market.market_id)
                try:
                    trade_shape = shape_from_fills(account_fills) if account_fills else PositionShape()
                except ValueError:
                    account_fills = []
                    trade_shape = PositionShape()
                audit_shape = shape_from_fills(audit_fills)

                # Matched bot audits are the immediate truth because positions
                # can lag after a fill. Account trades replace audits only when
                # they agree on remaining shares and provide actual fill prices.
                audit_is_newer = bool(audit_fills) and self._latest_fill_ts(audit_fills) > self._latest_fill_ts(account_fills)
                if audit_is_newer:
                    truth_fills, truth_shape = audit_fills, audit_shape
                elif positions and account_fills and self._same_inventory(trade_shape, account_shape):
                    truth_fills, truth_shape = account_fills, trade_shape
                elif audit_fills and account_fills and self._same_inventory(trade_shape, audit_shape):
                    truth_fills, truth_shape = account_fills, trade_shape
                elif audit_fills:
                    truth_fills, truth_shape = audit_fills, audit_shape
                elif account_fills and (not positions or self._same_inventory(trade_shape, account_shape)):
                    truth_fills, truth_shape = account_fills, trade_shape
                elif positions:
                    truth_fills, truth_shape = account_fills, account_shape
                else:
                    truth_fills, truth_shape = [], PositionShape()
                self.positions_by_market[market.market_id] = truth_shape
                self.fills_by_market[market.market_id] = truth_fills
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
        """Subscribe to authenticated user events for the active market."""
        if not self.monitoring_enabled or self.client is None or self._stream_market_id == market_id:
            return
        if self._user_stream_task:
            self._user_stream_task.cancel()
            await asyncio.gather(self._user_stream_task, return_exceptions=True)
        self._stream_market_id = market_id
        self._user_stream_task = asyncio.create_task(self._consume_user_stream(market_id))

    def get_position(self, market_id: str) -> PositionShape:
        """Return the current live position shape from account cache or audits."""
        account_shape = self.positions_by_market.get(market_id)
        if account_shape is not None:
            return account_shape
        audit_shape = shape_from_fills(self._audit_fills(market_id))
        return audit_shape

    def get_fills(self, market_id: str) -> list[Fill]:
        """Return live fills used by strategy state and risk reconstruction."""
        account_fills = self.fills_by_market.get(market_id)
        if account_fills:
            return account_fills
        audit_fills = self._audit_fills(market_id)
        return audit_fills

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
        metadata: Optional[dict[str, Any]] = None,
    ) -> LiveOrderResult:
        """Validate and submit a price-protected live BUY or SELL order.

        BUY uses the selected ask; SELL uses the selected bid. Every order is
        independently rechecked here even if the strategy already approved it.

        Args:
            timestamp: Strategy cycle timestamp when order was requested.
            window_start: Start timestamp of the 5-minute market.
            window_end: End timestamp of the 5-minute market.
            market_id: Polymarket market identifier.
            token_id: UP or DOWN token identifier.
            side: Outcome side being traded, UP or DOWN.
            action: BUY or SELL.
            price: Ask for BUY, bid for SELL.
            notional_usdc: USDC spent on BUY or received on SELL.
            shares: Number of conditional shares requested.
            decision_id: Unique strategy decision id.
            quote_ts: Timestamp of the PM quote used.
            reason: Strategy reason that selected the order.
            shape_before: Position shape before this live order.
            fills_before: Stored live fills before this order.
            metadata: Candidate/debug data stored in the live audit.
        """
        order_metadata = dict(metadata or {})
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
                "candidate": order_metadata,
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
            reason=reason,
            shape_before=shape_before,
            fills_before=fills_before,
            metadata=order_metadata,
        )
        if reject_reason:
            return self._store_rejection(payload, reject_reason)

        if action == "SELL":
            try:
                conditional = await self.client.get_balance_allowance(
                    asset_type="CONDITIONAL",
                    token_id=token_id,
                )
                decimals = max(0, int(self._get("live.conditional_decimals", 6)))
                available_shares = float(conditional.balance) / (10 ** decimals)
                payload["response_json"]["conditional_balance_shares"] = available_shares
                payload["response_json"]["conditional_allowances"] = dict(
                    getattr(conditional, "allowances", {}) or {}
                )
                if available_shares + 1e-9 < shares:
                    self._last_rejection_at = time.time()
                    return self._store_rejection(payload, "live_conditional_balance_insufficient")
            except Exception as error:
                self._last_rejection_at = time.time()
                return self._store_rejection(
                    payload,
                    f"live_conditional_balance_check_failed:{self._safe_error(error)}",
                )

        try:
            # Start cooldown before the network request so exceptions cannot
            # trigger another real-order request on the next decision cycle.
            self._last_order_at = time.time()
            response = await self.client.place_limit_order(
                token_id=token_id,
                price=float(price),
                size=float(shares),
                side=action,
                post_only=bool(self._get("live.post_only", False)),
            )
            response_payload = self._as_payload(response)
            response_payload["strategy_reason"] = reason
            response_payload["shape_before"] = asdict(shape_before)
            response_payload["candidate"] = order_metadata
            response_payload["requested_price"] = float(price)
            response_payload["requested_shares"] = float(shares)
            response_payload["requested_notional_usdc"] = float(notional_usdc)
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
                    cancel_payload = self._as_payload(cancel_response)
                    response_payload["cancel_response"] = cancel_payload
                    canceled_ids = {str(value) for value in cancel_payload.get("canceled", [])}
                    if str(external_id) not in canceled_ids:
                        status = "LIVE"
                        order_reason = "clob_order_cancel_not_confirmed"
                    else:
                        try:
                            matched_shares = await self._matched_shares_after_cancel(str(external_id))
                        except Exception as reconcile_error:
                            response_payload["partial_fill_reconciliation_error"] = self._safe_error(reconcile_error)
                            matched_shares = 0.0
                            order_reason = "canceled_partial_fill_reconciliation_pending"
                        if matched_shares > 0:
                            status = "MATCHED"
                            order_reason = "partially_matched_then_canceled"
                            shares = matched_shares
                            notional_usdc = matched_shares * float(price)
                        else:
                            status = "CANCELED"
                            if order_reason == "clob_order_accepted":
                                order_reason = "unmatched_marketable_limit_canceled"
                except Exception as cancel_error:
                    response_payload["cancel_error"] = self._safe_error(cancel_error)
                    order_reason = "clob_order_live_cancel_failed"
            elif accepted and status == "MATCHED":
                shares, notional_usdc = self._matched_amounts(
                    action,
                    response_payload,
                    requested_shares=shares,
                    requested_notional_usdc=notional_usdc,
                )
            if accepted and status == "MATCHED" and action == "BUY" and self.redeem_orders_armed:
                audit_fills = self._audit_fills(market_id)
                shape_for_redeem = shape_from_fills(audit_fills) if audit_fills else shape_before
                shape_after = apply_trade(shape_for_redeem, action, side, shares, notional_usdc)
                if shape_after.locked and min(shape_after.up_shares, shape_after.down_shares) > 1e-9:
                    try:
                        response_payload["redeem_response"] = await self.redeem(
                            validated=True,
                            market_id=market_id,
                            token_id=token_id,
                            side=side,
                            shape_after=shape_after,
                        )
                    except Exception as redeem_error:
                        response_payload["redeem_error"] = self._safe_error(redeem_error)
            payload.update(
                status=status,
                reason=order_reason,
                external_order_id=external_id,
                shares=shares,
                notional_usdc=notional_usdc,
                response_json=response_payload,
            )
            audit_id = self.db.insert_live_order_audit(payload)
            payload["id"] = audit_id
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
            error_reason = self._safe_error(error)
            self._last_rejection_at = time.time()
            balance_error = error_reason.lower()
            if bool(self._get("live.halt_on_balance_error", True)) and any(
                token in balance_error
                for token in ("not enough balance / allowance", "allowance is not enough", "not_enough_balance")
            ):
                self._orders_halted_reason = "live_balance_or_allowance_error"
                self.snapshot.orders_halted = True
                self.snapshot.halt_reason = self._orders_halted_reason
                self.logger.set_live_account_snapshot(asdict(self.snapshot))
            return self._store_rejection(payload, error_reason)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a live CLOB order after checking live execution is armed."""
        self._guard_orders()
        response = await self.client.cancel_order(order_id=order_id)
        return self._as_payload(response)

    async def redeem(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Redeem/merge locked live inventory only after a validated live BUY.

        The method probes the installed SDK for a supported redeem-like method
        and passes only accepted keyword arguments.
        """
        self._guard_redeem()
        if not bool(kwargs.pop("validated", False)):
            raise RuntimeError("Live redeem requires a validated live BUY.")
        shape_after = kwargs.pop("shape_after", None)
        if not isinstance(shape_after, PositionShape) or not shape_after.locked:
            return {"ok": False, "status": "SKIPPED", "reason": "live_redeem_not_needed"}
        redeemable_shares = min(shape_after.up_shares, shape_after.down_shares)
        if redeemable_shares <= 1e-9:
            return {"ok": False, "status": "SKIPPED", "reason": "live_redeem_not_needed"}
        kwargs.setdefault("shares", redeemable_shares)
        kwargs.setdefault("size", redeemable_shares)
        kwargs.setdefault("amount", redeemable_shares)
        kwargs.setdefault("quantity", redeemable_shares)
        kwargs.setdefault("market", kwargs.get("market_id"))
        kwargs.setdefault("asset_id", kwargs.get("token_id"))
        kwargs.setdefault("condition_id", kwargs.get("market_id"))
        redeem = None
        for name in ("redeem", "redeem_positions", "merge_positions", "merge"):
            redeem = getattr(self.client, name, None)
            if redeem is not None:
                break
        if redeem is None:
            raise RuntimeError("Live client does not support redeem.")
        try:
            signature = inspect.signature(redeem)
            accepts_var_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if accepts_var_kwargs:
                call_kwargs = {key: value for key, value in kwargs.items() if value is not None}
            else:
                allowed = {
                    name
                    for name, parameter in signature.parameters.items()
                    if name != "self"
                    and parameter.kind in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    )
                }
                call_kwargs = {
                    key: value for key, value in kwargs.items() if key in allowed and value is not None
                }
        except (TypeError, ValueError):
            call_kwargs = {key: value for key, value in kwargs.items() if value is not None}
        response = await redeem(*args, **call_kwargs)
        return self._as_payload(response)

    async def _create_client(self) -> Any:
        """Create the official SDK client from environment variables only."""
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
        """Persist authenticated order/trade events from the user stream."""
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
        reason: str,
        shape_before: PositionShape,
        fills_before: list[Fill],
        metadata: dict[str, Any],
    ) -> Optional[str]:
        """Return the first live safety rejection reason, or None if allowed."""
        if not self.orders_armed:
            return "live_orders_not_armed"
        if self.client is None:
            return "live_client_not_connected"
        if self._orders_halted_reason:
            return self._orders_halted_reason
        max_state_age = max(1.0, float(self._get("live.max_account_state_age_seconds", 10)))
        if (
            not self.snapshot.connected
            or self.snapshot.refreshed_at is None
            or time.time() - self.snapshot.refreshed_at > max_state_age
        ):
            return "live_account_state_stale"
        if self.snapshot.market_id == market_id and self.snapshot.open_orders_count > 0:
            return "live_open_order_pending"
        if action not in {"BUY", "SELL"} or side not in {"UP", "DOWN"}:
            return "only_up_down_buy_sell_orders_supported"
        if action == "SELL":
            if not self.sell_orders_armed:
                return "live_sell_orders_not_armed"
            if not bool(self._get("intra_window_sells.enabled", False)):
                return "intra_window_sells_disabled"
            allowed_modes = {str(value) for value in self._get("intra_window_sells.modes", ["paper"])}
            if self.mode not in allowed_modes:
                return "live_sell_mode_not_enabled"

        minimum_balance = max(0.0, float(self._get("live.minimum_collateral_balance_usdc", 10.0)))
        if (
            action == "BUY"
            and self.snapshot.collateral_balance_usdc is not None
            and self.snapshot.collateral_balance_usdc < minimum_balance
        ):
            return "live_minimum_balance_reached"
        max_drawdown = max(0.0, float(self._get("live.max_session_drawdown_fraction", 0.15)))
        start_balance = self.snapshot.session_start_balance_usdc
        if (
            action == "BUY"
            and start_balance
            and self.snapshot.collateral_balance_usdc is not None
            and self.snapshot.collateral_balance_usdc < start_balance * (1.0 - max_drawdown)
        ):
            return "live_session_drawdown_limit"
        if not decision_id:
            return "decision_id_missing"
        if price is None:
            return "price_missing"
        if price <= 0 or price >= 1:
            return "price_out_of_bounds"
        if notional_usdc <= 0:
            return "notional_non_positive"
        if abs(notional_usdc - shares * price) > 1e-6:
            return "notional_shares_price_mismatch"
        minimum_shares = max(
            float(self._get("sizing.min_order_shares", 1.0)),
            float(self._get("live.min_order_shares", 5.0)),
        )
        if action == "SELL":
            minimum_shares = max(
                minimum_shares,
                float(self._get("intra_window_sells.min_order_shares", minimum_shares)),
            )
        if shares < minimum_shares:
            return "shares_below_live_minimum"
        max_quote_age = float(self._get("polymarket.max_quote_age_seconds", 3))
        if quote_ts is None or timestamp - quote_ts > max_quote_age:
            return "quote_stale"
        if timestamp >= window_end:
            return "window_expired"
        if shape_before.locked:
            role = str(metadata.get("role") or "")
            if action == "SELL":
                allow_path = (
                    "intra_window_sells.directional_allow_sell_locked"
                    if role == "DIRECTIONAL_WEAK_SIDE_EXIT"
                    else "intra_window_sells.allow_sell_locked"
                )
                if not bool(self._get(allow_path, False)):
                    return "sell_locked_position_disabled"
            else:
                if bool(self._get("risk.stop_when_locked", True)):
                    return "window_locked"
                if not bool(self._get("risk.allow_post_lock_adds", False)):
                    return "post_lock_adds_disabled"
        cooldown = max(0.0, float(self._get("live.order_cooldown_seconds", 3)))
        if time.time() - self._last_order_at < cooldown:
            return "live_order_cooldown"
        rejection_backoff = max(0.0, float(self._get("live.rejection_backoff_seconds", 30)))
        if time.time() - self._last_rejection_at < rejection_backoff:
            return "live_rejection_backoff"

        audit_fills = self._audit_fills(market_id)
        risk_shape = shape_from_fills(audit_fills) if audit_fills else shape_before
        if action == "SELL":
            held = risk_shape.up_shares if side == "UP" else risk_shape.down_shares
            if shares > held + 1e-9:
                return "sell_exceeds_live_held_shares"
        shape_after = apply_trade(risk_shape, action, side, shares, notional_usdc)
        balance = (
            float(self.snapshot.collateral_balance_usdc)
            if self.snapshot.collateral_balance_usdc is not None
            else float(self._get("account.balance_usdc", 0.0))
        )
        high_conviction = reason == "high_conviction_same_side_add"
        max_notional, max_loss = window_risk_caps(
            self.config, balance, high_conviction=high_conviction
        )
        if shape_after.total_cost > max_notional:
            return "max_window_notional"
        if shape_after.worst_case_pnl < -max_loss:
            return "max_window_loss"
        starting_balance = float(
            self.snapshot.session_start_balance_usdc
            or self._get("account.balance_usdc", balance)
        )
        if not account_equity_floor_passes(
            self.config,
            balance,
            starting_balance,
            shape_after.worst_case_pnl,
        ):
            return "account_equity_floor"
        live_max_notional = max(0.0, float(self._get("live.max_window_notional_usdc", 7.0)))
        if live_max_notional and shape_after.total_cost > live_max_notional:
            return "live_max_window_notional"
        effective_fills = audit_fills if audit_fills else fills_before
        buy_fills = [fill for fill in effective_fills if str(getattr(fill, "action", "BUY")).upper() == "BUY"]
        sell_fills = [fill for fill in effective_fills if str(getattr(fill, "action", "BUY")).upper() == "SELL"]
        if action == "SELL":
            role = str(metadata.get("role") or "")
            if role == "DIRECTIONAL_WEAK_SIDE_EXIT":
                if not bool(self._get("intra_window_sells.directional_weak_side_exit_enabled", False)):
                    return "directional_weak_side_exit_disabled"
                held = risk_shape.up_shares if side == "UP" else risk_shape.down_shares
                opposite_held = risk_shape.down_shares if side == "UP" else risk_shape.up_shares
                if held > opposite_held + 1e-9:
                    return "directional_exit_must_sell_smaller_side"
                model_exit_edge = metadata.get("model_exit_edge")
                min_model_edge = float(self._get("intra_window_sells.directional_min_model_exit_edge", 0.01))
                if model_exit_edge is None or float(model_exit_edge) < min_model_edge:
                    return "directional_exit_model_edge_missing_or_too_low"
                deterioration = risk_shape.worst_case_pnl - shape_after.worst_case_pnl
                max_deterioration = float(
                    self._get("intra_window_sells.directional_max_worst_case_deterioration_usdc", 0.50)
                )
                if deterioration > max_deterioration + 1e-9:
                    return "directional_exit_worst_case_deterioration"
                max_post_loss = float(
                    self._get(
                        "intra_window_sells.directional_max_post_sell_worst_case_loss_usdc",
                        max_loss,
                    )
                )
                if shape_after.worst_case_pnl < -max_post_loss:
                    return "directional_exit_post_sell_loss"
            elif role == "INVENTORY_TRIM":
                if not bool(self._get("intra_window_sells.dominant_inventory_trim_enabled", True)):
                    return "dominant_inventory_trim_disabled"
                min_improvement = float(self._get("intra_window_sells.min_worst_case_improvement_usdc", 0.02))
                if shape_after.worst_case_pnl - risk_shape.worst_case_pnl < min_improvement:
                    return "sell_does_not_improve_worst_case"
            elif role in {"PROFIT_TRIM", "STOP_TRIM"}:
                if role == "PROFIT_TRIM" and not bool(self._get("intra_window_sells.profit_trim_enabled", False)):
                    return "profit_trim_disabled"
                if role == "STOP_TRIM" and not bool(self._get("intra_window_sells.stop_trim_enabled", False)):
                    return "stop_trim_disabled"
                deterioration = risk_shape.worst_case_pnl - shape_after.worst_case_pnl
                max_deterioration = float(
                    self._get("intra_window_sells.profit_trim_max_worst_case_deterioration_usdc", 3.0)
                )
                if deterioration > max_deterioration + 1e-9:
                    return "profit_stop_trim_worst_case_deterioration"
                max_post_loss = float(
                    self._get("intra_window_sells.profit_trim_max_post_sell_worst_case_loss_usdc", max_loss)
                )
                if shape_after.worst_case_pnl < -max_post_loss:
                    return "profit_stop_trim_post_sell_loss"
            else:
                return "live_sell_candidate_role_missing"

            max_sells = min(
                int(self._get("intra_window_sells.max_sells_per_window", 3)),
                int(self._get("live.max_sells_per_window", 3)),
            )
            if len(sell_fills) >= max_sells:
                return "max_sells_per_window"
            if len(effective_fills) >= int(self._get("live.max_total_actions_per_window", 8)):
                return "max_total_actions_per_window"
            return None

        max_orders = min(
            int(self._get("risk.max_orders_per_window", 3)),
            int(self._get("live.max_orders_per_window", 3)),
        )
        if len(buy_fills) >= max_orders:
            return "max_orders_per_window"
        if high_conviction:
            max_adds = int(self._get("high_conviction_same_side.max_adds_per_window", 2))
            filled_same_side = sum(1 for fill in buy_fills if fill.side == side)
            if max(0, filled_same_side - 1) >= max_adds:
                return "high_conviction_max_adds_reached"
            return None

        allow_post_lock_add = shape_before.locked and bool(self._get("risk.allow_post_lock_adds", False))
        if (
            int(self._get("risk.max_same_side_adds", 0)) == 0
            and not allow_post_lock_add
            and reason != "sold_side_recovery_after_weak_sell"
            and any(fill.side == side for fill in buy_fills)
        ):
            return "same_side_add_blocked"
        return None

    def _guard_orders(self) -> None:
        """Raise unless normal live order submission is armed."""
        if not self.orders_armed or self.client is None:
            raise RuntimeError("Live execution is not armed.")

    def _guard_redeem(self) -> None:
        """Raise unless the separate live redeem switch is armed."""
        if not self.redeem_orders_armed or self.client is None:
            raise RuntimeError("Live redeem is not armed.")

    def _audit_fills(self, market_id: str) -> list[Fill]:
        """Reconstruct bot-owned live fills from immutable matched-order audits."""
        list_matched = getattr(self.db, "list_matched_live_orders", None)
        if list_matched is None:
            return []
        fills: list[Fill] = []
        for row in list_matched(market_id):
            response = _loads_json(row.get("response_json"))
            metadata = response.get("candidate", {}) if isinstance(response, dict) else {}
            fill = Fill(
                ts=float(row["created_at"]),
                market_id=str(row["market_id"]),
                window_start_ts=int(row["window_start"]),
                side=str(row["side"]),
                price=float(row["price"]),
                notional_usdc=float(row["notional_usdc"]),
                shares=float(row["shares"]),
                reason=str(response.get("strategy_reason") or row.get("reason") or "live_matched_order_audit"),
                action=str(row.get("action", "BUY")),
            )
            setattr(fill, "metadata", {"candidate": metadata, "strategy_reason": fill.reason})
            fills.append(fill)
        return fills

    def _shape_from_positions(self, market: MarketInfo, positions: list[Any]) -> PositionShape:
        """Convert SDK position rows into Aries' UP/DOWN shape format."""
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
        """Convert SDK trade rows into BUY/SELL fills with cash accounting."""
        fills: list[Fill] = []
        for trade in trades:
            action = str(getattr(trade, "side", "")).upper()
            if action not in {"BUY", "SELL"}:
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
                    action=action,
                )
            )
        return sorted(fills, key=lambda fill: fill.ts)

    async def _matched_shares_after_cancel(self, order_id: str) -> float:
        """Return any partial quantity matched before a live order was canceled."""
        get_order = getattr(self.client, "get_order", None)
        if get_order is None:
            return 0.0
        order = self._as_payload(await get_order(order_id=order_id))
        return max(0.0, float(order.get("size_matched", 0.0) or 0.0))

    def _matched_amounts(
        self,
        action: str,
        response: dict[str, Any],
        *,
        requested_shares: float,
        requested_notional_usdc: float,
    ) -> tuple[float, float]:
        """Read actual full-match quantities from the SDK order response."""
        making = float(response.get("making_amount", 0.0) or 0.0)
        taking = float(response.get("taking_amount", 0.0) or 0.0)
        shares, notional = (taking, making) if action == "BUY" else (making, taking)
        if shares <= 0 or notional <= 0:
            return requested_shares, requested_notional_usdc
        return shares, notional

    def _same_inventory(self, left: PositionShape, right: PositionShape) -> bool:
        """Compare only held shares; total cost can differ after sells/fees."""
        return (
            abs(left.up_shares - right.up_shares) <= 1e-6
            and abs(left.down_shares - right.down_shares) <= 1e-6
        )

    def _latest_fill_ts(self, fills: list[Fill]) -> float:
        """Return the newest fill timestamp, or 0 for empty fill lists."""
        return max((float(fill.ts) for fill in fills), default=0.0)

    def _store_rejection(self, payload: dict[str, Any], reason: str) -> LiveOrderResult:
        """Persist a rejected live order audit and emit the rejection log."""
        payload["status"] = "REJECTED"
        payload["reason"] = reason
        audit_id = self.db.insert_live_order_audit(payload)
        payload["id"] = audit_id
        self.logger.live_order_rejected(payload)
        return LiveOrderResult(audit_id=audit_id, status="REJECTED", reason=reason)

    def _get(self, path: str, default: Any = None) -> Any:
        """Read dotted config paths from Config or plain dict objects."""
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
        """Read a required secret by configured environment-variable name."""
        name = str(self._get(config_path, ""))
        value = os.environ.get(name, "")
        if not value:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return value

    def _env_optional(self, config_path: str) -> Optional[str]:
        """Read an optional environment variable named by config."""
        name = str(self._get(config_path, ""))
        return os.environ.get(name) or None if name else None

    def _as_payload(self, value: Any) -> dict[str, Any]:
        """Normalize SDK/dataclass/dict responses for JSON logging."""
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if hasattr(value, "__dataclass_fields__"):
            return asdict(value)
        return {"value": str(value)}

    def _event_id(self, payload: dict[str, Any]) -> Optional[str]:
        """Extract a stable id from a user-stream event when available."""
        inner = payload.get("payload")
        if isinstance(inner, dict) and inner.get("id"):
            return str(inner["id"])
        return None

    def _safe_error(self, error: Exception) -> str:
        """Render SDK errors while redacting configured secret values."""
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
