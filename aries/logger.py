from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class JsonlLogger:
    def __init__(self, decisions_path: str, fills_path: str, console: bool = True):
        self.decisions_path = Path(decisions_path)
        self.fills_path = Path(fills_path)
        self.console = console
        self._last_status_len = 0
        self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
        self.fills_path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, path: Path, prefix: str, payload: dict[str, Any]) -> None:
        payload = {"logged_at": time.time(), **payload}
        line = json.dumps(payload, separators=(",", ":"), default=str)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def event(self, event_name: str, payload: dict[str, Any], stream: str = "decisions") -> None:
        path = self.decisions_path if stream == "decisions" else self.fills_path
        out = {"event": event_name, **payload}
        self._write(path, f"ARIES_{event_name}", out)

    def decision(self, payload: dict[str, Any]) -> None:
        self.event("DECISION", payload, stream="decisions")

    def candidate_evaluation(self, payload: dict[str, Any]) -> None:
        self.event("CANDIDATE_EVALUATION", payload, stream="decisions")

    def position_shape(self, payload: dict[str, Any]) -> None:
        self.event("POSITION_SHAPE", payload, stream="decisions")

    def paper_order_attempt(self, payload: dict[str, Any]) -> None:
        self.event("PAPER_ORDER_ATTEMPT", payload, stream="fills")

    def paper_order_accepted(self, payload: dict[str, Any]) -> None:
        self.event("PAPER_ORDER_ACCEPTED", payload, stream="fills")

    def paper_order_rejected(self, payload: dict[str, Any]) -> None:
        self.event("PAPER_ORDER_REJECTED", payload, stream="fills")

    def fill(self, payload: dict[str, Any]) -> None:
        self.event("PAPER_FILL", payload, stream="fills")

    def dry_fill(self, payload: dict[str, Any]) -> None:
        self.event("DRY_FILL", payload, stream="fills")

    def window_resolved(self, payload: dict[str, Any]) -> None:
        self.event("WINDOW_RESOLVED", payload, stream="decisions")

    def window_pnl(self, payload: dict[str, Any]) -> None:
        self.event("WINDOW_PNL", payload, stream="decisions")

    def validation_passed(self, payload: dict[str, Any]) -> None:
        self.event("VALIDATION_PASSED", payload, stream="decisions")

    def validation_failed(self, payload: dict[str, Any]) -> None:
        self.event("VALIDATION_FAILED", payload, stream="decisions")

    def info(self, message: str, **fields: Any) -> None:
        return

    def ma_status(self, ma_fast: float | None, ma_mid: float | None, ma_slow: float | None) -> None:
        if not self.console:
            return

        if ma_fast is None or ma_mid is None or ma_slow is None:
            line = "MA waiting for enough BTC data..."
        else:
            is_up = ma_fast > ma_slow and ma_mid > ma_slow
            color = "\033[92m" if is_up else "\033[91m"
            reset = "\033[0m"
            fast_vs_mid = ">" if ma_fast > ma_mid else "<="
            mid_vs_slow = ">" if ma_mid > ma_slow else "<="
            trend = "UP" if is_up else "DOWN"
            line = (
                f"{color}MA fast={ma_fast:.2f} mid={ma_mid:.2f} slow={ma_slow:.2f} "
                f"| fast{fast_vs_mid}mid | mid{mid_vs_slow}slow | {trend}{reset}"
            )

        padding = " " * max(0, self._last_status_len - len(line))
        print(f"\r{line}{padding}", end="", flush=True)
        self._last_status_len = len(line)
