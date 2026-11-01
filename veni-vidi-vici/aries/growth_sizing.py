from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .models import PositionShape
from .position_shape import weak_side


@dataclass
class GrowthSizingMetrics:
    enabled: bool = False
    net_price: Optional[float] = None
    full_kelly_fraction: float = 0.0
    applied_kelly_fraction: float = 0.0
    kelly_cap_usdc: float = 0.0
    repair_target_usdc: Optional[float] = None
    allowed_notional_usdc: Optional[float] = None
    account_scale: float = 1.0


def _get(config: dict, path: str, default):
    if hasattr(config, "get_path"):
        return config.get_path(path, default)
    current = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def account_scale(config: dict, bankroll: float, starting_bankroll: float) -> float:
    """Scale ladder clips as the settled account balance changes."""
    if not bool(_get(config, "growth_sizing.scale_with_account_balance", True)):
        return 1.0
    if starting_bankroll <= 0:
        return 1.0
    raw_scale = max(0.0, bankroll) / starting_bankroll
    minimum = float(_get(config, "growth_sizing.min_account_scale", 0.25))
    maximum = float(_get(config, "growth_sizing.max_account_scale", 3.0))
    return max(minimum, min(maximum, raw_scale))


def net_execution_price(config: dict, ask: float) -> float:
    fee_pp = float(_get(config, "paper.fees_bps", 0.0)) / 10_000.0
    slippage_pp = float(_get(config, "paper.slippage_bps", 0.0)) / 10_000.0
    safety_pp = float(_get(config, "growth_sizing.safety_buffer_pp", 0.002))
    return ask + fee_pp + slippage_pp + safety_pp


def growth_sizing_metrics(
    config: dict,
    *,
    price: float,
    p_side: Optional[float],
    side: str,
    role: str,
    shape: PositionShape,
    bankroll: float,
    starting_bankroll: float,
    max_window_notional_usdc: float,
    confidence: float = 1.0,
    kelly_multiplier: float = 1.0,
) -> GrowthSizingMetrics:
    """Calculate Kelly and remaining-risk caps used by `size_for_price`.

    Repairs normally use the exact outcome-PnL gap instead of the Kelly cap.
    """
    enabled = bool(_get(config, "growth_sizing.enabled", False))
    net_price = net_execution_price(config, price)
    scale = account_scale(config, bankroll, starting_bankroll)
    metrics = GrowthSizingMetrics(enabled=enabled, net_price=net_price, account_scale=scale)
    if not enabled or p_side is None or net_price <= 0 or net_price >= 1:
        return metrics

    # Binary-contract Kelly fraction, expressed as fraction of bankroll invested.
    full_kelly = max(0.0, (float(p_side) - net_price) / (1.0 - net_price))
    fraction = max(0.0, float(_get(config, "growth_sizing.fractional_kelly", 0.10)))
    if role == "SAME_SIDE_ADD" and bool(_get(config, "high_conviction_same_side.enabled", False)):
        fraction = max(0.0, float(_get(config, "high_conviction_same_side.fractional_kelly", fraction)))
    # Optionally temper Kelly by the model's confidence. Confidence is expected
    # to be in [0.0, 1.0]. Multiplying by confidence reduces allocation when
    # the model is less certain about a prediction.
    conf = max(0.0, min(1.0, float(confidence or 0.0)))
    applied_kelly = full_kelly * fraction * conf
    # Optional minimum model confidence required to apply any Kelly sizing.
    # Set `growth_sizing.min_confidence_for_kelly` in config to enable.
    min_conf_required = max(0.0, float(_get(config, "growth_sizing.min_confidence_for_kelly", 0.0)))
    if conf < min_conf_required:
        applied_kelly = 0.0
    # Adaptive layer: scale Kelly by measured calibration quality and the
    # drawdown throttle (see aries/adaptive.py).
    applied_kelly *= max(0.0, float(kelly_multiplier))
    kelly_cap = bankroll * applied_kelly

    repair_target = None
    if weak_side(shape) == side:
        repair_shares = abs(shape.pnl_if_up - shape.pnl_if_down)
        repair_target = repair_shares * net_price

    remaining_risk = max(0.0, max_window_notional_usdc - shape.total_cost)
    allowed = min(kelly_cap, remaining_risk)
    if role == "REPAIR" and not bool(_get(config, "growth_sizing.apply_kelly_to_repairs", False)):
        allowed = remaining_risk
        if repair_target is not None:
            allowed = min(allowed, repair_target)

    metrics.full_kelly_fraction = full_kelly
    metrics.applied_kelly_fraction = applied_kelly
    metrics.kelly_cap_usdc = kelly_cap
    metrics.repair_target_usdc = repair_target
    metrics.allowed_notional_usdc = allowed
    return metrics


def candidate_growth_score(
    config: dict,
    *,
    bankroll: float,
    p_up: Optional[float],
    p_down: Optional[float],
    shape_before: PositionShape,
    shape_after: PositionShape,
    order_cost: float,
) -> tuple[Optional[float], Optional[float], float]:
    """Score a candidate using expected growth, worst case, inventory, and cost."""
    imbalance = abs(shape_after.pnl_if_up - shape_after.pnl_if_down)
    if not bool(_get(config, "growth_sizing.enabled", False)) or p_up is None or p_down is None or bankroll <= 0:
        return None, None, imbalance

    before_up = bankroll + shape_before.pnl_if_up
    before_down = bankroll + shape_before.pnl_if_down
    after_up = bankroll + shape_after.pnl_if_up
    after_down = bankroll + shape_after.pnl_if_down
    if min(before_up, before_down, after_up, after_down) <= 0:
        return None, None, imbalance

    before_log = p_up * math.log(before_up) + p_down * math.log(before_down)
    after_log = p_up * math.log(after_up) + p_down * math.log(after_down)
    log_delta = after_log - before_log
    worst_delta = shape_after.worst_case_pnl - shape_before.worst_case_pnl

    worst_weight = float(_get(config, "growth_sizing.worst_case_weight", 0.50))
    inventory_weight = float(_get(config, "growth_sizing.inventory_weight", 0.10))
    cost_weight = float(_get(config, "growth_sizing.order_cost_weight", 0.01))
    score = (
        log_delta
        + worst_weight * (worst_delta / bankroll)
        - inventory_weight * (imbalance / bankroll)
        - cost_weight * (order_cost / bankroll)
    )
    return log_delta, score, imbalance
