from types import SimpleNamespace

from aries.config import Config
from aries.models import Candidate, PositionShape
from aries.position_shape import apply_buy
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


def test_complete_sell_moves_to_flat_after_exit():
    decision = SimpleNamespace(
        position_after={"locked": False, "total_cost": 0.5, "up_shares": 0.0, "down_shares": 0.0},
        order_status="FILLED",
        selected_reason="final_weak_inventory_trim",
        strategy_state="REPAIR_SEARCH",
    )

    assert AriesBot._post_execution_state(object(), decision) == "FLAT_AFTER_EXIT"


def test_time_based_exit_limits_require_both_switches():
    bot = SimpleNamespace(
        config=Config(
            {
                "intra_window_sells": {
                    "enabled": True,
                    "enforce_time_based_buy_limits": False,
                }
            }
        )
    )

    assert AriesBot._time_based_exit_limits_enabled(bot) is False
    bot.config["intra_window_sells"]["enforce_time_based_buy_limits"] = True
    assert AriesBot._time_based_exit_limits_enabled(bot) is True


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
                "entry": {"force_best_side_after_preferred": True, "force_entry_deadline_seconds": 42},
                "edge": {"min_edge": 0.07, "min_confidence": 0.10},
                "growth_sizing": {"use_growth_score_for_first_entry": True},
            }
        ),
        _risk_caps_pass=lambda candidate: True,
    )
    bot._first_entry_deadline_seconds = lambda: AriesBot._first_entry_deadline_seconds(bot)
    up = Candidate(side="UP", valid=True, reason="candidate_built", edge=0.02, net_edge=0.01, shape_after=PositionShape())
    down = Candidate(side="DOWN", valid=True, reason="candidate_built", edge=0.03, net_edge=0.02, shape_after=PositionShape())
    probability = SimpleNamespace(confidence=0.01)

    before_deadline = AriesBot._select_first_entry(
        bot,
        up,
        down,
        probability,
        seconds_since_open=41,
        seconds_remaining=259,
    )
    selected, decision, reason = AriesBot._select_first_entry(
        bot,
        up,
        down,
        probability,
        seconds_since_open=42,
        seconds_remaining=258,
    )

    assert before_deadline == (None, "SKIP", "confidence_too_low")
    assert selected is down
    assert decision == "BUY_DOWN"
    assert reason == "deadline_best_mispriced_side"


def test_preferred_time_relaxes_early_gate_before_deadline():
    bot = SimpleNamespace(
        config=Config(
            {
                "timing": {"preferred_first_entry_seconds": 18, "endgame_seconds_remaining": 30},
                "entry": {
                    "early_min_edge": 0.05,
                    "early_min_confidence": 0.20,
                    "force_best_side_after_preferred": True,
                    "force_entry_deadline_seconds": 42,
                },
                "edge": {"min_edge": 0.03, "min_confidence": 0.10},
                "growth_sizing": {"use_growth_score_for_first_entry": False},
            }
        ),
        _risk_caps_pass=lambda candidate: True,
    )
    bot._first_entry_deadline_seconds = lambda: AriesBot._first_entry_deadline_seconds(bot)
    up = Candidate(side="UP", valid=True, reason="candidate_built", edge=0.04, shape_after=PositionShape())
    down = Candidate(side="DOWN", valid=True, reason="candidate_built", edge=-0.04, shape_after=PositionShape())
    probability = SimpleNamespace(confidence=0.15)

    early = AriesBot._select_first_entry(bot, up, down, probability, 17, 283)
    preferred = AriesBot._select_first_entry(bot, up, down, probability, 18, 282)

    assert early == (None, "WATCH", "early_entry_waiting_for_conviction")
    assert preferred == (up, "BUY_UP", "first_directional_entry")


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
            up_stability_score=5 if side == "UP" else 1,
            down_stability_score=5 if side == "DOWN" else 1,
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


def queen_bot():
    bot = SimpleNamespace(
        mode="paper",
        config=Config(
            {
                "queen_of_heaven": {
                    "enabled": True,
                    "modes": ["paper", "live"],
                    "first_entry_only": True,
                    "min_pm_ask": 0.85,
                    "max_pm_ask": 0.90,
                    "min_model_probability": 0.92,
                    "min_confidence": 0.70,
                    "min_edge": 0.025,
                    "min_net_edge": 0.015,
                    "min_win_profit_fraction": 0.10,
                    "max_win_profit_fraction": 0.18,
                    "require_price_on_target_side": True,
                    "min_distance_bps": 4.0,
                    "require_ma_alignment": True,
                    "min_fast_slope_bps": 0.0,
                    "min_mid_slope_bps": -0.05,
                    "min_persistence_seconds": 6,
                    "require_orderbook_alignment": False,
                    "min_seconds_since_open": 20,
                    "max_seconds_since_open": 240,
                    "min_seconds_remaining": 35,
                }
            }
        ),
        _risk_caps_pass=lambda candidate: True,
    )
    bot._select_queen_of_heaven_first_entry = lambda *args: AriesBot._select_queen_of_heaven_first_entry(bot, *args)
    return bot


def queen_state(side="UP", probability=0.94):
    sign = 1 if side == "UP" else -1
    return (
        SimpleNamespace(
            p_up=probability if side == "UP" else 1.0 - probability,
            p_down=probability if side == "DOWN" else 1.0 - probability,
            confidence=abs(probability - 0.5) * 2.0,
            distance_bps=8.0 * sign,
        ),
        SimpleNamespace(
            pressure=side,
            ma_fast_slope_bps=0.20 * sign,
            ma_mid_slope_bps=0.05 * sign,
            persistence_above_target_seconds=10 if side == "UP" else 0,
            persistence_below_target_seconds=10 if side == "DOWN" else 0,
            orderbook_imbalance=0.0,
        ),
        SimpleNamespace(price=101.0 if side == "UP" else 99.0),
        SimpleNamespace(target_price=100.0),
    )


def test_queen_of_heaven_selects_high_price_high_probability_first_entry():
    bot = queen_bot()
    prob, indicators, binance_state, market = queen_state("UP", probability=0.94)
    up = Candidate(
        side="UP",
        valid=True,
        reason="candidate_built",
        role="FIRST_ENTRY",
        price=0.88,
        edge=0.06,
        net_edge=0.05,
        notional_usdc=3.0,
        shape_after=PositionShape(total_cost=3.0),
    )
    down = Candidate(side="DOWN", valid=False, reason="candidate_invalid", role="FIRST_ENTRY")

    selected, decision, reason = AriesBot._select_first_entry(
        bot,
        up,
        down,
        prob,
        45,
        200,
        indicators=indicators,
        binance_state=binance_state,
        market=market,
    )

    assert selected is up
    assert selected.role == "QUEEN_OF_HEAVEN_ENTRY"
    assert selected.model_probability == 0.94
    assert decision == "BUY_UP"
    assert reason == "queen_of_heaven_entry"


def test_queen_of_heaven_rejects_cheap_contracts():
    bot = queen_bot()
    prob, indicators, binance_state, market = queen_state("UP", probability=0.94)
    up = Candidate(
        side="UP",
        valid=True,
        reason="candidate_built",
        role="FIRST_ENTRY",
        price=0.84,
        edge=0.10,
        net_edge=0.09,
        notional_usdc=2.0,
        shape_after=PositionShape(total_cost=2.0),
    )
    down = Candidate(side="DOWN", valid=False, reason="candidate_invalid", role="FIRST_ENTRY")

    selected, decision, reason = AriesBot._select_first_entry(
        bot,
        up,
        down,
        prob,
        45,
        200,
        indicators=indicators,
        binance_state=binance_state,
        market=market,
    )

    assert selected is None
    assert decision == "WATCH"
    assert reason == "queen_ask_below_min"


def test_queen_of_heaven_rejects_low_model_probability():
    bot = queen_bot()
    prob, indicators, binance_state, market = queen_state("UP", probability=0.91)
    up = Candidate(
        side="UP",
        valid=True,
        reason="candidate_built",
        role="FIRST_ENTRY",
        price=0.87,
        edge=0.04,
        net_edge=0.03,
        notional_usdc=2.0,
        shape_after=PositionShape(total_cost=2.0),
    )
    down = Candidate(side="DOWN", valid=False, reason="candidate_invalid", role="FIRST_ENTRY")

    selected, _, reason = AriesBot._select_first_entry(
        bot,
        up,
        down,
        prob,
        45,
        200,
        indicators=indicators,
        binance_state=binance_state,
        market=market,
    )

    assert selected is None
    assert reason == "queen_probability_too_low"


def test_queen_of_heaven_time_preference_waits_until_second_half():
    bot = queen_bot()
    bot.config["queen_of_heaven"].update(
        {
            "time_preference_enabled": True,
            "min_elapsed_fraction": 0.50,
            "max_elapsed_fraction": 0.88,
        }
    )
    prob, indicators, binance_state, market = queen_state("UP", probability=0.94)
    up = Candidate(
        side="UP",
        valid=True,
        reason="candidate_built",
        role="FIRST_ENTRY",
        price=0.88,
        edge=0.06,
        net_edge=0.05,
        notional_usdc=3.0,
        shape_after=PositionShape(total_cost=3.0),
    )
    down = Candidate(side="DOWN", valid=False, reason="candidate_invalid", role="FIRST_ENTRY")

    selected, decision, reason = AriesBot._select_first_entry(
        bot,
        up,
        down,
        prob,
        120,
        180,
        indicators=indicators,
        binance_state=binance_state,
        market=market,
    )

    assert selected is None
    assert decision == "WATCH"
    assert reason == "queen_before_half_window"


def test_sold_side_recovery_can_buy_back_side_after_directional_sell():
    bot = object.__new__(AriesBot)
    bot.config = Config(
        {
            "intra_window_sells": {
                "sold_side_recovery_enabled": True,
                "sold_side_recovery_min_probability": 0.62,
                "sold_side_recovery_min_confidence": 0.20,
                "sold_side_recovery_min_edge": 0.0,
                "sold_side_recovery_min_net_edge": -0.01,
                "sold_side_recovery_min_stability_score": 3,
                "sold_side_recovery_min_seconds_after_sell": 8,
                "sold_side_recovery_min_seconds_remaining": 35,
                "sold_side_recovery_max_ask": 0.88,
                "sold_side_recovery_max_buys_per_side": 1,
                "sold_side_recovery_require_price_on_target_side": True,
                "sold_side_recovery_min_distance_bps": 1.0,
            }
        }
    )
    bot._risk_caps_pass = lambda candidate: True
    shape_after = apply_buy(PositionShape(up_shares=6.0, down_shares=2.0, total_cost=3.0), "DOWN", 2.0, 0.8)
    up = Candidate(side="UP", valid=True, reason="candidate_built", price=0.40, edge=0.0, net_edge=0.0)
    down = Candidate(
        side="DOWN",
        valid=True,
        reason="candidate_built",
        role="REPAIR",
        price=0.40,
        edge=0.25,
        net_edge=0.24,
        shape_after=shape_after,
    )
    prob, indicators, binance_state, market = conviction_state("DOWN")
    fill = SimpleNamespace(
        created_at=100.0,
        action="SELL",
        side="DOWN",
        reason="directional_weak_side_exit",
        metadata={"candidate": {"role": "DIRECTIONAL_WEAK_SIDE_EXIT"}},
    )

    selected, decision, reason = AriesBot._select_sold_side_recovery(
        bot, up, down, prob, indicators, binance_state, market,
        [fill], 130, 170, 130.0,
    )

    assert selected is down
    assert selected.role == "SOLD_SIDE_RECOVERY"
    assert decision == "BUY_DOWN"
    assert reason == "sold_side_recovery_after_weak_sell"


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
