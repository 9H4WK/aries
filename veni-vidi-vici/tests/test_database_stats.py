from pathlib import Path

from aries.database import Database


def test_win_rate_uses_profitable_settled_traded_windows(tmp_path: Path):
    db = Database(str(tmp_path / "aries.db"))
    for window_start, total_cost, realized_pnl in (
        (100, 2.0, 1.0),
        (200, 2.0, -0.5),
        (300, 2.0, 0.25),
        (400, 0.0, 1.0),
    ):
        db.upsert_window_pnl(
            {
                "window_start": window_start,
                "market_id": f"m{window_start}",
                "up_shares": 2.0,
                "down_shares": 2.0,
                "total_cost": total_cost,
                "pnl_if_up": realized_pnl,
                "pnl_if_down": realized_pnl,
                "realized_pnl": realized_pnl,
                "winning_side": "UP",
                "locked": realized_pnl > 0,
                "notes": "",
            }
        )

    assert db.win_rate_percent() == 2 / 3 * 100


def test_live_window_report_reconstructs_matched_order_pnl(tmp_path: Path):
    db = Database(str(tmp_path / "aries.db"))
    db.insert_live_order_audit(
        {
            "created_at": 110.0,
            "decision_id": "d1",
            "window_start": 100,
            "window_end": 400,
            "market_id": "m1",
            "token_id": "up",
            "side": "UP",
            "action": "BUY",
            "price": 0.50,
            "notional_usdc": 2.0,
            "shares": 4.0,
            "status": "MATCHED",
            "external_order_id": "o1",
            "reason": "clob_order_accepted",
            "response_json": {},
        }
    )
    db.upsert_window_result(
        {
            "window_start": 100,
            "window_end": 400,
            "market_id": "m1",
            "target_price": 100.0,
            "final_btc_price": 101.0,
            "winning_side": "UP",
            "resolved": True,
            "resolved_at": 401.0,
        }
    )

    row = db.list_live_window_report_rows()[0]

    assert row["total_cost"] == 2.0
    assert row["realized_pnl"] == 2.0
    assert row["number_of_fills"] == 1
