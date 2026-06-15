from __future__ import annotations

from .models import Fill, PositionShape


def recompute(shape: PositionShape) -> PositionShape:
    """Recalculate both resolution outcomes and whether both are profitable."""
    shape.pnl_if_up = shape.up_shares - shape.total_cost
    shape.pnl_if_down = shape.down_shares - shape.total_cost
    shape.worst_case_pnl = min(shape.pnl_if_up, shape.pnl_if_down)
    shape.locked = shape.worst_case_pnl > 0
    return shape


def apply_buy(shape: PositionShape, side: str, shares: float, cost: float) -> PositionShape:
    """Return the position shape after hypothetically buying one side."""
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


def apply_sell(shape: PositionShape, side: str, shares: float, proceeds: float) -> PositionShape:
    """Sell held shares and subtract executable bid proceeds from net cost."""
    quantity = max(0.0, shares)
    new = PositionShape(
        up_shares=shape.up_shares,
        down_shares=shape.down_shares,
        total_cost=shape.total_cost,
    )
    held = new.up_shares if side == "UP" else new.down_shares if side == "DOWN" else None
    if held is None:
        raise ValueError(f"Unknown side: {side}")
    if quantity > held + 1e-9:
        raise ValueError(f"Cannot sell {quantity} {side} shares; only {held} held")
    if side == "UP":
        new.up_shares = max(0.0, new.up_shares - quantity)
    else:
        new.down_shares = max(0.0, new.down_shares - quantity)
    new.total_cost -= max(0.0, proceeds)
    return recompute(new)


def apply_trade(shape: PositionShape, action: str, side: str, shares: float, notional_usdc: float) -> PositionShape:
    if action == "BUY":
        return apply_buy(shape, side, shares, notional_usdc)
    if action == "SELL":
        return apply_sell(shape, side, shares, notional_usdc)
    raise ValueError(f"Unknown action: {action}")


def shape_from_fills(fills: list[Fill]) -> PositionShape:
    """Rebuild reliable position state from the complete ordered fill history."""
    shape = PositionShape()
    for fill in fills:
        shape = apply_trade(
            shape,
            str(getattr(fill, "action", "BUY")).upper(),
            fill.side,
            fill.shares,
            fill.notional_usdc,
        )
    return recompute(shape)


def dominant_side(shape: PositionShape) -> str | None:
    """Return the side with more shares, or None for a balanced/empty shape."""
    if shape.up_shares > shape.down_shares:
        return "UP"
    if shape.down_shares > shape.up_shares:
        return "DOWN"
    return None


def weak_side(shape: PositionShape) -> str | None:
    """Return the side with fewer shares, which is normally the repair side."""
    dom = dominant_side(shape)
    if dom == "UP":
        return "DOWN"
    if dom == "DOWN":
        return "UP"
    return None
