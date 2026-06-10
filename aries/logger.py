from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class JsonlLogger:
    def __init__(
        self,
        decisions_path: str,
        fills_path: str,
        console: bool = True,
        account_balance_usdc: float = 0.0,
        max_decisions_log_mb: float = 50.0,
        max_fills_log_mb: float = 50.0,
        max_decisions_log_rows: int = 10000,
    ):
        self.decisions_path = Path(decisions_path)
        self.fills_path = Path(fills_path)
        self.console = console
        self.account_balance_usdc = account_balance_usdc
        self.max_decisions_log_bytes = max(0, int(max_decisions_log_mb * 1024 * 1024))
        self.max_fills_log_bytes = max(0, int(max_fills_log_mb * 1024 * 1024))
        self.max_decisions_log_rows = max(0, int(max_decisions_log_rows))
        self._last_status_len = 0
        self._live_account_snapshot: dict[str, Any] = {}
        self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
        self.fills_path.parent.mkdir(parents=True, exist_ok=True)
        self._line_counts = {
            self.decisions_path: self._count_lines(self.decisions_path),
            self.fills_path: self._count_lines(self.fills_path),
        }

    def _write(self, path: Path, prefix: str, payload: dict[str, Any]) -> None:
        payload = {"logged_at": time.time(), **payload}
        line = json.dumps(payload, separators=(",", ":"), default=str)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._line_counts[path] = self._line_counts.get(path, 0) + 1
        self._compact_if_needed(path)

    def _count_lines(self, path: Path) -> int:
        if not path.exists():
            return 0
        with path.open("rb") as f:
            return sum(1 for _ in f)

    def _max_bytes_for(self, path: Path) -> int:
        return self.max_decisions_log_bytes if path == self.decisions_path else self.max_fills_log_bytes

    def _compact_if_needed(self, path: Path) -> bool:
        """Keep the newest complete JSONL records when a configured cap is reached."""
        max_bytes = self._max_bytes_for(path)
        max_rows = self.max_decisions_log_rows if path == self.decisions_path else 0
        over_bytes = max_bytes > 0 and path.exists() and path.stat().st_size > max_bytes
        over_rows = max_rows > 0 and self._line_counts.get(path, 0) > max_rows
        if not path.exists() or not over_bytes and not over_rows:
            return False

        target_bytes = max(1, int(max_bytes * 0.90)) if max_bytes > 0 else None
        target_rows = max(1, int(max_rows * 0.90)) if max_rows > 0 else None
        lines = path.read_bytes().splitlines(keepends=True)
        kept: list[bytes] = []
        kept_bytes = 0
        for line in reversed(lines):
            if target_rows is not None and len(kept) >= target_rows:
                break
            if target_bytes is not None and kept and kept_bytes + len(line) > target_bytes:
                break
            if target_bytes is not None and len(line) > target_bytes:
                continue
            kept.append(line)
            kept_bytes += len(line)

        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("wb") as f:
            for line in reversed(kept):
                f.write(line)
        temp_path.replace(path)
        self._line_counts[path] = len(kept)
        return True

    def maintain_storage(self) -> dict[str, bool]:
        return {
            "decisions_compacted": self._compact_if_needed(self.decisions_path),
            "fills_compacted": self._compact_if_needed(self.fills_path),
        }

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

    def live_monitor_connected(self, payload: dict[str, Any]) -> None:
        self.event("LIVE_MONITOR_CONNECTED", payload, stream="fills")

    def live_monitor_failed(self, payload: dict[str, Any]) -> None:
        self.event("LIVE_MONITOR_FAILED", payload, stream="fills")

    def live_account_snapshot(self, payload: dict[str, Any]) -> None:
        self.event("LIVE_ACCOUNT_SNAPSHOT", payload, stream="fills")

    def live_user_event(self, payload: dict[str, Any]) -> None:
        self.event("LIVE_USER_EVENT", payload, stream="fills")

    def live_order_attempt(self, payload: dict[str, Any]) -> None:
        self.event("LIVE_ORDER_ATTEMPT", payload, stream="fills")

    def live_order_accepted(self, payload: dict[str, Any]) -> None:
        self.event("LIVE_ORDER_ACCEPTED", payload, stream="fills")

    def live_order_rejected(self, payload: dict[str, Any]) -> None:
        self.event("LIVE_ORDER_REJECTED", payload, stream="fills")

    def set_live_account_snapshot(self, payload: dict[str, Any]) -> None:
        self._live_account_snapshot = dict(payload)

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
        probable_side = None
        if p_up is not None and p_down is not None:
            probable_side = "UP" if p_up >= p_down else "DOWN"
        probable_color = green if probable_side == "UP" else red if probable_side == "DOWN" else reset
        btc_price = getattr(decision, "btc_price", None)
        target_price = getattr(decision, "target_price", None)
        actual_side = None
        if btc_price is not None and target_price is not None:
            actual_side = "UP" if btc_price > target_price else "DOWN"
        actual_color = green if actual_side == "UP" else red if actual_side == "DOWN" else reset
        shape = getattr(decision, "position_after", {}) or {}
        locked = bool(shape.get("locked", False))
        pnl_color = green if locked else red
        candidate_up = getattr(decision, "candidate_up", {}) or {}
        candidate_down = getattr(decision, "candidate_down", {}) or {}
        mode = str(getattr(decision, "mode", "paper")).upper()

        def sizing_line(label: str, candidate: dict[str, Any], side_color: str) -> str:
            ratio = candidate.get("ladder_bankroll_ratio")
            ratio_percent = ratio * 100.0 if ratio is not None else None
            kelly = candidate.get("applied_kelly_fraction")
            kelly_percent = kelly * 100.0 if kelly is not None else None
            return (
                f"{side_color}SIZE {label}{reset}: valid={candidate.get('valid', False)} "
                f"net={self._fmt(candidate.get('net_price'), 3)} net_edge={self._fmt(candidate.get('net_edge'), 3)} "
                f"kelly={self._fmt(kelly_percent, 2)}% cap=${self._fmt(candidate.get('kelly_cap_usdc'), 2)} "
                f"allowed=${self._fmt(candidate.get('allowed_notional_usdc'), 2)} "
                f"clip=${self._fmt(candidate.get('notional_usdc'), 2)} L{self._fmt(candidate.get('ladder_step'), 0)} "
                f"acct_ratio={self._fmt(ratio_percent, 2)}% repair=${self._fmt(candidate.get('repair_target_usdc'), 2)} "
                f"logG={self._fmt(candidate.get('expected_log_growth_delta'), 5)} "
                f"inv={self._fmt(candidate.get('inventory_imbalance'), 2)} score={self._fmt(candidate.get('growth_score'), 5)}"
            )

        lines = [
            (
                f"{cyan}=== ARIES BTC 5m {mode} MONITOR =================================================={reset}"
            ),
            (
                f"{cyan}ACCOUNT{reset}: "
                f"balance=${self._fmt(getattr(decision, 'account_balance_usdc', self.account_balance_usdc), 2)} "
                f"settled_pnl={self._fmt(getattr(decision, 'account_realized_pnl_usdc', 0.0), 2)} "
                f"win_rate={self._fmt(getattr(decision, 'account_win_rate_percent', 0.0), 1)}%{reset}"
            ),
            (
                f"{cyan}SIGNAL{reset}: likely={probable_color}{self._fmt(probable_side)}{reset} | "
                f"actual={actual_color}{self._fmt(actual_side)}{reset} "
                f"confidence={self._fmt(getattr(decision, 'confidence', None), 3)} "
                f"action={yellow}{getattr(decision, 'decision', 'SKIP')}{reset} "
                f"side={self._fmt(getattr(decision, 'selected_side', None))} "
                f"reason={getattr(decision, 'selected_reason', '')} "
                f"order={self._fmt(getattr(decision, 'order_status', None))}"
            ),
            (
                f"{cyan}STATE{reset}: state={yellow}{getattr(decision, 'strategy_state', 'UNKNOWN')}{reset} "
                f"open={self._fmt(getattr(decision, 'seconds_since_open', None), 0)}s "
                f"left={self._fmt(getattr(decision, 'seconds_remaining', None), 0)}s "
                f"T_ratio={self._fmt(getattr(decision, 'time_remaining_ratio', None), 3)} "
                f"since_first_fill={self._fmt(getattr(decision, 'seconds_since_first_fill', None), 1)}s "
                f"repair_min={self._fmt(getattr(decision, 'repair_start_after_first_entry_seconds', None), 0)}s "
                f"repair_preferred={self._fmt(getattr(decision, 'preferred_repair_after_first_entry_seconds', None), 0)}s "
                f"stop_when_locked={getattr(decision, 'stop_when_locked', True)} "
                f"allow_post_lock_adds={getattr(decision, 'allow_post_lock_adds', False)}"
            ),
            (
                f"{cyan}MARKET{reset}: btc={self._fmt(getattr(decision, 'btc_price', None))} "
                f"target={self._fmt(getattr(decision, 'target_price', None))} "
                f"S_t/target={self._fmt(getattr(decision, 'spot_target_ratio', None), 6)} "
                f"gap=${self._fmt(getattr(decision, 'distance_from_target', None), 2)} "
                f"gap_bps={self._fmt(getattr(decision, 'distance_bps', None), 2)} "
                f"PM up/down={self._fmt(getattr(decision, 'up_ask', None), 3)}/{self._fmt(getattr(decision, 'down_ask', None), 3)} "
                f"edge={self._fmt(getattr(decision, 'edge_up', None), 3)}/{self._fmt(getattr(decision, 'edge_down', None), 3)}"
            ),
            (
                f"{color}TREND: fast={self._fmt(ma_fast)} {fast_vs_mid} mid={self._fmt(ma_mid)} "
                f"| mid={self._fmt(ma_mid)} {mid_vs_slow} slow={self._fmt(ma_slow)} | {trend}{reset}"
            ),
            (
                f"{cyan}FLOW{reset}: cur_vol={self._fmt(getattr(decision, 'current_volume', None), 4)} "
                f"vma5={self._fmt(getattr(decision, 'volume_ma_5', None), 4)} "
                f"vma10={self._fmt(getattr(decision, 'volume_ma_10', None), 4)} "
                f"spike={self._fmt(getattr(decision, 'volume_spike', None), 3)} "
                f"obi={self._fmt(getattr(decision, 'orderbook_imbalance', None), 3)}"
            ),
            (
                f"{cyan}SCORES{reset}: fast_slope={self._fmt(getattr(decision, 'ma_fast_slope_bps', None), 3)}bps "
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
                f"{cyan}PROBS{reset}: UP {self._probability_bar(p_up, green)} {self._fmt(p_up, 3)} "
                f"DOWN {self._probability_bar(p_down, red)} {self._fmt(p_down, 3)} "
                f"P(S_T>target)={self._fmt(p_up, 3)} raw={self._fmt(getattr(decision, 'raw_score', None), 3)}"
            ),
            (
                f"{cyan}GROWTH{reset}: enabled={getattr(decision, 'growth_sizing_enabled', False)} "
                f"account_scale={self._fmt(candidate_up.get('account_scale', candidate_down.get('account_scale')), 3)} "
                f"binary_worst_floor=-max_window_loss | inventory=abs(pnl_up-pnl_down)"
            ),
            sizing_line("UP", candidate_up, green),
            sizing_line("DOWN", candidate_down, red),
            (
                f"{cyan}POSITION{reset}: cost=${self._fmt(shape.get('total_cost'), 2)} "
                f"up_shares={self._fmt(shape.get('up_shares'), 3)} down_shares={self._fmt(shape.get('down_shares'), 3)} "
                f"avg_up={self._fmt(getattr(decision, 'average_entry_up', None), 3)} "
                f"avg_down={self._fmt(getattr(decision, 'average_entry_down', None), 3)} "
                f"pnl_up={self._fmt(shape.get('pnl_if_up'), 2)} pnl_down={self._fmt(shape.get('pnl_if_down'), 2)} "
                f"worst={pnl_color}{self._fmt(shape.get('worst_case_pnl'), 2)} "
                f"{'LOCKED' if locked else 'EXPOSED'}{reset}"
            ),
            f"{cyan}================================================================================{reset}",
        ]
        live = self._live_account_snapshot
        if live.get("enabled"):
            live_line = (
                f"{cyan}LIVE ACCOUNT{reset}: connected={live.get('connected', False)} "
                f"open_orders={live.get('open_orders_count', 0)} "
                f"recent_trades={live.get('recent_trades_count', 0)} "
                f"open_positions={live.get('open_positions_count', 0)} "
                f"closed_positions={live.get('closed_positions_count', 0)} "
                f"cash=${self._fmt(live.get('collateral_balance_usdc'), 2)} "
                f"last_event={self._fmt(live.get('latest_event'))} "
                f"error={self._fmt(live.get('latest_error'))}"
            )
            lines.insert(2, live_line)
        print("\033[2J\033[H" + "\n".join(lines), end="", flush=True)
