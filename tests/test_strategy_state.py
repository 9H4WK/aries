from types import SimpleNamespace

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
