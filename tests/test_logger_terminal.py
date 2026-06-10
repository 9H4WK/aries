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
    output = capsys.readouterr().out

    assert "S_t/target=1.010000" in output
    assert "P(S_T>target)=0.600" in output
    assert "SIZE UP" in output
    assert "kelly=2.00%" in output
