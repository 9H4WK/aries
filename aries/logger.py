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

    def _fmt(self, value: Any, digits: int = 2) -> str:
        if value is None:
            return "n/a"
        if isinstance(value, float):
            return f"{value:.{digits}f}"
        return str(value)

    def terminal_status(self, decision: Any) -> None:
        if not self.console:
            return

        ma_fast = getattr(decision, "ma_fast", None)
        ma_mid = getattr(decision, "ma_mid", None)
        ma_slow = getattr(decision, "ma_slow", None)
        ma_is_up = ma_fast is not None and ma_mid is not None and ma_slow is not None and ma_fast > ma_slow and ma_mid > ma_slow
        color = "\033[92m" if ma_is_up else "\033[91m"
        reset = "\033[0m"
        trend = "UP" if ma_is_up else "DOWN"
        fast_vs_mid = ">" if ma_fast is not None and ma_mid is not None and ma_fast > ma_mid else "<="
        mid_vs_slow = ">" if ma_mid is not None and ma_slow is not None and ma_mid > ma_slow else "<="

        lines = [
            "ARIES BTC 5m probability monitor",
            (
                f"{color}MA: fast={self._fmt(ma_fast)} {fast_vs_mid} mid={self._fmt(ma_mid)} "
                f"| mid={self._fmt(ma_mid)} {mid_vs_slow} slow={self._fmt(ma_slow)} | {trend}{reset}"
            ),
            (
                f"Price/Target: btc={self._fmt(getattr(decision, 'btc_price', None))} "
                f"target={self._fmt(getattr(decision, 'target_price', None))} "
                f"dist_usd={self._fmt(getattr(decision, 'distance_from_target', None), 2)} "
                f"dist_bps={self._fmt(getattr(decision, 'distance_bps', None), 2)} "
                f"dist_score={self._fmt(getattr(decision, 'distance_score', None), 3)} "
                f"time_left={self._fmt(getattr(decision, 'seconds_remaining', None), 0)}s"
            ),
            (
                f"Volume/Book: cur_vol={self._fmt(getattr(decision, 'current_volume', None), 4)} "
                f"vma5={self._fmt(getattr(decision, 'volume_ma_5', None), 4)} "
                f"vma10={self._fmt(getattr(decision, 'volume_ma_10', None), 4)} "
                f"spike={self._fmt(getattr(decision, 'volume_spike', None), 3)} "
                f"obi={self._fmt(getattr(decision, 'orderbook_imbalance', None), 3)}"
            ),
            (
                f"Slopes/Scores: fast_slope={self._fmt(getattr(decision, 'ma_fast_slope_bps', None), 3)}bps "
                f"mid_slope={self._fmt(getattr(decision, 'ma_mid_slope_bps', None), 3)}bps "
                f"vel5={self._fmt(getattr(decision, 'ma_velocity_5s_bps', None), 3)}bps "
                f"pers_above={self._fmt(getattr(decision, 'persistence_above_target_seconds', None), 0)} "
                f"pers_below={self._fmt(getattr(decision, 'persistence_below_target_seconds', None), 0)} "
                f"ma_score={self._fmt(getattr(decision, 'ma_score', None), 3)} "
                f"slope_score={self._fmt(getattr(decision, 'slope_score', None), 3)} "
                f"vol_score={self._fmt(getattr(decision, 'volume_score', None), 3)} "
                f"imb_score={self._fmt(getattr(decision, 'imbalance_score', None), 3)} "
                f"time_factor={self._fmt(getattr(decision, 'time_factor', None), 3)}"
            ),
            (
                f"PM/Probs: up_ask={self._fmt(getattr(decision, 'up_ask', None), 3)} "
                f"down_ask={self._fmt(getattr(decision, 'down_ask', None), 3)} "
                f"p_up={self._fmt(getattr(decision, 'p_up', None), 3)} "
                f"p_down={self._fmt(getattr(decision, 'p_down', None), 3)} "
                f"raw={self._fmt(getattr(decision, 'raw_score', None), 3)} "
                f"edge_up={self._fmt(getattr(decision, 'edge_up', None), 3)} "
                f"edge_down={self._fmt(getattr(decision, 'edge_down', None), 3)}"
            ),
        ]
        print("\033[2J\033[H" + "\n".join(lines), end="", flush=True)
