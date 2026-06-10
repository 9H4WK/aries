from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict
from typing import Optional

from .binance_ws import BinanceStream
from .config import Config
from .database import Database
from .growth_sizing import candidate_growth_score
from .indicators import compute_indicators
from .logger import JsonlLogger
from .market_resolver import MarketResolver
from .models import Candidate, Decision, Fill, MarketInfo, PositionShape
from .paper_broker import PaperBroker
from .polymarket_api import PolymarketApi, current_window_start
from .polymarket_ws import PolymarketQuoteStore, PolymarketStream
from .position_shape import apply_buy, dominant_side, shape_from_fills, weak_side
from .probability import estimate_probability
from .reporter import Reporter
from .risk import binance_fresh, quote_age_seconds, quote_fresh, scaled_risk_cap, valid_spread
from .sizing import size_for_price
from .validator import Validator


class AriesBot:
    def __init__(self, config: Config):
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
        )
        self.paper_broker = PaperBroker(config, self.db, self.logger)
        self.reporter = Reporter(config, self.db)
        self.validator = Validator(config, self.db, self.logger)
        self.binance = BinanceStream(config)
        self.pm_api = PolymarketApi(config)
        self.resolver = MarketResolver(config, self.pm_api)
        self.quote_store = PolymarketQuoteStore()
        self.pm_stream: Optional[PolymarketStream] = None
        self.pm_stream_market_id: Optional[str] = None
        self.fills_by_window: dict[tuple[str, int], list[Fill]] = {}
        self.last_persisted_decision_ts = 0.0
        self.last_persisted_state: Optional[str] = None
        self.last_storage_maintenance_ts = 0.0
        interval = int(config.get_path("bot.interval_seconds", 300))
        current_start = current_window_start(int(time.time()), interval)
        self.first_trade_window_start = (
            current_start + interval
            if bool(config.get_path("timing.require_clean_window", True))
            else current_start
        )

    def run(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        self.logger.info("starting_aries_phase2", mode=self.mode)
        await self.binance.start()
        try:
            while True:
                await self._cycle()
                await asyncio.sleep(float(self.config.get_path("bot.decision_interval_seconds", 1)))
        finally:
            await self.binance.stop()
            if self.pm_stream:
                await self.pm_stream.stop()
            self._shutdown()

    async def _cycle(self) -> None:
        now = time.time()
        binance_state = self.binance.snapshot()
        market = await self.resolver.resolve(binance_state)

        if market:
            self.db.upsert_market({**asdict(market), "updated_at": now})
            await self._ensure_pm_stream(market)

        self._settle_finished_windows(now, binance_state)

        decision, selected, selected_quote = self._decide(now, market, binance_state)

        if market and selected and decision.decision in {"BUY_UP", "BUY_DOWN"}:
            if self.mode == "paper":
                self._execute_paper_order(decision, market, selected, selected_quote)
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

    def _should_persist_decision(self, decision: Decision, now: float) -> bool:
        """Always retain meaningful changes; sample repetitive monitor snapshots."""
        sample_seconds = max(1.0, float(self.config.get_path("storage.decision_sample_seconds", 15)))
        important = (
            decision.decision in {"BUY_UP", "BUY_DOWN"}
            or decision.order_id is not None
            or decision.fill_id is not None
            or decision.order_status is not None
            or decision.strategy_state != self.last_persisted_state
        )
        return important or now - self.last_persisted_decision_ts >= sample_seconds

    def _maintain_storage(self, now: float, force: bool = False) -> None:
        interval = max(1.0, float(self.config.get_path("storage.maintenance_interval_seconds", 60)))
        if not force and now - self.last_storage_maintenance_ts < interval:
            return
        self.logger.maintain_storage()
        self.db.maintain_storage(int(self.config.get_path("storage.decision_rows_to_keep", 5000)))
        self.last_storage_maintenance_ts = now

    async def _ensure_pm_stream(self, market: MarketInfo) -> None:
        if self.pm_stream_market_id == market.market_id:
            return
        if self.pm_stream:
            await self.pm_stream.stop()
        self.pm_stream = PolymarketStream(
            self.config,
            [market.up_token_id, market.down_token_id],
            self.quote_store,
        )
        self.pm_stream_market_id = market.market_id
        await self.pm_stream.start()
        self.logger.info("subscribed_polymarket_market", market_id=market.market_id, slug=market.slug)

    def _record_fill(self, fill: Fill) -> int:
        key = (fill.market_id, fill.window_start_ts)
        self.fills_by_window.setdefault(key, []).append(fill)
        payload = asdict(fill)
        fill_id = self.db.insert_fill(payload)
        payload["id"] = fill_id
        self.logger.dry_fill(payload)
        return fill_id

    def _fills_for_window(self, market: MarketInfo):
        if self.mode == "paper":
            return self.paper_broker.get_fills(market.market_id, market.window_start_ts)
        return self.fills_by_window.get((market.market_id, market.window_start_ts), [])

    def _shape(self, market: MarketInfo) -> PositionShape:
        if self.mode == "paper":
            return self.paper_broker.get_position(market.market_id, market.window_start_ts)
        return shape_from_fills(self.fills_by_window.get((market.market_id, market.window_start_ts), []))

    def _decide(self, now: float, market: Optional[MarketInfo], binance_state) -> tuple[Decision, Optional[Candidate], Optional[object]]:
        decision_id = uuid.uuid4().hex
        empty_position = PositionShape()
        if market is None:
            return (
                self._decision_payload(
                    decision_id=decision_id,
                    now=now,
                    market=None,
                    binance_state=binance_state,
                    indicators=None,
                    prob=None,
                    up_q=None,
                    down_q=None,
                    pos_before=empty_position,
                    cand_up=None,
                    cand_down=None,
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
        # logger.debug("PROBABILITY %s", ...)

        # 5. Compare probabilities against PM asks to calculate edge.
        # 6. Reconstruct current paper position shape.
        position_before = self._shape(market)
        fills = list(self._fills_for_window(market))
        seconds_since_first_fill = self._seconds_since_first_fill(now, fills)
        average_entry_up, average_entry_down = self._average_entry_prices(fills)

        # 7. Build both UP and DOWN candidates.
        candidate_up = self._build_candidate("UP", market, up_q.ask, prob.p_up, position_before, fills)
        candidate_down = self._build_candidate("DOWN", market, down_q.ask, prob.p_down, position_before, fills)
        # logger.debug("CANDIDATE_UP %s", ...)
        # logger.debug("CANDIDATE_DOWN %s", ...)

        selected, decision, reason = self._select_candidate(
            now=now,
            market=market,
            binance_state=binance_state,
            up_q=up_q,
            down_q=down_q,
            prob=prob,
            shape=position_before,
            fills=fills,
            seconds_since_first_fill=seconds_since_first_fill,
            up=candidate_up,
            down=candidate_down,
        )
        selected_quote = up_q if selected and selected.side == "UP" else down_q if selected else None
        position_after = selected.shape_after if selected and selected.shape_after else position_before

        return (
            self._decision_payload(
                decision_id=decision_id,
                now=now,
                market=market,
                binance_state=binance_state,
                indicators=indicators,
                prob=prob,
                up_q=up_q,
                down_q=down_q,
                pos_before=position_before,
                cand_up=candidate_up,
                cand_down=candidate_down,
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

    def _build_candidate(self, side: str, market: MarketInfo, price: Optional[float], p_side: Optional[float], shape: PositionShape, fills) -> Candidate:
        if price is None or p_side is None:
            return Candidate(side=side, valid=False, reason="price_or_probability_missing")

        orders = len(fills)
        dom = dominant_side(shape)
        same_side_count = sum(1 for fill in fills if fill.side == side)

        if orders == 0:
            role = "FIRST_ENTRY"
        elif dom == side:
            role = "SAME_SIDE_ADD"
        else:
            role = "REPAIR"

        # 8. Use ladder sizing to find candidate notional.
        starting_balance = float(self.config.get_path("account.balance_usdc", 0.0))
        bankroll = starting_balance + self.db.total_realized_pnl()
        max_window_notional = scaled_risk_cap(
            self.config,
            float(self.config.get_path("risk.max_window_notional_usdc", 3.0)),
            bankroll,
        )
        sizing = size_for_price(
            self.config,
            price,
            role,
            orders,
            same_side_count,
            side=side,
            p_side=p_side,
            shape=shape,
            bankroll=bankroll,
            starting_bankroll=starting_balance,
            max_window_notional_usdc=max_window_notional,
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

    def _select_candidate(self, now: float, market: MarketInfo, binance_state, up_q, down_q, prob, shape: PositionShape, fills, seconds_since_first_fill, up: Candidate, down: Candidate) -> tuple[Optional[Candidate], str, str]:
        seconds_since_open = int(now) - market.window_start_ts
        seconds_remaining = market.window_end_ts - int(now)

        if market.window_start_ts < self.first_trade_window_start:
            return None, "WATCH", "wait_for_clean_window"
        if market.target_price is None:
            return None, "SKIP", "target_missing"
        if not binance_fresh(binance_state, float(self.config.get_path("binance.max_state_age_seconds", 3))):
            return None, "SKIP", "binance_stale"
        max_quote_age = float(self.config.get_path("polymarket.max_quote_age_seconds", 3))
        if not quote_fresh(up_q, max_quote_age) or not quote_fresh(down_q, max_quote_age):
            return None, "SKIP", "pm_quote_missing_or_stale"
        if prob.p_up is None or prob.p_down is None:
            return None, "SKIP", prob.reason
        if prob.inside_noise_band:
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

        orders = len(fills)
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
        if shape.locked:
            return self._select_post_lock_add(up, down, shape, prob)
        repair_delay = int(self.config.get_path("timing.repair_start_after_first_entry_seconds", 24))
        if seconds_since_first_fill is not None and seconds_since_first_fill < repair_delay:
            return None, "WATCH", "one_sided_waiting_for_repair_delay"
        preferred_repair_delay = int(self.config.get_path("timing.preferred_repair_after_first_entry_seconds", 38))
        if seconds_since_first_fill is not None and seconds_since_first_fill < preferred_repair_delay:
            return self._select_lock_only(up, down)
        return self._select_after_entry(up, down, shape, prob)

    def _select_first_entry(self, up: Candidate, down: Candidate, prob, seconds_since_open: int, seconds_remaining: int) -> tuple[Optional[Candidate], str, str]:
        min_edge = float(self.config.get_path("edge.min_edge", 0.07))
        min_conf = float(self.config.get_path("edge.min_confidence", 0.10))
        if prob.confidence < min_conf:
            return None, "SKIP", "confidence_too_low"

        valid = [candidate for candidate in (up, down) if candidate.valid and candidate.edge is not None and candidate.edge >= min_edge]
        valid = [candidate for candidate in valid if self._risk_caps_pass(candidate)]
        if not valid:
            return None, "SKIP", "no_edge"
        if bool(self.config.get_path("growth_sizing.use_growth_score_for_first_entry", True)):
            selected = max(
                valid,
                key=lambda candidate: (
                    candidate.growth_score if candidate.growth_score is not None else -999.0,
                    candidate.edge or -999.0,
                ),
            )
        else:
            selected = max(valid, key=lambda candidate: candidate.edge or -999.0)

        preferred = int(self.config.get_path("timing.preferred_first_entry_seconds", 30))
        if seconds_since_open < preferred:
            early_edge = float(self.config.get_path("entry.early_min_edge", 0.12))
            early_confidence = float(self.config.get_path("entry.early_min_confidence", 0.50))
            if (selected.edge or -999.0) < early_edge or prob.confidence < early_confidence:
                return None, "WATCH", "early_entry_waiting_for_conviction"

        endgame = int(self.config.get_path("timing.endgame_seconds_remaining", 30))
        if seconds_remaining <= endgame:
            strong_edge = float(self.config.get_path("edge.strong_edge", 0.12))
            strong_confidence = float(self.config.get_path("edge.strong_confidence", 0.30))
            if (selected.edge or -999.0) < strong_edge or prob.confidence < strong_confidence:
                return None, "SKIP", "endgame_first_entry_not_strong_enough"

        return selected, f"BUY_{selected.side}", "first_directional_entry"

    def _select_after_entry(self, up: Candidate, down: Candidate, shape: PositionShape, prob) -> tuple[Optional[Candidate], str, str]:
        dom = dominant_side(shape)
        weak = weak_side(shape)
        min_improve = float(self.config.get_path("risk.min_shape_improvement_usdc", 0.01))
        allow_repair_negative = bool(self.config.get_path("risk.allow_repair_at_small_negative_edge", False))
        max_repair_neg = float(self.config.get_path("risk.max_repair_negative_edge", 0.02))

        valid = [candidate for candidate in (up, down) if candidate.valid and self._risk_caps_pass(candidate)]

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

        max_same_side_adds = int(self.config.get_path("risk.max_same_side_adds", 0))
        if dom and max_same_side_adds > 0 and prob.confidence >= float(self.config.get_path("edge.strong_confidence", 0.30)):
            strong_edge = float(self.config.get_path("edge.strong_edge", 0.12))
            same_side_candidates = [candidate for candidate in valid if candidate.side == dom and (candidate.edge or -999.0) >= strong_edge]
            if same_side_candidates:
                selected = max(same_side_candidates, key=lambda candidate: candidate.edge or -999.0)
                return selected, f"BUY_{selected.side}", "strong_same_side_add"

        return None, "HOLD_EXPOSED", "no_repair_or_lock_available"

    def _select_lock_only(self, up: Candidate, down: Candidate) -> tuple[Optional[Candidate], str, str]:
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

    def _risk_caps_pass(self, candidate: Candidate) -> bool:
        if candidate.shape_after is None:
            return False
        current_balance = float(self.config.get_path("account.balance_usdc", 0.0)) + self.db.total_realized_pnl()
        max_notional = scaled_risk_cap(
            self.config,
            float(self.config.get_path("risk.max_window_notional_usdc", 3.0)),
            current_balance,
        )
        max_loss = scaled_risk_cap(
            self.config,
            float(self.config.get_path("risk.max_window_loss_usdc", 1.0)),
            current_balance,
        )
        if candidate.shape_after.total_cost > max_notional:
            candidate.valid = False
            candidate.reason = "max_window_notional"
            return False
        if candidate.shape_after.worst_case_pnl < -max_loss:
            candidate.valid = False
            candidate.reason = "max_window_loss"
            return False
        return True

    def _execute_dry_run_fill(self, decision: Decision, market: MarketInfo, selected: Candidate) -> None:
        fill = Fill(
            ts=decision.timestamp,
            market_id=market.market_id,
            window_start_ts=market.window_start_ts,
            side=selected.side,
            price=float(selected.price or 0.0),
            notional_usdc=float(selected.notional_usdc or 0.0),
            shares=float(selected.shares or 0.0),
            reason=decision.selected_reason,
        )
        decision.fill_id = self._record_fill(fill)
        decision.order_status = "DRY_RUN"
        decision.order_reason = "dry_run_fill_recorded"
        decision.position_after = asdict(self._shape(market))
        decision.average_entry_up, decision.average_entry_down = self._average_entry_prices(self._fills_for_window(market))

    def _execute_paper_order(self, decision: Decision, market: MarketInfo, selected: Candidate, selected_quote) -> None:
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
            action="BUY",
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

    def _decision_payload(self, decision_id, now, market, binance_state, indicators, prob, up_q, down_q, pos_before, cand_up, cand_down, selected, decision, reason, pos_after, seconds_since_first_fill=None, average_entry_up=None, average_entry_down=None) -> Decision:
        seconds_since_open = None
        seconds_remaining = None
        if market:
            seconds_since_open = int(now) - market.window_start_ts
            seconds_remaining = market.window_end_ts - int(now)

        starting_balance = float(self.config.get_path("account.balance_usdc", 0.0))
        realized_pnl = self.db.total_realized_pnl()
        win_rate_percent = self.db.win_rate_percent()
        spot_target_ratio = None
        if market and market.target_price and binance_state.price is not None:
            spot_target_ratio = binance_state.price / market.target_price
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
            btc_price=binance_state.price,
            ma_fast=indicators.ma_fast if indicators else None,
            ma_mid=indicators.ma_mid if indicators else None,
            ma_slow=indicators.ma_slow if indicators else None,
            ma_pressure=indicators.pressure if indicators else None,
            ma_fast_slope_bps=indicators.ma_fast_slope_bps if indicators else None,
            ma_mid_slope_bps=indicators.ma_mid_slope_bps if indicators else None,
            ma_velocity_5s_bps=indicators.velocity_5s_bps if indicators else None,
            ma_velocity_15s_bps=indicators.velocity_15s_bps if indicators else None,
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
            selected_side=selected.side if selected else None,
            decision=decision,
            selected_reason=reason,
            position_after=asdict(pos_after),
        )

    def _strategy_state(self, market, shape, decision, reason, seconds_since_open, seconds_remaining) -> str:
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
        if shape.total_cost <= 0:
            return "LOOKING_FOR_FIRST_ENTRY"
        if reason in {"one_sided_waiting_for_repair_delay", "repair_waiting_for_preferred_delay"}:
            return "ONE_SIDED_EXPOSED"
        if decision == "HOLD_EXPOSED":
            return "HOLD_EXPOSED"
        return "REPAIR_SEARCH"

    def _post_execution_state(self, decision: Decision) -> str:
        shape = decision.position_after
        if shape.get("locked"):
            return "LOCKED"
        if decision.order_status in {"FILLED", "DRY_RUN"} and shape.get("total_cost", 0.0) > 0:
            return "ONE_SIDED_EXPOSED" if decision.selected_reason == "first_directional_entry" else "REPAIR_SEARCH"
        return decision.strategy_state

    def _seconds_since_first_fill(self, now: float, fills) -> Optional[float]:
        if not fills:
            return None
        first_fill_ts = min(float(getattr(fill, "created_at", getattr(fill, "ts", now))) for fill in fills)
        return max(0.0, now - first_fill_ts)

    def _average_entry_prices(self, fills) -> tuple[Optional[float], Optional[float]]:
        side_cost = {"UP": 0.0, "DOWN": 0.0}
        side_shares = {"UP": 0.0, "DOWN": 0.0}
        for fill in fills:
            side = str(fill.side)
            if side not in side_cost:
                continue
            side_cost[side] += float(fill.notional_usdc)
            side_shares[side] += float(fill.shares)
        average_up = side_cost["UP"] / side_shares["UP"] if side_shares["UP"] > 0 else None
        average_down = side_cost["DOWN"] / side_shares["DOWN"] if side_shares["DOWN"] > 0 else None
        return average_up, average_down

    def _settle_finished_windows(self, now: float, binance_state) -> None:
        if self.mode != "paper":
            return

        settled_any = False
        for market in self.db.list_unresolved_markets(now):
            target_price = market.get("target_price")
            final_price = self._final_price_for_window(binance_state.bars, int(market["window_end_ts"]))
            if target_price is None or final_price is None:
                continue
            settled = self.paper_broker.settle_window(
                timestamp=now,
                window_start=int(market["window_start_ts"]),
                window_end=int(market["window_end_ts"]),
                market_id=str(market["market_id"]),
                target_price=float(target_price),
                final_btc_price=float(final_price),
                notes="settled_from_binance_close_near_window_end",
            )
            settled_any = settled_any or settled is not None

        if settled_any and bool(self.config.get_path("reports.enabled", True)):
            self.reporter.write_reports()

    def _final_price_for_window(self, bars: list[tuple[int, float]], window_end: int) -> Optional[float]:
        if not bars:
            return None
        after = [price for second, price in bars if window_end <= second <= window_end + 15]
        if after:
            return after[0]
        before = [price for second, price in bars if window_end - 15 <= second < window_end]
        if before:
            return before[-1]
        return None

    def _shutdown(self) -> None:
        self._settle_finished_windows(time.time(), self.binance.snapshot())
        if bool(self.config.get_path("reports.enabled", True)):
            self.reporter.write_reports()
        if bool(self.config.get_path("validation.enabled", True)) and bool(self.config.get_path("validation.run_on_shutdown", True)):
            self.validator.validate_session()
        self._maintain_storage(time.time(), force=True)
