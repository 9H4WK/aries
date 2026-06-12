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


def high_conviction_bot():
    bot = SimpleNamespace(
        config=Config(
            {
                "high_conviction_same_side": {
                    "enabled": True,
                    "min_probability": 0.80,
                    "min_confidence": 0.60,
                    "min_edge": 0.03,
                    "min_net_edge": 0.02,
                    "max_ask": 0.88,
                    "require_price_on_target_side": True,
                    "min_distance_bps": 5.0,
                    "require_ma_alignment": True,
                    "min_fast_slope_bps": 0.10,
                    "min_mid_slope_bps": 0.0,
                    "min_velocity_5s_bps": 0.0,
                    "min_persistence_seconds": 10,
                    "require_volume_spike": True,
                    "min_volume_spike": 1.0,
                    "require_orderbook_alignment": True,
                    "min_orderbook_imbalance": 0.05,
                    "min_seconds_since_open": 30,
                    "max_seconds_since_open": 150,
                    "min_seconds_after_first_entry": 15,
                    "min_seconds_remaining": 45,
                    "max_adds_per_window": 2,
                }
            }
        ),
        _risk_caps_pass=lambda candidate: True,
    )
    bot._high_conviction_reject_reason = lambda *args: AriesBot._high_conviction_reject_reason(bot, *args)
    return bot


def conviction_state(side: str):
    sign = 1 if side == "UP" else -1
    return (
        SimpleNamespace(
            p_up=0.85 if side == "UP" else 0.15,
            p_down=0.15 if side == "UP" else 0.85,
            confidence=0.70,
            distance_bps=10.0 * sign,
        ),
        SimpleNamespace(
            pressure=side,
            ma_fast_slope_bps=0.50 * sign,
            ma_mid_slope_bps=0.25 * sign,
            velocity_5s_bps=0.40 * sign,
            persistence_above_target_seconds=15 if side == "UP" else 0,
            persistence_below_target_seconds=15 if side == "DOWN" else 0,
            volume_spike=1.5,
            orderbook_imbalance=0.20 * sign,
        ),
        SimpleNamespace(price=101.0 if side == "UP" else 99.0),
        SimpleNamespace(target_price=100.0),
    )


def test_high_conviction_same_side_add_supports_up_and_down():
    bot = high_conviction_bot()
    for side in ("UP", "DOWN"):
        shape = PositionShape(up_shares=2.0 if side == "UP" else 0.0, down_shares=2.0 if side == "DOWN" else 0.0, total_cost=1.0)
        up = Candidate(side="UP", valid=True, reason="candidate_built", role="SAME_SIDE_ADD" if side == "UP" else "REPAIR", price=0.70, edge=0.15, net_edge=0.14, shape_after=shape)
        down = Candidate(side="DOWN", valid=True, reason="candidate_built", role="SAME_SIDE_ADD" if side == "DOWN" else "REPAIR", price=0.70, edge=0.15, net_edge=0.14, shape_after=shape)
        prob, indicators, binance_state, market = conviction_state(side)

        selected, decision, reason = AriesBot._select_high_conviction_same_side(
            bot, up, down, shape, prob, indicators, binance_state, market,
            [SimpleNamespace(side=side)], 60, 180, 20,
        )

        assert selected is (up if side == "UP" else down)
        assert decision == f"BUY_{side}"
        assert reason == "high_conviction_same_side_add"


def test_high_conviction_rejects_misaligned_ma():
    bot = high_conviction_bot()
    shape = PositionShape(up_shares=2.0, total_cost=1.0)
    up = Candidate(side="UP", valid=True, reason="candidate_built", role="SAME_SIDE_ADD", price=0.70, edge=0.15, net_edge=0.14, shape_after=shape)
    down = Candidate(side="DOWN", valid=True, reason="candidate_built", role="REPAIR", price=0.30, edge=-0.15, net_edge=-0.16, shape_after=shape)
    prob, indicators, binance_state, market = conviction_state("UP")
    indicators.pressure = "CHOP"

    selected, _, reason = AriesBot._select_high_conviction_same_side(
        bot, up, down, shape, prob, indicators, binance_state, market,
        [SimpleNamespace(side="UP")], 60, 180, 20,
    )

    assert selected is None
    assert reason == "high_conviction_ma_not_aligned"


def test_high_conviction_rejects_after_configured_time_ceiling():
    bot = high_conviction_bot()
    shape = PositionShape(up_shares=2.0, total_cost=1.0)
    up = Candidate(side="UP", valid=True, reason="candidate_built", role="SAME_SIDE_ADD", price=0.70, edge=0.15, net_edge=0.14, shape_after=shape)
    down = Candidate(side="DOWN", valid=True, reason="candidate_built", role="REPAIR", price=0.30, edge=-0.15, net_edge=-0.16, shape_after=shape)
    prob, indicators, binance_state, market = conviction_state("UP")

    selected, _, reason = AriesBot._select_high_conviction_same_side(
        bot, up, down, shape, prob, indicators, binance_state, market,
        [SimpleNamespace(side="UP")], 151, 149, 120,
    )

    assert selected is None
    assert reason == "high_conviction_after_time_ceiling"


def test_live_sizing_bankroll_never_uses_paper_pnl():
    bot = SimpleNamespace(
        mode="live",
        config=Config({"account": {"balance_usdc": 100.0}}),
        live_executor=SimpleNamespace(
            snapshot=SimpleNamespace(
                collateral_balance_usdc=67.0,
                session_start_balance_usdc=67.0,
            )
        ),
        db=SimpleNamespace(total_realized_pnl=lambda: 200.0),
    )

    assert AriesBot._sizing_bankroll(bot) == (67.0, 67.0)
