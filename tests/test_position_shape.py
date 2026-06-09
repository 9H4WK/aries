from aries.models import PositionShape
from aries.position_shape import apply_buy


def test_buy_up_shape():
    shape = apply_buy(PositionShape(), "UP", shares=4.0, cost=1.0)
    assert shape.up_shares == 4.0
    assert shape.down_shares == 0.0
    assert shape.total_cost == 1.0
    assert shape.pnl_if_up == 3.0
    assert shape.pnl_if_down == -1.0


def test_buy_down_shape():
    shape = apply_buy(PositionShape(), "DOWN", shares=5.0, cost=2.0)
    assert shape.up_shares == 0.0
    assert shape.down_shares == 5.0
    assert shape.total_cost == 2.0
    assert shape.pnl_if_up == -2.0
    assert shape.pnl_if_down == 3.0


def test_locked_shape():
    shape = PositionShape()
    shape = apply_buy(shape, "UP", shares=20.0, cost=5.0)
    shape = apply_buy(shape, "DOWN", shares=20.0, cost=5.0)
    assert shape.locked
    assert shape.worst_case_pnl == 10.0


def test_worst_case_pnl_formula():
    shape = PositionShape()
    shape = apply_buy(shape, "UP", shares=3.0, cost=1.5)
    shape = apply_buy(shape, "DOWN", shares=2.0, cost=0.5)
    assert shape.pnl_if_up == shape.up_shares - shape.total_cost
    assert shape.pnl_if_down == shape.down_shares - shape.total_cost
    assert shape.worst_case_pnl == min(shape.pnl_if_up, shape.pnl_if_down)
