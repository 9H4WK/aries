from aries.position_shape import PositionShape, apply_buy


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
