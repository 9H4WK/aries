from aries.sizing import size_for_price


def base_config():
    return {
        "sizing": {
            "ladder_usdc": [1.0, 2.0, 3.0],
            "research_ladder_usdc": [5.0, 10.0],
            "use_research_ladder": False,
            "first_entry_step": 0,
            "repair_start_step": 0,
            "same_side_start_step": 1,
            "max_ladder_step_per_window": 2,
            "min_order_shares": 1.0,
            "min_order_notional_usdc": 1.0,
            "allow_bump_to_min_shares": False,
        }
    }


def test_ladder_first_entry():
    decision = size_for_price(base_config(), price=0.5, role="FIRST_ENTRY", orders_in_window=0, same_side_count=0)
    assert decision.valid
    assert decision.notional_usdc == 1.0
    assert decision.shares == 2.0
    assert decision.ladder_step == 0


def test_ladder_repair_uses_repair_step():
    decision = size_for_price(base_config(), price=0.5, role="REPAIR", orders_in_window=1, same_side_count=0)
    assert decision.valid
    assert decision.notional_usdc == 1.0
    assert decision.ladder_step == 0


def test_min_shares_rejects_small_clip():
    cfg = base_config()
    cfg["sizing"]["min_order_shares"] = 3.0
    decision = size_for_price(cfg, price=0.5, role="FIRST_ENTRY", orders_in_window=0, same_side_count=0)
    assert not decision.valid
    assert decision.reason == "min_shares_not_met"
