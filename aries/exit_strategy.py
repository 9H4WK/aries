from __future__ import annotations

from typing import Optional

from .models import Candidate, IndicatorState, PositionShape
from .position_shape import apply_sell, dominant_side


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

    Risk-reducing dominant-side trims are checked first. A directional weak-side
    exit is considered only when no inventory trim is available.
    """
    sell_config = config.get("intra_window_sells", {})
    if not bool(sell_config.get("enabled", False)):
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
    """Sell oversized inventory only when the trade improves worst-case PnL."""
    sell_config = config.get("intra_window_sells", {})
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
