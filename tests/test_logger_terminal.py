import re
from types import SimpleNamespace

from aries.logger import JsonlLogger


def test_terminal_shows_growth_and_market_ratios(tmp_path, capsys):
    logger = JsonlLogger(
        str(tmp_path / "decisions.jsonl"),
        str(tmp_path / "fills.jsonl"),
        console=True,
    )
    candidate = {
        "valid": True,
        "net_price": 0.502,
        "net_edge": 0.098,
        "applied_kelly_fraction": 0.02,
        "kelly_cap_usdc": 2.0,
        "allowed_notional_usdc": 2.0,
        "notional_usdc": 2.0,
        "ladder_step": 1,
        "ladder_bankroll_ratio": 0.02,
        "repair_target_usdc": None,
        "expected_log_growth_delta": 0.001,
        "inventory_imbalance": 4.0,
        "growth_score": 0.0005,
        "account_scale": 1.0,
    }
    decision = SimpleNamespace(
        p_up=0.60,
        p_down=0.40,
        btc_price=101.0,
        target_price=100.0,
        spot_target_ratio=1.01,
        time_remaining_ratio=0.50,
        growth_sizing_enabled=True,
        candidate_up=candidate,
        candidate_down=candidate,
        position_after={},
    )

    logger.terminal_status(decision)
    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)

    assert "ARIES // BTC 5M // PAPER TRADING" in output
    assert "S_t/target: 1.010000" in output
    assert "P(S_T>target): 0.600" in output
    assert "SIZING UP" in output
    assert "SIZING DOWN" in output
    assert "UP VALID" in output
    assert "kelly/account: 2.00% / 2.00%" in output


def test_live_terminal_uses_live_account_not_paper_stats(tmp_path, capsys):
    logger = JsonlLogger(
        str(tmp_path / "decisions.jsonl"),
        str(tmp_path / "fills.jsonl"),
        console=True,
    )
    logger.set_live_account_snapshot(
        {
            "enabled": True,
            "connected": True,
            "collateral_balance_usdc": 37.92,
            "open_orders_count": 1,
            "recent_trades_count": 2,
            "open_positions_count": 1,
            "closed_positions_count": 4,
            "latest_event": "trade",
        }
    )
    decision = SimpleNamespace(
        mode="live",
        account_balance_usdc=96.77,
        account_realized_pnl_usdc=-3.23,
        account_win_rate_percent=14.3,
        candidate_up={},
        candidate_down={},
        position_after={},
    )

    logger.terminal_status(decision)
    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)

    assert "ARIES // BTC 5M // LIVE EXECUTION" in output
    assert "LIVE ACCOUNT" in output
    assert "CLOB cash: $37.92" in output
    assert "Open orders: 1" in output
    assert "Paper balance" not in output
    assert "Settled PnL" not in output
    assert "Win rate" not in output


def test_terminal_can_show_sr_panel_and_hide_noisy_panels(tmp_path, capsys):
    logger = JsonlLogger(
        str(tmp_path / "decisions.jsonl"),
        str(tmp_path / "fills.jsonl"),
        console=True,
        dashboard_panels={
            "account": False,
            "window": False,
            "signal": False,
            "polymarket": False,
            "probability": False,
            "position": False,
            "trend": False,
            "support_resistance": True,
            "flow_scores": False,
            "trade_tape": False,
            "execution": False,
            "sizing_up": False,
            "sizing_down": False,
            "sell_plan": False,
            "risk_timing": False,
            "optimization": False,
        },
    )
    decision = SimpleNamespace(
        mode="paper",
        candidate_up={},
        candidate_down={},
        position_after={},
        support_resistance={
            "enabled": True,
            "reason": "near_support+support_bounce",
            "bias_side": "UP",
            "bias_score": 0.42,
            "p_up_bias": 0.71,
            "p_down_bias": 0.29,
            "nearest_support": 100.0,
            "support_strength": 4.0,
            "support_distance_bps": 12.5,
            "nearest_resistance": 110.0,
            "resistance_strength": 3.0,
            "resistance_distance_bps": 18.0,
            "bounce_score": 0.60,
            "rejection_score": 0.10,
            "breakout_state": "NONE",
            "retest_state": "NONE",
            "nested_structure": "SUPPORT_NESTED",
            "zones_count": 5,
        },
    )

    logger.terminal_status(decision)
    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)

    assert "SUPPORT / RESISTANCE" in output
    assert "Bias: UP score=0.420" in output
    assert "Support: $100.00 str=4.0 dist=12.50bps" in output
    assert "POLYMARKET" not in output
    assert "FLOW + SCORES" not in output
    assert "EXECUTION" not in output
    assert "RISK + TIMING" not in output
    assert "OPTIMIZATION" not in output


def test_trade_tape_only_shows_trades_from_current_window(tmp_path, capsys):
    logger = JsonlLogger(
        str(tmp_path / "decisions.jsonl"),
        str(tmp_path / "fills.jsonl"),
        console=True,
        dashboard_panels={
            "account": False,
            "window": False,
            "signal": False,
            "polymarket": False,
            "probability": False,
            "position": False,
            "trend": False,
            "support_resistance": False,
            "flow_scores": False,
            "trade_tape": True,
            "execution": False,
            "sizing_up": False,
            "sizing_down": False,
            "sell_plan": False,
            "risk_timing": False,
            "optimization": False,
        },
    )
    first_window = SimpleNamespace(
        mode="paper",
        market_id="m1",
        window_start=100,
        decision="BUY_UP",
        selected_side="UP",
        selected_reason="first_directional_entry",
        candidate_up={"price": 0.90, "role": "FIRST_ENTRY"},
        candidate_down={},
        sell_candidate={},
        order_status="FILLED",
        order_id=1,
        position_after={},
    )
    second_window = SimpleNamespace(
        mode="paper",
        market_id="m2",
        window_start=200,
        decision="BUY_DOWN",
        selected_side="DOWN",
        selected_reason="first_directional_entry",
        candidate_up={},
        candidate_down={"price": 0.20, "role": "FIRST_ENTRY"},
        sell_candidate={},
        order_status="FILLED",
        order_id=2,
        position_after={},
    )
    current = SimpleNamespace(
        mode="paper",
        market_id="m2",
        window_start=200,
        decision="WATCH",
        selected_side=None,
        selected_reason="hold",
        candidate_up={},
        candidate_down={},
        sell_candidate={},
        order_status=None,
        order_id=None,
        position_after={},
    )

    logger.terminal_status(first_window)
    logger.terminal_status(second_window)
    logger.terminal_status(current)
    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split("ARIES // BTC 5M")[-1]

    assert "Last 1: BUY_DOWN px=0.200" in output
    assert "px=0.900" not in output
