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
