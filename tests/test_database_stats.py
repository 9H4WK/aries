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
