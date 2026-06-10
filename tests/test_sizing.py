import pytest

from aries.models import PositionShape
from aries.position_shape import apply_buy
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


def test_growth_sizing_selects_largest_ladder_clip_below_kelly_cap():
    cfg = base_config()
    cfg["growth_sizing"] = {
        "enabled": True,
        "fractional_kelly": 0.10,
        "scale_with_account_balance": True,
        "safety_buffer_pp": 0.0,
    }
    cfg["account"] = {"balance_usdc": 100.0}
    cfg["paper"] = {"fees_bps": 0, "slippage_bps": 0}

    decision = size_for_price(
        cfg,
        price=0.5,
        role="FIRST_ENTRY",
        orders_in_window=0,
        same_side_count=0,
        side="UP",
        p_side=0.60,
        shape=PositionShape(),
        bankroll=100.0,
        starting_bankroll=100.0,
        max_window_notional_usdc=3.0,
    )

    assert decision.valid
    assert decision.notional_usdc == 2.0
    assert decision.applied_kelly_fraction == pytest.approx(0.02)
    assert decision.ladder_bankroll_ratio == 0.02


def test_growth_repair_uses_outcome_pnl_gap_cap():
    cfg = base_config()
    cfg["growth_sizing"] = {
        "enabled": True,
        "fractional_kelly": 0.10,
        "apply_kelly_to_repairs": False,
        "scale_with_account_balance": True,
        "safety_buffer_pp": 0.0,
    }
    cfg["paper"] = {"fees_bps": 0, "slippage_bps": 0}
    shape = apply_buy(PositionShape(), "UP", shares=2.0, cost=1.0)

    decision = size_for_price(
        cfg,
        price=0.5,
        role="REPAIR",
        orders_in_window=1,
        same_side_count=0,
        side="DOWN",
        p_side=0.45,
        shape=shape,
        bankroll=100.0,
        starting_bankroll=100.0,
        max_window_notional_usdc=3.0,
    )

    assert decision.valid
    assert decision.repair_target_usdc == 1.0
    assert decision.notional_usdc == 1.0


def test_growth_cap_never_bumps_order_above_allowed_notional():
    cfg = base_config()
    cfg["growth_sizing"] = {
        "enabled": True,
        "fractional_kelly": 0.10,
        "scale_with_account_balance": True,
        "min_account_scale": 0.25,
        "max_account_scale": 3.0,
        "safety_buffer_pp": 0.0,
    }
    cfg["paper"] = {"fees_bps": 0, "slippage_bps": 0}

    decision = size_for_price(
        cfg,
        price=0.5,
        role="FIRST_ENTRY",
        orders_in_window=0,
        same_side_count=0,
        side="UP",
        p_side=0.56,
        shape=PositionShape(),
        bankroll=75.0,
        starting_bankroll=100.0,
        max_window_notional_usdc=2.25,
    )

    assert not decision.valid
    assert decision.reason == "growth_cap_below_min_order"
