from __future__ import annotations

import html
import json
import math
import os
import time
import hashlib
from collections import deque
from pathlib import Path
from typing import Any, Optional


class LiveHtmlDashboard:
    """Write a self-refreshing HTML dashboard from Aries decision snapshots.

    This is intentionally lightweight: Aries keeps trading state in memory,
    and this class converts the latest snapshots into a browser-readable file.
    It does not place orders and it does not change strategy decisions.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.enabled = bool(config.get("enabled", False))
        self.output_path = Path(str(config.get("output_path", "reports/charts/live.html")))
        self.data_path = Path(str(config.get("data_path", self.output_path.with_name("live_data.json"))))
        self.refresh_seconds = max(1, int(config.get("refresh_seconds", 1)))
        self.write_interval_seconds = max(0.0, float(config.get("write_interval_seconds", 1.0)))
        self.history_points = max(60, int(config.get("history_points", 900)))
        self.candle_seconds = max(1, int(config.get("candle_seconds", 5)))
        self.max_trade_markers = max(10, int(config.get("max_trade_markers", 80)))
        self._points: deque[dict[str, Any]] = deque(maxlen=self.history_points)
        self._last_write_ts = 0.0
        self._last_html_hash: Optional[str] = None
        self._last_target_by_market: dict[str, float] = {}

    def update(
        self,
        decision: Any,
        *,
        live_account: dict[str, Any] | None = None,
        optimization: dict[str, Any] | None = None,
    ) -> None:
        """Add one strategy-cycle snapshot and rewrite live.html when due."""
        if not self.enabled:
            return
        point = self._decision_point(decision)
        self._points.append(point)
        now = time.time()
        if now - self._last_write_ts < self.write_interval_seconds:
            return
        self._write_files(live_account or {}, optimization or {})
        self._last_write_ts = now

    def _decision_point(self, decision: Any) -> dict[str, Any]:
        """Extract only chart-useful values from a full Decision object."""
        candidate = self._selected_candidate(decision)
        shape = self._dict(getattr(decision, "position_after", {}))
        sr_state = self._dict(getattr(decision, "support_resistance", {}))
        pm_flow = self._dict(getattr(decision, "polymarket_flow", {}))
        binance_flow = self._dict(getattr(decision, "binance_flow", {}))
        timestamp = float(getattr(decision, "timestamp", time.time()) or time.time())
        market_key = str(
            getattr(decision, "market_id", None)
            or getattr(decision, "market_slug", None)
            or ""
        )
        price = self._num(getattr(decision, "btc_price", None))
        target = self._num(getattr(decision, "target_price", None))
        distance = self._num(getattr(decision, "distance_from_target", None))
        distance_bps = self._num(getattr(decision, "distance_bps", None))

        # During a market transition the strategy can briefly log a WATCH decision
        # before the new target is attached. Keep the dashboard stable by deriving
        # it from price-distance or by reusing the last target for this same market.
        if target is None and price is not None and distance is not None:
            target = price - distance
        if target is None and market_key:
            target = self._last_target_by_market.get(market_key)
        if target is not None and market_key:
            self._last_target_by_market[market_key] = target
        if distance is None and price is not None and target is not None:
            distance = price - target
        if distance_bps is None and distance is not None and target:
            distance_bps = distance / target * 10_000.0

        return {
            "ts": timestamp,
            "time": time.strftime("%H:%M:%S", time.localtime(timestamp)),
            "mode": str(getattr(decision, "mode", "paper") or "paper"),
            "decision": str(getattr(decision, "decision", "WATCH") or "WATCH"),
            "strategy_state": getattr(decision, "strategy_state", None),
            "selected_side": getattr(decision, "selected_side", None),
            "selected_reason": getattr(decision, "selected_reason", None),
            "order_id": getattr(decision, "order_id", None),
            "fill_id": getattr(decision, "fill_id", None),
            "order_status": getattr(decision, "order_status", None),
            "order_reason": getattr(decision, "order_reason", None),
            "market_slug": getattr(decision, "market_slug", None),
            "seconds_since_open": self._num(getattr(decision, "seconds_since_open", None)),
            "seconds_remaining": self._num(getattr(decision, "seconds_remaining", None)),
            "account_balance": self._num(getattr(decision, "account_balance_usdc", None)),
            "settled_pnl": self._num(getattr(decision, "account_realized_pnl_usdc", None)),
            "win_rate": self._num(getattr(decision, "account_win_rate_percent", None)),
            "price": price,
            "target": target,
            "distance": distance,
            "distance_bps": distance_bps,
            "ma_fast": self._num(getattr(decision, "ma_fast", None)),
            "ma_mid": self._num(getattr(decision, "ma_mid", None)),
            "ma_slow": self._num(getattr(decision, "ma_slow", None)),
            "ma_anchor": self._num(getattr(decision, "ma_anchor", None)),
            "fast_slope": self._num(getattr(decision, "ma_fast_slope_bps", None)),
            "mid_slope": self._num(getattr(decision, "ma_mid_slope_bps", None)),
            "p_up": self._num(getattr(decision, "p_up", None)),
            "p_down": self._num(getattr(decision, "p_down", None)),
            "confidence": self._num(getattr(decision, "confidence", None)),
            "up_bid": self._num(getattr(decision, "up_bid", None)),
            "up_ask": self._num(getattr(decision, "up_ask", None)),
            "down_bid": self._num(getattr(decision, "down_bid", None)),
            "down_ask": self._num(getattr(decision, "down_ask", None)),
            "edge_up": self._num(getattr(decision, "edge_up", None)),
            "edge_down": self._num(getattr(decision, "edge_down", None)),
            "volume": self._num(getattr(decision, "current_volume", None)),
            "volume_ma_5": self._num(getattr(decision, "volume_ma_5", None)),
            "volume_ma_10": self._num(getattr(decision, "volume_ma_10", None)),
            "volume_spike": self._num(getattr(decision, "volume_spike", None)),
            "orderbook_imbalance": self._num(getattr(decision, "orderbook_imbalance", None)),
            "up_stability_score": self._num(getattr(decision, "up_stability_score", None)),
            "down_stability_score": self._num(getattr(decision, "down_stability_score", None)),
            "shape": shape,
            "up_shares": self._num(shape.get("up_shares")),
            "down_shares": self._num(shape.get("down_shares")),
            "total_cost": self._num(shape.get("total_cost")),
            "pnl_if_up": self._num(shape.get("pnl_if_up")),
            "pnl_if_down": self._num(shape.get("pnl_if_down")),
            "worst_case_pnl": self._num(shape.get("worst_case_pnl")),
            "locked": bool(shape.get("locked", False)),
            "average_entry_up": self._num(getattr(decision, "average_entry_up", None)),
            "average_entry_down": self._num(getattr(decision, "average_entry_down", None)),
            "sr_bias": sr_state.get("bias_side"),
            "sr_score": self._num(sr_state.get("bias_score")),
            "support": self._num(sr_state.get("nearest_support")),
            "resistance": self._num(sr_state.get("nearest_resistance")),
            "pm_flow_bias": pm_flow.get("bias_side"),
            "pm_flow_score": self._num(pm_flow.get("signal")),
            "pm_trade_imbalance": self._num(pm_flow.get("trade_imbalance")),
            "pm_trade_volume": self._num(pm_flow.get("trade_volume_shares")),
            "binance_flow_bias": binance_flow.get("bias_side"),
            "binance_flow_score": self._num(binance_flow.get("signal")),
            "binance_flow_adjustment": self._num(binance_flow.get("probability_adjustment")),
            "ofi_signal": self._num(binance_flow.get("ofi_signal")),
            "ofi_sum": self._num(binance_flow.get("ofi_sum")),
            "cvd_signal": self._num(binance_flow.get("cvd_signal")),
            "cvd": self._num(binance_flow.get("cvd")),
            "cvd_buy_volume": self._num(binance_flow.get("buy_volume")),
            "cvd_sell_volume": self._num(binance_flow.get("sell_volume")),
            "candidate_price": self._num(candidate.get("price")),
            "candidate_role": candidate.get("role"),
            "candidate_edge": self._num(candidate.get("edge")),
            "candidate_shares": self._num(candidate.get("shares")),
            "candidate_notional": self._num(candidate.get("notional_usdc")),
        }

    def _write_files(self, live_account: dict[str, Any], optimization: dict[str, Any]) -> None:
        """Write the stable HTML shell and the changing client-side data file."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        payload, summary = self._dashboard_payload(live_account, optimization)
        if not self._atomic_write_text(
            self.data_path,
            json.dumps(payload, separators=(",", ":"), default=str),
        ):
            return
        html_text = self._render(payload, summary)
        html_hash = hashlib.sha256(html_text.encode("utf-8")).hexdigest()
        if html_hash != self._last_html_hash or not self.output_path.exists():
            if self._atomic_write_text(self.output_path, html_text):
                self._last_html_hash = html_hash

    def _atomic_write_text(self, path: Path, text: str) -> bool:
        """Replace a dashboard file atomically so the browser never sees half a file."""
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        for attempt in range(3):
            try:
                temp_path.write_text(text, encoding="utf-8")
                temp_path.replace(path)
                return True
            except PermissionError:
                # Windows can briefly lock the JSON while the browser/dev server reads it.
                # Trading must continue; the next dashboard tick will write fresh data.
                if attempt < 2:
                    time.sleep(0.025 * (attempt + 1))
                    continue
                return False
            except OSError as exc:
                if getattr(exc, "winerror", None) == 5:
                    if attempt < 2:
                        time.sleep(0.025 * (attempt + 1))
                        continue
                    return False
                raise
            finally:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
        return False

    def _dashboard_payload(
        self,
        live_account: dict[str, Any],
        optimization: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build the current client payload and text summary once per write."""
        points = list(self._points)
        candles = self._candles(points)
        indicators = self._indicators(candles)
        self._attach_candle_tooltips(candles, indicators)
        summary = self._summary(points[-1] if points else {}, indicators, live_account, optimization)
        cards_html = "\n".join(self._cards(summary["cards"]))
        payload = {
            "generated_at": time.time(),
            "generated_label": time.strftime("%H:%M:%S"),
            "points": points,
            "candles": candles,
            "events": self._trade_events(points),
            "indicators": indicators,
            "summary": summary,
            "html": {
                "left": self._left_panel(summary, cards_html),
                "right": self._right_panel(summary),
                "price_line": summary["price_line"],
                "target": summary["target"],
                "vwap": summary["vwap"],
                "ma_line": summary["ma_line"],
            },
        }
        return payload, summary

    def _render(self, payload: dict[str, Any], summary: dict[str, Any]) -> str:
        """Render the complete browser dashboard document."""
        data_url = html.escape(self.data_path.name, quote=True)
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Aries Live BTC Dashboard</title>
  <style>{self._css()}</style>
</head>
<body>
  <script>
    window.ARIES_DATA = {{"points":[],"candles":[],"events":[],"indicators":{{}},"html":{{}}}};
    window.ARIES_DATA_URL = "{data_url}";
    window.ARIES_POLL_MS = {self.refresh_seconds * 1000};
  </script>
  <div class="topbar">
    <div><span class="bolt">&#9889;</span> Polymarket BTC Trading Dashboard</div>
    <div class="status"><span class="dot"></span> Background data refresh {self.refresh_seconds}s - <span id="lastUpdate">loading</span></div>
  </div>
  <main class="layout" id="dashboardRoot">
    <section class="panel left"><div class="session"><div><div class="label">Session</div><div class="big">Loading Aries data</div></div></div></section>
    <section class="panel center">
      <div class="chart-header">
        <div>
          <div class="label">Bitcoin / TetherUS - Binance-derived Aries feed</div>
          <div class="price-line" id="priceLine">Waiting for live_data.json</div>
        </div>
        <div class="pills">
          <span id="targetPill">Target n/a</span>
          <span id="vwapPill">VWAP n/a</span>
          <span id="maPill">MA n/a</span>
        </div>
      </div>
      <canvas id="priceChart" data-h="430" height="430"></canvas>
      <canvas id="rsiChart" data-h="145" height="145"></canvas>
      <canvas id="macdChart" data-h="165" height="165"></canvas>
      <div class="legend">
        <span class="green">BUY UP</span><span class="red">BUY DOWN</span>
        <span class="yellow">SELL UP</span><span class="blue">SELL DOWN</span>
        <span class="blue">Target</span><span class="yellow">VWAP</span>
        <span class="orange">MA lines</span>
      </div>
    </section>
    <section class="panel right"><div class="signal-box"><div class="label">Signal</div><div class="small">Loading live data...</div></div></section>
  </main>
  <div id="hoverTip" class="hover-tip"></div>
  <script>{self._js()}</script>
</body>
</html>
"""

    def _left_panel(self, summary: dict[str, Any], cards_html: str) -> str:
        return f"""
      <div class="session">
        <div>
          <div class="label">{self._label("Session", "Shows whether Aries is paper, live monitor, or live execution mode.")}</div>
          <div class="big">{html.escape(summary["session_name"])}</div>
        </div>
        <div class="clock">{html.escape(summary["clock"])}</div>
      </div>
      <div class="market-card">
        <div class="row"><span>{self._label("Market", "Current Polymarket BTC 5m slug Aries is evaluating.")}</span><b>{html.escape(summary["market"])}</b></div>
        <div class="row"><span>{self._label("Open / Left", "Seconds since this 5m window opened and seconds remaining.")}</span><b>{html.escape(summary["open_left"])}</b></div>
        <div class="split">
          <div><span>{self._label("Price to beat", "Polymarket target price for deciding UP or DOWN at settlement.")}</span><b>{html.escape(summary["target"])}</b></div>
          <div><span>{self._label("Current price", "Latest BTC price seen by Aries from Binance data.")}</span><b class="{summary["actual_class"]}">{html.escape(summary["price"])}</b></div>
        </div>
        <canvas id="miniChart" data-h="90" height="90"></canvas>
        <div class="split boxes">
          <div><span>{self._label("UP ask", "Current PM ask Aries would pay to buy UP.")}</span><b class="green">{html.escape(summary["up_ask"])}</b></div>
          <div><span>{self._label("DOWN ask", "Current PM ask Aries would pay to buy DOWN.")}</span><b class="red">{html.escape(summary["down_ask"])}</b></div>
          <div><span>{self._label("Gap", "BTC price minus the Polymarket target price.")}</span><b class="{summary["actual_class"]}">{html.escape(summary["gap"])}</b></div>
        </div>
      </div>
      <div class="section-title">{self._label("Live Indicators", "Compact snapshot of the inputs Aries uses for probability and execution context.")}</div>
      <div class="card-grid">{cards_html}</div>
"""

    def _right_panel(self, summary: dict[str, Any]) -> str:
        return f"""
      <div class="signal-box">
        <div class="label">{self._label("Signal", "Current UP/DOWN probability leader after all bounded side-information nudges.")}</div>
        <div class="signal {summary["signal_class"]}">{html.escape(summary["signal_icon"])} {html.escape(summary["likely"])}</div>
        <div class="gap-row"><span>{self._label("GAP", "BTC price minus target, also shown in basis points.")}</span><b>{html.escape(summary["gap"])}</b><em>{html.escape(summary["gap_bps"])}</em></div>
        <div class="bar-wrap"><span class="green">{html.escape(summary["p_up"])}</span><div class="probbar"><i style="width:{summary["p_up_percent"]}%"></i></div><span class="red">{html.escape(summary["p_down"])}</span></div>
        <div class="trade-call">{html.escape(summary["decision"])} @ {html.escape(summary["candidate_price"])}</div>
        <div class="small">reason: {html.escape(summary["reason"])}</div>
      </div>
      <div class="score-box">
        <div class="row"><span>{self._label("Confluence", "Confidence scaled to a 20-point glance score.")}</span><b>{html.escape(summary["confluence"])}/20 {html.escape(summary["likely"])}</b></div>
        <div class="scorebar"><i style="width:{summary["confluence_percent"]}%"></i></div>
        <div class="dots">{summary["dots"]}</div>
        <div class="row"><span>{self._label("Core score", "Count of aligned core inputs: target side, probability, trend, slopes, book, volume, S/R, PM flow, Binance flow.")}</span><b>{html.escape(summary["core_score"])}/9</b></div>
        <div class="scorebar redbar"><i style="width:{summary["core_percent"]}%"></i></div>
        <div class="chips">{summary["chips"]}</div>
      </div>
      <div class="history-box">
        <div class="row"><span>{self._label("History", "Current session win-rate and optimizer bucket if enough history exists.")}</span><b>{html.escape(summary["window_label"])}</b></div>
        <div class="history-rate"><span class="green">{html.escape(summary["win_rate"])}</span><span class="red">{html.escape(summary["loss_rate"])}</span></div>
        <div class="small">active PnL: {html.escape(summary["active_market_pnl"])} - if UP/DOWN {html.escape(summary["pnl_if_up_down"])}</div>
        <div class="small">settled PnL: {html.escape(summary["settled_pnl"])} - worst case {html.escape(summary["worst_case"])}</div>
      </div>
      <div class="momentum-box">
        <div class="label">{self._label("Momentum", "Distance, Binance book imbalance, and MA trend strength shown as quick meters.")}</div>
        <div class="meter"><span>Delta</span><i style="width:{summary["delta_meter"]}%"></i><b>{html.escape(summary["delta_text"])}</b></div>
        <div class="meter"><span>Book</span><i class="greenbar" style="width:{summary["book_meter"]}%"></i><b>{html.escape(summary["book_text"])}</b></div>
        <div class="meter"><span>Trend</span><i class="yellowbar" style="width:{summary["trend_meter"]}%"></i><b>{html.escape(summary["trend_text"])}</b></div>
      </div>
      <div class="reversal-box">
        <div class="label">{self._label("Reversal", "Warns when signal, actual side, S/R, or PM flow disagree.")}</div>
        <div class="{summary["reversal_class"]}">{html.escape(summary["reversal"])}</div>
      </div>
"""

    def _summary(
        self,
        last: dict[str, Any],
        indicators: dict[str, Any],
        live_account: dict[str, Any],
        optimization: dict[str, Any],
    ) -> dict[str, Any]:
        """Build display text and simple derived scores for the HTML cards."""
        price = last.get("price")
        target = last.get("target")
        p_up = last.get("p_up")
        p_down = last.get("p_down")
        likely = "UP" if (p_up or 0.0) >= (p_down or 0.0) else "DOWN"
        actual = "UP" if price is not None and target is not None and price > target else "DOWN"
        signal_class = "up" if likely == "UP" else "down"
        trend = self._trend(last)
        core_score = self._core_score(last, likely)
        confluence = max(0, min(20, round(float(last.get("confidence") or 0.0) * 20)))
        win_rate = last.get("win_rate")
        loss_rate = 100.0 - float(win_rate) if win_rate is not None else None
        rsi = indicators.get("last_rsi")
        stoch = indicators.get("last_stoch")
        vwap = indicators.get("last_vwap")
        distance = last.get("distance")
        sr_bias = last.get("sr_bias")
        pm_flow_bias = last.get("pm_flow_bias")
        binance_flow_bias = last.get("binance_flow_bias")
        reversal = "No reversal detected"
        reversal_class = "muted"
        if likely != actual:
            reversal = f"Signal {likely}, spot currently {actual}"
            reversal_class = "warn"
        elif sr_bias and sr_bias != likely:
            reversal = f"S/R opposes signal: {sr_bias}"
            reversal_class = "warn"
        elif binance_flow_bias and binance_flow_bias != "NEUTRAL" and binance_flow_bias != likely:
            reversal = f"Binance flow opposes signal: {binance_flow_bias}"
            reversal_class = "warn"
        elif pm_flow_bias and pm_flow_bias != likely:
            reversal = f"PM flow opposes signal: {pm_flow_bias}"
            reversal_class = "warn"
        active_market_pnl = last.get("pnl_if_up") if actual == "UP" else last.get("pnl_if_down")

        cards = [
            ("Trend", trend, "green" if trend == "UP" else "red", f"fast/mid/slow {self._fmt(last.get('ma_fast'))}/{self._fmt(last.get('ma_mid'))}/{self._fmt(last.get('ma_slow'))}", "Fast and mid MA versus slow MA. UP means both are above the slow MA."),
            ("RSI", self._fmt(rsi, 0), self._rsi_class(rsi), "14-period from live chart candles", "Relative Strength Index from the dashboard candles. High can mean stretched; low can mean washed out."),
            ("EMA Targets", self._fmt(last.get("ma_anchor")), "yellow", "anchor used for exit stability", "Long anchor MA used by exit logic to judge hold, trim, or sell stability."),
            ("OB Imbalance", self._fmt(last.get("orderbook_imbalance"), 3), self._signed_class(last.get("orderbook_imbalance")), "Binance book pressure", "Binance best bid/ask size imbalance. Positive supports UP pressure; negative supports DOWN pressure."),
            ("OFI / CVD", f"{self._fmt(last.get('ofi_signal'), 2)} / {self._fmt(last.get('cvd_signal'), 2)}", self._signed_class(last.get("binance_flow_score")), f"{last.get('binance_flow_bias') or 'n/a'} {self._fmt(last.get('binance_flow_adjustment') * 100.0 if last.get('binance_flow_adjustment') is not None else None, 2)}pp", "Binance order-flow imbalance and cumulative volume delta. Positive supports UP; negative supports DOWN."),
            ("Volume Spike", self._fmt(last.get("volume_spike"), 2), self._signed_class((last.get("volume_spike") or 1.0) - 1.0), "current volume / VMA10", "Current Binance volume divided by the 10-bar volume average."),
            ("Stochastic", self._fmt(stoch, 0), self._rsi_class(stoch), "close location in recent range", "Where the current close sits inside the recent high/low range."),
            ("Large Trade Flow", self._fmt(last.get("pm_trade_imbalance"), 3), self._signed_class(last.get("pm_trade_imbalance")), f"{self._fmt(last.get('pm_trade_volume'), 1)} shares", "Recent Polymarket matched trade imbalance across UP and DOWN tokens."),
            ("EMA Bull/Bear", f"{self._fmt(last.get('up_stability_score'), 0)} / {self._fmt(last.get('down_stability_score'), 0)}", "green" if trend == "UP" else "red", "MA stability scores", "UP and DOWN stability scores used by the sell/hold logic."),
            ("Support / Resist", f"{self._fmt(last.get('support'))} / {self._fmt(last.get('resistance'))}", "yellow", f"S/R bias {last.get('sr_bias') or 'n/a'}", "Nearest support and resistance levels built from Binance candles."),
            ("PM Flow", f"{last.get('pm_flow_bias') or 'n/a'} {self._fmt(last.get('pm_flow_score'), 2)}", "green" if last.get("pm_flow_bias") == "UP" else "red" if last.get("pm_flow_bias") == "DOWN" else "yellow", "book depth + matched trades", "Polymarket order-book depth and matched-trade side information."),
        ]

        return {
            "clock": time.strftime("%H:%M:%S"),
            "session_name": "LIVE SESSION" if last.get("mode") == "live" else "PAPER SESSION",
            "market": str(last.get("market_slug") or "waiting for market"),
            "open_left": f"{self._fmt(last.get('seconds_since_open'), 0)}s / {self._fmt(last.get('seconds_remaining'), 0)}s",
            "price": f"${self._fmt(price)}",
            "target": f"${self._fmt(target)}",
            "price_line": f"O/H/L/C derived from Aries ticks - last ${self._fmt(price)} - gap ${self._fmt(distance)}",
            "ma_line": f"{self._fmt(last.get('ma_fast'))}/{self._fmt(last.get('ma_mid'))}/{self._fmt(last.get('ma_slow'))}",
            "vwap": f"${self._fmt(vwap)}",
            "actual_class": "green" if actual == "UP" else "red",
            "up_ask": self._fmt(last.get("up_ask"), 3),
            "down_ask": self._fmt(last.get("down_ask"), 3),
            "gap": f"${self._fmt(distance)}",
            "gap_bps": f"{self._fmt(last.get('distance_bps'), 2)}bps",
            "likely": likely,
            "signal_icon": "^" if likely == "UP" else "v",
            "signal_class": signal_class,
            "p_up": self._fmt(p_up, 3),
            "p_down": self._fmt(p_down, 3),
            "p_up_percent": max(0, min(100, int((p_up or 0.0) * 100))),
            "decision": str(last.get("decision") or "WATCH"),
            "candidate_price": self._fmt(last.get("candidate_price"), 3),
            "reason": str(last.get("selected_reason") or "n/a"),
            "confluence": str(confluence),
            "confluence_percent": confluence * 5,
            "core_score": str(core_score),
            "core_percent": round(core_score / 9 * 100),
            "dots": self._dots(confluence, 20, likely),
            "chips": self._chips(last, likely),
            "window_label": str(optimization.get("best_first_entry_time") or "current session"),
            "win_rate": f"{self._fmt(win_rate, 1)}%",
            "loss_rate": f"{self._fmt(loss_rate, 1)}%",
            "settled_pnl": f"${self._fmt(last.get('settled_pnl'))}",
            "active_market_pnl": f"${self._fmt(active_market_pnl)} now-{actual}",
            "pnl_if_up_down": f"${self._fmt(last.get('pnl_if_up'))} / ${self._fmt(last.get('pnl_if_down'))}",
            "worst_case": f"${self._fmt(last.get('worst_case_pnl'))}",
            "delta_meter": self._meter(last.get("distance_bps")),
            "delta_text": self._fmt(last.get("distance_bps"), 2),
            "book_meter": self._meter(last.get("orderbook_imbalance"), scale=1.0),
            "book_text": self._fmt(last.get("orderbook_imbalance"), 2),
            "trend_meter": core_score / 9 * 100,
            "trend_text": trend,
            "reversal": reversal,
            "reversal_class": reversal_class,
            "cards": cards,
        }

    def _cards(self, cards: list[tuple[str, str, str, str, str]]) -> list[str]:
        output = []
        for title, value, color, subtitle, help_text in cards:
            output.append(
                f'<div class="mini-card {html.escape(color)}">'
                f'<span>{self._label(title, help_text)}</span>'
                f'<b>{html.escape(str(value))}</b>'
                f'<small>{html.escape(subtitle)}</small>'
                "</div>"
            )
        return output

    def _label(self, title: str, help_text: str) -> str:
        """Render a label with a small hoverable information icon."""
        return (
            f'{html.escape(title)} '
            f'<i class="info" title="{html.escape(help_text, quote=True)}">i</i>'
        )

    def _candles(self, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Group decision-cycle BTC prices into lightweight OHLCV candles."""
        buckets: list[dict[str, Any]] = []
        by_bucket: dict[int, dict[str, Any]] = {}
        for point in points:
            price = point.get("price")
            if price is None:
                continue
            bucket = int(point["ts"] // self.candle_seconds) * self.candle_seconds
            candle = by_bucket.get(bucket)
            volume = float(point.get("volume") or 0.0)
            if candle is None:
                candle = {
                    "ts": bucket,
                    "time": time.strftime("%H:%M:%S", time.localtime(bucket)),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume,
                    "target": point.get("target"),
                    "ma_fast": point.get("ma_fast"),
                    "ma_mid": point.get("ma_mid"),
                    "ma_slow": point.get("ma_slow"),
                    "ma_anchor": point.get("ma_anchor"),
                    "support": point.get("support"),
                    "resistance": point.get("resistance"),
                }
                by_bucket[bucket] = candle
                buckets.append(candle)
            else:
                candle["high"] = max(float(candle["high"]), price)
                candle["low"] = min(float(candle["low"]), price)
                candle["close"] = price
                candle["volume"] = float(candle["volume"]) + volume
                for key in ("target", "ma_fast", "ma_mid", "ma_slow", "ma_anchor", "support", "resistance"):
                    candle[key] = point.get(key)
        return buckets[-240:]

    def _attach_candle_tooltips(self, candles: list[dict[str, Any]], indicators: dict[str, Any]) -> None:
        """Precompute candle hover text so the browser only displays strings."""
        vwap = indicators.get("vwap") or []
        rsi = indicators.get("rsi") or []
        macd = indicators.get("macd") or []
        macd_signal = indicators.get("macd_signal") or []
        for index, candle in enumerate(candles):
            lines = [
                f"{candle.get('time')} BTC candle",
                f"O/H/L/C: {self._fmt(candle.get('open'))} / {self._fmt(candle.get('high'))} / {self._fmt(candle.get('low'))} / {self._fmt(candle.get('close'))}",
                f"Target: {self._fmt(candle.get('target'))}",
                f"MA fast/mid/slow: {self._fmt(candle.get('ma_fast'))} / {self._fmt(candle.get('ma_mid'))} / {self._fmt(candle.get('ma_slow'))}",
                f"VWAP: {self._fmt(vwap[index] if index < len(vwap) else None)}",
                f"RSI: {self._fmt(rsi[index] if index < len(rsi) else None, 1)}",
                f"MACD/signal: {self._fmt(macd[index] if index < len(macd) else None, 2)} / {self._fmt(macd_signal[index] if index < len(macd_signal) else None, 2)}",
                f"Support/resistance: {self._fmt(candle.get('support'))} / {self._fmt(candle.get('resistance'))}",
                f"Volume: {self._fmt(candle.get('volume'), 4)}",
            ]
            candle["tooltip"] = "\n".join(lines)

    def _indicators(self, candles: list[dict[str, Any]]) -> dict[str, Any]:
        closes = [float(c["close"]) for c in candles if c.get("close") is not None]
        volumes = [float(c.get("volume") or 1.0) for c in candles]
        vwap = self._vwap(closes, volumes)
        rsi = self._rsi(closes, 14)
        macd, signal, hist = self._macd(closes)
        stoch = self._stochastic(candles, 14)
        return {
            "vwap": vwap,
            "rsi": rsi,
            "macd": macd,
            "macd_signal": signal,
            "macd_hist": hist,
            "stoch": stoch,
            "last_vwap": vwap[-1] if vwap else None,
            "last_rsi": rsi[-1] if rsi else None,
            "last_stoch": stoch[-1] if stoch else None,
        }

    def _trade_events(self, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        events = []
        seen: set[tuple[Any, Any, Any]] = set()
        for point in points:
            decision = str(point.get("decision") or "")
            if not decision.startswith(("BUY_", "SELL_")):
                continue
            order_status = str(point.get("order_status") or "")
            if order_status != "FILLED" and not point.get("fill_id"):
                continue
            key = (point.get("ts"), decision, point.get("candidate_price"))
            if key in seen:
                continue
            seen.add(key)
            action = "BUY" if decision.startswith("BUY_") else "SELL"
            side = str(point.get("selected_side") or "")
            event_label = f"{action}_{side}" if side else action
            events.append(
                {
                    "ts": point.get("ts"),
                    "time": point.get("time"),
                    "action": action,
                    "side": side,
                    "label": event_label,
                    "marker_color": self._event_color(action, side),
                    "price": point.get("price"),
                    "pm_price": point.get("candidate_price"),
                    "shares": point.get("candidate_shares"),
                    "notional": point.get("candidate_notional"),
                    "edge": point.get("candidate_edge"),
                    "role": point.get("candidate_role"),
                    "order_id": point.get("order_id"),
                    "fill_id": point.get("fill_id"),
                    "order_status": point.get("order_status"),
                    "order_reason": point.get("order_reason"),
                    "reason": point.get("selected_reason"),
                    "tooltip": self._event_tooltip(point, event_label),
                }
            )
        return events[-self.max_trade_markers :]

    def _event_color(self, action: str, side: str) -> str:
        """Return the chart marker color for one buy/sell side."""
        if action == "BUY" and side == "UP":
            return "#23d18b"
        if action == "BUY" and side == "DOWN":
            return "#ff5c73"
        if action == "SELL" and side == "UP":
            return "#f7c948"
        if action == "SELL" and side == "DOWN":
            return "#4ea1ff"
        return "#d5dfef"

    def _event_tooltip(self, point: dict[str, Any], event_label: str) -> str:
        """Return full order details for hovering a chart execution marker."""
        return "\n".join(
            [
                f"{event_label} at {point.get('time')}",
                f"BTC price: {self._fmt(point.get('price'))}",
                f"PM price: {self._fmt(point.get('candidate_price'), 3)}",
                f"Shares/notional: {self._fmt(point.get('candidate_shares'), 3)} / ${self._fmt(point.get('candidate_notional'))}",
                f"Edge: {self._fmt(point.get('candidate_edge'), 3)}",
                f"Role/status: {point.get('candidate_role') or 'n/a'} / {point.get('order_status') or 'n/a'}",
                f"Order/fill: {point.get('order_id') or 'n/a'} / {point.get('fill_id') or 'n/a'}",
                f"Reason: {point.get('selected_reason') or 'n/a'}",
                f"Order reason: {point.get('order_reason') or 'n/a'}",
            ]
        )

    def _selected_candidate(self, decision: Any) -> dict[str, Any]:
        side = getattr(decision, "selected_side", None)
        name = str(getattr(decision, "decision", "") or "")
        if name.startswith("SELL_"):
            return self._dict(getattr(decision, "sell_candidate", {}))
        if side == "UP":
            return self._dict(getattr(decision, "candidate_up", {}))
        if side == "DOWN":
            return self._dict(getattr(decision, "candidate_down", {}))
        return {}

    def _core_score(self, point: dict[str, Any], likely: str) -> int:
        score = 0
        sign = 1 if likely == "UP" else -1
        price = point.get("price")
        target = point.get("target")
        if price is not None and target is not None and (price - target) * sign > 0:
            score += 1
        if (point.get("p_up") or 0.0) >= (point.get("p_down") or 0.0) and likely == "UP":
            score += 1
        if (point.get("p_down") or 0.0) > (point.get("p_up") or 0.0) and likely == "DOWN":
            score += 1
        if self._trend(point) == likely:
            score += 1
        if (point.get("fast_slope") or 0.0) * sign > 0:
            score += 1
        if (point.get("mid_slope") or 0.0) * sign >= 0:
            score += 1
        if (point.get("orderbook_imbalance") or 0.0) * sign > 0:
            score += 1
        if (point.get("volume_spike") or 0.0) >= 1.0:
            score += 1
        if point.get("sr_bias") == likely:
            score += 1
        if point.get("pm_flow_bias") == likely:
            score += 1
        if point.get("binance_flow_bias") == likely:
            score += 1
        return min(score, 9)

    def _trend(self, point: dict[str, Any]) -> str:
        fast = point.get("ma_fast")
        mid = point.get("ma_mid")
        slow = point.get("ma_slow")
        if fast is not None and mid is not None and slow is not None and fast > slow and mid > slow:
            return "UP"
        return "DOWN"

    def _chips(self, point: dict[str, Any], likely: str) -> str:
        chip_data = [
            ("S/R", point.get("sr_bias") == likely),
            ("PMF", point.get("pm_flow_bias") == likely),
            ("BFL", point.get("binance_flow_bias") == likely),
            ("OBI", (point.get("orderbook_imbalance") or 0.0) * (1 if likely == "UP" else -1) > 0),
            ("VOL", (point.get("volume_spike") or 0.0) >= 1.0),
        ]
        return "".join(
            f'<span class="chip {"ok" if ok else "bad"}">{html.escape(name)}</span>'
            for name, ok in chip_data
        )

    def _dots(self, active: int, total: int, side: str) -> str:
        color = "green" if side == "UP" else "red"
        return "".join(f'<i class="{color if idx < active else "off"}"></i>' for idx in range(total))

    def _vwap(self, closes: list[float], volumes: list[float]) -> list[Optional[float]]:
        output: list[Optional[float]] = []
        pv_sum = 0.0
        vol_sum = 0.0
        for price, volume in zip(closes, volumes):
            safe_volume = max(1e-9, volume)
            pv_sum += price * safe_volume
            vol_sum += safe_volume
            output.append(pv_sum / vol_sum if vol_sum else None)
        return output

    def _rsi(self, closes: list[float], period: int) -> list[Optional[float]]:
        if len(closes) < 2:
            return [None for _ in closes]
        output: list[Optional[float]] = [None]
        gains: list[float] = []
        losses: list[float] = []
        for prev, cur in zip(closes, closes[1:]):
            change = cur - prev
            gains.append(max(0.0, change))
            losses.append(max(0.0, -change))
            if len(gains) < period:
                output.append(None)
                continue
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            if avg_loss <= 1e-12:
                output.append(100.0)
            else:
                rs = avg_gain / avg_loss
                output.append(100.0 - (100.0 / (1.0 + rs)))
        return output

    def _macd(self, closes: list[float]) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
        fast = self._ema(closes, 12)
        slow = self._ema(closes, 26)
        macd = [
            (f - s) if f is not None and s is not None else None
            for f, s in zip(fast, slow)
        ]
        macd_values = [value if value is not None else 0.0 for value in macd]
        signal = self._ema(macd_values, 9)
        hist = [
            (m - s) if m is not None and s is not None else None
            for m, s in zip(macd, signal)
        ]
        return macd, signal, hist

    def _ema(self, values: list[float], period: int) -> list[Optional[float]]:
        if not values:
            return []
        alpha = 2.0 / (period + 1.0)
        output: list[Optional[float]] = []
        ema: Optional[float] = None
        for index, value in enumerate(values):
            ema = value if ema is None else (value * alpha) + (ema * (1.0 - alpha))
            output.append(ema if index >= period - 1 else None)
        return output

    def _stochastic(self, candles: list[dict[str, Any]], period: int) -> list[Optional[float]]:
        output: list[Optional[float]] = []
        for index, candle in enumerate(candles):
            if index + 1 < period:
                output.append(None)
                continue
            window = candles[index + 1 - period : index + 1]
            high = max(float(row["high"]) for row in window)
            low = min(float(row["low"]) for row in window)
            close = float(candle["close"])
            output.append(50.0 if math.isclose(high, low) else ((close - low) / (high - low) * 100.0))
        return output

    def _meter(self, value: Any, scale: float = 10.0) -> int:
        if value is None:
            return 0
        return max(0, min(100, round(abs(float(value)) / max(1e-9, scale) * 100)))

    def _rsi_class(self, value: Any) -> str:
        if value is None:
            return "yellow"
        value = float(value)
        if value >= 70:
            return "red"
        if value <= 30:
            return "green"
        return "yellow"

    def _signed_class(self, value: Any) -> str:
        if value is None:
            return "yellow"
        return "green" if float(value) >= 0 else "red"

    def _fmt(self, value: Any, digits: int = 2) -> str:
        if value is None:
            return "n/a"
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return str(value)

    def _num(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _dict(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _css(self) -> str:
        return """
:root{color-scheme:dark;--bg:#070b12;--panel:#0e1522;--panel2:#111a2a;--line:#263449;--text:#d5dfef;--muted:#7e8aa0;--green:#23d18b;--red:#ff5c73;--yellow:#f7c948;--blue:#4ea1ff;--orange:#ff9f43}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 55% 20%,#121b2d 0,#070b12 44%,#05070b 100%);color:var(--text);font:14px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.topbar{height:42px;display:flex;align-items:center;justify-content:space-between;padding:0 18px;border-bottom:1px solid #121b2d;background:#070b12cc;letter-spacing:.3px}.bolt{color:var(--orange)}.status{color:var(--muted);font-size:12px}.dot{display:inline-block;width:8px;height:8px;background:var(--green);border-radius:50%;box-shadow:0 0 10px var(--green);margin-right:6px}
.layout{display:grid;grid-template-columns:390px minmax(620px,1fr) 520px;gap:14px;padding:14px;align-items:start}.panel{border:1px solid #1b2840;background:linear-gradient(180deg,#101827cc,#09101bcc);border-radius:16px;box-shadow:0 0 22px #0008;padding:14px;min-width:0}.center{padding:0;overflow:hidden;min-height:860px}.label{color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:2px}.big{font-size:18px;font-weight:800}.clock{font-size:20px}.session,.row,.split{display:flex;justify-content:space-between;gap:12px}.market-card,.signal-box,.score-box,.history-box,.momentum-box,.reversal-box{margin-top:12px;padding:14px;border:1px solid #1b2840;border-radius:12px;background:#0b1320cc}.row span,.split span{display:block;color:var(--muted);font-size:11px}.split>div{flex:1;padding:9px;border:1px solid #162237;border-radius:9px;background:#0d1726}.boxes b{font-size:16px}.section-title{margin:14px 0 8px;color:#87a5cf;text-transform:uppercase;font-size:12px}.card-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.mini-card{min-height:78px;padding:10px;border:1px solid #1b2840;border-radius:10px;background:#111a2a}.mini-card span{display:block;color:#71829b;text-transform:uppercase;font-size:10px}.mini-card b{display:block;margin:4px 0;font-size:18px}.mini-card small{color:#8a96a8;font-size:10px}.green{color:var(--green)!important}.red{color:var(--red)!important}.yellow{color:var(--yellow)!important}.blue{color:var(--blue)!important}.orange{color:var(--orange)!important}
.chart-header{height:76px;padding:16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1b2840;background:#0a101bcc}.price-line{font-size:18px;margin-top:4px}.pills span{display:inline-block;margin-left:6px;padding:5px 8px;border-radius:6px;background:#101c2e;border:1px solid #22314b;color:#aab7ca}canvas{display:block;width:100%;background:#080d14;border-bottom:1px solid #151f31}#priceChart{height:430px!important}#rsiChart{height:145px!important}#macdChart{height:165px!important}#miniChart{height:90px!important}.legend{height:38px;padding:10px 14px}.legend span{margin-right:16px}
.signal{font-size:54px;font-weight:900;text-align:center;letter-spacing:5px;margin:18px 0}.signal.up{color:var(--green);text-shadow:0 0 30px #23d18b55}.signal.down{color:var(--red);text-shadow:0 0 30px #ff5c7355}.gap-row{display:grid;grid-template-columns:1fr auto auto;gap:12px;align-items:center;padding:11px;border-radius:10px;background:#111a2a}.gap-row span,.gap-row em{color:var(--muted);font-style:normal}.bar-wrap{display:grid;grid-template-columns:60px 1fr 60px;gap:10px;align-items:center;margin:18px 0}.probbar,.scorebar{height:9px;background:#1b2435;border-radius:99px;overflow:hidden}.probbar i,.scorebar i,.meter i{display:block;height:100%;background:linear-gradient(90deg,var(--green),var(--red))}.trade-call{text-align:center;border:1px solid #1d2a41;border-radius:10px;padding:14px;font-size:20px;font-weight:800}.small{color:var(--muted);font-size:11px;margin-top:8px}.dots{margin:12px 0}.dots i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:#25314a}.dots i.green{background:var(--green);box-shadow:0 0 8px #23d18b77}.dots i.red{background:var(--red);box-shadow:0 0 8px #ff5c7377}.chips{margin-top:10px}.chip{display:inline-block;margin-right:6px;padding:4px 7px;border-radius:6px;border:1px solid #263449;font-size:11px}.chip.ok{color:var(--green);background:#0e2a22}.chip.bad{color:var(--red);background:#2b1118}.history-rate{display:flex;justify-content:space-between;font-size:28px;font-weight:900}.meter{display:grid;grid-template-columns:70px 1fr 70px;gap:10px;align-items:center;margin:10px 0;color:var(--muted)}.meter i{height:8px;border-radius:99px;background:var(--red)}.meter i.greenbar{background:var(--green)}.meter i.yellowbar{background:var(--yellow)}.warn{color:var(--yellow);font-weight:800}.muted{color:var(--muted)}
.info{display:inline-flex;align-items:center;justify-content:center;width:13px;height:13px;border:1px solid #39506f;border-radius:50%;color:#8fb4df;font-style:normal;font-size:9px;margin-left:4px;cursor:help}.hover-tip{position:fixed;display:none;z-index:20;max-width:360px;white-space:pre-line;padding:10px 12px;border:1px solid #39506f;border-radius:9px;background:#050911ee;color:var(--text);box-shadow:0 10px 30px #000b;font-size:12px;pointer-events:none}.chart-hover{cursor:crosshair}
@media(max-width:1300px){.layout{grid-template-columns:1fr}.panel{width:100%}}
"""

    def _js(self) -> str:
        return """
let D=window.ARIES_DATA||{};
const C={bg:'#080d14',grid:'#1a2436',txt:'#8b98ad',green:'#23d18b',red:'#ff5c73',yellow:'#f7c948',blue:'#4ea1ff',orange:'#ff9f43',white:'#d5dfef'};
const STATE={price:null,rsi:null,macd:null,mini:null};
const TIP=document.getElementById('hoverTip');
function val(v){return typeof v==='number'&&isFinite(v)}
function fmt(v,d=2){return val(v)?Number(v).toFixed(d):'n/a'}
function showTip(text,x,y){if(!TIP||!text)return;TIP.textContent=text;TIP.style.display='block';const pad=16;const right=x+pad+TIP.offsetWidth;TIP.style.left=(right>window.innerWidth?x-TIP.offsetWidth-pad:x+pad)+'px';TIP.style.top=(y+pad)+'px'}
function hideTip(){if(TIP)TIP.style.display='none'}
function setup(id){const c=document.getElementById(id);const dpr=window.devicePixelRatio||1;const parent=c.parentElement||c;const w=Math.max(320,Math.floor(parent.clientWidth||c.clientWidth||320));const h=Number(c.dataset.h)||Number(c.getAttribute('height'))||240;c.style.width='100%';c.style.height=h+'px';c.width=Math.round(w*dpr);c.height=Math.round(h*dpr);c.classList.add('chart-hover');const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);return [c,x,w,h]}
function scaleY(values,h,pad=20){const xs=values.filter(val);if(!xs.length)return {min:0,max:1,y:v=>h/2};let min=Math.min(...xs),max=Math.max(...xs);if(min===max){min-=1;max+=1}const m=(max-min)*0.08;min-=m;max+=m;return {min,max,y:v=>h-pad-((v-min)/(max-min))*(h-pad*2)}}
function drawGrid(ctx,w,h){ctx.strokeStyle=C.grid;ctx.lineWidth=1;ctx.font='11px monospace';ctx.fillStyle=C.txt;for(let i=0;i<6;i++){const y=20+i*(h-40)/5;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}for(let i=0;i<8;i++){const x=i*w/7;ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h);ctx.stroke()}}
function line(ctx,arr,key,xFor,yFor,color,width=1.3){ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();let open=false;arr.forEach((d,i)=>{const v=d&&d[key];if(!val(v)){open=false;return}const x=xFor(i),y=yFor(v);if(!open){ctx.moveTo(x,y);open=true}else ctx.lineTo(x,y)});ctx.stroke()}
function candleIndexForEvent(candles,e){if(!candles.length)return -1;let best=0,bestDelta=Infinity;for(let i=0;i<candles.length;i++){const delta=Math.abs((candles[i].ts||0)-(e.ts||0));if(delta<bestDelta){best=i;bestDelta=delta}}return best}
function drawEventMarker(ctx,x,y,e){const size=8;ctx.fillStyle=e.marker_color||C.white;ctx.strokeStyle='#050911';ctx.lineWidth=1.5;ctx.beginPath();if(e.action==='BUY'){ctx.moveTo(x,y-12);ctx.lineTo(x-size,y+2);ctx.lineTo(x+size,y+2)}else{ctx.moveTo(x,y+12);ctx.lineTo(x-size,y-2);ctx.lineTo(x+size,y-2)}ctx.closePath();ctx.fill();ctx.stroke();ctx.font='10px monospace';ctx.fillText(e.label||'',x+10,y+(e.action==='BUY'?-8:12))}
function bindCanvasTooltip(canvas,state){canvas.onmouseleave=hideTip;canvas.onmousemove=ev=>{const r=canvas.getBoundingClientRect();const x=ev.clientX-r.left;const y=ev.clientY-r.top;if(state.kind==='price'){let hit=null;for(const m of state.markers){if(Math.hypot(x-m.x,y-m.y)<=14){hit=m;break}}if(hit){showTip(hit.event.tooltip,ev.clientX,ev.clientY);return}const i=Math.max(0,Math.min(state.candles.length-1,Math.round((x-state.step/2)/state.step)));const c=state.candles[i];showTip(c&&c.tooltip,ev.clientX,ev.clientY);return}const i=Math.max(0,Math.min(state.values.length-1,Math.round(x/state.step)));showTip(state.tooltips[i],ev.clientX,ev.clientY)}}
function drawPrice(){const [c,ctx,w,h]=setup('priceChart'), candles=D.candles||[];ctx.clearRect(0,0,w,h);drawGrid(ctx,w,h);if(!candles.length)return;const vals=[];candles.forEach(d=>['high','low','target','ma_fast','ma_mid','ma_slow','ma_anchor','support','resistance'].forEach(k=>{if(val(d[k]))vals.push(d[k])}));(D.indicators.vwap||[]).forEach(v=>{if(val(v))vals.push(v)});const sy=scaleY(vals,h,26);const step=w/Math.max(1,candles.length);const xFor=i=>i*step+step/2;line(ctx,candles,'target',xFor,sy.y,C.blue);line(ctx,candles,'ma_fast',xFor,sy.y,C.orange);line(ctx,candles,'ma_mid',xFor,sy.y,'#b96cff');line(ctx,candles,'ma_slow',xFor,sy.y,'#6c83ff');line(ctx,candles,'support',xFor,sy.y,'#1f8b5f');line(ctx,candles,'resistance',xFor,sy.y,'#953449');const vwap=(D.indicators.vwap||[]).map((v,i)=>({...candles[i],vwap:v}));line(ctx,vwap,'vwap',xFor,sy.y,C.yellow,1.1);candles.forEach((d,i)=>{const x=xFor(i),bw=Math.max(2,Math.min(10,step*.55));const up=d.close>=d.open;ctx.strokeStyle=up?C.green:C.red;ctx.fillStyle=up?'#23d18b99':'#ff5c7399';ctx.beginPath();ctx.moveTo(x,sy.y(d.high));ctx.lineTo(x,sy.y(d.low));ctx.stroke();const y1=sy.y(Math.max(d.open,d.close)),y2=sy.y(Math.min(d.open,d.close));ctx.fillRect(x-bw/2,y1,bw,Math.max(1,y2-y1))});const markers=[];(D.events||[]).forEach(e=>{const i=candleIndexForEvent(candles,e);if(i<0)return;const x=xFor(i),y=sy.y(e.price||candles[i].close);markers.push({x,y,event:e});drawEventMarker(ctx,x,y,e)});ctx.fillStyle=C.txt;ctx.font='11px monospace';ctx.fillText('hover candles, lines, and order markers for details',12,18);STATE.price={kind:'price',canvas:c,candles,markers,step};bindCanvasTooltip(c,STATE.price)}
function drawMini(){const el=document.getElementById('miniChart');if(!el)return;const [c,ctx,w,h]=setup('miniChart'), pts=D.points||[];ctx.clearRect(0,0,w,h);const vals=pts.map(p=>p.price).filter(val);const sy=scaleY(vals,h,8);ctx.strokeStyle=C.orange;ctx.lineWidth=2;ctx.beginPath();pts.forEach((p,i)=>{if(!val(p.price))return;const x=i*w/Math.max(1,pts.length-1),y=sy.y(p.price);if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y)});ctx.stroke();const t=pts[pts.length-1]?.target;if(val(t)){ctx.strokeStyle=C.yellow;ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(0,sy.y(t));ctx.lineTo(w,sy.y(t));ctx.stroke();ctx.setLineDash([])}}
function drawRsi(){const [c,ctx,w,h]=setup('rsiChart'), arr=D.indicators.rsi||[];ctx.clearRect(0,0,w,h);drawGrid(ctx,w,h);const y=v=>h-15-(v/100)*(h-30), step=w/Math.max(1,arr.length-1);ctx.strokeStyle=C.grid;[30,70].forEach(v=>{ctx.beginPath();ctx.moveTo(0,y(v));ctx.lineTo(w,y(v));ctx.stroke()});ctx.strokeStyle='#8d6cff';ctx.lineWidth=1.4;ctx.beginPath();let open=false;arr.forEach((v,i)=>{if(!val(v)){open=false;return}const x=i*step;if(!open){ctx.moveTo(x,y(v));open=true}else ctx.lineTo(x,y(v))});ctx.stroke();ctx.fillStyle=C.txt;ctx.font='11px monospace';ctx.fillText('RSI 14 - hover for value',12,18);STATE.rsi={kind:'indicator',canvas:c,values:arr,step,tooltips:arr.map((v,i)=>`RSI 14\\nCandle: ${(D.candles||[])[i]?.time||'n/a'}\\nValue: ${fmt(v,1)}\\nZone: ${val(v)&&v>=70?'overbought':val(v)&&v<=30?'oversold':'neutral'}`)};bindCanvasTooltip(c,STATE.rsi)}
function drawMacd(){const [c,ctx,w,h]=setup('macdChart'), m=D.indicators.macd||[],s=D.indicators.macd_signal||[],hist=D.indicators.macd_hist||[];ctx.clearRect(0,0,w,h);drawGrid(ctx,w,h);const vals=[...m,...s,...hist].filter(val);const sy=scaleY(vals,h,18), step=w/Math.max(1,m.length-1);hist.forEach((v,i)=>{if(!val(v))return;ctx.fillStyle=v>=0?C.green:C.red;const x=i*step,y=sy.y(v),zero=sy.y(0);ctx.fillRect(x-2,Math.min(y,zero),4,Math.max(1,Math.abs(zero-y)))});function arrLine(a,color){ctx.strokeStyle=color;ctx.lineWidth=1.3;ctx.beginPath();let open=false;a.forEach((v,i)=>{if(!val(v)){open=false;return}const x=i*step,y=sy.y(v);if(!open){ctx.moveTo(x,y);open=true}else ctx.lineTo(x,y)});ctx.stroke()}arrLine(m,C.blue);arrLine(s,C.orange);ctx.fillStyle=C.txt;ctx.font='11px monospace';ctx.fillText('MACD 12 26 9 - hover for value',12,18);STATE.macd={kind:'indicator',canvas:c,values:m,step,tooltips:m.map((v,i)=>`MACD 12/26/9\\nCandle: ${(D.candles||[])[i]?.time||'n/a'}\\nMACD: ${fmt(v,2)}\\nSignal: ${fmt(s[i],2)}\\nHist: ${fmt(hist[i],2)}`)};bindCanvasTooltip(c,STATE.macd)}
let pendingDraw=false;
function redraw(){if(pendingDraw)return;pendingDraw=true;requestAnimationFrame(()=>{pendingDraw=false;drawPrice();drawMini();drawRsi();drawMacd()})}
function applyData(next){D=next||D;const html=D.html||{};const left=document.querySelector('.panel.left');const right=document.querySelector('.panel.right');if(left&&html.left&&left.innerHTML!==html.left)left.innerHTML=html.left;if(right&&html.right&&right.innerHTML!==html.right)right.innerHTML=html.right;const priceLine=document.getElementById('priceLine');if(priceLine&&html.price_line)priceLine.textContent=html.price_line;const targetPill=document.getElementById('targetPill');if(targetPill&&html.target)targetPill.textContent='Target '+html.target;const vwapPill=document.getElementById('vwapPill');if(vwapPill&&html.vwap)vwapPill.textContent='VWAP '+html.vwap;const maPill=document.getElementById('maPill');if(maPill&&html.ma_line)maPill.textContent='MA '+html.ma_line;const last=document.getElementById('lastUpdate');if(last)last.textContent=D.generated_label||new Date().toLocaleTimeString();redraw()}
async function pollData(){try{const res=await fetch((window.ARIES_DATA_URL||'live_data.json')+'?t='+Date.now(),{cache:'no-store'});if(!res.ok)return;const next=await res.json();applyData(next)}catch(e){}}
applyData(D);
pollData();
setInterval(pollData,window.ARIES_POLL_MS||1000);
"""
