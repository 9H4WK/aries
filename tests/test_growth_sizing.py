import pytest

from aries.config import Config
from aries.growth_sizing import candidate_growth_score
from aries.models import PositionShape
from aries.position_shape import apply_buy
from aries.risk import scaled_risk_cap


def test_scaled_risk_cap_tracks_settled_account_growth():
    config = Config(
        {
            "account": {"balance_usdc": 100.0},
            "growth_sizing": {
                "enabled": True,
                "scale_with_account_balance": True,
                "min_account_scale": 0.25,
                "max_account_scale": 3.0,
            },
        }
    )

    assert scaled_risk_cap(config, 3.0, current_balance=150.0) == 4.5


def test_inventory_repair_candidate_has_positive_growth_score():
    config = {
        "growth_sizing": {
            "enabled": True,
            "worst_case_weight": 0.50,
            "inventory_weight": 0.10,
            "order_cost_weight": 0.01,
        }
    }
    before = apply_buy(PositionShape(), "UP", shares=2.0, cost=1.0)
    after = apply_buy(before, "DOWN", shares=2.0, cost=1.0)

    log_delta, score, imbalance = candidate_growth_score(
        config,
        bankroll=100.0,
        p_up=0.50,
        p_down=0.50,
        shape_before=before,
        shape_after=after,
        order_cost=1.0,
    )

    assert log_delta is not None
    assert score is not None and score > 0
    assert imbalance == pytest.approx(0.0)
