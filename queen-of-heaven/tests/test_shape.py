from types import SimpleNamespace

from aries.position_shape import PositionShape, apply_buy, average_open_entry


def test_position_shape_buy_both_sides_locks():
    s = PositionShape()
    s = apply_buy(s, "UP", shares=20.0, cost=5.0)
    assert s.pnl_if_up == 15.0
    assert s.pnl_if_down == -5.0
    assert s.worst_case_pnl == -5.0
    assert not s.locked

    s = apply_buy(s, "DOWN", shares=25.0, cost=5.0)
    assert s.pnl_if_up == 10.0
    assert s.pnl_if_down == 15.0
    assert s.worst_case_pnl == 10.0
    assert s.locked


def test_average_open_entry_clears_after_full_sell():
    fills = [
        SimpleNamespace(action="BUY", side="UP", shares=5.0, notional_usdc=1.0),
        SimpleNamespace(action="SELL", side="UP", shares=5.0, notional_usdc=2.0),
    ]

    assert average_open_entry(fills, "UP") is None


def test_average_open_entry_resets_for_rebuy_after_partial_sell():
    fills = [
        SimpleNamespace(action="BUY", side="UP", shares=10.0, notional_usdc=2.0),
        SimpleNamespace(action="SELL", side="UP", shares=5.0, notional_usdc=2.0),
        SimpleNamespace(action="BUY", side="UP", shares=5.0, notional_usdc=3.0),
    ]

    assert average_open_entry(fills, "UP") == 0.4
