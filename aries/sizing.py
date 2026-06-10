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


def active_ladder(config: dict) -> list[float]:
    sizing = config["sizing"]
    clips = sizing["research_ladder_usdc"] if sizing.get("use_research_ladder") else sizing["ladder_usdc"]
    return [float(x) for x in clips if float(x) > 0]


def choose_clip(config: dict, role: str, orders_in_window: int, same_side_count: int) -> tuple[int, float]:
    ladder = active_ladder(config)
    if not ladder:
        raise ValueError("sizing ladder is empty")
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
) -> SizingDecision:
    if price is None or price <= 0:
        return SizingDecision(False, "price_missing", None, None, None, None, False)

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
        )
        ladder = active_ladder(config)
        scale = account_scale(config, bankroll, starting_bankroll)
        max_step = min(int(config["sizing"].get("max_ladder_step_per_window", len(ladder) - 1)), len(ladder) - 1)
        start_step = step
        choices = [
            (index, float(ladder[index]) * scale)
            for index in range(start_step, max_step + 1)
            if metrics.allowed_notional_usdc is not None
            and float(ladder[index]) * scale <= metrics.allowed_notional_usdc + 1e-9
        ]
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
            )
        step, clip = choices[-1]

    min_shares = float(config["sizing"].get("min_order_shares", 1.0))
    min_notional = float(config["sizing"].get("min_order_notional_usdc", 0.0))
    allow_bump = bool(config["sizing"].get("allow_bump_to_min_shares", False))
    min_notional_for_min_shares = min_shares * price

    notional = max(clip, min_notional)
    bumped = False
    if metrics.enabled and metrics.allowed_notional_usdc is not None and notional > metrics.allowed_notional_usdc + 1e-9:
        return SizingDecision(
            False, "growth_cap_below_min_order", notional, notional / price, step, min_notional_for_min_shares, False,
            net_price=metrics.net_price, full_kelly_fraction=metrics.full_kelly_fraction,
            applied_kelly_fraction=metrics.applied_kelly_fraction, kelly_cap_usdc=metrics.kelly_cap_usdc,
            repair_target_usdc=metrics.repair_target_usdc, allowed_notional_usdc=metrics.allowed_notional_usdc,
            account_scale=metrics.account_scale, ladder_bankroll_ratio=(notional / bankroll) if bankroll else None,
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
            )
    if metrics.enabled and metrics.allowed_notional_usdc is not None and notional > metrics.allowed_notional_usdc + 1e-9:
        return SizingDecision(
            False, "growth_cap_below_min_shares", notional, notional / price, step, min_notional_for_min_shares, bumped,
            net_price=metrics.net_price, full_kelly_fraction=metrics.full_kelly_fraction,
            applied_kelly_fraction=metrics.applied_kelly_fraction, kelly_cap_usdc=metrics.kelly_cap_usdc,
            repair_target_usdc=metrics.repair_target_usdc, allowed_notional_usdc=metrics.allowed_notional_usdc,
            account_scale=metrics.account_scale, ladder_bankroll_ratio=(notional / bankroll) if bankroll else None,
        )

    shares = notional / price
    reason = "sized_from_growth_ladder" if metrics.enabled else "sized_from_ladder"
    return SizingDecision(
        True, reason, notional, shares, step, min_notional_for_min_shares, bumped,
        net_price=metrics.net_price, full_kelly_fraction=metrics.full_kelly_fraction,
        applied_kelly_fraction=metrics.applied_kelly_fraction, kelly_cap_usdc=metrics.kelly_cap_usdc,
        repair_target_usdc=metrics.repair_target_usdc, allowed_notional_usdc=metrics.allowed_notional_usdc,
        account_scale=metrics.account_scale, ladder_bankroll_ratio=(notional / bankroll) if bankroll else None,
    )
