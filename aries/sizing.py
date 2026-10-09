from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .growth_sizing import GrowthSizingMetrics, account_scale, growth_sizing_metrics
from .models import PositionShape


@dataclass
class SizingDecision:
    valid: bool
    reason: str
    notional_usdc: Optional[float]
    shares: Optional[float]
    ladder_step: Optional[int]
    min_notional_for_min_shares: Optional[float]
    notional_bumped_to_min_shares: bool
    net_price: Optional[float] = None
    full_kelly_fraction: float = 0.0
    applied_kelly_fraction: float = 0.0
    kelly_cap_usdc: float = 0.0
    repair_target_usdc: Optional[float] = None
    allowed_notional_usdc: Optional[float] = None
    account_scale: float = 1.0
    ladder_bankroll_ratio: Optional[float] = None
    time_kelly_multiplier: float = 1.0


def active_ladder(config: dict, role: Optional[str] = None) -> list[float]:
    """Return the configured USDC order clips for this candidate role."""
    if role == "FIRST_ENTRY" and bool(config.get("queen_of_heaven", {}).get("enabled", False)):
        clips = config["queen_of_heaven"].get("stake_ladder_usdc", [1.0, 2.0, 3.0, 5.0])
        return [float(x) for x in clips if float(x) > 0]
    if role == "SAME_SIDE_ADD" and bool(config.get("high_conviction_same_side", {}).get("enabled", False)):
        clips = config["high_conviction_same_side"].get("ladder_usdc", [2.0, 3.0, 5.0])
        return [float(x) for x in clips if float(x) > 0]
    sizing = config["sizing"]
    clips = sizing["research_ladder_usdc"] if sizing.get("use_research_ladder") else sizing["ladder_usdc"]
    return [float(x) for x in clips if float(x) > 0]


def choose_clip(config: dict, role: str, orders_in_window: int, same_side_count: int) -> tuple[int, float]:
    """Choose the starting ladder amount before growth/risk caps are applied."""
    ladder = active_ladder(config, role)
    if not ladder:
        raise ValueError("sizing ladder is empty")
    if role == "FIRST_ENTRY" and bool(config.get("queen_of_heaven", {}).get("enabled", False)):
        start = int(config["queen_of_heaven"].get("start_ladder_step", 0))
        max_step = min(int(config["queen_of_heaven"].get("max_ladder_step", len(ladder) - 1)), len(ladder) - 1)
        step = max(0, min(start, max_step))
        return step, ladder[step]
    if role == "SAME_SIDE_ADD" and bool(config.get("high_conviction_same_side", {}).get("enabled", False)):
        start = int(config["high_conviction_same_side"].get("start_ladder_step", 0))
        max_step = min(int(config["high_conviction_same_side"].get("max_ladder_step", len(ladder) - 1)), len(ladder) - 1)
        step = start + same_side_count
        return max(0, min(step, max_step)), ladder[max(0, min(step, max_step))]

    max_step = min(int(config["sizing"].get("max_ladder_step_per_window", len(ladder)-1)), len(ladder)-1)

    if role == "FIRST_ENTRY":
        step = int(config["sizing"].get("first_entry_step", 0))
    elif role == "SAME_SIDE_ADD":
        start = int(config["sizing"].get("same_side_start_step", 1))
        step = start + same_side_count
    else:  # REPAIR, LOCK, ENDGAME_REPAIR
        step = int(config["sizing"].get("repair_start_step", 0))

    step = max(0, min(step, max_step))
    return step, ladder[step]


def size_for_price(
    config: dict,
    price: Optional[float],
    role: str,
    orders_in_window: int,
    same_side_count: int,
    *,
    side: str = "UP",
    p_side: Optional[float] = None,
    shape: Optional[PositionShape] = None,
    bankroll: Optional[float] = None,
    starting_bankroll: Optional[float] = None,
    max_window_notional_usdc: Optional[float] = None,
    force_first_entry_floor: bool = False,
    min_order_shares_override: Optional[float] = None,
    seconds_since_open: Optional[int] = None,
    seconds_remaining: Optional[int] = None,
) -> SizingDecision:
    """Choose executable notional/shares from the ladder and safety limits.

    `allowed_notional_usdc` is the maximum permitted by Kelly/repair/window
    rules. The final order must also satisfy minimum notional and share sizes.
    """
    if price is None or price <= 0:
        return SizingDecision(False, "price_missing", None, None, None, None, False)

    high_conviction_add = (
        role == "SAME_SIDE_ADD"
        and bool(config.get("high_conviction_same_side", {}).get("enabled", False))
    )
    queen_entry = (
        role == "FIRST_ENTRY"
        and bool(config.get("queen_of_heaven", {}).get("enabled", False))
    )
    used_allowed_notional_fallback = False
    step, clip = choose_clip(config, role, orders_in_window, same_side_count)
    metrics = GrowthSizingMetrics()
    if (
        bool(config.get("growth_sizing", {}).get("enabled", False))
        and shape is not None
        and bankroll is not None
        and starting_bankroll is not None
        and max_window_notional_usdc is not None
    ):
        metrics = growth_sizing_metrics(
            config,
            price=price,
            p_side=p_side,
            side=side,
            role=role,
            shape=shape,
            bankroll=bankroll,
            starting_bankroll=starting_bankroll,
            max_window_notional_usdc=max_window_notional_usdc,
            seconds_since_open=seconds_since_open,
            seconds_remaining=seconds_remaining,
        )
        ladder = active_ladder(config, role)
        conviction_config = config.get("high_conviction_same_side", {})
        scale_ladder = not high_conviction_add or bool(
            conviction_config.get("scale_ladder_with_account_balance", False)
        )
        scale = account_scale(config, bankroll, starting_bankroll) if scale_ladder else 1.0
        if high_conviction_add:
            max_step = min(int(config["high_conviction_same_side"].get("max_ladder_step", len(ladder) - 1)), len(ladder) - 1)
        elif queen_entry:
            max_step = min(int(config["queen_of_heaven"].get("max_ladder_step", len(ladder) - 1)), len(ladder) - 1)
        else:
            max_step = min(int(config["sizing"].get("max_ladder_step_per_window", len(ladder) - 1)), len(ladder) - 1)
        start_step = step
        choices = [
            (index, float(ladder[index]) * scale)
            for index in range(start_step, max_step + 1)
            if metrics.allowed_notional_usdc is not None
            and float(ladder[index]) * scale <= metrics.allowed_notional_usdc + 1e-9
        ]
        if not choices and role == "FIRST_ENTRY" and force_first_entry_floor:
            first_clip = float(ladder[start_step]) * scale
            remaining_risk = max(0.0, max_window_notional_usdc - shape.total_cost)
            if first_clip <= remaining_risk + 1e-9:
                choices = [(start_step, first_clip)]
        if (
            not choices
            and high_conviction_add
            and bool(conviction_config.get("allow_allowed_notional_below_ladder", True))
            and metrics.allowed_notional_usdc is not None
        ):
            fallback_min = max(
                float(config["sizing"].get("min_order_notional_usdc", 0.0)),
                float(conviction_config.get("min_allowed_notional_fallback_usdc", 1.0)),
            )
            if metrics.allowed_notional_usdc + 1e-9 >= fallback_min:
                choices = [(start_step, metrics.allowed_notional_usdc)]
                used_allowed_notional_fallback = True
        if (
            not choices
            and queen_entry
            and bool(config["queen_of_heaven"].get("allow_allowed_notional_below_ladder", True))
            and metrics.allowed_notional_usdc is not None
        ):
            fallback_min = max(
                float(config["sizing"].get("min_order_notional_usdc", 0.0)),
                float(config["queen_of_heaven"].get("min_stake_usdc", 1.0)),
            )
            if metrics.allowed_notional_usdc + 1e-9 >= fallback_min:
                choices = [(start_step, metrics.allowed_notional_usdc)]
                used_allowed_notional_fallback = True
        if not choices:
            return SizingDecision(
                False,
                "growth_cap_below_ladder",
                None,
                None,
                None,
                None,
                False,
                net_price=metrics.net_price,
                full_kelly_fraction=metrics.full_kelly_fraction,
                applied_kelly_fraction=metrics.applied_kelly_fraction,
                kelly_cap_usdc=metrics.kelly_cap_usdc,
                repair_target_usdc=metrics.repair_target_usdc,
                allowed_notional_usdc=metrics.allowed_notional_usdc,
                account_scale=metrics.account_scale,
                time_kelly_multiplier=metrics.time_kelly_multiplier,
            )
        step, clip = choices[-1]

    min_shares = float(config["sizing"].get("min_order_shares", 1.0))
    if min_order_shares_override is not None:
        min_shares = max(min_shares, float(min_order_shares_override))
    min_notional = float(config["sizing"].get("min_order_notional_usdc", 0.0))
    allow_bump = bool(config["sizing"].get("allow_bump_to_min_shares", False))
    min_notional_for_min_shares = min_shares * price

    notional = max(clip, min_notional)
    bumped = False
    enforce_growth_cap = not (role == "FIRST_ENTRY" and force_first_entry_floor)
    if enforce_growth_cap and metrics.enabled and metrics.allowed_notional_usdc is not None and notional > metrics.allowed_notional_usdc + 1e-9:
        return SizingDecision(
            False, "growth_cap_below_min_order", notional, notional / price, step, min_notional_for_min_shares, False,
            net_price=metrics.net_price, full_kelly_fraction=metrics.full_kelly_fraction,
            applied_kelly_fraction=metrics.applied_kelly_fraction, kelly_cap_usdc=metrics.kelly_cap_usdc,
            repair_target_usdc=metrics.repair_target_usdc, allowed_notional_usdc=metrics.allowed_notional_usdc,
            account_scale=metrics.account_scale, ladder_bankroll_ratio=(notional / bankroll) if bankroll else None,
            time_kelly_multiplier=metrics.time_kelly_multiplier,
        )
    if notional / price < min_shares:
        if allow_bump:
            notional = max(notional, min_notional_for_min_shares)
            bumped = True
        else:
            return SizingDecision(
                False, "min_shares_not_met", notional, notional / price, step, min_notional_for_min_shares, False,
                net_price=metrics.net_price, full_kelly_fraction=metrics.full_kelly_fraction,
                applied_kelly_fraction=metrics.applied_kelly_fraction, kelly_cap_usdc=metrics.kelly_cap_usdc,
                repair_target_usdc=metrics.repair_target_usdc, allowed_notional_usdc=metrics.allowed_notional_usdc,
                account_scale=metrics.account_scale, ladder_bankroll_ratio=(notional / bankroll) if bankroll else None,
                time_kelly_multiplier=metrics.time_kelly_multiplier,
            )
    if enforce_growth_cap and metrics.enabled and metrics.allowed_notional_usdc is not None and notional > metrics.allowed_notional_usdc + 1e-9:
        return SizingDecision(
            False, "growth_cap_below_min_shares", notional, notional / price, step, min_notional_for_min_shares, bumped,
            net_price=metrics.net_price, full_kelly_fraction=metrics.full_kelly_fraction,
            applied_kelly_fraction=metrics.applied_kelly_fraction, kelly_cap_usdc=metrics.kelly_cap_usdc,
            repair_target_usdc=metrics.repair_target_usdc, allowed_notional_usdc=metrics.allowed_notional_usdc,
            account_scale=metrics.account_scale, ladder_bankroll_ratio=(notional / bankroll) if bankroll else None,
            time_kelly_multiplier=metrics.time_kelly_multiplier,
        )

    shares = notional / price
    if used_allowed_notional_fallback:
        reason = "sized_from_allowed_notional_fallback"
    else:
        reason = "sized_from_growth_ladder" if metrics.enabled else "sized_from_ladder"
    return SizingDecision(
        True, reason, notional, shares, step, min_notional_for_min_shares, bumped,
        net_price=metrics.net_price, full_kelly_fraction=metrics.full_kelly_fraction,
        applied_kelly_fraction=metrics.applied_kelly_fraction, kelly_cap_usdc=metrics.kelly_cap_usdc,
        repair_target_usdc=metrics.repair_target_usdc, allowed_notional_usdc=metrics.allowed_notional_usdc,
        account_scale=metrics.account_scale, ladder_bankroll_ratio=(notional / bankroll) if bankroll else None,
        time_kelly_multiplier=metrics.time_kelly_multiplier,
    )
