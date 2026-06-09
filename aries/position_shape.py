from __future__ import annotations

from .models import Fill, PositionShape


def recompute(shape: PositionShape) -> PositionShape:
    shape.pnl_if_up = shape.up_shares - shape.total_cost
    shape.pnl_if_down = shape.down_shares - shape.total_cost
    shape.worst_case_pnl = min(shape.pnl_if_up, shape.pnl_if_down)
    shape.locked = shape.worst_case_pnl > 0
    return shape


def apply_buy(shape: PositionShape, side: str, shares: float, cost: float) -> PositionShape:
    new = PositionShape(
        up_shares=shape.up_shares,
        down_shares=shape.down_shares,
        total_cost=shape.total_cost,
    )
    if side == "UP":
        new.up_shares += max(0.0, shares)
    elif side == "DOWN":
        new.down_shares += max(0.0, shares)
    else:
        raise ValueError(f"Unknown side: {side}")
    new.total_cost += max(0.0, cost)
    return recompute(new)


def shape_from_fills(fills: list[Fill]) -> PositionShape:
    shape = PositionShape()
    for fill in fills:
        shape = apply_buy(shape, fill.side, fill.shares, fill.notional_usdc)
    return recompute(shape)


def dominant_side(shape: PositionShape) -> str | None:
    if shape.up_shares > shape.down_shares:
        return "UP"
    if shape.down_shares > shape.up_shares:
        return "DOWN"
    return None


def weak_side(shape: PositionShape) -> str | None:
    dom = dominant_side(shape)
    if dom == "UP":
        return "DOWN"
    if dom == "DOWN":
        return "UP"
    return None
