import json
from pathlib import Path
from types import SimpleNamespace

from aries.logger import JsonlLogger
from aries.live_html import LiveHtmlDashboard


def test_live_html_writes_self_refreshing_dashboard(tmp_path):
    output_path = tmp_path / "charts" / "live.html"
    data_path = tmp_path / "charts" / "live_data.json"
    logger = JsonlLogger(
        str(tmp_path / "decisions.jsonl"),
        str(tmp_path / "fills.jsonl"),
        console=False,
        live_html_config={
            "enabled": True,
            "output_path": str(output_path),
            "data_path": str(data_path),
            "refresh_seconds": 1,
            "write_interval_seconds": 0,
            "history_points": 80,
            "candle_seconds": 1,
        },
    )
    decision = SimpleNamespace(
        timestamp=1_800_000_000.0,
        mode="paper",
        decision="BUY_UP",
        strategy_state="LOOKING_FOR_FIRST_ENTRY",
        selected_side="UP",
        selected_reason="first_directional_entry",
        order_status="FILLED",
        market_slug="btc-updown-5m-1800000000",
        seconds_since_open=18,
        seconds_remaining=282,
        account_balance_usdc=50.0,
        account_realized_pnl_usdc=0.0,
        account_win_rate_percent=50.0,
        btc_price=100_010.0,
        target_price=100_000.0,
        distance_from_target=10.0,
        distance_bps=1.0,
        ma_fast=100_005.0,
        ma_mid=100_003.0,
        ma_slow=100_000.0,
        ma_anchor=99_990.0,
        ma_fast_slope_bps=0.2,
        ma_mid_slope_bps=0.1,
        p_up=0.62,
        p_down=0.38,
        confidence=0.24,
        up_bid=0.58,
        up_ask=0.60,
        down_bid=0.40,
        down_ask=0.42,
        edge_up=0.02,
        edge_down=-0.04,
        current_volume=0.5,
        volume_ma_5=0.3,
        volume_ma_10=0.25,
        volume_spike=2.0,
        orderbook_imbalance=0.25,
        up_stability_score=5,
        down_stability_score=1,
        position_after={
            "up_shares": 1.0,
            "down_shares": 0.0,
            "total_cost": 0.60,
            "pnl_if_up": 0.40,
            "pnl_if_down": -0.60,
            "worst_case_pnl": -0.60,
            "locked": False,
        },
        average_entry_up=0.60,
        average_entry_down=None,
        support_resistance={
            "bias_side": "UP",
            "bias_score": 0.4,
            "nearest_support": 99_900.0,
            "nearest_resistance": 100_200.0,
        },
        polymarket_flow={
            "bias_side": "UP",
            "signal": 0.3,
            "trade_imbalance": 0.2,
            "trade_volume_shares": 25.0,
        },
        candidate_up={
            "price": 0.60,
            "role": "FIRST_ENTRY",
            "edge": 0.02,
            "shares": 1.0,
            "notional_usdc": 0.60,
        },
        candidate_down={},
        sell_candidate={},
    )

    logger.terminal_status(decision)
    down_decision = SimpleNamespace(**decision.__dict__)
    down_decision.timestamp = decision.timestamp + 1
    down_decision.time = None
    down_decision.decision = "BUY_DOWN"
    down_decision.selected_side = "DOWN"
    down_decision.selected_reason = "deadline_best_mispriced_side"
    down_decision.p_up = 0.42
    down_decision.p_down = 0.58
    down_decision.candidate_up = {}
    down_decision.candidate_down = {
        "price": 0.42,
        "role": "FIRST_ENTRY",
        "edge": 0.16,
        "shares": 2.0,
        "notional_usdc": 0.84,
    }
    logger.terminal_status(down_decision)

    missing_target_decision = SimpleNamespace(**decision.__dict__)
    missing_target_decision.timestamp = decision.timestamp + 2
    missing_target_decision.target_price = None
    missing_target_decision.distance_from_target = 10.0
    missing_target_decision.distance_bps = None
    logger.terminal_status(missing_target_decision)

    rejected_decision = SimpleNamespace(**decision.__dict__)
    rejected_decision.timestamp = decision.timestamp + 3
    rejected_decision.decision = "BUY_DOWN"
    rejected_decision.selected_side = "DOWN"
    rejected_decision.selected_reason = "locks_structure"
    rejected_decision.order_status = "REJECTED"
    rejected_decision.order_reason = "same_side_add_blocked"
    rejected_decision.candidate_up = {}
    rejected_decision.candidate_down = {
        "price": 0.41,
        "role": "REPAIR",
        "edge": -0.02,
        "shares": 3.0,
        "notional_usdc": 1.23,
    }
    logger.terminal_status(rejected_decision)

    html = output_path.read_text(encoding="utf-8")
    data = data_path.read_text(encoding="utf-8")
    assert "Polymarket BTC Trading Dashboard" in html
    assert 'http-equiv="refresh"' not in html
    assert "window.ARIES_DATA_URL" in html
    assert "setInterval(pollData" in html
    assert "pollData();" in html
    assert "live_data.json" in html
    assert "btc-updown-5m-1800000000" in data
    payload = json.loads(data)
    assert payload["points"][-1]["target"] == 100_000.0
    assert all(event["order_status"] == "FILLED" for event in payload["events"])
    assert all(event["order_reason"] != "same_side_add_blocked" for event in payload["events"])
    assert "Price to beat" in payload["html"]["left"]
    assert "$100000.00" in payload["html"]["left"]
    assert "active PnL:" in data
    assert len(html) < 80_000
    assert "BUY_UP" in data
    assert "BUY_DOWN" in data
    assert "BUY_DOWN at" in data
    assert "marker_color" in html
    assert "#ff5c73" in data
    assert 'class=\\"info\\"' in data
    assert "hover candles, lines, and order markers for details" in html
    assert "Order/fill:" in data
    assert "priceChart" in html
    assert "RSI 14" in html
    assert "MACD 12 26 9" in html
    assert 'data-h="430"' in html
    assert "requestAnimationFrame" in html


def test_live_html_skips_locked_windows_data_file(tmp_path, monkeypatch):
    dashboard = LiveHtmlDashboard(
        {
            "enabled": True,
            "output_path": str(tmp_path / "charts" / "live.html"),
            "data_path": str(tmp_path / "charts" / "live_data.json"),
        }
    )
    locked_path = dashboard.data_path
    locked_path.parent.mkdir(parents=True, exist_ok=True)
    original_replace = Path.replace

    def locked_replace(self, target):
        if Path(target) == locked_path:
            raise PermissionError(5, "Access is denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", locked_replace)

    assert dashboard._atomic_write_text(locked_path, "{}") is False
    assert not list(locked_path.parent.glob("*.tmp"))
