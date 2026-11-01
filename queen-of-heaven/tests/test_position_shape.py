from aries.models import PositionShape
import pytest

from aries.position_shape import apply_buy, apply_sell


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


def test_sell_dominant_side_recovers_cash_and_improves_weak_outcome():
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)

    after = apply_sell(shape, "UP", shares=4.0, proceeds=2.4)

    assert after.up_shares == 6.0
    assert after.total_cost == pytest.approx(2.6)
    assert after.pnl_if_up == pytest.approx(3.4)
    assert after.pnl_if_down == pytest.approx(-2.6)
    assert after.worst_case_pnl > shape.worst_case_pnl


def test_sell_cannot_exceed_held_shares():
    shape = apply_buy(PositionShape(), "DOWN", shares=2.0, cost=1.0)

    with pytest.raises(ValueError, match="Cannot sell"):
        apply_sell(shape, "DOWN", shares=3.0, proceeds=1.2)
