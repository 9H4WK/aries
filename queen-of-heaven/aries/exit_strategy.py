from __future__ import annotations

from typing import Optional

from .models import Candidate, IndicatorState, PositionShape
from .position_shape import apply_sell, average_open_entry, dominant_side
from .queen_of_heaven import stake_return


def build_inventory_sell_candidate(
    config: dict,
    *,
    mode: str,
    shape: PositionShape,
    fills: list,
    indicators: IndicatorState,
    p_up: Optional[float],
    p_down: Optional[float],
    up_bid: Optional[float],
    down_bid: Optional[float],
    up_spread: Optional[float],
    down_spread: Optional[float],
    now_ts: float,
    seconds_since_open: int,
    seconds_remaining: int,
) -> Optional[Candidate]:
    """Return the highest-priority valid SELL candidate, if one exists.

    Enabled sell roles are independent:
    - dominant_inventory_trim_enabled controls worst-case-improving dominant trims.
    - directional_weak_side_exit_enabled controls capped weak-side exits.

    When both are enabled, risk-reducing dominant-side trims keep priority.

    Args:
        config: Loaded `config.yaml` settings.
        mode: Current bot mode: dry_run, paper, live_monitor, or live.
        shape: Current UP/DOWN inventory and PnL shape before selling.
        fills: Stored fills for this market window.
        indicators: Binance MA, slope, volume, and stability state.
        p_up: Aries model probability that UP wins.
        p_down: Aries model probability that DOWN wins.
        up_bid: Current executable PM bid for selling UP.
        down_bid: Current executable PM bid for selling DOWN.
        up_spread: Current PM UP ask-bid spread.
        down_spread: Current PM DOWN ask-bid spread.
        now_ts: Current strategy cycle timestamp.
        seconds_since_open: Seconds elapsed in this 5-minute window.
        seconds_remaining: Seconds left before the window closes.
    """
    sell_config = config.get("intra_window_sells", {})
    if not bool(sell_config.get("enabled", False)):
        return None
    if mode == "live" and not bool(config.get("live", {}).get("allow_sell_orders", False)):
        return None
    if seconds_remaining <= 0:
        return None
    allowed_modes = {str(value) for value in sell_config.get("modes", ["paper"])}
    if mode not in allowed_modes:
        return None
    if shape.locked and not (
        bool(sell_config.get("allow_sell_locked", False))
        or bool(sell_config.get("directional_allow_sell_locked", False))
    ):
        return None

    sell_fills = [fill for fill in fills if str(getattr(fill, "action", "BUY")).upper() == "SELL"]
    if len(sell_fills) >= int(sell_config.get("max_sells_per_window", 3)):
        return None
    if sell_fills:
        last_sell_ts = max(float(getattr(fill, "created_at", getattr(fill, "ts", 0.0))) for fill in sell_fills)
        if now_ts - last_sell_ts < float(sell_config.get("cooldown_seconds", 15)):
            return None

    trim = _build_profit_or_stop_trim(
        config=config,
        shape=shape,
        fills=fills,
        indicators=indicators,
        p_up=p_up,
        p_down=p_down,
        up_bid=up_bid,
        down_bid=down_bid,
        up_spread=up_spread,
        down_spread=down_spread,
        seconds_since_open=seconds_since_open,
        seconds_remaining=seconds_remaining,
    )
    if trim is not None:
        return trim

    if bool(sell_config.get("dominant_inventory_trim_enabled", True)):
        inventory_trim = _build_dominant_inventory_trim(
            config=config,
            shape=shape,
            indicators=indicators,
            p_up=p_up,
            p_down=p_down,
            up_bid=up_bid,
            down_bid=down_bid,
            up_spread=up_spread,
            down_spread=down_spread,
            seconds_since_open=seconds_since_open,
            seconds_remaining=seconds_remaining,
        )
        if inventory_trim is not None:
            return inventory_trim

    if bool(sell_config.get("directional_weak_side_exit_enabled", False)):
        return _build_directional_weak_side_exit(
            config=config,
            shape=shape,
            indicators=indicators,
            p_up=p_up,
            p_down=p_down,
            up_bid=up_bid,
            down_bid=down_bid,
            up_spread=up_spread,
            down_spread=down_spread,
            seconds_since_open=seconds_since_open,
            seconds_remaining=seconds_remaining,
        )
    return None


def build_inventory_sell_probe(
    config: dict,
    *,
    mode: str,
    shape: PositionShape,
    fills: list,
    indicators: IndicatorState,
    p_up: Optional[float],
    p_down: Optional[float],
    up_bid: Optional[float],
    down_bid: Optional[float],
    up_spread: Optional[float],
    down_spread: Optional[float],
    now_ts: float,
    seconds_since_open: int,
    seconds_remaining: int,
) -> dict:
    """Return terminal-only sell diagnostics without approving an order.

    Args:
        config: Loaded `config.yaml` settings.
        mode: Current bot mode.
        shape: Current position before any new sell.
        fills: Stored fills for this window.
        indicators: Latest Binance-derived indicator state.
        p_up: Aries model probability for UP.
        p_down: Aries model probability for DOWN.
        up_bid: PM bid available for selling UP.
        down_bid: PM bid available for selling DOWN.
        up_spread: PM UP quote spread.
        down_spread: PM DOWN quote spread.
        now_ts: Current strategy timestamp.
        seconds_since_open: Window age in seconds.
        seconds_remaining: Time left in the window.
    """
    sell_config = config.get("intra_window_sells", {})
    live_sell_armed = mode != "live" or bool(config.get("live", {}).get("allow_sell_orders", False))
    executable = build_inventory_sell_candidate(
        config,
        mode=mode,
        shape=shape,
        fills=fills,
        indicators=indicators,
        p_up=p_up,
        p_down=p_down,
        up_bid=up_bid,
        down_bid=down_bid,
        up_spread=up_spread,
        down_spread=down_spread,
        now_ts=now_ts,
        seconds_since_open=seconds_since_open,
        seconds_remaining=seconds_remaining,
    )
    dominant = _dominant_sell_probe(
        config,
        shape=shape,
        indicators=indicators,
        p_up=p_up,
        p_down=p_down,
        up_bid=up_bid,
        down_bid=down_bid,
        up_spread=up_spread,
        down_spread=down_spread,
        seconds_since_open=seconds_since_open,
        seconds_remaining=seconds_remaining,
        live_sell_armed=live_sell_armed,
    )
    weak = _weak_side_sell_probe(
        config,
        shape=shape,
        indicators=indicators,
        p_up=p_up,
        p_down=p_down,
        up_bid=up_bid,
        down_bid=down_bid,
        up_spread=up_spread,
        down_spread=down_spread,
        seconds_since_open=seconds_since_open,
        seconds_remaining=seconds_remaining,
        live_sell_armed=live_sell_armed,
    )
    profit = _profit_stop_sell_probe(
        config,
        shape=shape,
        fills=fills,
        indicators=indicators,
        p_up=p_up,
        p_down=p_down,
        up_bid=up_bid,
        down_bid=down_bid,
        up_spread=up_spread,
        down_spread=down_spread,
        seconds_since_open=seconds_since_open,
        seconds_remaining=seconds_remaining,
        live_sell_armed=live_sell_armed,
    )
    sell_fills = [fill for fill in fills if str(getattr(fill, "action", "BUY")).upper() == "SELL"]
    in_cooldown = False
    if sell_fills:
        last_sell_ts = max(float(getattr(fill, "created_at", getattr(fill, "ts", 0.0))) for fill in sell_fills)
        in_cooldown = now_ts - last_sell_ts < float(sell_config.get("cooldown_seconds", 15))

    if not bool(sell_config.get("enabled", False)):
        global_reason = "intra_window_sells_disabled"
    elif mode not in {str(value) for value in sell_config.get("modes", ["paper"])}:
        global_reason = "mode_not_allowed_for_sells"
    elif not live_sell_armed:
        global_reason = "live_sell_orders_not_armed"
    elif seconds_remaining <= 0:
        global_reason = "window_expired"
    elif len(sell_fills) >= int(sell_config.get("max_sells_per_window", 3)):
        global_reason = "max_sells_per_window"
    elif in_cooldown:
        global_reason = "sell_cooldown"
    elif shape.locked and not (
        bool(sell_config.get("allow_sell_locked", False))
        or bool(sell_config.get("directional_allow_sell_locked", False))
    ):
        global_reason = "locked_sell_disabled"
    else:
        global_reason = "watching_sell_gates"

    return {
        "valid": executable.valid if executable else False,
        "action": executable.action if executable else "SELL",
        "side": executable.side if executable else (profit.get("side") or dominant.get("side") or weak.get("side")),
        "role": executable.role if executable else profit.get("role") or dominant.get("role") or weak.get("role"),
        "reason": executable.reason if executable else global_reason,
        "profit_stop": profit,
        "dominant": dominant,
        "weak_side": weak,
        "selected": _candidate_probe(executable) if executable else {},
    }


def _build_profit_or_stop_trim(
    *,
    config: dict,
    shape: PositionShape,
    fills: list,
    indicators: IndicatorState,
    p_up: Optional[float],
    p_down: Optional[float],
    up_bid: Optional[float],
    down_bid: Optional[float],
    up_spread: Optional[float],
    down_spread: Optional[float],
    seconds_since_open: int,
    seconds_remaining: int,
) -> Optional[Candidate]:
    """Sell a profitable leg or an invalidated leg using tiered exits.

    Args:
        config: Loaded `config.yaml` settings.
        shape: Current inventory shape before the sell.
        fills: Stored fills used to compute average entry and prior trims.
        indicators: Stability scores and MA state.
        p_up: Aries model probability for UP.
        p_down: Aries model probability for DOWN.
        up_bid: Current PM bid for selling UP.
        down_bid: Current PM bid for selling DOWN.
        up_spread: UP quote spread.
        down_spread: DOWN quote spread.
        seconds_since_open: Window age in seconds.
        seconds_remaining: Time left in the window.
    """
    sell_config = config.get("intra_window_sells", {})
    if not bool(sell_config.get("profit_trim_enabled", False)) and not bool(sell_config.get("stop_trim_enabled", False)):
        return None
    if seconds_since_open < int(sell_config.get("profit_trim_min_seconds_since_open", 30)):
        return None
    if seconds_remaining < int(sell_config.get("profit_trim_min_seconds_remaining", 20)):
        return None

    choices: list[tuple[float, Candidate]] = []
    for side in ("UP", "DOWN"):
        held = shape.up_shares if side == "UP" else shape.down_shares
        if held <= 0:
            continue
        avg_entry = _average_buy_entry(fills, side)
        if avg_entry is None or avg_entry <= 0:
            continue
        bid = up_bid if side == "UP" else down_bid
        spread = up_spread if side == "UP" else down_spread
        probability = p_up if side == "UP" else p_down
        opposite_probability = p_down if side == "UP" else p_up
        stability = indicators.up_stability_score if side == "UP" else indicators.down_stability_score
        opposite_stability = indicators.down_stability_score if side == "UP" else indicators.up_stability_score
        if bid is None or bid <= 0 or bid >= 1:
            continue
        if spread is None or spread < 0 or spread > float(sell_config.get("max_exit_spread", 0.10)):
            continue
        if bid < float(sell_config.get("min_exit_bid", 0.05)):
            continue

        profit_per_share = bid - avg_entry
        profit_fraction = stake_return(avg_entry, bid) or 0.0
        role = None
        reason = None
        fraction = 0.0
        priority = 0.0
        trim_count = _sell_role_count(fills, side, {"PROFIT_TRIM"})

        if bool(sell_config.get("profit_trim_enabled", False)):
            use_stake_return = bool(sell_config.get("profit_trim_use_stake_return_targets", False))
            tp3 = float(
                sell_config.get(
                    "profit_trim_tp3_stake_return" if use_stake_return else "profit_trim_tp3_delta",
                    0.18 if use_stake_return else 0.25,
                )
            )
            tp2 = float(
                sell_config.get(
                    "profit_trim_tp2_stake_return" if use_stake_return else "profit_trim_tp2_delta",
                    0.15 if use_stake_return else 0.12,
                )
            )
            tp1 = float(
                sell_config.get(
                    "profit_trim_tp1_stake_return" if use_stake_return else "profit_trim_tp1_delta",
                    0.10 if use_stake_return else 0.06,
                )
            )
            profit_metric = profit_fraction if use_stake_return else profit_per_share
            weak_max = int(sell_config.get("profit_trim_weak_stability_max_score", 3))
            runner_score = int(sell_config.get("profit_trim_runner_stability_score", 5))
            require_weakness = bool(sell_config.get("profit_trim_require_weakness_for_all_tiers", False))
            side_weakening = stability <= weak_max
            if profit_metric >= tp3 and trim_count < 3 and (side_weakening or not require_weakness):
                # TP3: large profit; sell most only after weakness if that safety is enabled.
                role = "PROFIT_TRIM"
                reason = "profit_trim_tp3"
                fraction = float(
                    sell_config.get(
                        "profit_trim_tp3_strong_fraction" if stability >= runner_score else "profit_trim_tp3_fraction",
                        0.50 if stability >= runner_score else 0.80,
                    )
                )
                priority = 300.0 + profit_per_share
            elif profit_metric >= tp2 and trim_count < 2 and (side_weakening or not require_weakness):
                # TP2: medium profit; take partial profit once trend is no longer clean.
                role = "PROFIT_TRIM"
                reason = "profit_trim_tp2"
                fraction = float(sell_config.get("profit_trim_tp2_fraction", 0.50))
                priority = 200.0 + profit_per_share
            elif profit_metric >= tp1 and side_weakening and trim_count < 1:
                # TP1: small profit; requires weakening so Aries does not exit too early.
                role = "PROFIT_TRIM"
                reason = "profit_trim_tp1_weakening"
                fraction = float(sell_config.get("profit_trim_tp1_fraction", 0.30))
                priority = 100.0 + profit_per_share

        if role is None and bool(sell_config.get("stop_trim_enabled", False)):
            soft_loss = -abs(float(sell_config.get("stop_trim_soft_loss_delta", 0.07)))
            hard_loss = -abs(float(sell_config.get("stop_trim_hard_loss_delta", 0.15)))
            disaster_bid = float(sell_config.get("stop_trim_disaster_bid", 0.15))
            opposite_min = int(sell_config.get("stop_trim_opposite_stability_min_score", 4))
            late_seconds = int(sell_config.get("stop_trim_late_seconds_remaining", 90))
            if bid <= disaster_bid and opposite_stability >= opposite_min and seconds_remaining <= late_seconds:
                # Disaster stop: very low bid, late window, and opposite side is confirmed.
                role = "STOP_TRIM"
                reason = "stop_trim_disaster"
                fraction = float(sell_config.get("stop_trim_disaster_fraction", 1.00))
                priority = 90.0 + abs(profit_per_share)
            elif profit_per_share <= hard_loss and opposite_stability >= opposite_min:
                # Hard stop: large loss and opposite side confirms invalidation.
                role = "STOP_TRIM"
                reason = "stop_trim_hard_invalidation"
                fraction = float(sell_config.get("stop_trim_hard_fraction", 0.80))
                priority = 80.0 + abs(profit_per_share)
            elif profit_per_share <= soft_loss and opposite_stability >= opposite_min:
                # Soft stop: smaller loss, but only with opposite-side confirmation.
                role = "STOP_TRIM"
                reason = "stop_trim_soft_invalidation"
                fraction = float(sell_config.get("stop_trim_soft_fraction", 0.50))
                priority = 70.0 + abs(profit_per_share)

        if role is None:
            continue

        shares = held * max(0.0, min(1.0, fraction))
        min_shares = max(
            float(config.get("sizing", {}).get("min_order_shares", 1.0)),
            float(sell_config.get("min_order_shares", 1.0)),
        )
        full_exit_for_dust = False
        if 0 < held - shares < min_shares and bool(sell_config.get("profit_trim_allow_full_exit_for_dust", True)):
            shares = held
            full_exit_for_dust = True
        if shares < min_shares:
            continue
        proceeds = shares * bid
        if proceeds < float(sell_config.get("min_exit_notional_usdc", 0.25)):
            continue
        shape_after = apply_sell(shape, side, shares, proceeds)
        deterioration = max(0.0, shape.worst_case_pnl - shape_after.worst_case_pnl)
        max_deterioration = float(sell_config.get("profit_trim_max_worst_case_deterioration_usdc", 3.0))
        if deterioration > max_deterioration + 1e-9:
            continue
        max_post_loss = float(
            sell_config.get(
                "profit_trim_max_post_sell_worst_case_loss_usdc",
                config.get("risk", {}).get("max_window_loss_usdc", 1.0),
            )
        )
        if shape_after.worst_case_pnl < -max_post_loss:
            continue
        choices.append(
            (
                priority,
                Candidate(
                    side=side,
                    action="SELL",
                    valid=True,
                    reason=f"{reason}_full_exit_to_avoid_dust" if full_exit_for_dust else reason,
                    role=role,
                    price=bid,
                    notional_usdc=proceeds,
                    shares=shares,
                    shape_after=shape_after,
                    shape_improvement=shape_after.worst_case_pnl - shape.worst_case_pnl,
                    would_lock=shape_after.locked,
                    min_shares_check_passed=True,
                    stability_score=stability,
                    opposite_stability_score=opposite_stability,
                    held_shares_before=held,
                    sell_fraction=(shares / held) if held > 0 else 0.0,
                    model_probability=probability,
                    opposite_probability=opposite_probability,
                    model_exit_edge=bid - probability if probability is not None else None,
                    worst_case_deterioration=deterioration,
                ),
            )
        )

    if not choices:
        return None
    return max(choices, key=lambda item: item[0])[1]


def _build_dominant_inventory_trim(
    *,
    config: dict,
    shape: PositionShape,
    indicators: IndicatorState,
    p_up: Optional[float],
    p_down: Optional[float],
    up_bid: Optional[float],
    down_bid: Optional[float],
    up_spread: Optional[float],
    down_spread: Optional[float],
    seconds_since_open: int,
    seconds_remaining: int,
) -> Optional[Candidate]:
    """Sell oversized inventory only when the trade improves worst-case PnL.

    Args:
        config: Loaded `config.yaml` settings.
        shape: Current inventory shape before the sell.
        indicators: Stability scores and MA state used for exit timing.
        p_up: Model probability for UP.
        p_down: Model probability for DOWN.
        up_bid: Executable sell price for UP.
        down_bid: Executable sell price for DOWN.
        up_spread: UP quote spread.
        down_spread: DOWN quote spread.
        seconds_since_open: Window age in seconds.
        seconds_remaining: Time left in the window.
    """
    sell_config = config.get("intra_window_sells", {})
    if not bool(sell_config.get("dominant_inventory_trim_enabled", True)):
        return None
    if shape.locked and not bool(sell_config.get("allow_sell_locked", False)):
        return None
    if seconds_since_open < int(sell_config.get("min_seconds_since_open", 60)):
        return None
    if bool(sell_config.get("require_anchor_ready", True)):
        if indicators.ma_anchor is None or indicators.ma_anchor_slope_bps is None:
            return None

    side = dominant_side(shape)
    if side is None:
        return None
    held = shape.up_shares if side == "UP" else shape.down_shares
    opposite_held = shape.down_shares if side == "UP" else shape.up_shares
    bid = up_bid if side == "UP" else down_bid
    spread = up_spread if side == "UP" else down_spread
    probability = p_up if side == "UP" else p_down
    stability = indicators.up_stability_score if side == "UP" else indicators.down_stability_score
    opposite_stability = indicators.down_stability_score if side == "UP" else indicators.up_stability_score

    if probability is None:
        return None
    if bid is None or bid <= 0 or bid >= 1:
        return None
    if spread is None or spread < 0 or spread > float(sell_config.get("max_exit_spread", 0.10)):
        return None
    if bid < float(sell_config.get("min_exit_bid", 0.05)):
        return None
    if probability - bid > float(sell_config.get("max_probability_premium_sacrifice", 0.15)):
        return None

    weak_max = int(sell_config.get("weak_stability_max_score", 3))
    unstable_max = int(sell_config.get("unstable_stability_max_score", 1))
    flip_min = int(sell_config.get("confirmed_flip_min_opposite_score", 4))
    late_seconds = int(sell_config.get("late_seconds_remaining", 90))
    final_seconds = int(sell_config.get("final_seconds_remaining", 45))
    confirmed_flip = stability <= unstable_max and opposite_stability >= flip_min
    weak_late = seconds_remaining <= late_seconds and stability <= weak_max
    loss_trigger = shape.worst_case_pnl <= -float(sell_config.get("min_worst_case_loss_to_trim_usdc", 0.25))

    if seconds_remaining <= final_seconds and stability <= weak_max:
        reason = "final_weak_inventory_trim"
        max_fraction = float(sell_config.get("final_max_sell_fraction", 1.0))
    elif confirmed_flip:
        reason = "confirmed_flip_inventory_trim"
        max_fraction = float(sell_config.get("flip_max_sell_fraction", 0.50))
    elif weak_late:
        reason = "late_weak_inventory_trim"
        max_fraction = float(sell_config.get("late_max_sell_fraction", 0.50))
    elif loss_trigger and stability <= weak_max:
        reason = "exposed_weak_inventory_trim"
        max_fraction = float(sell_config.get("middle_max_sell_fraction", 0.25))
    else:
        return None

    if stability >= int(sell_config.get("stable_min_score", 4)) and not confirmed_flip:
        return None

    target_tilt = (
        float(sell_config.get("unstable_target_tilt_shares", 0.0))
        if stability <= unstable_max
        else float(sell_config.get("weak_target_tilt_shares", 2.0))
    )
    current_tilt = held - opposite_held
    desired = max(0.0, current_tilt - target_tilt)
    shares = min(desired, held * max(0.0, min(1.0, max_fraction)))
    min_shares = max(
        float(config.get("sizing", {}).get("min_order_shares", 1.0)),
        float(sell_config.get("min_order_shares", 1.0)),
    )
    if 0 < held - shares < min_shares:
        shares = held
    if shares < min_shares:
        return None

    proceeds = shares * bid
    if proceeds < float(sell_config.get("min_exit_notional_usdc", 0.25)):
        return None
    shape_after = apply_sell(shape, side, shares, proceeds)
    improvement = shape_after.worst_case_pnl - shape.worst_case_pnl
    if bool(sell_config.get("require_worst_case_improvement", True)):
        if improvement < float(sell_config.get("min_worst_case_improvement_usdc", 0.02)):
            return None

    return Candidate(
        side=side,
        action="SELL",
        valid=True,
        reason=reason,
        role="INVENTORY_TRIM",
        price=bid,
        notional_usdc=proceeds,
        shares=shares,
        shape_after=shape_after,
        shape_improvement=improvement,
        would_lock=shape_after.locked,
        min_shares_check_passed=True,
        stability_score=stability,
        opposite_stability_score=opposite_stability,
        target_tilt_shares=target_tilt,
        held_shares_before=held,
        sell_fraction=(shares / held) if held > 0 else 0.0,
        model_probability=probability,
        opposite_probability=p_down if side == "UP" else p_up,
        model_exit_edge=bid - probability,
        worst_case_deterioration=max(0.0, shape.worst_case_pnl - shape_after.worst_case_pnl),
    )


def _build_directional_weak_side_exit(
    *,
    config: dict,
    shape: PositionShape,
    indicators: IndicatorState,
    p_up: Optional[float],
    p_down: Optional[float],
    up_bid: Optional[float],
    down_bid: Optional[float],
    up_spread: Optional[float],
    down_spread: Optional[float],
    seconds_since_open: int,
    seconds_remaining: int,
) -> Optional[Candidate]:
    """Sell low-conviction smaller inventory when its bid exceeds model value.

    This intentionally increases directional exposure, so it has independent
    confirmation and downside caps from the worst-case-improving trim.

    Args:
        config: Loaded `config.yaml` settings.
        shape: Current inventory shape before the sell.
        indicators: Stability scores and MA state.
        p_up: Aries model probability for UP.
        p_down: Aries model probability for DOWN.
        up_bid: Current PM bid for selling UP.
        down_bid: Current PM bid for selling DOWN.
        up_spread: UP quote spread.
        down_spread: DOWN quote spread.
        seconds_since_open: Window age in seconds.
        seconds_remaining: Time left before close.
    """
    sell_config = config.get("intra_window_sells", {})
    if not bool(sell_config.get("directional_weak_side_exit_enabled", False)):
        return None
    if shape.locked and not bool(sell_config.get("directional_allow_sell_locked", False)):
        return None
    if seconds_since_open < int(sell_config.get("directional_min_seconds_since_open", 0)):
        return None
    if seconds_remaining < int(sell_config.get("directional_min_seconds_remaining", 5)):
        return None
    if bool(sell_config.get("directional_require_anchor_ready", False)):
        if indicators.ma_anchor is None or indicators.ma_anchor_slope_bps is None:
            return None

    choices = []
    for side in ("UP", "DOWN"):
        held = shape.up_shares if side == "UP" else shape.down_shares
        opposite_held = shape.down_shares if side == "UP" else shape.up_shares
        probability = p_up if side == "UP" else p_down
        opposite_probability = p_down if side == "UP" else p_up
        bid = up_bid if side == "UP" else down_bid
        spread = up_spread if side == "UP" else down_spread
        stability = indicators.up_stability_score if side == "UP" else indicators.down_stability_score
        opposite_stability = indicators.down_stability_score if side == "UP" else indicators.up_stability_score

        # This exits the model-weak leg of a balanced or already tilted box.
        if held <= 0 or held > opposite_held + 1e-9:
            continue
        if probability is None or opposite_probability is None:
            continue
        if bid is None or bid <= 0 or bid >= 1:
            continue
        if spread is None or spread < 0 or spread > float(sell_config.get("max_exit_spread", 0.10)):
            continue
        if bid < float(sell_config.get("min_exit_bid", 0.05)):
            continue
        if probability > float(sell_config.get("directional_max_weak_side_probability", 0.25)):
            continue
        if opposite_probability < float(sell_config.get("directional_min_retained_side_probability", 0.75)):
            continue
        probability_gap = opposite_probability - probability
        if probability_gap < float(sell_config.get("directional_min_probability_gap", 0.50)):
            continue
        if stability > int(sell_config.get("directional_max_weak_side_stability_score", 2)):
            continue
        if opposite_stability < int(sell_config.get("directional_min_retained_side_stability_score", 4)):
            continue

        model_exit_edge = bid - probability
        if model_exit_edge < float(sell_config.get("directional_min_model_exit_edge", 0.01)):
            continue
        choices.append(
            (
                model_exit_edge,
                probability_gap,
                side,
                held,
                bid,
                probability,
                opposite_probability,
                stability,
                opposite_stability,
            )
        )

    if not choices:
        return None

    (
        model_exit_edge,
        _,
        side,
        held,
        bid,
        probability,
        opposite_probability,
        stability,
        opposite_stability,
    ) = max(choices)

    late_seconds = int(sell_config.get("late_seconds_remaining", 90))
    final_seconds = int(sell_config.get("final_seconds_remaining", 45))
    if seconds_remaining <= final_seconds:
        max_fraction = float(sell_config.get("directional_final_max_sell_fraction", 1.0))
    elif seconds_remaining <= late_seconds:
        max_fraction = float(sell_config.get("directional_late_max_sell_fraction", 0.50))
    else:
        max_fraction = float(sell_config.get("directional_max_sell_fraction", 0.25))

    shares = held * max(0.0, min(1.0, max_fraction))
    max_deterioration = max(
        0.0,
        float(sell_config.get("directional_max_worst_case_deterioration_usdc", 0.50)),
    )
    if bid < 1.0:
        shares = min(shares, max_deterioration / (1.0 - bid))

    min_shares = max(
        float(config.get("sizing", {}).get("min_order_shares", 1.0)),
        float(sell_config.get("min_order_shares", 1.0)),
    )
    if 0 < held - shares < min_shares:
        full_exit_deterioration = (1.0 - bid) * held
        if full_exit_deterioration <= max_deterioration + 1e-9:
            shares = held
    if shares < min_shares:
        return None

    proceeds = shares * bid
    if proceeds < float(sell_config.get("min_exit_notional_usdc", 0.25)):
        return None
    shape_after = apply_sell(shape, side, shares, proceeds)
    deterioration = max(0.0, shape.worst_case_pnl - shape_after.worst_case_pnl)
    max_post_loss = float(
        sell_config.get(
            "directional_max_post_sell_worst_case_loss_usdc",
            config.get("risk", {}).get("max_window_loss_usdc", 1.0),
        )
    )
    if shape_after.worst_case_pnl < -max_post_loss:
        return None

    return Candidate(
        side=side,
        action="SELL",
        valid=True,
        reason="directional_weak_side_exit",
        role="DIRECTIONAL_WEAK_SIDE_EXIT",
        price=bid,
        notional_usdc=proceeds,
        shares=shares,
        shape_after=shape_after,
        shape_improvement=shape_after.worst_case_pnl - shape.worst_case_pnl,
        would_lock=shape_after.locked,
        min_shares_check_passed=True,
        stability_score=stability,
        opposite_stability_score=opposite_stability,
        held_shares_before=held,
        sell_fraction=(shares / held) if held > 0 else 0.0,
        model_probability=probability,
        opposite_probability=opposite_probability,
        model_exit_edge=model_exit_edge,
        worst_case_deterioration=deterioration,
    )


def _candidate_probe(candidate: Optional[Candidate]) -> dict:
    if candidate is None:
        return {}
    return {
        "valid": candidate.valid,
        "action": candidate.action,
        "side": candidate.side,
        "role": candidate.role,
        "reason": candidate.reason,
        "price": candidate.price,
        "notional_usdc": candidate.notional_usdc,
        "shares": candidate.shares,
        "sell_fraction": candidate.sell_fraction,
        "model_probability": candidate.model_probability,
        "opposite_probability": candidate.opposite_probability,
        "model_exit_edge": candidate.model_exit_edge,
        "stability_score": candidate.stability_score,
        "opposite_stability_score": candidate.opposite_stability_score,
        "shape_improvement": candidate.shape_improvement,
        "worst_case_deterioration": candidate.worst_case_deterioration,
    }


def _profit_stop_sell_probe(
    config: dict,
    *,
    shape: PositionShape,
    fills: list,
    indicators: IndicatorState,
    p_up: Optional[float],
    p_down: Optional[float],
    up_bid: Optional[float],
    down_bid: Optional[float],
    up_spread: Optional[float],
    down_spread: Optional[float],
    seconds_since_open: int,
    seconds_remaining: int,
    live_sell_armed: bool,
) -> dict:
    """Explain the nearest profit/stop trim gate for the terminal.

    Args:
        config: Loaded `config.yaml` settings.
        shape: Current inventory shape.
        fills: Stored fills used for average entry and prior trim counts.
        indicators: Stability and MA state.
        p_up: Model probability for UP.
        p_down: Model probability for DOWN.
        up_bid: Current UP sell bid.
        down_bid: Current DOWN sell bid.
        up_spread: UP quote spread.
        down_spread: DOWN quote spread.
        seconds_since_open: Window age in seconds.
        seconds_remaining: Time left in the window.
        live_sell_armed: True only when live SELLs are allowed.
    """
    sell_config = config.get("intra_window_sells", {})
    best: Optional[dict] = None
    for side in ("UP", "DOWN"):
        held = shape.up_shares if side == "UP" else shape.down_shares
        avg_entry = _average_buy_entry(fills, side)
        bid = up_bid if side == "UP" else down_bid
        spread = up_spread if side == "UP" else down_spread
        probability = p_up if side == "UP" else p_down
        opposite_probability = p_down if side == "UP" else p_up
        stability = indicators.up_stability_score if side == "UP" else indicators.down_stability_score
        opposite_stability = indicators.down_stability_score if side == "UP" else indicators.up_stability_score
        profit_per_share = (bid - avg_entry) if bid is not None and avg_entry is not None else None
        profit_pct = (profit_per_share / avg_entry) if profit_per_share is not None and avg_entry else None
        trim_count = _sell_role_count(fills, side, {"PROFIT_TRIM"})
        reason = "ready_or_checking_size"

        if not bool(sell_config.get("enabled", False)):
            reason = "intra_window_sells_disabled"
        elif not bool(sell_config.get("profit_trim_enabled", False)) and not bool(sell_config.get("stop_trim_enabled", False)):
            reason = "profit_and_stop_trim_disabled"
        elif not live_sell_armed:
            reason = "live_sell_orders_not_armed"
        elif held <= 0:
            reason = "no_inventory"
        elif avg_entry is None:
            reason = "avg_entry_missing"
        elif bid is None or bid <= 0 or bid >= 1:
            reason = "exit_bid_missing"
        elif spread is None or spread < 0 or spread > float(sell_config.get("max_exit_spread", 0.10)):
            reason = "exit_spread_too_wide"
        elif seconds_since_open < int(sell_config.get("profit_trim_min_seconds_since_open", 30)):
            reason = "before_profit_trim_start"
        elif seconds_remaining < int(sell_config.get("profit_trim_min_seconds_remaining", 20)):
            reason = "too_late_for_profit_trim"
        else:
            tp1 = float(sell_config.get("profit_trim_tp1_delta", 0.06))
            tp2 = float(sell_config.get("profit_trim_tp2_delta", 0.12))
            tp3 = float(sell_config.get("profit_trim_tp3_delta", 0.25))
            weak_max = int(sell_config.get("profit_trim_weak_stability_max_score", 3))
            require_weakness = bool(sell_config.get("profit_trim_require_weakness_for_all_tiers", False))
            side_weakening = stability <= weak_max
            soft_loss = -abs(float(sell_config.get("stop_trim_soft_loss_delta", 0.07)))
            hard_loss = -abs(float(sell_config.get("stop_trim_hard_loss_delta", 0.15)))
            if bool(sell_config.get("profit_trim_enabled", False)) and profit_per_share is not None:
                if profit_per_share >= tp3:
                    if trim_count >= 3:
                        reason = "profit_trim_tp3_already_used"
                    elif require_weakness and not side_weakening:
                        reason = "profit_trim_tp3_waiting_for_weakness"
                    else:
                        reason = "profit_trim_tp3_ready"
                elif profit_per_share >= tp2:
                    if trim_count >= 2:
                        reason = "profit_trim_tp2_already_used"
                    elif require_weakness and not side_weakening:
                        reason = "profit_trim_tp2_waiting_for_weakness"
                    else:
                        reason = "profit_trim_tp2_ready"
                elif profit_per_share >= tp1:
                    reason = "profit_trim_tp1_waiting_for_weakness" if not side_weakening else "profit_trim_tp1_ready"
                else:
                    reason = "profit_below_tp1"
            if (
                bool(sell_config.get("stop_trim_enabled", False))
                and profit_per_share is not None
                and profit_per_share <= soft_loss
            ):
                opposite_min = int(sell_config.get("stop_trim_opposite_stability_min_score", 4))
                if opposite_stability >= opposite_min:
                    reason = "stop_trim_hard_ready" if profit_per_share <= hard_loss else "stop_trim_soft_ready"
                else:
                    reason = "stop_trim_waiting_for_opposite_confirmation"

        probe = {
            "valid": False,
            "action": "SELL",
            "side": side,
            "role": "PROFIT_STOP_TRIM",
            "reason": reason,
            "price": bid,
            "avg_entry": avg_entry,
            "profit_per_share": profit_per_share,
            "profit_pct": profit_pct,
            "held_shares": held,
            "model_probability": probability,
            "opposite_probability": opposite_probability,
            "model_exit_edge": (bid - probability) if bid is not None and probability is not None else None,
            "stability_score": stability,
            "opposite_stability_score": opposite_stability,
        }
        score = abs(profit_per_share or 0.0) + (0.01 if held > 0 else 0.0)
        if best is None or score > float(best.get("_score", -1.0)):
            probe["_score"] = score
            best = probe
    if best is None:
        return {"valid": False, "action": "SELL", "role": "PROFIT_STOP_TRIM", "reason": "no_inventory"}
    best.pop("_score", None)
    return best


def _dominant_sell_probe(
    config: dict,
    *,
    shape: PositionShape,
    indicators: IndicatorState,
    p_up: Optional[float],
    p_down: Optional[float],
    up_bid: Optional[float],
    down_bid: Optional[float],
    up_spread: Optional[float],
    down_spread: Optional[float],
    seconds_since_open: int,
    seconds_remaining: int,
    live_sell_armed: bool,
) -> dict:
    """Explain the nearest dominant-inventory trim gate for the terminal.

    Args:
        config: Loaded `config.yaml` settings.
        shape: Current inventory shape.
        indicators: Stability and MA state.
        p_up: Model probability for UP.
        p_down: Model probability for DOWN.
        up_bid: Current UP sell bid.
        down_bid: Current DOWN sell bid.
        up_spread: UP quote spread.
        down_spread: DOWN quote spread.
        seconds_since_open: Window age in seconds.
        seconds_remaining: Time left in the window.
        live_sell_armed: True only when live SELLs are allowed.
    """
    sell_config = config.get("intra_window_sells", {})
    side = dominant_side(shape)
    held = shape.up_shares if side == "UP" else shape.down_shares if side == "DOWN" else 0.0
    opposite_held = shape.down_shares if side == "UP" else shape.up_shares if side == "DOWN" else 0.0
    bid = up_bid if side == "UP" else down_bid if side == "DOWN" else None
    spread = up_spread if side == "UP" else down_spread if side == "DOWN" else None
    probability = p_up if side == "UP" else p_down if side == "DOWN" else None
    stability = indicators.up_stability_score if side == "UP" else indicators.down_stability_score if side == "DOWN" else None
    opposite_stability = (
        indicators.down_stability_score if side == "UP" else indicators.up_stability_score if side == "DOWN" else None
    )

    reason = "ready_or_checking_size"
    if not bool(sell_config.get("enabled", False)):
        reason = "intra_window_sells_disabled"
    elif not bool(sell_config.get("dominant_inventory_trim_enabled", True)):
        reason = "dominant_inventory_trim_disabled"
    elif not live_sell_armed:
        reason = "live_sell_orders_not_armed"
    elif side is None:
        reason = "no_dominant_inventory"
    elif shape.locked and not bool(sell_config.get("allow_sell_locked", False)):
        reason = "locked_dominant_trim_disabled"
    elif seconds_since_open < int(sell_config.get("min_seconds_since_open", 60)):
        reason = "before_dominant_sell_start"
    elif bool(sell_config.get("require_anchor_ready", True)) and (
        indicators.ma_anchor is None or indicators.ma_anchor_slope_bps is None
    ):
        reason = "anchor_not_ready"
    elif probability is None:
        reason = "model_probability_missing"
    elif bid is None or bid <= 0 or bid >= 1:
        reason = "exit_bid_missing"
    elif spread is None or spread < 0 or spread > float(sell_config.get("max_exit_spread", 0.10)):
        reason = "exit_spread_too_wide"
    elif bid < float(sell_config.get("min_exit_bid", 0.05)):
        reason = "exit_bid_below_min"
    elif probability - bid > float(sell_config.get("max_probability_premium_sacrifice", 0.15)):
        reason = "bid_too_far_below_model"
    elif stability is not None and stability >= int(sell_config.get("stable_min_score", 4)):
        reason = "dominant_side_still_stable"
    else:
        weak_max = int(sell_config.get("weak_stability_max_score", 3))
        unstable_max = int(sell_config.get("unstable_stability_max_score", 1))
        flip_min = int(sell_config.get("confirmed_flip_min_opposite_score", 4))
        late_seconds = int(sell_config.get("late_seconds_remaining", 90))
        final_seconds = int(sell_config.get("final_seconds_remaining", 45))
        confirmed_flip = (stability or 0) <= unstable_max and (opposite_stability or 0) >= flip_min
        weak_late = seconds_remaining <= late_seconds and (stability or 0) <= weak_max
        final_weak = seconds_remaining <= final_seconds and (stability or 0) <= weak_max
        loss_trigger = shape.worst_case_pnl <= -float(sell_config.get("min_worst_case_loss_to_trim_usdc", 0.25))
        if not (confirmed_flip or weak_late or final_weak or loss_trigger):
            reason = "waiting_for_late_weakness_or_loss"

    return {
        "valid": False,
        "action": "SELL",
        "side": side,
        "role": "INVENTORY_TRIM",
        "reason": reason,
        "price": bid,
        "held_shares": held,
        "opposite_shares": opposite_held,
        "model_probability": probability,
        "model_exit_edge": (bid - probability) if bid is not None and probability is not None else None,
        "stability_score": stability,
        "opposite_stability_score": opposite_stability,
    }


def _weak_side_sell_probe(
    config: dict,
    *,
    shape: PositionShape,
    indicators: IndicatorState,
    p_up: Optional[float],
    p_down: Optional[float],
    up_bid: Optional[float],
    down_bid: Optional[float],
    up_spread: Optional[float],
    down_spread: Optional[float],
    seconds_since_open: int,
    seconds_remaining: int,
    live_sell_armed: bool,
) -> dict:
    """Explain the nearest directional weak-side exit gate for the terminal.

    Args:
        config: Loaded `config.yaml` settings.
        shape: Current inventory shape.
        indicators: Stability and MA state.
        p_up: Model probability for UP.
        p_down: Model probability for DOWN.
        up_bid: Current UP sell bid.
        down_bid: Current DOWN sell bid.
        up_spread: UP quote spread.
        down_spread: DOWN quote spread.
        seconds_since_open: Window age in seconds.
        seconds_remaining: Time left in the window.
        live_sell_armed: True only when live SELLs are allowed.
    """
    sell_config = config.get("intra_window_sells", {})
    side = "UP" if shape.up_shares <= shape.down_shares else "DOWN"
    held = shape.up_shares if side == "UP" else shape.down_shares
    opposite_held = shape.down_shares if side == "UP" else shape.up_shares
    bid = up_bid if side == "UP" else down_bid
    spread = up_spread if side == "UP" else down_spread
    probability = p_up if side == "UP" else p_down
    opposite_probability = p_down if side == "UP" else p_up
    stability = indicators.up_stability_score if side == "UP" else indicators.down_stability_score
    opposite_stability = indicators.down_stability_score if side == "UP" else indicators.up_stability_score
    model_exit_edge = (bid - probability) if bid is not None and probability is not None else None

    reason = "ready_or_checking_size"
    if not bool(sell_config.get("enabled", False)):
        reason = "intra_window_sells_disabled"
    elif not bool(sell_config.get("directional_weak_side_exit_enabled", False)):
        reason = "weak_side_exit_disabled"
    elif not live_sell_armed:
        reason = "live_sell_orders_not_armed"
    elif shape.locked and not bool(sell_config.get("directional_allow_sell_locked", False)):
        reason = "locked_weak_side_exit_disabled"
    elif held <= 0:
        reason = "no_weak_side_inventory"
    elif held > opposite_held + 1e-9:
        reason = "weak_exit_needs_smaller_or_equal_side"
    elif seconds_since_open < int(sell_config.get("directional_min_seconds_since_open", 0)):
        reason = "before_weak_side_sell_start"
    elif seconds_remaining < int(sell_config.get("directional_min_seconds_remaining", 5)):
        reason = "too_late_for_weak_side_sell"
    elif bool(sell_config.get("directional_require_anchor_ready", False)) and (
        indicators.ma_anchor is None or indicators.ma_anchor_slope_bps is None
    ):
        reason = "anchor_not_ready"
    elif probability is None or opposite_probability is None:
        reason = "model_probability_missing"
    elif bid is None or bid <= 0 or bid >= 1:
        reason = "exit_bid_missing"
    elif spread is None or spread < 0 or spread > float(sell_config.get("max_exit_spread", 0.10)):
        reason = "exit_spread_too_wide"
    elif bid < float(sell_config.get("min_exit_bid", 0.05)):
        reason = "exit_bid_below_min"
    elif probability > float(sell_config.get("directional_max_weak_side_probability", 0.25)):
        reason = "weak_side_probability_too_high"
    elif opposite_probability < float(sell_config.get("directional_min_retained_side_probability", 0.75)):
        reason = "retained_side_probability_too_low"
    elif opposite_probability - probability < float(sell_config.get("directional_min_probability_gap", 0.50)):
        reason = "probability_gap_too_low"
    elif stability > int(sell_config.get("directional_max_weak_side_stability_score", 2)):
        reason = "weak_side_stability_too_high"
    elif opposite_stability < int(sell_config.get("directional_min_retained_side_stability_score", 4)):
        reason = "retained_side_stability_too_low"
    elif model_exit_edge is None or model_exit_edge < float(sell_config.get("directional_min_model_exit_edge", 0.01)):
        reason = "bid_not_above_model_enough"

    return {
        "valid": False,
        "action": "SELL",
        "side": side,
        "role": "DIRECTIONAL_WEAK_SIDE_EXIT",
        "reason": reason,
        "price": bid,
        "held_shares": held,
        "opposite_shares": opposite_held,
        "model_probability": probability,
        "opposite_probability": opposite_probability,
        "model_exit_edge": model_exit_edge,
        "stability_score": stability,
        "opposite_stability_score": opposite_stability,
    }


def _average_buy_entry(fills: list, side: str) -> Optional[float]:
    """Return the weighted-average cost of currently open shares."""
    return average_open_entry(fills, side)


def _sell_role_count(fills: list, side: str, roles: set[str]) -> int:
    """Count prior SELL fills for one side whose candidate role matches."""
    count = 0
    for fill in fills:
        if str(getattr(fill, "action", "BUY")).upper() != "SELL":
            continue
        if str(getattr(fill, "side", "")).upper() != side:
            continue
        metadata = getattr(fill, "metadata", {}) or {}
        candidate = metadata.get("candidate") if isinstance(metadata, dict) else {}
        role = str(candidate.get("role") or getattr(fill, "role", "") or "").upper()
        if role in roles:
            count += 1
    return count
