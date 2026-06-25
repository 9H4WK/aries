from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Optional

from .models import BinanceState, ProbabilityState


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalized_difference(left: float, right: float) -> float:
    total = abs(left) + abs(right)
    return 0.0 if total <= 1e-12 else _clamp((left - right) / total, -1.0, 1.0)


def _ema(previous: Optional[float], value: float, elapsed: Optional[float], period_seconds: float) -> float:
    if previous is None or elapsed is None:
        return value
    alpha = _clamp(elapsed / max(period_seconds, elapsed, 1e-9), 0.0, 1.0)
    return (value * alpha) + (previous * (1.0 - alpha))


@dataclass
class BinanceFlowState:
    """Rolling Binance OFI/CVD side-information for probability nudges."""

    enabled: bool = False
    ready: bool = False
    updated_at: float = 0.0
    lookback_seconds: float = 0.0
    ofi_age_seconds: Optional[float] = None
    cvd_age_seconds: Optional[float] = None
    ofi_events_count: int = 0
    cvd_events_count: int = 0
    ofi_sum: float = 0.0
    ofi_abs_sum: float = 0.0
    ofi_signal: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    cvd: float = 0.0
    cvd_total_volume: float = 0.0
    cvd_signal: float = 0.0
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


class BinanceFlowEngine:
    """Convert Binance order flow into a small, capped probability nudge.

    OFI measures short-term liquidity pressure from best bid/ask changes.
    CVD measures aggressive taker-buy volume minus taker-sell volume. Neither
    signal can place trades directly; both only adjust Aries' existing p_up.
    """

    def __init__(self, config: dict):
        self.config = config
        self.state = BinanceFlowState(enabled=bool(_get(config, "binance_flow.enabled", False)))
        self._fast_ema: Optional[float] = None
        self._slow_ema: Optional[float] = None
        self._last_update_ts: Optional[float] = None

    def update(self, state: BinanceState, now_ts: Optional[float] = None) -> BinanceFlowState:
        """Recalculate rolling Binance OFI/CVD state from the stream snapshot."""
        now = float(now_ts or time.time())
        flow_config = self.config.get("binance_flow", {})
        if not bool(flow_config.get("enabled", False)):
            self.state = BinanceFlowState(enabled=False, updated_at=now, reason="disabled")
            return self.state

        lookback = max(1.0, float(flow_config.get("lookback_seconds", 20.0)))
        max_age = max(0.1, float(flow_config.get("max_age_seconds", 3.0)))
        since = now - lookback
        ofi_events = [(ts, value) for ts, value in state.ofi_events if ts >= since]
        cvd_events = [(ts, value) for ts, value in state.cvd_events if ts >= since]
        ofi_age = now - ofi_events[-1][0] if ofi_events else None
        cvd_age = now - cvd_events[-1][0] if cvd_events else None

        min_book_events = max(1, int(flow_config.get("min_book_events", 3)))
        ofi_ready = bool(ofi_events) and len(ofi_events) >= min_book_events and ofi_age is not None and ofi_age <= max_age
        ofi_sum = sum(value for _, value in ofi_events)
        ofi_abs_sum = sum(abs(value) for _, value in ofi_events)
        ofi_signal = _clamp(ofi_sum / ofi_abs_sum, -1.0, 1.0) if ofi_ready and ofi_abs_sum > 1e-12 else 0.0

        buy_volume = sum(max(0.0, value) for _, value in cvd_events)
        sell_volume = sum(max(0.0, -value) for _, value in cvd_events)
        total_volume = buy_volume + sell_volume
        min_trade_volume = max(0.0, float(flow_config.get("min_trade_volume_btc", 0.001)))
        cvd_ready = bool(cvd_events) and total_volume >= min_trade_volume and cvd_age is not None and cvd_age <= max_age
        cvd = buy_volume - sell_volume
        cvd_signal = _normalized_difference(buy_volume, sell_volume) if cvd_ready else 0.0

        components: list[tuple[float, float]] = []
        if ofi_ready:
            components.append((float(flow_config.get("ofi_weight", 0.50)), ofi_signal))
        if cvd_ready:
            components.append((float(flow_config.get("cvd_weight", 0.50)), cvd_signal))
        positive_weight = sum(max(0.0, weight) for weight, _ in components)
        raw_signal = (
            sum(max(0.0, weight) * value for weight, value in components) / positive_weight
            if positive_weight > 0.0
            else 0.0
        )
        raw_signal = _clamp(raw_signal, -1.0, 1.0)

        if not components:
            self.state = BinanceFlowState(
                enabled=True,
                ready=False,
                updated_at=now,
                lookback_seconds=lookback,
                ofi_age_seconds=ofi_age,
                cvd_age_seconds=cvd_age,
                ofi_events_count=len(ofi_events),
                cvd_events_count=len(cvd_events),
                ofi_sum=ofi_sum,
                ofi_abs_sum=ofi_abs_sum,
                buy_volume=buy_volume,
                sell_volume=sell_volume,
                cvd=cvd,
                cvd_total_volume=total_volume,
                reason="flow_missing_or_stale",
            )
            return self.state

        elapsed = max(0.001, now - self._last_update_ts) if self._last_update_ts is not None else None
        self._fast_ema = _ema(self._fast_ema, raw_signal, elapsed, float(flow_config.get("fast_ema_seconds", 3.0)))
        self._slow_ema = _ema(self._slow_ema, raw_signal, elapsed, float(flow_config.get("slow_ema_seconds", 15.0)))
        self._last_update_ts = now
        fast_weight = _clamp(float(flow_config.get("fast_ema_weight", 0.70)), 0.0, 1.0)
        signal = _clamp((fast_weight * self._fast_ema) + ((1.0 - fast_weight) * self._slow_ema), -1.0, 1.0)
        bias_side = "UP" if signal > 0.05 else "DOWN" if signal < -0.05 else "NEUTRAL"
        p_up_bias = _clamp(0.50 + (signal * 0.50), 0.0, 1.0)
        reason_parts = []
        if ofi_ready:
            reason_parts.append("ofi")
        if cvd_ready:
            reason_parts.append("cvd")

        self.state = BinanceFlowState(
            enabled=True,
            ready=True,
            updated_at=now,
            lookback_seconds=lookback,
            ofi_age_seconds=ofi_age,
            cvd_age_seconds=cvd_age,
            ofi_events_count=len(ofi_events),
            cvd_events_count=len(cvd_events),
            ofi_sum=ofi_sum,
            ofi_abs_sum=ofi_abs_sum,
            ofi_signal=ofi_signal,
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            cvd=cvd,
            cvd_total_volume=total_volume,
            cvd_signal=cvd_signal,
            raw_signal=raw_signal,
            fast_ema=self._fast_ema,
            slow_ema=self._slow_ema,
            signal=signal,
            bias_side=bias_side,
            p_up_bias=p_up_bias,
            p_down_bias=1.0 - p_up_bias,
            reason="+".join(reason_parts),
        )
        return self.state


def apply_binance_flow_bias(
    config: dict,
    probability: ProbabilityState,
    flow_state: BinanceFlowState,
) -> ProbabilityState:
    """Apply OFI/CVD as a capped side-information nudge to p_up/p_down."""
    if probability.p_up is None or probability.p_down is None or not flow_state.ready:
        return probability
    flow_config = config.get("binance_flow", {})
    if not bool(flow_config.get("enabled", False)) or not bool(flow_config.get("apply_to_probability", True)):
        return probability
    max_adjustment = abs(float(flow_config.get("max_probability_adjustment", 0.06)))
    delta = _clamp(
        flow_state.signal * float(flow_config.get("probability_weight", 0.08)),
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
        reason=f"{probability.reason}+binance_flow_{flow_state.bias_side.lower()}",
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


def _get(config: dict, path: str, default=None):
    current = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current
