from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict
from typing import Any, Optional

from .adaptive import AdaptiveEngine
from .binance_flow import BinanceFlowEngine, apply_binance_flow_bias
from .binance_ws import BinanceStream
from .chainlink_feed import ChainlinkPriceFeed
from .config import Config
from .database import Database
from .exit_strategy import build_inventory_sell_candidate, build_inventory_sell_probe
from .growth_sizing import candidate_growth_score
from .indicators import compute_indicators
from .live_execution import LiveExecutor
from .logger import JsonlLogger
from .market_resolver import MarketResolver
from .models import Candidate, Decision, Fill, MarketInfo, PositionShape
from .optimizer import StrategyOptimizer
from .paper_broker import PaperBroker
from .polymarket_api import PolymarketApi, current_window_start
from .polymarket_flow import PolymarketFlowEngine, apply_polymarket_flow_bias
from .polymarket_ws import PolymarketQuoteStore, PolymarketStream
from .position_shape import apply_buy, average_open_entry, dominant_side, shape_from_fills, weak_side
from .probability import estimate_probability
from .reporter import Reporter
from .risk import (
    account_equity_floor_passes,
    binance_fresh,
    quote_age_seconds,
    quote_fresh,
    valid_spread,
    window_risk_caps,
)
from .settlement_check import format_settlement_report, verify_chainlink_settlement
from .sizing import size_for_price
from .support_resistance import SupportResistanceEngine, SupportResistanceState, apply_support_resistance_bias
from .validator import Validator


class AriesBot:
    def __init__(self, config: Config):
        """Wire shared services and calculate the first tradable window.

        Most behavior is configured in `config.yaml`; this constructor should
        stay focused on dependency setup, not strategy rules.
        """
        self.config = config
        self.mode = str(config.get("mode", "dry_run"))
        self.db = Database(
            config.get_path("database.path", "aries.db"),
            float(config.get_path("storage.max_database_mb", 50.0)),
        )
        self.logger = JsonlLogger(
            config.get_path("logging.decisions_log", "logs/decisions.jsonl"),
            config.get_path("logging.fills_log", "logs/fills.jsonl"),
            bool(config.get_path("logging.console", True)),
            float(config.get_path("account.balance_usdc", 0.0)),
            float(config.get_path("storage.max_decisions_log_mb", 50.0)),
            float(config.get_path("storage.max_fills_log_mb", 50.0)),
            int(config.get_path("storage.max_decisions_log_rows", 10000)),
            config.get_path("dashboard.panels", {}),
            config.get_path("live_html", {}),
        )
        self.paper_broker = PaperBroker(config, self.db, self.logger)
        self.live_executor = LiveExecutor(config, self.db, self.logger)
        self.reporter = Reporter(config, self.db)
        self.validator = Validator(config, self.db, self.logger)
        self.optimizer = StrategyOptimizer(config, self.db)
        self.binance = BinanceStream(config)
        self.binance_flow = BinanceFlowEngine(config)
        self.support_resistance = SupportResistanceEngine(config)
        self.pm_api = PolymarketApi(config)
        self.resolver = MarketResolver(config, self.pm_api)
        self.chainlink_feed = ChainlinkPriceFeed(config, self.pm_api)
        self.quote_store = PolymarketQuoteStore(
            int(config.get_path("polymarket_flow.max_trade_events", 2000))
        )
        self.polymarket_flow = PolymarketFlowEngine(config, self.quote_store)
        self.pm_stream: Optional[PolymarketStream] = None
        self.pm_stream_market_id: Optional[str] = None
        self.fills_by_window: dict[tuple[str, int], list[Fill]] = {}
        self.last_persisted_decision_ts = 0.0
        self.last_persisted_state: Optional[str] = None
        self.last_storage_maintenance_ts = 0.0
        self.last_optimization_refresh_ts = 0.0
        self.sell_signal_streaks: dict[tuple[str, str, str], int] = {}
        self.settlement_check_report: dict[str, Any] = {}
        self._official_poll_ts: dict[int, float] = {}
        self.adaptive = AdaptiveEngine(config.get_path, self.db)
        self.adaptive.load()
        warm_start_samples = 0
        if bool(config.get_path("adaptive.warm_start_on_boot", True)):
            try:
                warm_start_samples = self.adaptive.warm_start_from_db()
            except Exception as exc:
                self.logger.info("adaptive_warm_start_failed", error=str(exc))
        if warm_start_samples:
            self.logger.info(
                "adaptive_warm_start",
                samples=warm_start_samples,
                a=round(self.adaptive.state.a, 4),
                b=round(self.adaptive.state.b, 4),
            )
        self._window_first_p_up: dict[tuple[str, int], float] = {}
        interval = int(config.get_path("bot.interval_seconds", 300))
        current_start = current_window_start(int(time.time()), interval)
        self.first_trade_window_start = (
            current_start + interval
            if bool(config.get_path("timing.require_clean_window", True))
            else current_start
        )

    def run(self) -> None:
        """Synchronous entry point used by `main.py`."""
        asyncio.run(self._run())

    async def _run(self) -> None:
        """Start streams, run the decision loop, and cleanly stop resources."""
        self.logger.info("starting_aries_phase2", mode=self.mode)
        await self._verify_settlement_source()
        self._refresh_optimization(time.time(), force=True)
        await self.live_executor.start()
        try:
            await self.binance.bootstrap_history()
            await self.binance.start()
            while True:
                await self._cycle()
                await asyncio.sleep(float(self.config.get_path("bot.decision_interval_seconds", 1)))
        finally:
            await self.binance.stop()
            if self.pm_stream:
                await self.pm_stream.stop()
            await self.live_executor.stop()
            await self._shutdown()

    async def _verify_settlement_source(self) -> None:
        """Warn loudly at startup if settlement would not use official Chainlink data.

        Binance-close settlement can disagree with Polymarket's actual payout
        side, so the official endpoint must be reachable and correctly
        configured before results can be trusted.
        """
        try:
            report = await verify_chainlink_settlement(self.config, self.pm_api)
        except Exception as exc:
            report = {
                "ok": False,
                "warnings": [f"settlement_check_error: {exc}"],
                "probe": {},
            }
        self.settlement_check_report = report
        self.logger.event("SETTLEMENT_SOURCE_CHECK", report)
        print(format_settlement_report(report), flush=True)

    async def _cycle(self) -> None:
        """Run one live bot cycle: refresh data, decide, execute, log, maintain."""
        now = time.time()
        binance_state = self.binance.snapshot()
        sr_state = self.support_resistance.update(binance_state, now)
        market = await self.resolver.resolve(binance_state)

        if market:
            self.db.upsert_market({**asdict(market), "updated_at": now})
            await self._ensure_pm_stream(market)
            await self.live_executor.refresh_account(market)

        # Refresh the official Chainlink reference and anchor the fast Binance
        # signal levels to it. Binance drives the signals; Chainlink (the real
        # resolution source) drives the price-versus-target reference.
        await self.chainlink_feed.update(market, binance_state.price, now)
        signal_state = self.chainlink_feed.anchor_state(binance_state)

        await self._settle_finished_windows(now, binance_state)

        decision, selected, selected_quote = self._decide(now, market, signal_state, sr_state)

        if market and selected and decision.decision in {"BUY_UP", "BUY_DOWN", "SELL_UP", "SELL_DOWN"}:
            if self.mode == "paper":
                self._execute_paper_order(decision, market, selected, selected_quote)
            elif self.mode == "live":
                await self._execute_live_order(decision, market, selected, selected_quote)
            elif self.mode == "live_monitor":
                decision.order_status = "MONITOR_ONLY"
                decision.order_reason = "live_monitor_never_places_orders"
                decision.position_after = asdict(self._shape(market))
            else:
                self._execute_dry_run_fill(decision, market, selected)
            decision.strategy_state = self._post_execution_state(decision)

        self.logger.terminal_status(decision)
        if self._should_persist_decision(decision, now):
            self.logger.candidate_evaluation(
                {
                    "decision_id": decision.decision_id,
                    "market_id": decision.market_id,
                    "window_start": decision.window_start,
                    "candidate_up": decision.candidate_up,
                    "candidate_down": decision.candidate_down,
                    "sell_candidate": decision.sell_candidate,
                    "selected_side": decision.selected_side,
                    "selected_reason": decision.selected_reason,
                }
            )
            self.logger.position_shape(
                {
                    "decision_id": decision.decision_id,
                    "market_id": decision.market_id,
                    "window_start": decision.window_start,
                    "shape_before": decision.position_before,
                    "shape_after": decision.position_after,
                    "mode": self.mode,
                    "order_id": decision.order_id,
                    "fill_id": decision.fill_id,
                }
            )
            payload = asdict(decision)
            self.db.insert_decision(payload)
            self.logger.decision(payload)
            self.last_persisted_decision_ts = now
            self.last_persisted_state = decision.strategy_state

        self._maintain_storage(now)
        self._refresh_optimization(now)

    def _refresh_optimization(self, now: float, force: bool = False) -> None:
        """Refresh the advisory optimizer snapshot shown on the terminal."""
        if not bool(self.config.get_path("optimization.enabled", True)):
            return
        interval = max(10.0, float(self.config.get_path("optimization.refresh_seconds", 60)))
        if not force and now - self.last_optimization_refresh_ts < interval:
            return
        self.logger.set_optimization_snapshot(self.optimizer.terminal_snapshot())
        self.last_optimization_refresh_ts = now

    def _should_persist_decision(self, decision: Decision, now: float) -> bool:
        """Always retain meaningful changes; sample repetitive monitor snapshots."""
        sample_seconds = max(1.0, float(self.config.get_path("storage.decision_sample_seconds", 15)))
        important = (
            decision.decision in {"BUY_UP", "BUY_DOWN", "SELL_UP", "SELL_DOWN"}
            or decision.order_id is not None
            or decision.fill_id is not None
            or decision.order_status is not None
            or decision.strategy_state != self.last_persisted_state
        )
        return important or now - self.last_persisted_decision_ts >= sample_seconds

    def _maintain_storage(self, now: float, force: bool = False) -> None:
        """Compact high-frequency logs/database rows according to storage caps."""
        interval = max(1.0, float(self.config.get_path("storage.maintenance_interval_seconds", 60)))
        if not force and now - self.last_storage_maintenance_ts < interval:
            return
        self.logger.maintain_storage()
        self.db.maintain_storage(int(self.config.get_path("storage.decision_rows_to_keep", 5000)))
        self.last_storage_maintenance_ts = now

    async def _ensure_pm_stream(self, market: MarketInfo) -> None:
        """Subscribe to the current market's UP/DOWN CLOB quotes."""
        if self.pm_stream_market_id == market.market_id:
            return
        if self.pm_stream:
            await self.pm_stream.stop()
        self.pm_stream = PolymarketStream(
            self.config,
            [market.up_token_id, market.down_token_id],
            self.quote_store,
        )
        self.quote_store.retain_tokens([market.up_token_id, market.down_token_id])
        self.pm_stream_market_id = market.market_id
        await self.pm_stream.start()
        await self.live_executor.subscribe_market(market.market_id)
        self.logger.info("subscribed_polymarket_market", market_id=market.market_id, slug=market.slug)

    def _record_fill(self, fill: Fill) -> int:
        """Store an in-memory dry-run fill and mirror it into SQLite/logs."""
        key = (fill.market_id, fill.window_start_ts)
        self.fills_by_window.setdefault(key, []).append(fill)
        payload = asdict(fill)
        fill_id = self.db.insert_fill(payload)
        payload["id"] = fill_id
        self.logger.dry_fill(payload)
        return fill_id

    def _fills_for_window(self, market: MarketInfo):
        """Return fills from the active mode's source of truth."""
        if self.mode == "paper":
            return self.paper_broker.get_fills(market.market_id, market.window_start_ts)
        if self.mode in {"live_monitor", "live"}:
            return self.live_executor.get_fills(market.market_id)
        return self.fills_by_window.get((market.market_id, market.window_start_ts), [])

    def _shape(self, market: MarketInfo) -> PositionShape:
        """Return the current position shape reconstructed for this mode."""
        if self.mode == "paper":
            return self.paper_broker.get_position(market.market_id, market.window_start_ts)
        if self.mode in {"live_monitor", "live"}:
            return self.live_executor.get_position(market.market_id)
        return shape_from_fills(self.fills_by_window.get((market.market_id, market.window_start_ts), []))

    def _sizing_bankroll(self) -> tuple[float, float]:
        """Use real collateral in live mode; never mix paper PnL into live sizing."""
        configured_start = float(self.config.get_path("account.balance_usdc", 0.0))
        if self.mode == "live":
            snapshot = self.live_executor.snapshot
            bankroll = snapshot.collateral_balance_usdc
            session_start = snapshot.session_start_balance_usdc
            if bankroll is not None:
                return float(bankroll), float(session_start or configured_start or bankroll)
        return configured_start + self.db.total_realized_pnl(), configured_start

    def _decide(self, now: float, market: Optional[MarketInfo], binance_state, sr_state: Optional[SupportResistanceState] = None) -> tuple[Decision, Optional[Candidate], Optional[object]]:
        """Run one complete strategy decision without placing an order.

        This is the main function to read when tracing why Aries chose BUY,
        SELL, WATCH, or SKIP. Execution happens later in `_cycle`.
        """
        decision_id = uuid.uuid4().hex
        empty_position = PositionShape()
        pm_flow_state = self.polymarket_flow.update(
            market.up_token_id if market else None,
            market.down_token_id if market else None,
            now,
        )
        binance_flow_state = self.binance_flow.update(binance_state, now)
        chainlink_ref = self.chainlink_feed.snapshot(
            binance_state.price, now, market.window_end_ts if market else None
        )
        if market is None:
            return (
                self._decision_payload(
                    decision_id=decision_id,
                    now=now,
                    market=None,
                    binance_state=binance_state,
                    chainlink_ref=chainlink_ref,
                    indicators=None,
                    prob=None,
                    up_q=None,
                    down_q=None,
                    pos_before=empty_position,
                    cand_up=None,
                    cand_down=None,
                    support_resistance=sr_state.to_dict() if sr_state else {},
                    polymarket_flow=pm_flow_state.to_dict(),
                    binance_flow=binance_flow_state.to_dict(),
                    selected=None,
                    decision="SKIP",
                    reason="market_missing",
                    pos_after=empty_position,
                ),
                None,
                None,
            )

        # 1. Get the active BTC 5m market and target price.
        up_q = self.quote_store.quote(market.up_token_id)
        down_q = self.quote_store.quote(market.down_token_id)

        # 2. Read latest Binance state and MA pressure.
        indicators = compute_indicators(
            binance_state,
            market.target_price,
            int(self.config.get_path("binance.ma_fast", 7)),
            int(self.config.get_path("binance.ma_mid", 25)),
            int(self.config.get_path("binance.ma_slow", 69)),
            int(self.config.get_path("binance.ma_anchor", 144)),
            int(self.config.get_path("binance.volume_ma_fast", 5)),
            int(self.config.get_path("binance.volume_ma_slow", 10)),
        )
        # logger.debug("BINANCE_STATE %s", ...)
        # logger.debug("MA_PRESSURE %s", ...)

        # 3. Read latest Polymarket UP/DOWN quotes.
        # logger.debug("PM_QUOTES %s", ...)

        # 4. Convert Binance pressure into p_up / p_down.
        prob = estimate_probability(
            self.config,
            binance_state,
            indicators,
            market.target_price,
            market.window_end_ts - int(now),
        )
        # OFI/CVD, PM depth/trades, and S/R only apply bounded nudges to the base model.
        prob = apply_binance_flow_bias(self.config, prob, binance_flow_state)
        prob = apply_polymarket_flow_bias(self.config, prob, pm_flow_state)
        if sr_state is not None:
            prob = apply_support_resistance_bias(self.config, prob, sr_state)
        # Adaptive layer: blend the feature model with a Brownian-bridge prior
        # and the Polymarket mid-price, then apply online Platt calibration
        # learned from settled windows.
        prob, adaptive_comps = self.adaptive.augment_probability(
            prob,
            indicators=indicators,
            up_quote=up_q,
            down_quote=down_q,
            seconds_remaining=market.window_end_ts - int(now),
        )
        prob.bridge_p_up = adaptive_comps.p_bridge
        prob.market_mid_p_up = adaptive_comps.p_market_mid
        prob.blended_p_up = adaptive_comps.blended
        prob.calibrated = adaptive_comps.calibrated
        # logger.debug("PROBABILITY %s", ...)

        # 5. Compare probabilities against PM asks to calculate edge.
        # 6. Reconstruct the current position shape from this mode's fills/account state.
        position_before = self._shape(market)
        fills = list(self._fills_for_window(market))
        seconds_since_first_fill = self._seconds_since_first_fill(now, fills)
        average_entry_up, average_entry_down = self._average_entry_prices(fills)

        # 7. Build both UP and DOWN candidates.
        force_first_entry = (
            not fills
            and int(now) - market.window_start_ts >= self._first_entry_deadline_seconds()
            and bool(self.config.get_path("entry.force_best_side_after_preferred", True))
        )
        candidate_up = self._build_candidate(
            "UP",
            market,
            up_q.ask,
            prob.p_up,
            position_before,
            fills,
            force_first_entry,
            p_confidence=prob.confidence,
        )
        candidate_down = self._build_candidate(
            "DOWN",
            market,
            down_q.ask,
            prob.p_down,
            position_before,
            fills,
            force_first_entry,
            p_confidence=prob.confidence,
        )
        # logger.debug("CANDIDATE_UP %s", ...)
        # logger.debug("CANDIDATE_DOWN %s", ...)

        selected, decision, reason = self._select_candidate(
            now=now,
            market=market,
            binance_state=binance_state,
            up_q=up_q,
            down_q=down_q,
            prob=prob,
            indicators=indicators,
            shape=position_before,
            fills=fills,
            seconds_since_first_fill=seconds_since_first_fill,
            up=candidate_up,
            down=candidate_down,
        )
        if decision in {"BUY_UP", "BUY_DOWN"} and prob.p_up is not None:
            key = (market.market_id, market.window_start_ts)
            self._window_first_p_up.setdefault(key, float(prob.p_up))
            if len(self._window_first_p_up) > 64:
                for stale in sorted(self._window_first_p_up, key=lambda k: k[1])[:-32]:
                    self._window_first_p_up.pop(stale, None)

        # Sell candidates are evaluated after both buy candidates. Inventory
        # trims reduce worst-case risk; directional weak-side exits deliberately
        # add a tightly capped tilt only when PM bid exceeds our model value.
        max_quote_age = float(self.config.get_path("polymarket.max_quote_age_seconds", 3))
        sell_inputs_fresh = (
            binance_fresh(binance_state, float(self.config.get_path("binance.max_state_age_seconds", 3)))
            and quote_fresh(up_q, max_quote_age)
            and quote_fresh(down_q, max_quote_age)
        )
        sell_probe = {
            "valid": False,
            "reason": "sell_inputs_stale",
            "dominant": {"reason": "sell_inputs_stale"},
            "weak_side": {"reason": "sell_inputs_stale"},
        }
        sell_candidate = None
        if indicators and prob and up_q and down_q:
            sell_probe = build_inventory_sell_probe(
                self.config,
                mode=self.mode,
                shape=position_before,
                fills=fills,
                indicators=indicators,
                p_up=prob.p_up,
                p_down=prob.p_down,
                up_bid=up_q.bid,
                down_bid=down_q.bid,
                up_spread=up_q.spread,
                down_spread=down_q.spread,
                now_ts=now,
                seconds_since_open=int(now) - market.window_start_ts,
                seconds_remaining=market.window_end_ts - int(now),
            )
            if not sell_inputs_fresh:
                sell_probe["valid"] = False
                sell_probe["reason"] = "sell_inputs_stale"
                sell_probe["selected"] = {}
        if sell_inputs_fresh:
            sell_candidate = build_inventory_sell_candidate(
                self.config,
                mode=self.mode,
                shape=position_before,
                fills=fills,
                indicators=indicators,
                p_up=prob.p_up,
                p_down=prob.p_down,
                up_bid=up_q.bid,
                down_bid=down_q.bid,
                up_spread=up_q.spread,
                down_spread=down_q.spread,
                now_ts=now,
                seconds_since_open=int(now) - market.window_start_ts,
                seconds_remaining=market.window_end_ts - int(now),
            )
        sell_candidate = self._confirm_sell_candidate(market, sell_candidate)
        if sell_candidate and sell_candidate.valid:
            selected = sell_candidate
            decision = f"SELL_{sell_candidate.side}"
            reason = sell_candidate.reason
            sell_probe["selected"] = sell_probe.get("selected") or asdict(sell_candidate)
            sell_probe["valid"] = True
            sell_probe["reason"] = sell_candidate.reason
        elif sell_candidate is not None:
            selected = None
            decision = "WATCH"
            reason = sell_candidate.reason
        selected_quote = up_q if selected and selected.side == "UP" else down_q if selected else None
        position_after = selected.shape_after if selected and selected.shape_after else position_before

        return (
            self._decision_payload(
                decision_id=decision_id,
                now=now,
                market=market,
                binance_state=binance_state,
                chainlink_ref=chainlink_ref,
                indicators=indicators,
                prob=prob,
                up_q=up_q,
                down_q=down_q,
                pos_before=position_before,
                cand_up=candidate_up,
                cand_down=candidate_down,
                support_resistance=sr_state.to_dict() if sr_state else {},
                polymarket_flow=pm_flow_state.to_dict(),
                binance_flow=binance_flow_state.to_dict(),
                sell_candidate=sell_candidate,
                sell_probe=sell_probe,
                selected=selected,
                decision=decision,
                reason=reason,
                pos_after=position_after,
                seconds_since_first_fill=seconds_since_first_fill,
                average_entry_up=average_entry_up,
                average_entry_down=average_entry_down,
            ),
            selected,
            selected_quote,
        )

    def _build_candidate(self, side: str, market: MarketInfo, price: Optional[float], p_side: Optional[float], shape: PositionShape, fills, force_first_entry: bool = False, *, p_confidence: float = 1.0) -> Candidate:
        """Build one hypothetical BUY using the selected side's current ask.

        `role` determines which ladder/rules apply. `edge = p_side - ask`, while
        `shape_after` shows the position if this candidate fills.

        Args:
            side: Candidate side to buy, UP or DOWN.
            market: Current Polymarket BTC 5-minute market.
            price: Current PM ask used for this BUY candidate.
            p_side: Aries model probability for this side.
            shape: Current position before this candidate.
            fills: Stored fills for the current window.
            force_first_entry: True when deadline mode allows floor sizing.
        """
        if price is None or p_side is None:
            return Candidate(side=side, valid=False, reason="price_or_probability_missing")

        buy_fills = [fill for fill in fills if str(getattr(fill, "action", "BUY")).upper() == "BUY"]
        orders = len(buy_fills)
        dom = dominant_side(shape)
        same_side_orders = sum(1 for fill in buy_fills if fill.side == side)

        if orders == 0:
            role = "FIRST_ENTRY"
        elif dom == side:
            role = "SAME_SIDE_ADD"
        else:
            role = "REPAIR"
        same_side_count = max(0, same_side_orders - 1) if role == "SAME_SIDE_ADD" else same_side_orders

        # 8. Use ladder sizing to find candidate notional.
        bankroll, starting_balance = self._sizing_bankroll()
        max_window_notional, _ = window_risk_caps(
            self.config,
            bankroll,
            high_conviction=role == "SAME_SIDE_ADD",
        )
        sizing = size_for_price(
            self.config,
            price,
            role,
            orders,
            same_side_count,
            side=side,
            p_side=p_side,
            p_confidence=p_confidence,
            shape=shape,
            bankroll=bankroll,
            starting_bankroll=starting_balance,
            max_window_notional_usdc=max_window_notional,
            force_first_entry_floor=force_first_entry,
            kelly_multiplier=self.adaptive.kelly_multiplier(),
            min_order_shares_override=(
                float(self.config.get_path("live.min_order_shares", 5.0))
                if self.mode == "live"
                else None
            ),
        )
        edge = p_side - price
        net_edge = p_side - sizing.net_price if sizing.net_price is not None else None
        if not sizing.valid:
            return Candidate(
                side=side,
                valid=False,
                reason=sizing.reason,
                role=role,
                price=price,
                edge=edge,
                notional_usdc=sizing.notional_usdc,
                shares=sizing.shares,
                ladder_step=sizing.ladder_step,
                net_price=sizing.net_price,
                net_edge=net_edge,
                full_kelly_fraction=sizing.full_kelly_fraction,
                applied_kelly_fraction=sizing.applied_kelly_fraction,
                kelly_cap_usdc=sizing.kelly_cap_usdc,
                repair_target_usdc=sizing.repair_target_usdc,
                allowed_notional_usdc=sizing.allowed_notional_usdc,
                account_scale=sizing.account_scale,
                ladder_bankroll_ratio=sizing.ladder_bankroll_ratio,
            )
        if self.mode == "live":
            live_min_shares = float(self.config.get_path("live.min_order_shares", 5.0))
            if (sizing.shares or 0.0) < live_min_shares:
                return Candidate(
                    side=side,
                    valid=False,
                    reason="shares_below_live_minimum",
                    role=role,
                    price=price,
                    edge=edge,
                    notional_usdc=sizing.notional_usdc,
                    shares=sizing.shares,
                    ladder_step=sizing.ladder_step,
                    net_price=sizing.net_price,
                    net_edge=net_edge,
                    full_kelly_fraction=sizing.full_kelly_fraction,
                    applied_kelly_fraction=sizing.applied_kelly_fraction,
                    kelly_cap_usdc=sizing.kelly_cap_usdc,
                    repair_target_usdc=sizing.repair_target_usdc,
                    allowed_notional_usdc=sizing.allowed_notional_usdc,
                    account_scale=sizing.account_scale,
                    ladder_bankroll_ratio=sizing.ladder_bankroll_ratio,
                )

        # 9. Simulate shape_after for each candidate.
        shape_after = apply_buy(shape, side, sizing.shares or 0.0, sizing.notional_usdc or 0.0)
        improvement = shape_after.worst_case_pnl - shape.worst_case_pnl
        p_up = p_side if side == "UP" else 1.0 - p_side
        p_down = p_side if side == "DOWN" else 1.0 - p_side
        log_growth, growth_score, imbalance = candidate_growth_score(
            self.config,
            bankroll=bankroll,
            p_up=p_up,
            p_down=p_down,
            shape_before=shape,
            shape_after=shape_after,
            order_cost=sizing.notional_usdc or 0.0,
        )
        return Candidate(
            side=side,
            valid=True,
            reason="candidate_built",
            role=role,
            price=price,
            edge=edge,
            notional_usdc=sizing.notional_usdc,
            shares=sizing.shares,
            ladder_step=sizing.ladder_step,
            shape_after=shape_after,
            shape_improvement=improvement,
            would_lock=shape_after.locked,
            min_shares_check_passed=True,
            net_price=sizing.net_price,
            net_edge=net_edge,
            full_kelly_fraction=sizing.full_kelly_fraction,
            applied_kelly_fraction=sizing.applied_kelly_fraction,
            kelly_cap_usdc=sizing.kelly_cap_usdc,
            repair_target_usdc=sizing.repair_target_usdc,
            allowed_notional_usdc=sizing.allowed_notional_usdc,
            account_scale=sizing.account_scale,
            ladder_bankroll_ratio=sizing.ladder_bankroll_ratio,
            expected_log_growth_delta=log_growth,
            growth_score=growth_score,
            inventory_imbalance=imbalance,
        )

    def _select_candidate(self, now: float, market: MarketInfo, binance_state, up_q, down_q, prob, indicators, shape: PositionShape, fills, seconds_since_first_fill, up: Candidate, down: Candidate) -> tuple[Optional[Candidate], str, str]:
        """Apply shared safety gates, then route to entry or after-entry logic.

        Change this function only when changing global timing/freshness/lock
        behavior. Side preference belongs in the smaller selector functions.

        Args:
            now: Current strategy cycle timestamp.
            market: Active Polymarket BTC 5-minute market.
            binance_state: Latest Binance price/order-book snapshot.
            up_q: Latest PM UP quote.
            down_q: Latest PM DOWN quote.
            prob: Current UP/DOWN probability estimate.
            indicators: Latest MA, slope, volume, and stability values.
            shape: Current position before any new order.
            fills: Stored fills for this window.
            seconds_since_first_fill: Seconds since first BUY fill, if any.
            up: Built BUY_UP candidate.
            down: Built BUY_DOWN candidate.
        """
        seconds_since_open = int(now) - market.window_start_ts
        seconds_remaining = market.window_end_ts - int(now)
        orders = sum(1 for fill in fills if str(getattr(fill, "action", "BUY")).upper() == "BUY")

        if market.window_start_ts < self.first_trade_window_start:
            return None, "WATCH", "wait_for_clean_window"
        if market.target_price is None:
            return None, "SKIP", "target_missing"
        entry_allowed, reference_reason = self.chainlink_feed.entry_allowed(binance_state.price, now)
        if not entry_allowed:
            return None, "SKIP", reference_reason
        if not binance_fresh(binance_state, float(self.config.get_path("binance.max_state_age_seconds", 3))):
            return None, "SKIP", "binance_stale"
        max_quote_age = float(self.config.get_path("polymarket.max_quote_age_seconds", 3))
        if not quote_fresh(up_q, max_quote_age) or not quote_fresh(down_q, max_quote_age):
            return None, "SKIP", "pm_quote_missing_or_stale"
        if prob.p_up is None or prob.p_down is None:
            return None, "SKIP", prob.reason
        preferred_first_entry = self._first_entry_deadline_seconds()
        force_first_entry = (
            orders == 0
            and seconds_since_open >= preferred_first_entry
            and bool(self.config.get_path("entry.force_best_side_after_preferred", True))
        )
        if prob.inside_noise_band and not force_first_entry:
            return None, "WATCH", "inside_noise_band"
        if seconds_since_open < int(self.config.get_path("timing.first_entry_start_seconds", 8)):
            return None, "WATCH", "wait_after_open"
        if seconds_remaining <= int(self.config.get_path("timing.min_seconds_remaining", 20)):
            return None, "SKIP", "too_late_in_window"
        if shape.locked:
            if bool(self.config.get_path("risk.stop_when_locked", True)):
                return None, "SKIP", "locked_stop_trading_window"
            if not bool(self.config.get_path("risk.allow_post_lock_adds", False)):
                return None, "SKIP", "post_lock_adds_disabled"

        if orders >= int(self.config.get_path("risk.max_orders_per_window", 3)):
            return None, "SKIP", "max_orders_per_window"

        if not valid_spread(up_q.spread, float(self.config.get_path("edge.max_spread", 0.06))):
            up.valid = False
            up.reason = "spread_too_wide"
        if not valid_spread(down_q.spread, float(self.config.get_path("edge.max_spread", 0.06))):
            down.valid = False
            down.reason = "spread_too_wide"

        if orders == 0:
            return self._select_first_entry(up, down, prob, seconds_since_open, seconds_remaining)
        if self._time_based_exit_limits_enabled() and seconds_remaining <= int(
            self.config.get_path("intra_window_sells.final_seconds_remaining", 45)
        ):
            lock = self._select_lock_only(up, down)
            if lock[0] is not None:
                return lock
            return None, "HOLD_EXPOSED", "final_window_buy_lock_only"
        if shape.locked:
            return self._select_post_lock_add(up, down, shape, prob)
        repair_delay = int(self.config.get_path("timing.repair_start_after_first_entry_seconds", 24))
        if seconds_since_first_fill is not None and seconds_since_first_fill < repair_delay:
            conviction = self._select_high_conviction_same_side(
                up, down, shape, prob, indicators, binance_state, market, fills,
                seconds_since_open, seconds_remaining, seconds_since_first_fill,
            )
            if conviction[0] is not None:
                return conviction
            return None, "WATCH", "one_sided_waiting_for_repair_delay"
        preferred_repair_delay = int(self.config.get_path("timing.preferred_repair_after_first_entry_seconds", 38))
        if seconds_since_first_fill is not None and seconds_since_first_fill < preferred_repair_delay:
            lock = self._select_lock_only(up, down)
            if lock[0] is not None:
                return lock
            conviction = self._select_high_conviction_same_side(
                up, down, shape, prob, indicators, binance_state, market, fills,
                seconds_since_open, seconds_remaining, seconds_since_first_fill,
            )
            return conviction if conviction[0] is not None else lock
        return self._select_after_entry(
            up, down, shape, prob, indicators, binance_state, market, fills,
            seconds_since_open, seconds_remaining, seconds_since_first_fill,
        )

    def _select_first_entry(self, up: Candidate, down: Candidate, prob, seconds_since_open: int, seconds_remaining: int) -> tuple[Optional[Candidate], str, str]:
        """Choose the first side to buy after edge, confidence, and risk checks.

        Before the deadline Aries requires normal/early conviction. At the
        configured deadline it chooses the safest valid side with the best edge.

        Args:
            up: Built BUY_UP candidate.
            down: Built BUY_DOWN candidate.
            prob: Current UP/DOWN probability estimate.
            seconds_since_open: Window age in seconds.
            seconds_remaining: Time left in the window.
        """
        preferred = int(self.config.get_path("timing.preferred_first_entry_seconds", 18))
        deadline = self._first_entry_deadline_seconds()
        after_preferred = seconds_since_open >= preferred
        force_at_deadline = (
            seconds_since_open >= deadline
            and bool(self.config.get_path("entry.force_best_side_after_preferred", True))
        )
        min_edge = float(self.config.get_path("edge.min_edge", 0.07))
        min_conf = float(self.config.get_path("edge.min_confidence", 0.10))
        if not force_at_deadline and prob.confidence < min_conf:
            return None, "SKIP", "confidence_too_low"

        valid = [candidate for candidate in (up, down) if candidate.valid and candidate.edge is not None]
        valid = [candidate for candidate in valid if self._risk_caps_pass(candidate)]
        if not valid:
            return None, "WATCH", "no_safe_first_entry_candidate"

        max_ask = self.config.get_path("entry.max_ask", None)
        if max_ask is not None:
            valid = [
                candidate
                for candidate in valid
                if candidate.price is not None and candidate.price <= float(max_ask)
            ]
            if not valid:
                return None, "WATCH", "no_safe_first_entry_below_max_ask"

        # Favorite-longshot bias guard: cheap tails are systematically
        # overpriced on 5-minute BTC markets; require a minimum ask.
        min_ask = self.config.get_path("entry.min_ask", None)
        if min_ask is not None:
            valid = [
                candidate
                for candidate in valid
                if candidate.price is not None and candidate.price >= float(min_ask)
            ]
            if not valid:
                return None, "WATCH", "no_safe_first_entry_above_min_ask"

        if not force_at_deadline:
            valid = [candidate for candidate in valid if candidate.edge is not None and candidate.edge >= min_edge]
            if not valid:
                return None, "WATCH", "no_edge_before_preferred_entry"

        if force_at_deadline:
            # A deadline is a timing fallback, not an edge exemption: forced
            # entries must still clear a net-edge and confidence floor.
            min_force_edge = float(self.config.get_path("entry.force_min_net_edge", 0.0))
            min_force_confidence = float(self.config.get_path("entry.force_min_confidence", 0.0))
            valid = [
                candidate
                for candidate in valid
                if (candidate.net_edge if candidate.net_edge is not None else candidate.edge or -999.0)
                >= min_force_edge
            ]
            if not valid:
                return None, "WATCH", "deadline_entry_edge_too_low"
            if prob.confidence < min_force_confidence:
                return None, "WATCH", "deadline_entry_confidence_too_low"
            selected = max(
                valid,
                key=lambda candidate: (
                    candidate.net_edge if candidate.net_edge is not None else candidate.edge or -999.0,
                    candidate.edge or -999.0,
                ),
            )
        elif bool(self.config.get_path("growth_sizing.use_growth_score_for_first_entry", True)):
            selected = max(
                valid,
                key=lambda candidate: (
                    candidate.growth_score if candidate.growth_score is not None else -999.0,
                    candidate.edge or -999.0,
                ),
            )
        else:
            selected = max(valid, key=lambda candidate: candidate.edge or -999.0)

        if not force_at_deadline and not after_preferred:
            early_edge = float(self.config.get_path("entry.early_min_edge", 0.12))
            early_confidence = float(self.config.get_path("entry.early_min_confidence", 0.50))
            if (selected.edge or -999.0) < early_edge or prob.confidence < early_confidence:
                return None, "WATCH", "early_entry_waiting_for_conviction"

        endgame = int(self.config.get_path("timing.endgame_seconds_remaining", 30))
        if seconds_remaining <= endgame and not force_at_deadline:
            strong_edge = float(self.config.get_path("edge.strong_edge", 0.12))
            strong_confidence = float(self.config.get_path("edge.strong_confidence", 0.30))
            if (selected.edge or -999.0) < strong_edge or prob.confidence < strong_confidence:
                return None, "SKIP", "endgame_first_entry_not_strong_enough"

        reason = "deadline_best_mispriced_side" if force_at_deadline else "first_directional_entry"
        return selected, f"BUY_{selected.side}", reason

    def _first_entry_deadline_seconds(self) -> int:
        """Read the forced first-entry deadline, falling back to preferred time."""
        return int(
            self.config.get_path(
                "entry.force_entry_deadline_seconds",
                self.config.get_path("timing.preferred_first_entry_seconds", 18),
            )
        )

    def _confirm_sell_candidate(self, market: MarketInfo, candidate: Optional[Candidate]) -> Optional[Candidate]:
        """Require repeated composite weakness before sending an inventory sale."""
        market_keys = [key for key in self.sell_signal_streaks if key[0] == market.market_id]
        if candidate is None:
            for key in market_keys:
                self.sell_signal_streaks.pop(key, None)
            return None

        key = (market.market_id, candidate.side, str(candidate.role or ""))
        for existing in market_keys:
            if existing != key:
                self.sell_signal_streaks.pop(existing, None)
        streak = self.sell_signal_streaks.get(key, 0) + 1
        self.sell_signal_streaks[key] = streak
        required = max(1, int(self.config.get_path("intra_window_sells.confirmation_cycles", 3)))
        if streak < required:
            candidate.valid = False
            candidate.reason = f"sell_signal_confirmation_{streak}_of_{required}"
        return candidate

    def _select_after_entry(self, up: Candidate, down: Candidate, shape: PositionShape, prob, indicators=None, binance_state=None, market=None, fills=None, seconds_since_open: int = 0, seconds_remaining: int = 0, seconds_since_first_fill=None) -> tuple[Optional[Candidate], str, str]:
        """Choose additional BUYs in priority order: lock, repair, then add.

        Repairs buy the weak/missing side only when `shape_improvement` passes.
        Same-side additions are considered last and require explicit settings.

        Args:
            up: Built BUY_UP candidate.
            down: Built BUY_DOWN candidate.
            shape: Current position before the next BUY.
            prob: Current UP/DOWN probability estimate.
            indicators: Latest MA and stability state.
            binance_state: Latest Binance price snapshot.
            market: Current PM market.
            fills: Stored fills for this window.
            seconds_since_open: Window age in seconds.
            seconds_remaining: Time left in the window.
            seconds_since_first_fill: Seconds since first BUY fill, if any.
        """
        dom = dominant_side(shape)
        weak = weak_side(shape)
        min_improve = float(self.config.get_path("risk.min_shape_improvement_usdc", 0.01))
        allow_repair_negative = bool(self.config.get_path("risk.allow_repair_at_small_negative_edge", False))
        max_repair_neg = float(self.config.get_path("risk.max_repair_negative_edge", 0.02))

        valid = [
            candidate
            for candidate in (up, down)
            if candidate.valid
            and self._risk_caps_pass(candidate)
            and not self._same_side_buy_blocked(candidate.side, shape, fills)
        ]

        # 10. Select final candidate using lock/repair/risk priority.
        lock_candidates = [
            candidate
            for candidate in valid
            if candidate.would_lock
            and (
                (candidate.edge or -999.0) >= 0.0
                or (allow_repair_negative and (candidate.edge or -999.0) >= -max_repair_neg)
            )
        ]
        if lock_candidates:
            selected = min(lock_candidates, key=lambda candidate: candidate.notional_usdc or 999999.0)
            return selected, f"BUY_{selected.side}", "locks_structure"

        recovery = self._select_sold_side_recovery(
            up, down, prob, indicators, binance_state, market, fills or [],
            seconds_since_open, seconds_remaining,
            current_ts=(market.window_start_ts + seconds_since_open) if market else time.time(),
        )
        if recovery[0] is not None:
            return recovery

        repair_candidates = [
            candidate
            for candidate in valid
            if weak is not None
            and candidate.side == weak
            and candidate.shape_improvement >= min_improve
            and (
                (candidate.edge or -999.0) >= 0.0
                or (allow_repair_negative and (candidate.edge or -999.0) >= -max_repair_neg)
            )
        ]
        if repair_candidates:
            selected = max(repair_candidates, key=lambda candidate: candidate.shape_improvement)
            return selected, f"BUY_{selected.side}", "opposite_side_repair_improves_worst_case"

        if bool(self.config.get_path("high_conviction_same_side.enabled", False)):
            conviction = self._select_high_conviction_same_side(
                up, down, shape, prob, indicators, binance_state, market, fills or [],
                seconds_since_open, seconds_remaining, seconds_since_first_fill,
            )
            if conviction[0] is not None:
                return conviction
        else:
            max_same_side_adds = int(self.config.get_path("risk.max_same_side_adds", 0))
            late_directional_add_blocked = (
                self._time_based_exit_limits_enabled()
                and seconds_remaining <= int(self.config.get_path("intra_window_sells.late_seconds_remaining", 90))
            )
            if (
                not late_directional_add_blocked
                and dom
                and max_same_side_adds > 0
                and prob.confidence >= float(self.config.get_path("edge.strong_confidence", 0.30))
            ):
                strong_edge = float(self.config.get_path("edge.strong_edge", 0.12))
                same_side_candidates = [candidate for candidate in valid if candidate.side == dom and (candidate.edge or -999.0) >= strong_edge]
                if same_side_candidates:
                    selected = max(same_side_candidates, key=lambda candidate: candidate.edge or -999.0)
                    return selected, f"BUY_{selected.side}", "strong_same_side_add"

        return None, "HOLD_EXPOSED", "no_repair_or_lock_available"

    def _select_sold_side_recovery(self, up: Candidate, down: Candidate, prob, indicators, binance_state, market, fills, seconds_since_open: int, seconds_remaining: int, current_ts: float) -> tuple[Optional[Candidate], str, str]:
        """Buy back a side previously sold by `DIRECTIONAL_WEAK_SIDE_EXIT`.

        This is stricter than normal repair: it requires the sold side's model
        probability and stability to recover before rebuilding that inventory.

        Args:
            up: Built BUY_UP candidate.
            down: Built BUY_DOWN candidate.
            prob: Current UP/DOWN probability estimate.
            indicators: Latest MA and stability state.
            binance_state: Latest Binance price snapshot.
            market: Current PM market.
            fills: Stored fills for this window.
            seconds_since_open: Window age in seconds.
            seconds_remaining: Time left in the window.
            current_ts: Current strategy timestamp for sell cooldown checks.
        """
        if not bool(self.config.get_path("intra_window_sells.sold_side_recovery_enabled", False)):
            return None, "HOLD_EXPOSED", "sold_side_recovery_disabled"
        last_sell = self._last_directional_weak_side_sell(fills)
        if last_sell is None:
            return None, "HOLD_EXPOSED", "sold_side_recovery_no_prior_weak_sell"

        side = str(getattr(last_sell, "side", "")).upper()
        candidate = up if side == "UP" else down if side == "DOWN" else None
        if candidate is None:
            return None, "HOLD_EXPOSED", "sold_side_recovery_side_missing"

        reject_reason = self._sold_side_recovery_reject_reason(
            candidate, last_sell, prob, indicators, binance_state, market, fills,
            seconds_since_open, seconds_remaining, current_ts,
        )
        if reject_reason:
            if candidate.valid:
                candidate.reason = reject_reason
            return None, "HOLD_EXPOSED", reject_reason
        if not candidate.valid or not self._risk_caps_pass(candidate):
            return None, "HOLD_EXPOSED", candidate.reason
        candidate.role = "SOLD_SIDE_RECOVERY"
        return candidate, f"BUY_{candidate.side}", "sold_side_recovery_after_weak_sell"

    def _sold_side_recovery_reject_reason(self, candidate: Candidate, last_sell, prob, indicators, binance_state, market, fills, seconds_since_open: int, seconds_remaining: int, current_ts: float) -> Optional[str]:
        """Return the first failed BUY-back gate after a weak-side sell.

        Args:
            candidate: BUY candidate for the previously sold side.
            last_sell: Latest directional weak-side SELL fill.
            prob: Current UP/DOWN probability estimate.
            indicators: Latest MA and stability state.
            binance_state: Latest Binance price snapshot.
            market: Current PM market.
            fills: Stored fills for this window.
            seconds_since_open: Window age in seconds.
            seconds_remaining: Time left in the window.
            current_ts: Current strategy timestamp.
        """
        side = candidate.side
        sign = 1.0 if side == "UP" else -1.0
        side_probability = prob.p_up if side == "UP" else prob.p_down
        sell_ts = self._fill_ts(last_sell)
        buys_after_sell = [
            fill
            for fill in fills
            if str(getattr(fill, "action", "BUY")).upper() == "BUY"
            and str(getattr(fill, "side", "")).upper() == side
            and self._fill_ts(fill) > sell_ts
        ]

        if len(buys_after_sell) >= int(self.config.get_path("intra_window_sells.sold_side_recovery_max_buys_per_side", 1)):
            return "sold_side_recovery_max_buys_reached"
        if side_probability is None or side_probability < float(self.config.get_path("intra_window_sells.sold_side_recovery_min_probability", 0.62)):
            return "sold_side_recovery_probability_too_low"
        if prob.confidence < float(self.config.get_path("intra_window_sells.sold_side_recovery_min_confidence", 0.24)):
            return "sold_side_recovery_confidence_too_low"
        if (candidate.edge or -999.0) < float(self.config.get_path("intra_window_sells.sold_side_recovery_min_edge", 0.0)):
            return "sold_side_recovery_edge_too_low"
        if (candidate.net_edge or -999.0) < float(self.config.get_path("intra_window_sells.sold_side_recovery_min_net_edge", -0.01)):
            return "sold_side_recovery_net_edge_too_low"
        if candidate.price is None or candidate.price > float(self.config.get_path("intra_window_sells.sold_side_recovery_max_ask", 0.88)):
            return "sold_side_recovery_ask_too_high"
        if current_ts - sell_ts < float(self.config.get_path("intra_window_sells.sold_side_recovery_min_seconds_after_sell", 8)):
            return "sold_side_recovery_wait_after_sell"
        if seconds_remaining < int(self.config.get_path("intra_window_sells.sold_side_recovery_min_seconds_remaining", 35)):
            return "sold_side_recovery_too_late"
        if indicators is None or binance_state is None or market is None:
            return "sold_side_recovery_state_missing"

        stability = indicators.up_stability_score if side == "UP" else indicators.down_stability_score
        if stability < int(self.config.get_path("intra_window_sells.sold_side_recovery_min_stability_score", 3)):
            return "sold_side_recovery_stability_too_low"
        if bool(self.config.get_path("intra_window_sells.sold_side_recovery_require_price_on_target_side", True)):
            if binance_state.price is None or market.target_price is None or sign * (binance_state.price - market.target_price) <= 0:
                return "sold_side_recovery_price_not_on_target_side"
        distance_bps = prob.distance_bps or 0.0
        if sign * distance_bps < float(self.config.get_path("intra_window_sells.sold_side_recovery_min_distance_bps", 1.0)):
            return "sold_side_recovery_distance_too_small"
        return None

    def _last_directional_weak_side_sell(self, fills):
        """Return the latest SELL created by the directional weak-side exit."""
        directional_sells = []
        for fill in fills:
            if str(getattr(fill, "action", "BUY")).upper() != "SELL":
                continue
            metadata = getattr(fill, "metadata", {}) or {}
            candidate = metadata.get("candidate") if isinstance(metadata, dict) else {}
            role = str(candidate.get("role") or getattr(fill, "role", "") or "").upper()
            reason = str(getattr(fill, "reason", "") or metadata.get("strategy_reason", "")).lower()
            if role == "DIRECTIONAL_WEAK_SIDE_EXIT" or reason == "directional_weak_side_exit":
                directional_sells.append(fill)
        if not directional_sells:
            return None
        return max(directional_sells, key=self._fill_ts)

    def _fill_ts(self, fill: Any) -> float:
        """Normalize paper/live fill timestamp attribute names."""
        return float(getattr(fill, "created_at", getattr(fill, "ts", 0.0)) or 0.0)

    def _time_based_exit_limits_enabled(self) -> bool:
        """Gate the optional late-window BUY restrictions used with sell tests."""
        return bool(self.config.get_path("intra_window_sells.enabled", False)) and bool(
            self.config.get_path("intra_window_sells.enforce_time_based_buy_limits", False)
        )

    def _select_high_conviction_same_side(self, up: Candidate, down: Candidate, shape: PositionShape, prob, indicators, binance_state, market, fills, seconds_since_open: int, seconds_remaining: int, seconds_since_first_fill) -> tuple[Optional[Candidate], str, str]:
        """Select an additional BUY on the already dominant side.

        `_high_conviction_reject_reason` contains every required trigger and is
        the main place to inspect when a same-side add does not happen.

        Args:
            up: Built BUY_UP candidate.
            down: Built BUY_DOWN candidate.
            shape: Current position before the add.
            prob: Current UP/DOWN probability estimate.
            indicators: Latest MA and stability state.
            binance_state: Latest Binance price snapshot.
            market: Current PM market.
            fills: Stored fills for this window.
            seconds_since_open: Window age in seconds.
            seconds_remaining: Time left in the window.
            seconds_since_first_fill: Seconds since first BUY fill.
        """
        if not bool(self.config.get_path("high_conviction_same_side.enabled", False)):
            return None, "HOLD_EXPOSED", "high_conviction_same_side_disabled"

        dom = dominant_side(shape)
        if dom is None:
            return None, "HOLD_EXPOSED", "high_conviction_no_dominant_side"
        candidate = up if dom == "UP" else down
        reject_reason = self._high_conviction_reject_reason(
            candidate, prob, indicators, binance_state, market, fills,
            seconds_since_open, seconds_remaining, seconds_since_first_fill,
        )
        if reject_reason:
            if candidate.valid:
                candidate.reason = reject_reason
            return None, "HOLD_EXPOSED", reject_reason
        if not candidate.valid or not self._risk_caps_pass(candidate):
            return None, "HOLD_EXPOSED", candidate.reason
        return candidate, f"BUY_{candidate.side}", "high_conviction_same_side_add"

    def _high_conviction_reject_reason(self, candidate: Candidate, prob, indicators, binance_state, market, fills, seconds_since_open: int, seconds_remaining: int, seconds_since_first_fill) -> Optional[str]:
        """Return the first failed same-side-add gate, or `None` when approved.

        Args:
            candidate: Dominant-side BUY candidate being checked.
            prob: Current UP/DOWN probability estimate.
            indicators: Latest MA and stability state.
            binance_state: Latest Binance price snapshot.
            market: Current PM market.
            fills: Stored fills for this window.
            seconds_since_open: Window age in seconds.
            seconds_remaining: Time left in the window.
            seconds_since_first_fill: Seconds since first BUY fill.
        """
        side = candidate.side
        sign = 1.0 if side == "UP" else -1.0
        side_probability = prob.p_up if side == "UP" else prob.p_down
        same_side_orders = sum(1 for fill in fills if fill.side == side)
        same_side_adds = max(0, same_side_orders - 1)

        if same_side_adds >= int(self.config.get_path("high_conviction_same_side.max_adds_per_window", 2)):
            return "high_conviction_max_adds_reached"
        if side_probability is None or side_probability < float(self.config.get_path("high_conviction_same_side.min_probability", 0.80)):
            return "high_conviction_probability_too_low"
        if prob.confidence < float(self.config.get_path("high_conviction_same_side.min_confidence", 0.60)):
            return "high_conviction_confidence_too_low"
        if (candidate.edge or -999.0) < float(self.config.get_path("high_conviction_same_side.min_edge", 0.03)):
            return "high_conviction_edge_too_low"
        if (candidate.net_edge or -999.0) < float(self.config.get_path("high_conviction_same_side.min_net_edge", 0.02)):
            return "high_conviction_net_edge_too_low"
        if candidate.price is None or candidate.price > float(self.config.get_path("high_conviction_same_side.max_ask", 0.88)):
            return "high_conviction_ask_too_high"
        if seconds_since_open < int(self.config.get_path("high_conviction_same_side.min_seconds_since_open", 30)):
            return "high_conviction_too_early"
        if seconds_since_open > int(self.config.get_path("high_conviction_same_side.max_seconds_since_open", 300)):
            return "high_conviction_after_time_ceiling"
        if seconds_remaining < int(self.config.get_path("high_conviction_same_side.min_seconds_remaining", 45)):
            return "high_conviction_too_late"
        if seconds_since_first_fill is None or seconds_since_first_fill < float(self.config.get_path("high_conviction_same_side.min_seconds_after_first_entry", 15)):
            return "high_conviction_wait_after_first_entry"
        if indicators is None or binance_state is None or market is None:
            return "high_conviction_state_missing"

        if bool(self.config.get_path("high_conviction_same_side.require_price_on_target_side", True)):
            if binance_state.price is None or market.target_price is None or sign * (binance_state.price - market.target_price) <= 0:
                return "high_conviction_price_not_on_target_side"
        distance_bps = prob.distance_bps or 0.0
        if sign * distance_bps < float(self.config.get_path("high_conviction_same_side.min_distance_bps", 5.0)):
            return "high_conviction_distance_too_small"
        if bool(self.config.get_path("high_conviction_same_side.require_ma_alignment", True)) and indicators.pressure != side:
            return "high_conviction_ma_not_aligned"
        if sign * (indicators.ma_fast_slope_bps or 0.0) < float(self.config.get_path("high_conviction_same_side.min_fast_slope_bps", 0.10)):
            return "high_conviction_fast_slope_not_aligned"
        if sign * (indicators.ma_mid_slope_bps or 0.0) < float(self.config.get_path("high_conviction_same_side.min_mid_slope_bps", 0.0)):
            return "high_conviction_mid_slope_not_aligned"
        if sign * (indicators.velocity_5s_bps or 0.0) < float(self.config.get_path("high_conviction_same_side.min_velocity_5s_bps", 0.0)):
            return "high_conviction_velocity_not_aligned"
        persistence = indicators.persistence_above_target_seconds if side == "UP" else indicators.persistence_below_target_seconds
        if persistence < int(self.config.get_path("high_conviction_same_side.min_persistence_seconds", 10)):
            return "high_conviction_persistence_too_low"
        if bool(self.config.get_path("high_conviction_same_side.require_volume_spike", True)):
            if indicators.volume_spike is None or indicators.volume_spike < float(self.config.get_path("high_conviction_same_side.min_volume_spike", 1.0)):
                return "high_conviction_volume_too_low"
        if bool(self.config.get_path("high_conviction_same_side.require_orderbook_alignment", True)):
            if sign * (indicators.orderbook_imbalance or 0.0) < float(self.config.get_path("high_conviction_same_side.min_orderbook_imbalance", 0.05)):
                return "high_conviction_orderbook_not_aligned"
        return None

    def _select_lock_only(self, up: Candidate, down: Candidate) -> tuple[Optional[Candidate], str, str]:
        """During the repair delay, allow only a BUY that immediately locks."""
        allow_repair_negative = bool(self.config.get_path("risk.allow_repair_at_small_negative_edge", False))
        max_repair_neg = float(self.config.get_path("risk.max_repair_negative_edge", 0.02))
        valid = [candidate for candidate in (up, down) if candidate.valid and self._risk_caps_pass(candidate)]
        lock_candidates = [
            candidate
            for candidate in valid
            if candidate.would_lock
            and (
                (candidate.edge or -999.0) >= 0.0
                or (allow_repair_negative and (candidate.edge or -999.0) >= -max_repair_neg)
            )
        ]
        if not lock_candidates:
            return None, "WATCH", "repair_waiting_for_preferred_delay"
        selected = min(lock_candidates, key=lambda candidate: candidate.notional_usdc or 999999.0)
        return selected, f"BUY_{selected.side}", "early_lock_opportunity"

    def _select_post_lock_add(self, up: Candidate, down: Candidate, shape: PositionShape, prob) -> tuple[Optional[Candidate], str, str]:
        """Optionally add after lock only when the structure remains locked."""
        strong_edge = float(self.config.get_path("edge.strong_edge", 0.12))
        strong_confidence = float(self.config.get_path("edge.strong_confidence", 0.50))
        min_improve = float(self.config.get_path("risk.min_shape_improvement_usdc", 0.01))
        if prob.confidence < strong_confidence:
            return None, "LOCKED", "post_lock_confidence_too_low"

        valid = [
            candidate
            for candidate in (up, down)
            if candidate.valid
            and self._risk_caps_pass(candidate)
            and candidate.shape_after is not None
            and candidate.shape_after.locked
            and candidate.shape_improvement >= min_improve
            and (candidate.edge or -999.0) >= strong_edge
        ]
        if not valid:
            return None, "LOCKED", "no_safe_post_lock_add"
        selected = max(valid, key=lambda candidate: candidate.shape_improvement)
        return selected, f"BUY_{selected.side}", "strong_post_lock_add"

    def _same_side_buy_blocked(self, side: str, shape: PositionShape, fills) -> bool:
        """Mirror the broker's same-side rule so selection never picks a BUY it rejects."""
        if int(self.config.get_path("risk.max_same_side_adds", 0)) != 0:
            return False
        if shape.locked and bool(self.config.get_path("risk.allow_post_lock_adds", False)):
            return False
        return any(
            str(getattr(fill, "action", "BUY")).upper() == "BUY" and fill.side == side for fill in fills or []
        )

    def _risk_caps_pass(self, candidate: Candidate) -> bool:
        """Reject a simulated candidate that exceeds window or account limits."""
        if candidate.shape_after is None:
            return False
        current_balance, starting_balance = self._sizing_bankroll()
        max_notional, max_loss = window_risk_caps(
            self.config,
            current_balance,
            high_conviction=candidate.role == "SAME_SIDE_ADD",
        )
        if candidate.shape_after.total_cost > max_notional:
            candidate.valid = False
            candidate.reason = "max_window_notional"
            return False
        if candidate.shape_after.worst_case_pnl < -max_loss:
            candidate.valid = False
            candidate.reason = "max_window_loss"
            return False
        if not account_equity_floor_passes(
            self.config,
            current_balance,
            starting_balance,
            candidate.shape_after.worst_case_pnl,
        ):
            candidate.valid = False
            candidate.reason = "account_equity_floor"
            return False
        return True

    def _execute_dry_run_fill(self, decision: Decision, market: MarketInfo, selected: Candidate) -> None:
        """Record the approved candidate as a dry-run fill without broker checks.

        Args:
            decision: Full strategy decision payload for this cycle.
            market: Current PM market.
            selected: Approved BUY or SELL candidate.
        """
        fill = Fill(
            ts=decision.timestamp,
            market_id=market.market_id,
            window_start_ts=market.window_start_ts,
            side=selected.side,
            price=float(selected.price or 0.0),
            notional_usdc=float(selected.notional_usdc or 0.0),
            shares=float(selected.shares or 0.0),
            reason=decision.selected_reason,
            action=selected.action,
        )
        decision.fill_id = self._record_fill(fill)
        decision.order_status = "DRY_RUN"
        decision.order_reason = "dry_run_fill_recorded"
        decision.position_after = asdict(self._shape(market))
        decision.average_entry_up, decision.average_entry_down = self._average_entry_prices(self._fills_for_window(market))

    def _execute_paper_order(self, decision: Decision, market: MarketInfo, selected: Candidate, selected_quote) -> None:
        """Send an approved candidate to PaperBroker and refresh stored shape.

        Args:
            decision: Full strategy decision payload for this cycle.
            market: Current PM market.
            selected: Approved BUY or SELL candidate.
            selected_quote: PM quote for the selected side.
        """
        if selected_quote is None:
            decision.order_status = "REJECTED"
            decision.order_reason = "quote_missing_for_selected_side"
            decision.position_after = decision.position_before
            return

        token_id = market.up_token_id if selected.side == "UP" else market.down_token_id
        # 11. Send approved decision to paper broker.
        # logger.debug("PAPER_ORDER_REQUEST %s", ...)
        order, fill = self.paper_broker.submit_order(
            timestamp=decision.timestamp,
            window_start=market.window_start_ts,
            window_end=market.window_end_ts,
            market_id=market.market_id,
            token_id=token_id,
            side=selected.side,
            action=selected.action,
            price=selected.price,
            notional_usdc=float(selected.notional_usdc or 0.0),
            shares=float(selected.shares or 0.0),
            decision_id=decision.decision_id,
            quote_ts=selected_quote.ts,
            reason=decision.selected_reason,
            metadata={
                "candidate": asdict(selected),
                "mode": self.mode,
            },
        )
        decision.order_id = order.id
        decision.fill_id = fill.id if fill else None
        decision.order_status = order.status
        decision.order_reason = order.reason
        decision.position_after = asdict(self.paper_broker.get_position(market.market_id, market.window_start_ts))
        decision.average_entry_up, decision.average_entry_down = self._average_entry_prices(self._fills_for_window(market))

    async def _execute_live_order(self, decision: Decision, market: MarketInfo, selected: Candidate, selected_quote) -> None:
        """Send the approved candidate to the guarded live executor.

        Args:
            decision: Full strategy decision payload for this cycle.
            market: Current PM market.
            selected: Approved BUY or SELL candidate.
            selected_quote: PM quote for the selected side.
        """
        if selected_quote is None:
            decision.order_status = "REJECTED"
            decision.order_reason = "quote_missing_for_selected_side"
            decision.position_after = decision.position_before
            return

        token_id = market.up_token_id if selected.side == "UP" else market.down_token_id
        result = await self.live_executor.place_order(
            timestamp=decision.timestamp,
            window_start=market.window_start_ts,
            window_end=market.window_end_ts,
            market_id=market.market_id,
            token_id=token_id,
            side=selected.side,
            action=selected.action,
            price=selected.price,
            notional_usdc=float(selected.notional_usdc or 0.0),
            shares=float(selected.shares or 0.0),
            decision_id=decision.decision_id,
            quote_ts=selected_quote.ts,
            reason=decision.selected_reason,
            shape_before=self._shape(market),
            fills_before=list(self._fills_for_window(market)),
            metadata=asdict(selected),
        )
        decision.order_id = result.audit_id
        decision.order_status = result.status
        decision.order_reason = result.reason
        if result.response and result.response.get("redeem_response"):
            decision.order_reason = f"{decision.order_reason}; redeem_ok"
        elif result.response and result.response.get("redeem_error"):
            decision.order_reason = f"{decision.order_reason}; redeem_error"

        # CLOB account state, not the submitted order size, is the live position truth.
        await self.live_executor.refresh_account(market, force=True)
        decision.position_after = asdict(self._shape(market))
        decision.average_entry_up, decision.average_entry_down = self._average_entry_prices(self._fills_for_window(market))

    def _decision_payload(self, decision_id, now, market, binance_state, indicators, prob, up_q, down_q, pos_before, cand_up, cand_down, selected, decision, reason, pos_after, sell_candidate=None, sell_probe=None, support_resistance=None, polymarket_flow=None, binance_flow=None, seconds_since_first_fill=None, average_entry_up=None, average_entry_down=None, chainlink_ref=None) -> Decision:
        """Assemble the full structured decision payload used by logs/terminal.

        Args:
            decision_id: Unique id for this strategy decision.
            now: Current strategy cycle timestamp.
            market: Active PM market, if available.
            binance_state: Latest Binance price/order-book snapshot.
            indicators: Latest MA, volume, slope, and stability state.
            prob: Current UP/DOWN probability estimate.
            up_q: Latest PM UP quote.
            down_q: Latest PM DOWN quote.
            pos_before: Position shape before selected order.
            cand_up: Built BUY_UP candidate.
            cand_down: Built BUY_DOWN candidate.
            selected: Final BUY or SELL candidate, if any.
            decision: Decision label such as BUY_UP, SELL_DOWN, or WATCH.
            reason: Human-readable reason for the decision.
            pos_after: Expected/current shape after the decision.
            sell_candidate: Executable sell candidate, if approved.
            sell_probe: Terminal-only sell diagnostics.
            support_resistance: S/R side-information state for this cycle.
            polymarket_flow: PM order-book/trade side information for this cycle.
            binance_flow: Binance OFI/CVD side information for this cycle.
            seconds_since_first_fill: Seconds since first BUY fill.
            average_entry_up: Average UP BUY entry price.
            average_entry_down: Average DOWN BUY entry price.
        """
        seconds_since_open = None
        seconds_remaining = None
        if market:
            seconds_since_open = int(now) - market.window_start_ts
            seconds_remaining = market.window_end_ts - int(now)

        starting_balance = float(self.config.get_path("account.balance_usdc", 0.0))
        realized_pnl = self.db.total_realized_pnl()
        win_rate_percent = self.db.win_rate_percent()
        chainlink_ref = chainlink_ref or {}
        basis = chainlink_ref.get("price_basis")
        raw_binance_price = binance_state.price
        if raw_binance_price is not None and basis is not None:
            raw_binance_price = raw_binance_price - basis
        display_price = chainlink_ref.get("reference_price")
        if display_price is None:
            display_price = binance_state.price
        spot_target_ratio = None
        if market and market.target_price and display_price is not None:
            spot_target_ratio = display_price / market.target_price
        time_remaining_ratio = None
        if seconds_remaining is not None:
            interval = max(1, int(self.config.get_path("bot.interval_seconds", 300)))
            time_remaining_ratio = max(0.0, min(1.0, seconds_remaining / interval))
        return Decision(
            decision_id=decision_id,
            timestamp=now,
            mode=self.mode,
            strategy_state=self._strategy_state(market, pos_before, decision, reason, seconds_since_open, seconds_remaining),
            window_start=market.window_start_ts if market else None,
            window_end=market.window_end_ts if market else None,
            market_id=market.market_id if market else None,
            market_slug=market.slug if market else None,
            seconds_since_open=seconds_since_open,
            seconds_remaining=seconds_remaining,
            seconds_since_first_fill=seconds_since_first_fill,
            average_entry_up=average_entry_up,
            average_entry_down=average_entry_down,
            account_starting_balance_usdc=starting_balance,
            account_realized_pnl_usdc=realized_pnl,
            account_balance_usdc=starting_balance + realized_pnl,
            account_win_rate_percent=win_rate_percent,
            stop_when_locked=bool(self.config.get_path("risk.stop_when_locked", True)),
            allow_post_lock_adds=bool(self.config.get_path("risk.allow_post_lock_adds", False)),
            repair_start_after_first_entry_seconds=int(self.config.get_path("timing.repair_start_after_first_entry_seconds", 24)),
            preferred_repair_after_first_entry_seconds=int(self.config.get_path("timing.preferred_repair_after_first_entry_seconds", 38)),
            target_price=market.target_price if market else None,
            btc_price=display_price,
            binance_price=raw_binance_price,
            chainlink_price=chainlink_ref.get("chainlink_price"),
            chainlink_age_seconds=chainlink_ref.get("chainlink_age_seconds"),
            price_basis=basis,
            reference_price=chainlink_ref.get("reference_price"),
            reference_source=str(chainlink_ref.get("reference_source") or "unknown"),
            twap_lock=chainlink_ref.get("twap") or {},
            bridge_p_up=prob.bridge_p_up if prob else None,
            market_mid_p_up=prob.market_mid_p_up if prob else None,
            kelly_multiplier=self.adaptive.kelly_multiplier() if self.adaptive.enabled() else None,
            ma_fast=indicators.ma_fast if indicators else None,
            ma_mid=indicators.ma_mid if indicators else None,
            ma_slow=indicators.ma_slow if indicators else None,
            ma_anchor=indicators.ma_anchor if indicators else None,
            ma_pressure=indicators.pressure if indicators else None,
            ma_fast_slope_bps=indicators.ma_fast_slope_bps if indicators else None,
            ma_mid_slope_bps=indicators.ma_mid_slope_bps if indicators else None,
            ma_anchor_slope_bps=indicators.ma_anchor_slope_bps if indicators else None,
            up_stability_score=indicators.up_stability_score if indicators else None,
            down_stability_score=indicators.down_stability_score if indicators else None,
            ma_velocity_5s_bps=indicators.velocity_5s_bps if indicators else None,
            ma_velocity_15s_bps=indicators.velocity_15s_bps if indicators else None,
            volatility_60s=indicators.volatility_60s if indicators else None,
            current_volume=indicators.current_volume if indicators else None,
            volume_ma_5=indicators.volume_ma_5 if indicators else None,
            volume_ma_10=indicators.volume_ma_10 if indicators else None,
            volume_spike=indicators.volume_spike if indicators else None,
            orderbook_imbalance=indicators.orderbook_imbalance if indicators else None,
            persistence_above_target_seconds=indicators.persistence_above_target_seconds if indicators else None,
            persistence_below_target_seconds=indicators.persistence_below_target_seconds if indicators else None,
            p_up=prob.p_up if prob else None,
            p_down=prob.p_down if prob else None,
            confidence=prob.confidence if prob else None,
            distance_from_target=prob.distance_from_target if prob else None,
            distance_bps=prob.distance_bps if prob else None,
            raw_score=prob.raw_score if prob else None,
            distance_score=prob.distance_score if prob else None,
            ma_score=prob.ma_score if prob else None,
            slope_score=prob.slope_score if prob else None,
            volume_score=prob.volume_score if prob else None,
            imbalance_score=prob.imbalance_score if prob else None,
            time_factor=prob.time_factor if prob else None,
            spot_target_ratio=spot_target_ratio,
            time_remaining_ratio=time_remaining_ratio,
            growth_sizing_enabled=bool(self.config.get_path("growth_sizing.enabled", False)),
            high_conviction_same_side_enabled=bool(self.config.get_path("high_conviction_same_side.enabled", False)),
            high_conviction_min_probability=float(self.config.get_path("high_conviction_same_side.min_probability", 0.80)),
            high_conviction_max_window_notional_usdc=float(self.config.get_path("high_conviction_same_side.max_window_notional_usdc", 7.0)),
            up_bid=up_q.bid if up_q else None,
            up_ask=up_q.ask if up_q else None,
            down_bid=down_q.bid if down_q else None,
            down_ask=down_q.ask if down_q else None,
            up_spread=up_q.spread if up_q else None,
            down_spread=down_q.spread if down_q else None,
            up_quote_age_seconds=quote_age_seconds(up_q) if up_q else None,
            down_quote_age_seconds=quote_age_seconds(down_q) if down_q else None,
            edge_up=cand_up.edge if cand_up else None,
            edge_down=cand_down.edge if cand_down else None,
            position_before=asdict(pos_before),
            candidate_up=asdict(cand_up) if cand_up else {},
            candidate_down=asdict(cand_down) if cand_down else {},
            sell_candidate=asdict(sell_candidate) if sell_candidate else {},
            sell_probe=sell_probe or {},
            support_resistance=support_resistance or {},
            polymarket_flow=polymarket_flow or {},
            binance_flow=binance_flow or {},
            selected_side=selected.side if selected else None,
            decision=decision,
            selected_reason=reason,
            position_after=asdict(pos_after),
        )

    def _strategy_state(self, market, shape, decision, reason, seconds_since_open, seconds_remaining) -> str:
        """Convert raw decision/shape information into a terminal state label."""
        if market is None:
            return "WAITING_FOR_OPEN"
        if seconds_remaining is not None and seconds_remaining <= 0:
            return "WINDOW_CLOSED"
        if reason == "wait_for_clean_window":
            return "WARMUP"
        if reason == "wait_after_open":
            return "WAIT_AFTER_OPEN"
        if shape.locked:
            return "LOCKED"
        if shape.up_shares <= 0 and shape.down_shares <= 0:
            return "FLAT_AFTER_EXIT" if abs(shape.total_cost) > 1e-9 else "LOOKING_FOR_FIRST_ENTRY"
        if reason in {"one_sided_waiting_for_repair_delay", "repair_waiting_for_preferred_delay"}:
            return "ONE_SIDED_EXPOSED"
        if decision == "HOLD_EXPOSED":
            return "HOLD_EXPOSED"
        return "REPAIR_SEARCH"

    def _post_execution_state(self, decision: Decision) -> str:
        """Recompute the displayed state after an order result updates shape."""
        shape = decision.position_after
        if shape.get("locked"):
            return "LOCKED"
        has_position = (
            shape.get("up_shares", 0.0) > 0
            or shape.get("down_shares", 0.0) > 0
            or ("up_shares" not in shape and shape.get("total_cost", 0.0) > 0)
        )
        if decision.order_status in {"FILLED", "DRY_RUN", "MATCHED"} and has_position:
            first_entry_reasons = {"first_directional_entry", "deadline_best_mispriced_side"}
            return "ONE_SIDED_EXPOSED" if decision.selected_reason in first_entry_reasons else "REPAIR_SEARCH"
        if decision.order_status in {"FILLED", "DRY_RUN", "MATCHED"} and not has_position:
            return "FLAT_AFTER_EXIT"
        return decision.strategy_state

    def _seconds_since_first_fill(self, now: float, fills) -> Optional[float]:
        """Return seconds since the first BUY fill; SELL fills do not reset it."""
        buy_fills = [fill for fill in fills if str(getattr(fill, "action", "BUY")).upper() == "BUY"]
        if not buy_fills:
            return None
        first_fill_ts = min(float(getattr(fill, "created_at", getattr(fill, "ts", now))) for fill in buy_fills)
        return max(0.0, now - first_fill_ts)

    def _average_entry_prices(self, fills) -> tuple[Optional[float], Optional[float]]:
        """Return weighted-average cost for currently open UP/DOWN shares."""
        return average_open_entry(fills, "UP"), average_open_entry(fills, "DOWN")

    async def _settle_finished_windows(self, now: float, binance_state) -> None:
        """Resolve completed windows and write PnL/report data.

        Prefers Polymarket's official Chainlink open/close prices (the real
        resolution source). The completed TWAP can take several minutes to be
        published after window end, so within the grace period a window is
        left unresolved instead of settling from Binance, which can point at
        the wrong side. After the grace period the Binance close is only an
        extended-outage fallback.
        """
        if self.mode not in {"paper", "live"}:
            return

        grace = float(self.config.get_path("polymarket.settlement_official_grace_seconds", 420.0))
        max_age = float(self.config.get_path("polymarket.settlement_official_max_age_seconds", 3600.0))
        oldest_start = now - max_age - float(self.config.get_path("bot.interval_seconds", 300))
        for key in [key for key in self._official_poll_ts if key < oldest_start]:
            self._official_poll_ts.pop(key, None)
        # One official-price request per cycle keeps a backlog of unresolved
        # windows from stalling the decision loop; old windows are never polled.
        poll_budget = 1
        settled_any = False
        for market in self.db.list_unresolved_markets(now):
            official = None
            if now - float(market["window_end_ts"]) <= max_age:
                window_start_ts = int(market["window_start_ts"])
                polled_at = self._official_poll_ts.get(window_start_ts)
                official = await self._official_window_prices(window_start_ts, allow_poll=poll_budget > 0)
                poll_budget -= int(self._official_poll_ts.get(window_start_ts) != polled_at)
            have_official = bool(
                official and official.get("open_price") is not None and official.get("close_price") is not None
            )
            if not have_official and now < float(market["window_end_ts"]) + grace:
                continue
            target_price = market.get("target_price")
            final_price = None
            winning_side_override = None
            notes = "settled_from_binance_close_near_window_end"
            if official and official.get("open_price") is not None and official.get("close_price") is not None:
                target_price = official["open_price"]
                final_price = official["close_price"]
                # Market rule: resolves Up when the TWAP is >= the price to beat.
                winning_side_override = "UP" if float(final_price) >= float(target_price) else "DOWN"
                notes = "settled_from_polymarket_chainlink_twap"
            else:
                final_price = self._final_price_for_window(binance_state.bars, int(market["window_end_ts"]))
            if target_price is None or final_price is None:
                continue
            # Feed the settled outcome back into the adaptive layer: the
            # calibrator, Brier EMA, and drawdown throttle all learn here.
            effective_winning_side = winning_side_override or (
                "UP" if float(final_price) >= float(target_price) else "DOWN"
            )
            settle_key = (str(market["market_id"]), int(market["window_start_ts"]))
            first_p_up = None
            window_first_p_up = getattr(self, "_window_first_p_up", None)
            if isinstance(window_first_p_up, dict):
                first_p_up = window_first_p_up.pop(settle_key, None)
            adaptive_engine = getattr(self, "adaptive", None)
            if adaptive_engine is not None:
                adaptive_engine.observe_outcome(first_p_up, effective_winning_side == "UP")
            if self.mode == "paper":
                settled = self.paper_broker.settle_window(
                    timestamp=now,
                    window_start=int(market["window_start_ts"]),
                    window_end=int(market["window_end_ts"]),
                    market_id=str(market["market_id"]),
                    target_price=float(target_price),
                    final_btc_price=float(final_price),
                    winning_side=winning_side_override,
                    notes=notes,
                )
            else:
                winning_side = winning_side_override or (
                    "UP" if float(final_price) >= float(target_price) else "DOWN"
                )
                result_payload = {
                    "window_start": int(market["window_start_ts"]),
                    "window_end": int(market["window_end_ts"]),
                    "market_id": str(market["market_id"]),
                    "target_price": float(target_price),
                    "final_btc_price": float(final_price),
                    "winning_side": winning_side,
                    "resolved": True,
                    "resolved_at": now,
                }
                self.db.upsert_window_result(result_payload)
                self.logger.window_resolved(result_payload)
                shape = self.live_executor.get_position(str(market["market_id"]))
                realized_pnl = shape.pnl_if_up if winning_side == "UP" else shape.pnl_if_down
                self.logger.window_pnl(
                    {
                        **result_payload,
                        **asdict(shape),
                        "realized_pnl": realized_pnl,
                        "notes": "settled_from_live_matched_audit",
                    }
                )
                settled = result_payload
            settled_any = settled_any or settled is not None

        if settled_any and bool(self.config.get_path("reports.enabled", True)):
            self.reporter.write_reports()

    def _final_price_for_window(self, bars: list[tuple[int, float]], window_end: int) -> Optional[float]:
        """Pick the Binance close nearest to Polymarket's window end."""
        if not bars:
            return None
        after = [price for second, price in bars if window_end <= second <= window_end + 15]
        if after:
            return after[0]
        before = [price for second, price in bars if window_end - 15 <= second < window_end]
        if before:
            return before[-1]
        return None

    async def _official_window_prices(self, window_start_ts: int, allow_poll: bool = True) -> Optional[dict[str, Any]]:
        """Return Polymarket's official Chainlink open/close prices for a window.

        Reuses the resolver's cached snapshot when available and only polls the
        official endpoint for windows that are old enough to be complete.
        Polling is throttled per window because the completed TWAP can take
        several minutes to appear after window end.
        """
        cached = self.resolver.crypto_price_by_window.get(window_start_ts)
        if cached and cached.get("completed") and cached.get("close_price") is not None:
            return cached
        now_ts = time.time()
        if int(now_ts) < window_start_ts + int(self.config.get_path("bot.interval_seconds", 300)):
            return cached
        poll_interval = max(5.0, float(self.config.get_path("polymarket.settlement_poll_seconds", 15.0)))
        if not allow_poll or now_ts - self._official_poll_ts.get(window_start_ts, 0.0) < poll_interval:
            return cached
        self._official_poll_ts[window_start_ts] = now_ts
        snapshot = await self.pm_api.fetch_crypto_price(window_start_ts)
        if snapshot:
            self.resolver.crypto_price_by_window[window_start_ts] = snapshot
            return snapshot
        return cached

    async def _shutdown(self) -> None:
        """Run final settlement, reports, validation, and storage maintenance."""
        await self._settle_finished_windows(time.time(), self.binance.snapshot())
        if bool(self.config.get_path("reports.enabled", True)):
            self.reporter.write_reports()
        if bool(self.config.get_path("validation.enabled", True)) and bool(self.config.get_path("validation.run_on_shutdown", True)):
            self.validator.validate_session()
        self._maintain_storage(time.time(), force=True)
