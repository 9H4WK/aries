from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class JsonlLogger:
    def __init__(self, decisions_path: str, fills_path: str, console: bool = True, account_balance_usdc: float = 0.0):
        self.decisions_path = Path(decisions_path)
        self.fills_path = Path(fills_path)
        self.console = console
        self.account_balance_usdc = account_balance_usdc
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

    def _probability_bar(self, value: Any, color: str, width: int = 20) -> str:
        if value is None:
            return "[" + ("-" * width) + "]"
        filled = max(0, min(width, round(float(value) * width)))
        return f"{color}[{'#' * filled}{'-' * (width - filled)}]\033[0m"

    def terminal_status(self, decision: Any) -> None:
        if not self.console:
            return

        ma_fast = getattr(decision, "ma_fast", None)
        ma_mid = getattr(decision, "ma_mid", None)
        ma_slow = getattr(decision, "ma_slow", None)
        ma_is_up = ma_fast is not None and ma_mid is not None and ma_slow is not None and ma_fast > ma_slow and ma_mid > ma_slow
        color = "\033[92m" if ma_is_up else "\033[91m"
        reset = "\033[0m"
        green = "\033[92m"
        red = "\033[91m"
        yellow = "\033[93m"
        cyan = "\033[96m"
        trend = "UP" if ma_is_up else "DOWN"
        fast_vs_mid = ">" if ma_fast is not None and ma_mid is not None and ma_fast > ma_mid else "<="
        mid_vs_slow = ">" if ma_mid is not None and ma_slow is not None and ma_mid > ma_slow else "<="

        p_up = getattr(decision, "p_up", None)
        p_down = getattr(decision, "p_down", None)
        probable_side = "UP" if p_up is not None and p_down is not None and p_up >= p_down else "DOWN"
        probable_color = green if probable_side == "UP" else red
        shape = getattr(decision, "position_after", {}) or {}
        locked = bool(shape.get("locked", False))
        pnl_color = green if locked else red

        lines = [
            (
                f"{cyan}ARIES BTC 5m paper monitor | "
                f"balance=${self._fmt(getattr(decision, 'account_balance_usdc', self.account_balance_usdc), 2)} "
                f"settled_pnl={self._fmt(getattr(decision, 'account_realized_pnl_usdc', 0.0), 2)}{reset}"
            ),
            (
                f"State/Decision: state={yellow}{getattr(decision, 'strategy_state', 'UNKNOWN')}{reset} "
                f"action={getattr(decision, 'decision', 'SKIP')} side={self._fmt(getattr(decision, 'selected_side', None))} "
                f"reason={getattr(decision, 'selected_reason', '')} "
                f"order={self._fmt(getattr(decision, 'order_status', None))}"
            ),
            (
                f"Timing/Lock: open={self._fmt(getattr(decision, 'seconds_since_open', None), 0)}s "
                f"since_first_fill={self._fmt(getattr(decision, 'seconds_since_first_fill', None), 1)}s "
                f"repair_min={self._fmt(getattr(decision, 'repair_start_after_first_entry_seconds', None), 0)}s "
                f"repair_preferred={self._fmt(getattr(decision, 'preferred_repair_after_first_entry_seconds', None), 0)}s "
                f"stop_when_locked={getattr(decision, 'stop_when_locked', True)} "
                f"allow_post_lock_adds={getattr(decision, 'allow_post_lock_adds', False)}"
            ),
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
                f"Probabilities: UP {self._probability_bar(p_up, green)} {self._fmt(p_up, 3)} "
                f"DOWN {self._probability_bar(p_down, red)} {self._fmt(p_down, 3)} "
                f"likely={probable_color}{probable_side}{reset} confidence={self._fmt(getattr(decision, 'confidence', None), 3)}"
            ),
            (
                f"PM/Edge: up_ask={self._fmt(getattr(decision, 'up_ask', None), 3)} "
                f"down_ask={self._fmt(getattr(decision, 'down_ask', None), 3)} "
                f"raw={self._fmt(getattr(decision, 'raw_score', None), 3)} "
                f"edge_up={self._fmt(getattr(decision, 'edge_up', None), 3)} "
                f"edge_down={self._fmt(getattr(decision, 'edge_down', None), 3)}"
            ),
            (
                f"Position: cost=${self._fmt(shape.get('total_cost'), 2)} "
                f"up_shares={self._fmt(shape.get('up_shares'), 3)} down_shares={self._fmt(shape.get('down_shares'), 3)} "
                f"pnl_up={self._fmt(shape.get('pnl_if_up'), 2)} pnl_down={self._fmt(shape.get('pnl_if_down'), 2)} "
                f"worst={pnl_color}{self._fmt(shape.get('worst_case_pnl'), 2)} "
                f"{'LOCKED' if locked else 'EXPOSED'}{reset}"
            ),
        ]
        print("\033[2J\033[H" + "\n".join(lines), end="", flush=True)
