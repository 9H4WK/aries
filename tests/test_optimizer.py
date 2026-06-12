import json

from aries.config import Config
from aries.optimizer import StrategyOptimizer


class FakeDb:
    def __init__(self):
        self.windows = []
        self.fills = []
        self.decisions = []
        self.live = []
        self.live_windows = []

    def list_window_report_rows(self):
        return self.windows

    def list_all_paper_fills(self):
        return self.fills

    def list_decisions(self):
        return self.decisions

    def list_live_order_audits(self):
        return self.live

    def list_live_window_report_rows(self):
        return self.live_windows

    def list_all_matched_live_orders(self):
        return [row for row in self.live if row.get("status") == "MATCHED"]


def make_optimizer():
    db = FakeDb()
    config = Config(
        {
            "optimization": {
                "min_settled_windows": 20,
                "min_samples_per_bucket": 10,
                "min_calibration_samples": 20,
                "validation_fraction": 0.30,
                "time_bucket_seconds": 30,
                "price_bucket_width": 0.10,
            }
        }
    )
    for index in range(30):
        window_start = index * 300
        won = index % 4 != 0
        pnl = 0.50 if won else -1.0
        market_id = f"market-{index}"
        db.windows.append(
            {
                "window_start": window_start,
                "market_id": market_id,
                "winning_side": "UP" if won else "DOWN",
                "total_cost": 1.0,
                "realized_pnl": pnl,
                "number_of_fills": 1,
            }
        )
        db.fills.append(
            {
                "created_at": window_start + 65,
                "window_start": window_start,
                "market_id": market_id,
                "side": "UP",
                "price": 2.0 / 3.0,
                "notional_usdc": 1.0,
                "metadata_json": json.dumps(
                    {
                        "strategy_reason": "high_conviction_same_side_add",
                        "candidate": {"role": "SAME_SIDE_ADD"},
                    }
                ),
            }
        )
        db.decisions.append(
            {
                "payload": {
                    "decision": "BUY_UP",
                    "market_id": market_id,
                    "window_start": window_start,
                    "p_up": 0.70,
                }
            }
        )
    return StrategyOptimizer(config, db)


def test_optimizer_requires_walk_forward_profitability():
    report = make_optimizer().analyze()

    assert report["status"] == "ready"
    assert report["recommendations"]["fill_time"]["bucket"] == "060-089s"
    assert report["recommendations"]["role"]["bucket"] == "SAME_SIDE_ADD"
    assert report["probability_calibration"]["status"] == "ready"


def test_optimizer_writes_advisory_report(tmp_path):
    path = tmp_path / "optimization_summary.json"

    payload = make_optimizer().write_report(path)

    assert payload["mode"] == "advisory_only"
    assert json.loads(path.read_text(encoding="utf-8"))["settled_traded_windows"] == 30


def test_optimizer_uses_live_settled_source_in_live_mode():
    optimizer = make_optimizer()
    optimizer.config["mode"] = "live"
    optimizer.db.live_windows = list(optimizer.db.windows)
    optimizer.db.live = [
        {
            "status": "MATCHED",
            "created_at": fill["created_at"],
            "window_start": fill["window_start"],
            "market_id": fill["market_id"],
            "side": fill["side"],
            "price": fill["price"],
            "notional_usdc": fill["notional_usdc"],
            "response_json": fill["metadata_json"],
        }
        for fill in optimizer.db.fills
    ]

    report = optimizer.analyze()

    assert report["data_source"] == "live"
    assert report["live_matched_orders_excluded"] == 0
