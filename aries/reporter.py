from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .database import Database
from .optimizer import StrategyOptimizer


class Reporter:
    def __init__(self, config: dict, db: Database):
        self.config = config
        self.db = db
        self.output_dir = Path(str(self.config.get_path("reports.output_dir", "reports")))

    def write_reports(self) -> None:
        if not bool(self.config.get_path("reports.enabled", True)):
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        window_rows = self.db.list_window_report_rows()
        fill_rows = self.db.list_all_paper_fills()
        order_rows = self.db.list_all_paper_orders()
        live_window_rows = self.db.list_live_window_report_rows()
        live_fill_rows = self.db.list_all_matched_live_orders()

        self._write_json(self.output_dir / "session_summary.json", self._session_summary(window_rows, order_rows, fill_rows))
        self._write_csv(self.output_dir / "window_pnl.csv", window_rows)
        self._write_csv(self.output_dir / "paper_fills.csv", fill_rows)
        self._write_json(
            self.output_dir / "live_session_summary.json",
            self._live_session_summary(live_window_rows, live_fill_rows),
        )
        self._write_csv(self.output_dir / "live_window_pnl.csv", live_window_rows)
        self._write_csv(self.output_dir / "live_matched_orders.csv", live_fill_rows)
        if bool(self.config.get_path("optimization.enabled", True)):
            StrategyOptimizer(self.config, self.db).write_report(self.output_dir / "optimization_summary.json")

    def _session_summary(
        self,
        window_rows: list[dict[str, Any]],
        order_rows: list[dict[str, Any]],
        fill_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        traded = [row for row in window_rows if float(row.get("total_cost", 0.0) or 0.0) > 0.0]
        realized_values = [float(row.get("realized_pnl", 0.0) or 0.0) for row in traded]
        total_realized = sum(realized_values)
        biggest_win = max(realized_values) if realized_values else 0.0
        biggest_loss = min(realized_values) if realized_values else 0.0

        return {
            "total_windows_traded": len(traded),
            "total_paper_orders": len(order_rows),
            "total_paper_fills": len(fill_rows),
            "total_realized_pnl": total_realized,
            "win_count": sum(1 for pnl in realized_values if pnl > 0),
            "loss_count": sum(1 for pnl in realized_values if pnl < 0),
            "average_pnl_per_window": (total_realized / len(traded)) if traded else 0.0,
            "locked_windows_count": sum(1 for row in traded if bool(row.get("locked"))),
            "exposed_windows_count": sum(1 for row in traded if not bool(row.get("locked"))),
            "biggest_win": biggest_win,
            "biggest_loss": biggest_loss,
        }

    def _live_session_summary(
        self,
        window_rows: list[dict[str, Any]],
        matched_orders: list[dict[str, Any]],
    ) -> dict[str, Any]:
        summary = self._session_summary(window_rows, matched_orders, matched_orders)
        summary["total_live_matched_orders"] = summary.pop("total_paper_orders")
        summary.pop("total_paper_fills")
        return summary

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)

    def _write_csv(self, path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        fieldnames = list(rows[0].keys())
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
