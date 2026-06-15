from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


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
        self._optimization_snapshot: dict[str, Any] = {}
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

    def set_optimization_snapshot(self, payload: dict[str, Any]) -> None:
        self._optimization_snapshot = dict(payload)

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

    def _visible_len(self, value: str) -> int:
        return len(ANSI_ESCAPE.sub("", value))

    def _fit(self, value: str, width: int) -> str:
        """Fit colored text into a fixed terminal cell."""
        if self._visible_len(value) > width:
            plain = ANSI_ESCAPE.sub("", value)
            value = plain[: max(0, width - 3)] + ("..." if width >= 3 else "")
        return value + (" " * max(0, width - self._visible_len(value)))

    def _box(self, title: str, lines: list[str], width: int, title_color: str = "\033[96m") -> list[str]:
        width = max(28, width)
        clean_title = title[: max(1, width - 6)]
        top = f"+- {title_color}{clean_title}\033[0m " + ("-" * max(0, width - len(clean_title) - 5)) + "+"
        body = [f"| {self._fit(line, width - 4)} |" for line in lines]
        return [top, *body, "+" + ("-" * (width - 2)) + "+"]

    def _box_row(self, boxes: list[list[str]], gap: int = 2) -> list[str]:
        height = max(len(box) for box in boxes)
        normalized: list[list[str]] = []
        for box in boxes:
            missing = height - len(box)
            blank = " " * self._visible_len(box[0])
            normalized.append([*box[:-1], *([blank] * missing), box[-1]])
        output: list[str] = []
        for row in range(height):
            output.append(
                (" " * gap).join(box[row] for box in normalized)
            )
        return output

    def terminal_status(self, decision: Any) -> None:
        if not self.console:
            return

        reset = "\033[0m"
        green = "\033[92m"
        red = "\033[91m"
        yellow = "\033[93m"
        cyan = "\033[96m"

        ma_fast = getattr(decision, "ma_fast", None)
        ma_mid = getattr(decision, "ma_mid", None)
        ma_slow = getattr(decision, "ma_slow", None)
        ma_is_up = ma_fast is not None and ma_mid is not None and ma_slow is not None and ma_fast > ma_slow and ma_mid > ma_slow
        trend_color = green if ma_is_up else red
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
        sell_candidate = getattr(decision, "sell_candidate", {}) or {}
        mode = str(getattr(decision, "mode", "paper")).lower()
        mode_label = {
            "live": "LIVE EXECUTION",
            "live_monitor": "LIVE MONITOR",
            "paper": "PAPER TRADING",
            "dry_run": "DRY RUN",
        }.get(mode, mode.upper())
        live = self._live_account_snapshot
        optimization = self._optimization_snapshot

        def candidate_lines(label: str, candidate: dict[str, Any], side_color: str) -> list[str]:
            ratio = candidate.get("ladder_bankroll_ratio")
            ratio_percent = ratio * 100.0 if ratio is not None else None
            kelly = candidate.get("applied_kelly_fraction")
            kelly_percent = kelly * 100.0 if kelly is not None else None
            valid_color = green if candidate.get("valid", False) else red
            return [
                f"{side_color}{label}{reset} {valid_color}{'VALID' if candidate.get('valid', False) else 'BLOCKED'}{reset} "
                f"{self._fmt(candidate.get('role'))}",
                f"ask/net: {self._fmt(candidate.get('price'), 3)} / {self._fmt(candidate.get('net_price'), 3)}",
                f"edge/net: {self._fmt(candidate.get('edge'), 3)} / {self._fmt(candidate.get('net_edge'), 3)}",
                f"clip/cap: ${self._fmt(candidate.get('notional_usdc'), 2)} / ${self._fmt(candidate.get('kelly_cap_usdc'), 2)}",
                f"kelly/account: {self._fmt(kelly_percent, 2)}% / {self._fmt(ratio_percent, 2)}%",
                f"reason: {self._fmt(candidate.get('reason'))}",
            ]

        if mode in {"live", "live_monitor"}:
            account_lines = [
                f"Connection: {green if live.get('connected') else red}{'CONNECTED' if live.get('connected') else 'OFFLINE'}{reset}",
                f"CLOB cash: ${self._fmt(live.get('collateral_balance_usdc'), 2)}",
                f"Open orders: {live.get('open_orders_count', 0)}",
                f"Recent trades: {live.get('recent_trades_count', 0)}",
                f"Positions: {live.get('open_positions_count', 0)} open / {live.get('closed_positions_count', 0)} closed",
                f"Session drawdown: {self._fmt(live.get('session_drawdown_percent'), 1)}%",
                f"Orders halted: {red if live.get('orders_halted') else green}{live.get('orders_halted', False)}{reset}",
                f"Last event: {self._fmt(live.get('latest_event'))}",
            ]
            if live.get("halt_reason"):
                account_lines.append(f"{red}Halt: {live.get('halt_reason')}{reset}")
            if live.get("latest_error"):
                account_lines.append(f"{red}Error: {live.get('latest_error')}{reset}")
            account_title = "LIVE ACCOUNT"
        else:
            account_lines = [
                f"Paper balance: ${self._fmt(getattr(decision, 'account_balance_usdc', self.account_balance_usdc), 2)}",
                f"Settled PnL: ${self._fmt(getattr(decision, 'account_realized_pnl_usdc', 0.0), 2)}",
                f"Win rate: {self._fmt(getattr(decision, 'account_win_rate_percent', 0.0), 1)}%",
                f"Mode: {mode_label}",
            ]
            if live.get("enabled"):
                account_lines.extend(
                    [
                        f"CLOB monitor: {'CONNECTED' if live.get('connected') else 'OFFLINE'}",
                        f"CLOB cash: ${self._fmt(live.get('collateral_balance_usdc'), 2)}",
                    ]
                )
            account_title = "PAPER ACCOUNT" if mode == "paper" else "SESSION"

        account_panel = (account_title, account_lines)
        window_panel = (
            "BTC 5M WINDOW",
            [
                f"Slug: {self._fmt(getattr(decision, 'market_slug', None))}",
                f"Open / left: {self._fmt(getattr(decision, 'seconds_since_open', None), 0)}s / {self._fmt(getattr(decision, 'seconds_remaining', None), 0)}s",
                f"BTC: ${self._fmt(btc_price, 2)}",
                f"Target: ${self._fmt(target_price, 2)}",
                f"Gap: ${self._fmt(getattr(decision, 'distance_from_target', None), 2)} ({self._fmt(getattr(decision, 'distance_bps', None), 2)}bps)",
                f"S_t/target: {self._fmt(getattr(decision, 'spot_target_ratio', None), 6)}",
            ],
        )
        signal_panel = (
            "SIGNAL",
            [
                f"Likely: {probable_color}{self._fmt(probable_side)}{reset}",
                f"Actual: {actual_color}{self._fmt(actual_side)}{reset}",
                f"Confidence: {self._fmt(getattr(decision, 'confidence', None), 3)}",
                f"Action: {yellow}{getattr(decision, 'decision', 'SKIP')}{reset}",
                f"Selected: {self._fmt(getattr(decision, 'selected_side', None))}",
                f"State: {self._fmt(getattr(decision, 'strategy_state', None))}",
            ],
        )
        market_panel = (
            "POLYMARKET",
            [
                f"UP bid / ask: {self._fmt(getattr(decision, 'up_bid', None), 3)} / {self._fmt(getattr(decision, 'up_ask', None), 3)}",
                f"DOWN bid / ask: {self._fmt(getattr(decision, 'down_bid', None), 3)} / {self._fmt(getattr(decision, 'down_ask', None), 3)}",
                f"UP / DOWN edge: {self._fmt(getattr(decision, 'edge_up', None), 3)} / {self._fmt(getattr(decision, 'edge_down', None), 3)}",
                f"Spreads: {self._fmt(getattr(decision, 'up_spread', None), 3)} / {self._fmt(getattr(decision, 'down_spread', None), 3)}",
                f"Quote age: {self._fmt(getattr(decision, 'up_quote_age_seconds', None), 2)}s / {self._fmt(getattr(decision, 'down_quote_age_seconds', None), 2)}s",
            ],
        )
        probability_panel = (
            "PROBABILITY",
            [
                f"UP   {self._probability_bar(p_up, green, 14)} {self._fmt(p_up, 3)}",
                f"DOWN {self._probability_bar(p_down, red, 14)} {self._fmt(p_down, 3)}",
                f"P(S_T>target): {self._fmt(p_up, 3)}",
                f"Raw / time: {self._fmt(getattr(decision, 'raw_score', None), 3)} / {self._fmt(getattr(decision, 'time_factor', None), 3)}",
                f"Likely / actual: {self._fmt(probable_side)} / {self._fmt(actual_side)}",
            ],
        )
        position_panel = (
            "POSITION",
            [
                f"Status: {green if locked else yellow}{'LOCKED' if locked else 'EXPOSED'}{reset}",
                f"Net cost: ${self._fmt(shape.get('total_cost'), 2)}",
                f"UP shares / avg: {self._fmt(shape.get('up_shares'), 3)} / {self._fmt(getattr(decision, 'average_entry_up', None), 3)}",
                f"DOWN shares / avg: {self._fmt(shape.get('down_shares'), 3)} / {self._fmt(getattr(decision, 'average_entry_down', None), 3)}",
                f"PnL if UP / DOWN: {self._fmt(shape.get('pnl_if_up'), 2)} / {self._fmt(shape.get('pnl_if_down'), 2)}",
                f"Worst case: {pnl_color}${self._fmt(shape.get('worst_case_pnl'), 2)}{reset}",
            ],
        )
        trend_panel = (
            "TREND",
            [
                f"Direction: {trend_color}{trend}{reset}",
                f"Fast {fast_vs_mid} Mid: {self._fmt(ma_fast, 2)} / {self._fmt(ma_mid, 2)}",
                f"Mid {mid_vs_slow} Slow: {self._fmt(ma_mid, 2)} / {self._fmt(ma_slow, 2)}",
                f"Anchor / slope: {self._fmt(getattr(decision, 'ma_anchor', None), 2)} / {self._fmt(getattr(decision, 'ma_anchor_slope_bps', None), 3)}bps",
                f"Stability UP / DOWN: {self._fmt(getattr(decision, 'up_stability_score', None), 0)} / {self._fmt(getattr(decision, 'down_stability_score', None), 0)}",
                f"Fast / mid slope: {self._fmt(getattr(decision, 'ma_fast_slope_bps', None), 3)} / {self._fmt(getattr(decision, 'ma_mid_slope_bps', None), 3)}bps",
                f"Velocity 5s: {self._fmt(getattr(decision, 'ma_velocity_5s_bps', None), 3)}bps",
                f"Persistence A/B: {self._fmt(getattr(decision, 'persistence_above_target_seconds', None), 0)} / {self._fmt(getattr(decision, 'persistence_below_target_seconds', None), 0)}s",
            ],
        )
        flow_panel = (
            "FLOW + SCORES",
            [
                f"Volume / VMA5: {self._fmt(getattr(decision, 'current_volume', None), 4)} / {self._fmt(getattr(decision, 'volume_ma_5', None), 4)}",
                f"VMA10 / spike: {self._fmt(getattr(decision, 'volume_ma_10', None), 4)} / {self._fmt(getattr(decision, 'volume_spike', None), 3)}",
                f"Book imbalance: {self._fmt(getattr(decision, 'orderbook_imbalance', None), 3)}",
                f"MA / slope score: {self._fmt(getattr(decision, 'ma_score', None), 3)} / {self._fmt(getattr(decision, 'slope_score', None), 3)}",
                f"Volume / OBI score: {self._fmt(getattr(decision, 'volume_score', None), 3)} / {self._fmt(getattr(decision, 'imbalance_score', None), 3)}",
                f"Account scale: {self._fmt(candidate_up.get('account_scale', candidate_down.get('account_scale')), 3)}",
            ],
        )
        execution_panel = (
            "EXECUTION",
            [
                f"Decision: {yellow}{getattr(decision, 'decision', 'SKIP')}{reset}",
                f"Reason: {getattr(decision, 'selected_reason', '')}",
                f"Order: {self._fmt(getattr(decision, 'order_status', None))} / {self._fmt(getattr(decision, 'order_id', None))}",
                f"Order reason: {self._fmt(getattr(decision, 'order_reason', None))}",
                f"Since first fill: {self._fmt(getattr(decision, 'seconds_since_first_fill', None), 1)}s",
                f"Lock stop / adds: {getattr(decision, 'stop_when_locked', True)} / {getattr(decision, 'allow_post_lock_adds', False)}",
            ],
        )
        sizing_up_panel = ("SIZING UP", candidate_lines("UP", candidate_up, green))
        sizing_down_panel = ("SIZING DOWN", candidate_lines("DOWN", candidate_down, red))
        sell_panel = (
            "SELL PLAN",
            [
                f"Status: {green if sell_candidate.get('valid') else yellow}{'READY' if sell_candidate.get('valid') else 'OFF / WAIT'}{reset}",
                f"Action / side: {self._fmt(sell_candidate.get('action'))} / {self._fmt(sell_candidate.get('side'))}",
                f"Role: {self._fmt(sell_candidate.get('role'))}",
                f"Bid / proceeds: {self._fmt(sell_candidate.get('price'), 3)} / ${self._fmt(sell_candidate.get('notional_usdc'), 2)}",
                f"Shares / fraction: {self._fmt(sell_candidate.get('shares'), 3)} / {self._fmt((sell_candidate.get('sell_fraction') or 0.0) * 100.0, 1)}%",
                f"Model p / bid-p: {self._fmt(sell_candidate.get('model_probability'), 3)} / {self._fmt(sell_candidate.get('model_exit_edge'), 3)}",
                f"Stability / opposite: {self._fmt(sell_candidate.get('stability_score'), 0)} / {self._fmt(sell_candidate.get('opposite_stability_score'), 0)}",
                f"Worst change / deterioration: ${self._fmt(sell_candidate.get('shape_improvement'), 2)} / ${self._fmt(sell_candidate.get('worst_case_deterioration'), 2)}",
                f"Reason: {self._fmt(sell_candidate.get('reason'))}",
            ],
        )
        risk_panel = (
            "RISK + TIMING",
            [
                f"Growth sizing: {getattr(decision, 'growth_sizing_enabled', False)}",
                f"Conviction adds: {getattr(decision, 'high_conviction_same_side_enabled', False)}",
                f"Conviction p/cap: {self._fmt(getattr(decision, 'high_conviction_min_probability', None), 2)} / ${self._fmt(getattr(decision, 'high_conviction_max_window_notional_usdc', None), 2)}",
                f"Time remaining ratio: {self._fmt(getattr(decision, 'time_remaining_ratio', None), 3)}",
                f"Repair min / preferred: {self._fmt(getattr(decision, 'repair_start_after_first_entry_seconds', None), 0)}s / {self._fmt(getattr(decision, 'preferred_repair_after_first_entry_seconds', None), 0)}s",
                f"Since first fill: {self._fmt(getattr(decision, 'seconds_since_first_fill', None), 1)}s",
                f"Stop when locked: {getattr(decision, 'stop_when_locked', True)}",
                f"Allow post-lock adds: {getattr(decision, 'allow_post_lock_adds', False)}",
            ],
        )
        optimization_panel = (
            "OPTIMIZATION",
            [
                f"Status / windows: {self._fmt(optimization.get('status'))} / {optimization.get('windows', 0)}",
                f"Profit factor: {self._fmt(optimization.get('profit_factor'), 2)}",
                f"Payoff / breakeven: {self._fmt(optimization.get('payoff_ratio'), 2)} / {self._fmt((optimization.get('breakeven_win_rate') or 0.0) * 100.0, 1)}%",
                f"Max drawdown: ${self._fmt(optimization.get('max_drawdown_usdc'), 2)}",
                f"Best first / fill: {self._fmt(optimization.get('best_first_entry_time'))} / {self._fmt(optimization.get('best_fill_time'))}",
                f"Best sell time: {self._fmt(optimization.get('best_sell_time'))}",
                f"Best price / role: {self._fmt(optimization.get('best_entry_price'))} / {self._fmt(optimization.get('best_role'))}",
                f"Brier / warnings: {self._fmt(optimization.get('calibration_brier'), 3)} / {optimization.get('warnings', 0)}",
            ],
        )

        terminal_width = max(80, shutil.get_terminal_size((160, 40)).columns)
        gap = 2
        panels = [
            account_panel,
            window_panel,
            signal_panel,
            market_panel,
            probability_panel,
            position_panel,
            trend_panel,
            flow_panel,
            execution_panel,
            sizing_up_panel,
            sizing_down_panel,
            sell_panel,
            risk_panel,
            optimization_panel,
        ]
        lines = [
            f"{cyan}ARIES // BTC 5M // {mode_label} // {time.strftime('%Y-%m-%d %H:%M:%S')}{reset}",
            "",
        ]
        if terminal_width >= 120:
            panel_width = (terminal_width - (gap * 2)) // 3
            for start in range(0, len(panels), 3):
                row = [
                    self._box(title, content, panel_width)
                    for title, content in panels[start : start + 3]
                ]
                lines.extend(self._box_row(row, gap=gap))
                lines.append("")
        else:
            panel_width = terminal_width
            for title, content in panels:
                lines.extend(self._box(title, content, panel_width))
                lines.append("")

        print("\033[2J\033[H" + "\n".join(lines), end="", flush=True)
