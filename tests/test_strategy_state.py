from types import SimpleNamespace

from aries.config import Config
from aries.models import Candidate, PositionShape
from aries.strategy import AriesBot


def test_first_fill_moves_to_one_sided_exposed():
    decision = SimpleNamespace(
        position_after={"locked": False, "total_cost": 1.0},
        order_status="FILLED",
        selected_reason="first_directional_entry",
        strategy_state="LOOKING_FOR_FIRST_ENTRY",
    )

    assert AriesBot._post_execution_state(object(), decision) == "ONE_SIDED_EXPOSED"


def test_locking_fill_moves_to_locked():
    decision = SimpleNamespace(
        position_after={"locked": True, "total_cost": 2.0},
        order_status="FILLED",
        selected_reason="locks_structure",
        strategy_state="REPAIR_SEARCH",
    )

    assert AriesBot._post_execution_state(object(), decision) == "LOCKED"


def test_seconds_since_first_fill_uses_fill_timestamp():
    fill = SimpleNamespace(created_at=100.0)

    assert AriesBot._seconds_since_first_fill(object(), 124.5, [fill]) == 24.5


def test_average_entry_prices_are_weighted_by_shares():
    fills = [
        SimpleNamespace(side="UP", notional_usdc=1.0, shares=2.0),
        SimpleNamespace(side="UP", notional_usdc=2.0, shares=2.0),
        SimpleNamespace(side="DOWN", notional_usdc=1.0, shares=4.0),
    ]

    average_up, average_down = AriesBot._average_entry_prices(object(), fills)

    assert average_up == 0.75
    assert average_down == 0.25


def test_first_entry_after_preferred_time_uses_best_net_edge_despite_low_confidence():
    bot = SimpleNamespace(
        config=Config(
            {
                "timing": {"preferred_first_entry_seconds": 18, "endgame_seconds_remaining": 30},
                "entry": {"force_best_side_after_preferred": True},
                "edge": {"min_edge": 0.07, "min_confidence": 0.10},
                "growth_sizing": {"use_growth_score_for_first_entry": True},
            }
        ),
        _risk_caps_pass=lambda candidate: True,
    )
    up = Candidate(side="UP", valid=True, reason="candidate_built", edge=0.02, net_edge=0.01, shape_after=PositionShape())
    down = Candidate(side="DOWN", valid=True, reason="candidate_built", edge=0.03, net_edge=0.02, shape_after=PositionShape())
    probability = SimpleNamespace(confidence=0.01)

    selected, decision, reason = AriesBot._select_first_entry(
        bot,
        up,
        down,
        probability,
        seconds_since_open=18,
        seconds_remaining=282,
    )

    assert selected is down
    assert decision == "BUY_DOWN"
    assert reason == "preferred_time_best_mispriced_side"
