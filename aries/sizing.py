from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class SizingDecision:
    valid: bool
    reason: str
    notional_usdc: Optional[float]
    shares: Optional[float]
    ladder_step: Optional[int]
    min_notional_for_min_shares: Optional[float]
    notional_bumped_to_min_shares: bool


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


def size_for_price(config: dict, price: Optional[float], role: str, orders_in_window: int, same_side_count: int) -> SizingDecision:
    if price is None or price <= 0:
        return SizingDecision(False, "price_missing", None, None, None, None, False)

    step, clip = choose_clip(config, role, orders_in_window, same_side_count)
    min_shares = float(config["sizing"].get("min_order_shares", 1.0))
    min_notional = float(config["sizing"].get("min_order_notional_usdc", 0.0))
    allow_bump = bool(config["sizing"].get("allow_bump_to_min_shares", False))
    min_notional_for_min_shares = min_shares * price

    notional = max(clip, min_notional)
    bumped = False
    if notional / price < min_shares:
        if allow_bump:
            notional = max(notional, min_notional_for_min_shares)
            bumped = True
        else:
            return SizingDecision(False, "min_shares_not_met", notional, notional / price, step, min_notional_for_min_shares, False)

    shares = notional / price
    return SizingDecision(True, "sized_from_ladder", notional, shares, step, min_notional_for_min_shares, bumped)
