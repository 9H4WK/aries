from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

from . import clock


class StrategyOptimizer:
    """Advisory, walk-forward analysis of settled Aries trading history.

    The optimizer never changes trading settings. It reports only buckets that
    remain profitable in both an earlier training segment and a later validation
    segment, reducing the chance of promoting an in-sample accident.
    """

    def __init__(self, config: dict, db: Any):
        self.config = config
        self.db = db

    def analyze(self) -> dict[str, Any]:
        paper_windows = [
            row
            for row in self.db.list_window_report_rows()
            if int(row.get("number_of_fills", 0) or 0) > 0
            or abs(float(row.get("total_cost", 0.0) or 0.0)) > 1e-9
        ]
        list_live_windows = getattr(self.db, "list_live_window_report_rows", None)
        live_windows = list_live_windows() if list_live_windows else []
        requested_source = str(self._get("optimization.source", "auto")).lower()
        mode = str(self._get("mode", "paper")).lower()
        use_live = requested_source == "live" or (
            requested_source == "auto" and mode == "live" and bool(live_windows)
        )
        source = "live" if use_live else "paper"
        windows = live_windows if use_live else paper_windows
        windows.sort(key=lambda row: int(row.get("window_start", 0)))
        results = {
            (str(row["market_id"]), int(row["window_start"])): row
            for row in windows
        }
        fills = self._fill_observations(results, source)
        buy_fills = [row for row in fills if row["action"] == "BUY"]
        sell_fills = [row for row in fills if row["action"] == "SELL"]
        first_entries = self._first_entry_observations(windows, buy_fills)
        min_samples = max(5, int(self._get("optimization.min_samples_per_bucket", 20)))
        validation_fraction = min(
            0.50,
            max(0.20, float(self._get("optimization.validation_fraction", 0.30))),
        )

        window_stats = self._stats(windows, "realized_pnl", "gross_buy_cost")
        buckets = {
            "first_entry_time": self._bucket_stats(
                first_entries,
                lambda row: self._seconds_bucket(row["seconds_since_open"]),
                "window_pnl",
                "window_cost",
            ),
            "fill_time": self._bucket_stats(
                fills,
                lambda row: self._seconds_bucket(row["seconds_since_open"]),
                "order_pnl",
                "notional_usdc",
            ),
            "sell_time": self._bucket_stats(
                sell_fills,
                lambda row: self._seconds_bucket(row["seconds_since_open"]),
                "order_pnl",
                "notional_usdc",
            ),
            "action": self._bucket_stats(
                fills,
                lambda row: str(row["action"]),
                "order_pnl",
                "notional_usdc",
            ),
            "entry_price": self._bucket_stats(
                fills,
                lambda row: self._price_bucket(row["price"]),
                "order_pnl",
                "notional_usdc",
            ),
            "role": self._bucket_stats(
                fills,
                lambda row: str(row.get("role") or "UNKNOWN"),
                "order_pnl",
                "notional_usdc",
            ),
            "reason": self._bucket_stats(
                fills,
                lambda row: str(row.get("strategy_reason") or "UNKNOWN"),
                "order_pnl",
                "notional_usdc",
            ),
            "fills_per_window": self._bucket_stats(
                windows,
                lambda row: str(int(row.get("number_of_fills", 0) or 0)),
                "realized_pnl",
                "gross_buy_cost",
            ),
        }
        recommendations = {
            name: self._stable_best(
                rows,
                key,
                pnl_field,
                cost_field,
                min_samples,
                validation_fraction,
            )
            for name, rows, key, pnl_field, cost_field in (
                (
                    "first_entry_time",
                    first_entries,
                    lambda row: self._seconds_bucket(row["seconds_since_open"]),
                    "window_pnl",
                    "window_cost",
                ),
                (
                    "fill_time",
                    fills,
                    lambda row: self._seconds_bucket(row["seconds_since_open"]),
                    "order_pnl",
                    "notional_usdc",
                ),
                (
                    "sell_time",
                    sell_fills,
                    lambda row: self._seconds_bucket(row["seconds_since_open"]),
                    "order_pnl",
                    "notional_usdc",
                ),
                (
                    "entry_price",
                    fills,
                    lambda row: self._price_bucket(row["price"]),
                    "order_pnl",
                    "notional_usdc",
                ),
                (
                    "role",
                    fills,
                    lambda row: str(row.get("role") or "UNKNOWN"),
                    "order_pnl",
                    "notional_usdc",
                ),
            )
        }
        calibration = self._probability_calibration(results)
        live_matched = self._live_matched_count()
        warnings: list[str] = []
        if live_matched and source != "live":
            warnings.append(
                "Live matched orders are not included in settled paper PnL; do not use this report as live realized PnL."
            )
        if calibration["status"] != "ready":
            warnings.append("Probability calibration is unavailable or below the configured sample threshold.")
        if len(windows) < int(self._get("optimization.min_settled_windows", 100)):
            warnings.append("Settled-window sample is too small for automatic parameter promotion.")

        return {
            "generated_at": clock.now(),
            "mode": "advisory_only",
            "data_source": source,
            "status": "ready" if windows else "insufficient_data",
            "settled_traded_windows": len(windows),
            "settled_fills": len(fills),
            "settled_buy_fills": len(buy_fills),
            "settled_sell_fills": len(sell_fills),
            "live_matched_orders_excluded": live_matched if source != "live" else 0,
            "overall": {
                **window_stats,
                "max_drawdown_usdc": self._max_drawdown(windows),
                "max_loss_streak": self._max_loss_streak(windows),
            },
            "probability_calibration": calibration,
            "recommendations": recommendations,
            "buckets": buckets,
            "warnings": warnings,
        }

    def write_report(self, path: str | Path) -> dict[str, Any]:
        payload = self.analyze()
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        return payload

    def terminal_snapshot(self) -> dict[str, Any]:
        report = self.analyze()
        recommendations = report.get("recommendations", {})
        return {
            "status": report.get("status"),
            "windows": report.get("settled_traded_windows", 0),
            "profit_factor": report.get("overall", {}).get("profit_factor"),
            "payoff_ratio": report.get("overall", {}).get("payoff_ratio"),
            "breakeven_win_rate": report.get("overall", {}).get("breakeven_win_rate"),
            "max_drawdown_usdc": report.get("overall", {}).get("max_drawdown_usdc"),
            "best_first_entry_time": self._recommendation_label(recommendations.get("first_entry_time")),
            "best_fill_time": self._recommendation_label(recommendations.get("fill_time")),
            "best_sell_time": self._recommendation_label(recommendations.get("sell_time")),
            "best_entry_price": self._recommendation_label(recommendations.get("entry_price")),
            "best_role": self._recommendation_label(recommendations.get("role")),
            "calibration_brier": report.get("probability_calibration", {}).get("brier_score"),
            "warnings": len(report.get("warnings", [])),
        }

    def _fill_observations(
        self,
        results: dict[tuple[str, int], dict[str, Any]],
        source: str,
    ) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        if source == "live":
            list_fills = getattr(self.db, "list_all_matched_live_orders", None)
            source_fills = list_fills() if list_fills else []
        else:
            source_fills = self.db.list_all_paper_fills()
        for fill in source_fills:
            key = (str(fill["market_id"]), int(fill["window_start"]))
            window = results.get(key)
            if not window:
                continue
            metadata = self._loads(
                fill.get("response_json") if source == "live" else fill.get("metadata_json")
            )
            candidate = metadata.get("candidate", {}) if isinstance(metadata, dict) else {}
            price = float(fill["price"])
            notional = float(fill["notional_usdc"])
            won = str(fill["side"]) == str(window["winning_side"])
            action = str(fill.get("action", "BUY")).upper()
            shares = float(fill.get("shares") or (notional / price if price > 0 else 0.0))
            order_pnl = (
                ((shares if won else 0.0) - notional)
                if action == "BUY"
                else (notional - (shares if won else 0.0))
            )
            observations.append(
                {
                    "window_start": int(fill["window_start"]),
                    "seconds_since_open": float(fill["created_at"]) - int(fill["window_start"]),
                    "price": price,
                    "notional_usdc": notional,
                    "side_won": won,
                    "action": action,
                    "order_pnl": order_pnl,
                    "role": candidate.get("role"),
                    "strategy_reason": metadata.get("strategy_reason"),
                }
            )
        observations.sort(key=lambda row: (row["window_start"], row["seconds_since_open"]))
        return observations

    def _first_entry_observations(
        self,
        windows: list[dict[str, Any]],
        fills: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        first_by_window: dict[int, dict[str, Any]] = {}
        for fill in fills:
            first_by_window.setdefault(int(fill["window_start"]), fill)
        observations = []
        for window in windows:
            first = first_by_window.get(int(window["window_start"]))
            if first:
                observations.append(
                    {
                        "window_start": int(window["window_start"]),
                        "seconds_since_open": first["seconds_since_open"],
                        "window_pnl": float(window["realized_pnl"]),
                        "window_cost": float(window.get("gross_buy_cost", window["total_cost"])),
                    }
                )
        return observations

    def _probability_calibration(self, results: dict[tuple[str, int], dict[str, Any]]) -> dict[str, Any]:
        first_decision: dict[tuple[str, int], dict[str, Any]] = {}
        for row in self.db.list_decisions():
            payload = row.get("payload", {})
            if payload.get("decision") not in {"BUY_UP", "BUY_DOWN"}:
                continue
            if payload.get("market_id") is None or payload.get("window_start") is None:
                continue
            key = (str(payload["market_id"]), int(payload["window_start"]))
            if key in results:
                first_decision.setdefault(key, payload)

        samples = []
        for key, decision in first_decision.items():
            result = results[key]
            p_up = decision.get("p_up")
            if p_up is None:
                continue
            samples.append((float(p_up), 1.0 if result["winning_side"] == "UP" else 0.0))
        minimum = max(20, int(self._get("optimization.min_calibration_samples", 100)))
        if len(samples) < minimum:
            return {"status": "insufficient_data", "samples": len(samples), "brier_score": None}
        brier = sum((probability - outcome) ** 2 for probability, outcome in samples) / len(samples)
        return {
            "status": "ready",
            "samples": len(samples),
            "brier_score": brier,
            "buckets": self._calibration_buckets(samples),
        }

    def _calibration_buckets(self, samples: list[tuple[float, float]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for probability, outcome in samples:
            lower = min(0.9, math.floor(probability * 10.0) / 10.0)
            grouped[f"{lower:.1f}-{lower + 0.1:.1f}"].append((probability, outcome))
        return [
            {
                "bucket": label,
                "samples": len(rows),
                "mean_prediction": sum(row[0] for row in rows) / len(rows),
                "actual_up_rate": sum(row[1] for row in rows) / len(rows),
            }
            for label, rows in sorted(grouped.items())
        ]

    def _bucket_stats(
        self,
        rows: list[dict[str, Any]],
        key: Callable[[dict[str, Any]], str],
        pnl_field: str,
        cost_field: str,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[key(row)].append(row)
        return [
            {"bucket": label, **self._stats(group, pnl_field, cost_field)}
            for label, group in sorted(grouped.items())
        ]

    def _stable_best(
        self,
        rows: list[dict[str, Any]],
        key: Callable[[dict[str, Any]], str],
        pnl_field: str,
        cost_field: str,
        min_samples: int,
        validation_fraction: float,
    ) -> Optional[dict[str, Any]]:
        if len(rows) < min_samples:
            return None
        split = max(1, int(len(rows) * (1.0 - validation_fraction)))
        train = rows[:split]
        validation = rows[split:]
        grouped_train: dict[str, list[dict[str, Any]]] = defaultdict(list)
        grouped_validation: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in train:
            grouped_train[key(row)].append(row)
        for row in validation:
            grouped_validation[key(row)].append(row)

        eligible = []
        min_validation = max(5, int(min_samples * validation_fraction))
        for label, train_rows in grouped_train.items():
            validation_rows = grouped_validation.get(label, [])
            if len(train_rows) + len(validation_rows) < min_samples or len(validation_rows) < min_validation:
                continue
            train_stats = self._stats(train_rows, pnl_field, cost_field)
            validation_stats = self._stats(validation_rows, pnl_field, cost_field)
            if train_stats["expectancy"] <= 0 or validation_stats["expectancy"] <= 0:
                continue
            eligible.append(
                {
                    "bucket": label,
                    "samples": len(train_rows) + len(validation_rows),
                    "train": train_stats,
                    "validation": validation_stats,
                    "conservative_expectancy": min(
                        train_stats["expectancy"],
                        validation_stats["expectancy"],
                    ),
                }
            )
        if not eligible:
            return None
        return max(eligible, key=lambda row: (row["conservative_expectancy"], row["samples"]))

    def _stats(self, rows: list[dict[str, Any]], pnl_field: str, cost_field: str) -> dict[str, Any]:
        pnls = [float(row.get(pnl_field, 0.0) or 0.0) for row in rows]
        costs = [
            float(
                row.get(
                    cost_field,
                    row.get("total_cost", 0.0) if cost_field == "gross_buy_cost" else 0.0,
                )
                or 0.0
            )
            for row in rows
        ]
        profits = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(profits)
        gross_loss = -sum(losses)
        average_win = (gross_profit / len(profits)) if profits else 0.0
        average_loss = (gross_loss / len(losses)) if losses else 0.0
        breakeven_win_rate = (
            average_loss / (average_win + average_loss)
            if average_win + average_loss > 0
            else None
        )
        return {
            "samples": len(rows),
            "total_pnl": sum(pnls),
            "expectancy": (sum(pnls) / len(rows)) if rows else 0.0,
            "win_rate": (len(profits) / len(rows)) if rows else 0.0,
            "profit_factor": (gross_profit / gross_loss) if gross_loss else None,
            "payoff_ratio": (average_win / average_loss) if average_loss else None,
            "breakeven_win_rate": breakeven_win_rate,
            "roi": (sum(pnls) / sum(costs)) if sum(costs) else 0.0,
            "average_win": average_win,
            "average_loss": average_loss,
        }

    def _max_drawdown(self, windows: list[dict[str, Any]]) -> float:
        equity = 0.0
        peak = 0.0
        maximum = 0.0
        for row in windows:
            equity += float(row.get("realized_pnl", 0.0) or 0.0)
            peak = max(peak, equity)
            maximum = max(maximum, peak - equity)
        return maximum

    def _max_loss_streak(self, windows: list[dict[str, Any]]) -> int:
        current = 0
        maximum = 0
        for row in windows:
            if float(row.get("realized_pnl", 0.0) or 0.0) < 0:
                current += 1
                maximum = max(maximum, current)
            else:
                current = 0
        return maximum

    def _live_matched_count(self) -> int:
        list_audits = getattr(self.db, "list_live_order_audits", None)
        if list_audits is None:
            return 0
        return sum(1 for row in list_audits() if str(row.get("status")) == "MATCHED")

    def _seconds_bucket(self, seconds: float) -> str:
        width = max(10, int(self._get("optimization.time_bucket_seconds", 30)))
        lower = max(0, int(float(seconds) // width) * width)
        return f"{lower:03d}-{lower + width - 1:03d}s"

    def _price_bucket(self, price: float) -> str:
        width = max(0.01, float(self._get("optimization.price_bucket_width", 0.10)))
        lower = min(1.0 - width, math.floor(float(price) / width) * width)
        return f"{lower:.2f}-{lower + width:.2f}"

    def _recommendation_label(self, recommendation: Optional[dict[str, Any]]) -> Optional[str]:
        return str(recommendation["bucket"]) if recommendation else None

    def _loads(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            return json.loads(str(value))
        except json.JSONDecodeError:
            return {}

    def _get(self, path: str, default: Any) -> Any:
        getter = getattr(self.config, "get_path", None)
        if getter:
            return getter(path, default)
        current: Any = self.config
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current
