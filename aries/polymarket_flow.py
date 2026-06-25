from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Optional

from .models import ProbabilityState
from .polymarket_ws import PolymarketQuoteStore


@dataclass
class PolymarketFlowState:
    """Current PM order-book and matched-trade side information."""

    enabled: bool = False
    ready: bool = False
    updated_at: float = 0.0
    book_age_seconds: Optional[float] = None
    up_best_bid_size: float = 0.0
    up_best_ask_size: float = 0.0
    down_best_bid_size: float = 0.0
    down_best_ask_size: float = 0.0
    up_bid_depth: float = 0.0
    up_ask_depth: float = 0.0
    down_bid_depth: float = 0.0
    down_ask_depth: float = 0.0
    up_queue_imbalance: float = 0.0
    down_queue_imbalance: float = 0.0
    cross_queue_signal: float = 0.0
    up_depth_imbalance: float = 0.0
    down_depth_imbalance: float = 0.0
    cross_depth_signal: float = 0.0
    up_microprice: Optional[float] = None
    down_microprice: Optional[float] = None
    cross_microprice_signal: float = 0.0
    up_buy_shares: float = 0.0
    up_sell_shares: float = 0.0
    down_buy_shares: float = 0.0
    down_sell_shares: float = 0.0
    trade_volume_shares: float = 0.0
    trade_imbalance: float = 0.0
    raw_signal: float = 0.0
    fast_ema: float = 0.0
    slow_ema: float = 0.0
    signal: float = 0.0
    bias_side: str = "NEUTRAL"
    p_up_bias: float = 0.50
    p_down_bias: float = 0.50
    probability_adjustment: float = 0.0
    reason: str = "not_ready"

    def to_dict(self) -> dict:
        return asdict(self)


class PolymarketFlowEngine:
    """Combine PM L2 depth and matched trades into a bounded side signal.

    Queue imbalance follows the best-level predictor studied by Gould and
    Bonart. Depth and matched-flow imbalance follow the supply/demand framing
    of Cont, Kukanov, and Stoikov. The weights remain configurable because the
    published results do not calibrate BTC five-minute prediction markets.
    """

    def __init__(self, config: dict, quote_store: PolymarketQuoteStore):
        self.config = config
        self.quote_store = quote_store
        self.state = PolymarketFlowState(enabled=bool(_get(config, "polymarket_flow.enabled", False)))
        self._market_key: Optional[tuple[str, str]] = None
        self._fast_ema: Optional[float] = None
        self._slow_ema: Optional[float] = None
        self._last_update_ts: Optional[float] = None

    def update(
        self,
        up_token_id: Optional[str],
        down_token_id: Optional[str],
        now_ts: Optional[float] = None,
    ) -> PolymarketFlowState:
        """Recalculate PM flow state for the active UP/DOWN token pair."""
        now = float(now_ts or time.time())
        flow_config = self.config.get("polymarket_flow", {})
        if not bool(flow_config.get("enabled", False)):
            self.state = PolymarketFlowState(enabled=False, updated_at=now, reason="disabled")
            return self.state
        if not up_token_id or not down_token_id:
            self.state = PolymarketFlowState(enabled=True, updated_at=now, reason="market_missing")
            return self.state

        market_key = (up_token_id, down_token_id)
        if market_key != self._market_key:
            self._market_key = market_key
            self._fast_ema = None
            self._slow_ema = None
            self._last_update_ts = None

        levels = max(1, int(flow_config.get("levels", 5)))
        up_book = self.quote_store.book_levels(up_token_id, levels)
        down_book = self.quote_store.book_levels(down_token_id, levels)
        timestamps = [value for value in (up_book.get("ts"), down_book.get("ts")) if value is not None]
        age = max((max(0.0, now - float(value)) for value in timestamps), default=None)
        max_age = float(flow_config.get("max_age_seconds", 3.0))
        if len(timestamps) < 2 or age is None or age > max_age:
            self.state = PolymarketFlowState(
                enabled=True,
                updated_at=now,
                book_age_seconds=age,
                reason="book_missing_or_stale",
            )
            return self.state

        min_depth = max(0.0, float(flow_config.get("min_depth_shares", 5.0)))
        up = _book_metrics(up_book, min_depth)
        down = _book_metrics(down_book, min_depth)
        if not up["ready"] or not down["ready"]:
            self.state = PolymarketFlowState(
                enabled=True,
                updated_at=now,
                book_age_seconds=age,
                reason="insufficient_two_sided_depth",
            )
            return self.state

        cross_queue = _clamp((up["queue"] - down["queue"]) / 2.0, -1.0, 1.0)
        cross_depth = _clamp((up["depth"] - down["depth"]) / 2.0, -1.0, 1.0)
        cross_microprice = _clamp((up["micro_signal"] - down["micro_signal"]) / 2.0, -1.0, 1.0)

        lookback = max(1.0, float(flow_config.get("trade_lookback_seconds", 30.0)))
        up_trades = self.quote_store.recent_trades(up_token_id, now - lookback)
        down_trades = self.quote_store.recent_trades(down_token_id, now - lookback)
        trade_sizes = _trade_sizes(up_trades, down_trades)
        total_trade_volume = sum(trade_sizes.values())
        bullish_trade_volume = trade_sizes["up_buy"] + trade_sizes["down_sell"]
        bearish_trade_volume = trade_sizes["up_sell"] + trade_sizes["down_buy"]
        trade_imbalance = _normalized_difference(bullish_trade_volume, bearish_trade_volume)
        trade_ready = total_trade_volume >= float(flow_config.get("min_trade_volume_shares", 1.0))

        components = [
            (float(flow_config.get("queue_weight", 0.30)), cross_queue),
            (float(flow_config.get("depth_weight", 0.25)), cross_depth),
            (float(flow_config.get("microprice_weight", 0.15)), cross_microprice),
        ]
        if trade_ready:
            components.append((float(flow_config.get("trade_weight", 0.30)), trade_imbalance))
        positive_weight = sum(max(0.0, weight) for weight, _ in components)
        raw_signal = (
            sum(max(0.0, weight) * value for weight, value in components) / positive_weight
            if positive_weight > 0
            else 0.0
        )
        raw_signal = _clamp(raw_signal, -1.0, 1.0)

        elapsed = max(0.001, now - self._last_update_ts) if self._last_update_ts is not None else None
        self._fast_ema = _ema(self._fast_ema, raw_signal, elapsed, float(flow_config.get("fast_ema_seconds", 5.0)))
        self._slow_ema = _ema(self._slow_ema, raw_signal, elapsed, float(flow_config.get("slow_ema_seconds", 20.0)))
        self._last_update_ts = now
        fast_weight = _clamp(float(flow_config.get("fast_ema_weight", 0.70)), 0.0, 1.0)
        signal = _clamp((fast_weight * self._fast_ema) + ((1.0 - fast_weight) * self._slow_ema), -1.0, 1.0)
        bias_side = "UP" if signal > 0.05 else "DOWN" if signal < -0.05 else "NEUTRAL"
        p_up_bias = _clamp(0.50 + (signal * 0.50), 0.0, 1.0)

        self.state = PolymarketFlowState(
            enabled=True,
            ready=True,
            updated_at=now,
            book_age_seconds=age,
            up_best_bid_size=up["best_bid_size"],
            up_best_ask_size=up["best_ask_size"],
            down_best_bid_size=down["best_bid_size"],
            down_best_ask_size=down["best_ask_size"],
            up_bid_depth=up["bid_depth"],
            up_ask_depth=up["ask_depth"],
            down_bid_depth=down["bid_depth"],
            down_ask_depth=down["ask_depth"],
            up_queue_imbalance=up["queue"],
            down_queue_imbalance=down["queue"],
            cross_queue_signal=cross_queue,
            up_depth_imbalance=up["depth"],
            down_depth_imbalance=down["depth"],
            cross_depth_signal=cross_depth,
            up_microprice=up["microprice"],
            down_microprice=down["microprice"],
            cross_microprice_signal=cross_microprice,
            up_buy_shares=trade_sizes["up_buy"],
            up_sell_shares=trade_sizes["up_sell"],
            down_buy_shares=trade_sizes["down_buy"],
            down_sell_shares=trade_sizes["down_sell"],
            trade_volume_shares=total_trade_volume,
            trade_imbalance=trade_imbalance if trade_ready else 0.0,
            raw_signal=raw_signal,
            fast_ema=self._fast_ema,
            slow_ema=self._slow_ema,
            signal=signal,
            bias_side=bias_side,
            p_up_bias=p_up_bias,
            p_down_bias=1.0 - p_up_bias,
            reason="book_and_trade_flow" if trade_ready else "book_flow_no_recent_trades",
        )
        return self.state


def apply_polymarket_flow_bias(
    config: dict,
    probability: ProbabilityState,
    flow_state: PolymarketFlowState,
) -> ProbabilityState:
    """Apply a conservative, capped PM-flow nudge to existing probability."""
    if probability.p_up is None or probability.p_down is None or not flow_state.ready:
        return probability
    flow_config = config.get("polymarket_flow", {})
    if not bool(flow_config.get("enabled", False)) or not bool(flow_config.get("apply_to_probability", True)):
        return probability
    max_adjustment = abs(float(flow_config.get("max_probability_adjustment", 0.04)))
    delta = _clamp(
        flow_state.signal * float(flow_config.get("probability_weight", 0.06)),
        -max_adjustment,
        max_adjustment,
    )
    flow_state.probability_adjustment = delta
    pconf = config.get("probability", {})
    p_up = _clamp(
        float(probability.p_up) + delta,
        float(pconf.get("clamp_min", 0.01)),
        float(pconf.get("clamp_max", 0.99)),
    )
    p_down = 1.0 - p_up
    confidence = min(1.0, abs(p_up - 0.5) * 2.0)
    if probability.inside_noise_band:
        confidence *= 0.25
    return ProbabilityState(
        p_up=p_up,
        p_down=p_down,
        confidence=confidence,
        distance_from_target=probability.distance_from_target,
        distance_bps=probability.distance_bps,
        inside_noise_band=probability.inside_noise_band,
        reason=f"{probability.reason}+pm_flow_{flow_state.bias_side.lower()}",
        raw_score=probability.raw_score,
        distance_score=probability.distance_score,
        ma_score=probability.ma_score,
        slope_score=probability.slope_score,
        volume_score=probability.volume_score,
        imbalance_score=probability.imbalance_score,
        time_factor=probability.time_factor,
        adx_score=probability.adx_score,
        vwap_score=probability.vwap_score,
        macd_score=probability.macd_score,
    )


def _book_metrics(book: dict, min_depth: float) -> dict:
    bids = list(book.get("bids") or [])
    asks = list(book.get("asks") or [])
    if not bids or not asks:
        return {"ready": False}
    best_bid, best_bid_size = bids[0]
    best_ask, best_ask_size = asks[0]
    bid_depth = sum(float(size) for _, size in bids)
    ask_depth = sum(float(size) for _, size in asks)
    if min(best_bid_size + best_ask_size, bid_depth + ask_depth) < min_depth or best_ask <= best_bid:
        return {"ready": False}
    queue = _normalized_difference(best_bid_size, best_ask_size)
    depth = _normalized_difference(bid_depth, ask_depth)
    mid = (best_bid + best_ask) / 2.0
    spread_half = (best_ask - best_bid) / 2.0
    microprice = ((best_ask * best_bid_size) + (best_bid * best_ask_size)) / (best_bid_size + best_ask_size)
    micro_signal = _clamp((microprice - mid) / spread_half, -1.0, 1.0) if spread_half > 0 else 0.0
    return {
        "ready": True,
        "best_bid_size": float(best_bid_size),
        "best_ask_size": float(best_ask_size),
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "queue": queue,
        "depth": depth,
        "microprice": microprice,
        "micro_signal": micro_signal,
    }


def _trade_sizes(up_trades: list[dict], down_trades: list[dict]) -> dict[str, float]:
    sizes = {"up_buy": 0.0, "up_sell": 0.0, "down_buy": 0.0, "down_sell": 0.0}
    for prefix, trades in (("up", up_trades), ("down", down_trades)):
        for trade in trades:
            side = str(trade.get("side") or "").lower()
            if side in {"buy", "sell"}:
                sizes[f"{prefix}_{side}"] += max(0.0, float(trade.get("size") or 0.0))
    return sizes


def _normalized_difference(positive: float, negative: float) -> float:
    total = float(positive) + float(negative)
    return _clamp((float(positive) - float(negative)) / total, -1.0, 1.0) if total > 0 else 0.0


def _ema(previous: Optional[float], value: float, elapsed: Optional[float], seconds: float) -> float:
    if previous is None or elapsed is None:
        return value
    alpha = 1.0 - math.exp(-max(0.0, elapsed) / max(0.001, seconds))
    return previous + (alpha * (value - previous))


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _get(config: dict, path: str, default):
    if hasattr(config, "get_path"):
        return config.get_path(path, default)
    current = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current
