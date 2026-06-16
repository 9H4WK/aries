from types import SimpleNamespace

from aries.exit_strategy import build_inventory_sell_candidate
from aries.models import Fill, PositionShape
from aries.position_shape import apply_buy


def config(enabled=True):
    return {
        "sizing": {"min_order_shares": 1.0},
        "intra_window_sells": {
            "enabled": enabled,
            "modes": ["paper"],
            "allow_sell_locked": False,
            "dominant_inventory_trim_enabled": True,
            "require_anchor_ready": True,
            "stable_min_score": 4,
            "weak_stability_max_score": 3,
            "unstable_stability_max_score": 1,
            "confirmed_flip_min_opposite_score": 4,
            "min_seconds_since_open": 60,
            "late_seconds_remaining": 90,
            "final_seconds_remaining": 45,
            "middle_max_sell_fraction": 0.25,
            "late_max_sell_fraction": 0.50,
            "flip_max_sell_fraction": 0.50,
            "final_max_sell_fraction": 1.0,
            "weak_target_tilt_shares": 2.0,
            "unstable_target_tilt_shares": 0.0,
            "min_exit_bid": 0.05,
            "max_exit_spread": 0.10,
            "max_probability_premium_sacrifice": 0.15,
            "min_exit_notional_usdc": 0.25,
            "min_worst_case_loss_to_trim_usdc": 0.25,
            "require_worst_case_improvement": True,
            "min_worst_case_improvement_usdc": 0.02,
            "cooldown_seconds": 0,
            "max_sells_per_window": 3,
        },
    }


def indicators(up_score, down_score):
    return SimpleNamespace(
        ma_anchor=100.0,
        ma_anchor_slope_bps=0.1,
        up_stability_score=up_score,
        down_stability_score=down_score,
    )


def candidate(shape, state, *, seconds_remaining=80, p_up=0.55, p_down=0.45, cfg=None, up_bid=0.50, down_bid=0.45):
    return build_inventory_sell_candidate(
        cfg or config(),
        mode="paper",
        shape=shape,
        fills=[
            Fill(
                ts=100.0,
                market_id="m1",
                window_start_ts=0,
                side="UP",
                price=0.5,
                notional_usdc=5.0,
                shares=10.0,
                reason="seed",
            )
        ],
        indicators=state,
        p_up=p_up,
        p_down=p_down,
        up_bid=up_bid,
        down_bid=down_bid,
        up_spread=0.02,
        down_spread=0.02,
        now_ts=200.0,
        seconds_since_open=220,
        seconds_remaining=seconds_remaining,
    )


def test_weak_dominant_side_builds_partial_inventory_trim():
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)

    sell = candidate(shape, indicators(3, 3))

    assert sell is not None
    assert sell.action == "SELL"
    assert sell.side == "UP"
    assert sell.shares == 5.0
    assert sell.reason == "late_weak_inventory_trim"
    assert sell.shape_after.worst_case_pnl > shape.worst_case_pnl


def test_dominant_inventory_trim_can_be_disabled():
    cfg = config()
    cfg["intra_window_sells"]["dominant_inventory_trim_enabled"] = False
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)

    assert candidate(shape, indicators(3, 3), cfg=cfg) is None


def test_stable_dominant_side_is_held():
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)

    assert candidate(shape, indicators(5, 1)) is None


def test_final_confirmed_weakness_can_exit_all_dominant_inventory():
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)

    sell = candidate(shape, indicators(1, 5), seconds_remaining=40, p_up=0.30, p_down=0.70)

    assert sell is not None
    assert sell.shares == 10.0
    assert sell.shape_after.up_shares == 0.0


def test_anchor_must_be_ready():
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)
    state = indicators(1, 5)
    state.ma_anchor = None

    assert candidate(shape, state, seconds_remaining=40, p_up=0.30, p_down=0.70) is None


def test_live_sell_planner_waits_until_live_sell_orders_are_armed():
    cfg = config()
    cfg["intra_window_sells"]["modes"] = ["paper", "live"]
    cfg["live"] = {"allow_sell_orders": False}
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)

    sell = build_inventory_sell_candidate(
        cfg,
        mode="live",
        shape=shape,
        fills=[],
        indicators=indicators(1, 5),
        p_up=0.30,
        p_down=0.70,
        up_bid=0.50,
        down_bid=0.45,
        up_spread=0.02,
        down_spread=0.02,
        now_ts=200.0,
        seconds_since_open=220,
        seconds_remaining=40,
    )

    assert sell is None


def directional_config():
    cfg = config()
    cfg["intra_window_sells"].update(
        {
            "directional_weak_side_exit_enabled": True,
            "directional_require_anchor_ready": False,
            "directional_min_seconds_since_open": 0,
            "directional_min_seconds_remaining": 5,
            "directional_max_weak_side_probability": 0.25,
            "directional_min_retained_side_probability": 0.75,
            "directional_min_probability_gap": 0.50,
            "directional_max_weak_side_stability_score": 2,
            "directional_min_retained_side_stability_score": 4,
            "directional_min_model_exit_edge": 0.01,
            "directional_max_sell_fraction": 0.25,
            "directional_late_max_sell_fraction": 0.50,
            "directional_final_max_sell_fraction": 1.00,
            "directional_max_worst_case_deterioration_usdc": 3.0,
            "directional_max_post_sell_worst_case_loss_usdc": 4.0,
        }
    )
    return cfg


def test_directional_exit_can_sell_model_weak_smaller_side():
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)
    shape = apply_buy(shape, "DOWN", shares=5.0, cost=1.0)

    sell = candidate(
        shape,
        indicators(5, 1),
        seconds_remaining=80,
        p_up=0.85,
        p_down=0.15,
        cfg=directional_config(),
        down_bid=0.22,
    )

    assert sell is not None
    assert sell.side == "DOWN"
    assert sell.role == "DIRECTIONAL_WEAK_SIDE_EXIT"
    assert sell.model_exit_edge == 0.07
    assert sell.shape_improvement < 0
    assert sell.worst_case_deterioration > 0


def test_weak_side_only_works_when_dominant_trim_is_disabled():
    cfg = directional_config()
    cfg["intra_window_sells"]["dominant_inventory_trim_enabled"] = False
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)
    shape = apply_buy(shape, "DOWN", shares=5.0, cost=1.0)

    sell = candidate(
        shape,
        indicators(5, 1),
        seconds_remaining=80,
        p_up=0.85,
        p_down=0.15,
        cfg=cfg,
        down_bid=0.22,
    )

    assert sell is not None
    assert sell.side == "DOWN"
    assert sell.role == "DIRECTIONAL_WEAK_SIDE_EXIT"


def test_both_sell_modes_prioritize_dominant_inventory_trim():
    cfg = directional_config()
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)

    sell = candidate(shape, indicators(3, 3), cfg=cfg)

    assert sell is not None
    assert sell.side == "UP"
    assert sell.role == "INVENTORY_TRIM"


def test_directional_exit_requires_bid_above_model_probability():
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=5.0)
    shape = apply_buy(shape, "DOWN", shares=5.0, cost=1.0)

    sell = candidate(
        shape,
        indicators(5, 1),
        p_up=0.85,
        p_down=0.15,
        cfg=directional_config(),
        down_bid=0.14,
    )

    assert sell is None


def test_directional_exit_is_clipped_by_worst_case_deterioration_cap():
    cfg = directional_config()
    cfg["sizing"]["min_order_shares"] = 0.1
    cfg["intra_window_sells"]["min_order_shares"] = 0.1
    cfg["intra_window_sells"]["directional_max_worst_case_deterioration_usdc"] = 0.50
    cfg["intra_window_sells"]["min_exit_notional_usdc"] = 0.10
    shape = apply_buy(PositionShape(), "UP", shares=10.0, cost=4.0)
    shape = apply_buy(shape, "DOWN", shares=5.0, cost=1.0)

    sell = candidate(
        shape,
        indicators(5, 1),
        seconds_remaining=40,
        p_up=0.85,
        p_down=0.15,
        cfg=cfg,
        down_bid=0.20,
    )

    assert sell is not None
    assert abs(sell.worst_case_deterioration - 0.50) < 1e-9
    assert abs(sell.shares - 0.625) < 1e-9
