from pathlib import Path

from aries.database import Database
from aries.logger import JsonlLogger


def test_jsonl_logger_keeps_file_under_configured_cap(tmp_path: Path):
    logger = JsonlLogger(
        str(tmp_path / "decisions.jsonl"),
        str(tmp_path / "fills.jsonl"),
        console=False,
        max_decisions_log_mb=0.003,
        max_fills_log_mb=0.003,
        max_decisions_log_rows=10,
    )

    for index in range(100):
        logger.decision({"index": index, "payload": "x" * 200})

    assert (tmp_path / "decisions.jsonl").stat().st_size <= int(0.003 * 1024 * 1024)
    assert len((tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()) <= 10


def test_database_maintenance_prunes_only_decision_telemetry(tmp_path: Path):
    db = Database(str(tmp_path / "aries.db"), max_database_mb=1)
    for index in range(20):
        db.insert_decision(
            {
                "timestamp": float(index),
                "window_start": 100,
                "market_id": "m1",
                "decision": "SKIP",
                "selected_reason": "routine_snapshot",
            }
        )
    db.upsert_window_pnl(
        {
            "window_start": 100,
            "market_id": "m1",
            "up_shares": 2.0,
            "down_shares": 2.0,
            "total_cost": 1.5,
            "pnl_if_up": 0.5,
            "pnl_if_down": 0.5,
            "realized_pnl": 0.5,
            "winning_side": "UP",
            "locked": True,
            "notes": "must_be_retained",
        }
    )

    result = db.maintain_storage(decision_rows_to_keep=5)

    assert result["decisions_after"] == 5
    assert db.total_realized_pnl() == 0.5
    assert db.get_window_pnl("m1", 100)["notes"] == "must_be_retained"
