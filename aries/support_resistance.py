from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Optional

from .models import BinanceState, ProbabilityState


@dataclass
class SRCandle:
    ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class SRZone:
    side: str
    price: float
    lower: float
    upper: float
    strength: float
    touches: int
    windows: list[int]
    last_ts: int


@dataclass
class SupportResistanceState:
    enabled: bool = False
    updated_at: float = 0.0
    candle_seconds: int = 15
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    support_strength: float = 0.0
    resistance_strength: float = 0.0
    support_distance_usd: Optional[float] = None
    resistance_distance_usd: Optional[float] = None
    support_distance_bps: Optional[float] = None
    resistance_distance_bps: Optional[float] = None
    bounce_score: float = 0.0
    rejection_score: float = 0.0
    breakout_state: str = "NONE"
    retest_state: str = "NONE"
    nested_structure: str = "NONE"
    bias_side: str = "NEUTRAL"
    bias_score: float = 0.0
    p_up_bias: float = 0.50
    p_down_bias: float = 0.50
    reason: str = "not_ready"
    zones_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class SupportResistanceEngine:
    """Maintain BTC support/resistance state from Binance candles.

    The implementation is intentionally rule-based and transparent: confirmed
    local extrema create candidate levels, nearby levels are clustered into
    zones, and the current price is scored against the nearest zones.
    """

    def __init__(self, config: dict):
        self.config = config
        self.state = SupportResistanceState(enabled=bool(_get(config, "support_resistance.enabled", False)))
        self.last_update_ts = 0.0

    def update(self, state: BinanceState, now_ts: Optional[float] = None, force: bool = False) -> SupportResistanceState:
        """Refresh S/R state from the latest Binance OHLC bars.

        Args:
            state: Latest Binance snapshot with OHLC bars.
            now_ts: Current strategy timestamp.
            force: Recalculate even if refresh interval has not elapsed.
        """
        now = float(now_ts or time.time())
        sr_config = self.config.get("support_resistance", {})
        if not bool(sr_config.get("enabled", False)):
            self.state = SupportResistanceState(enabled=False, updated_at=now, reason="disabled")
            return self.state
        refresh = max(1.0, float(sr_config.get("refresh_seconds", 5)))
        if not force and self.state.enabled and now - self.last_update_ts < refresh:
            return self.state

        candles = aggregate_candles(
            state.ohlc_bars or _close_bars_to_ohlc(state.bars),
            int(sr_config.get("candle_seconds", 15)),
        )
        min_candles = int(sr_config.get("min_candles", 20))
        if state.price is None or len(candles) < min_candles:
            self.state = SupportResistanceState(
                enabled=True,
                updated_at=now,
                candle_seconds=int(sr_config.get("candle_seconds", 15)),
                reason="insufficient_candles",
            )
            self.last_update_ts = now
            return self.state

        self.state = calculate_support_resistance(self.config, candles, state.price, now)
        self.last_update_ts = now
        return self.state


def calculate_support_resistance(
    config: dict,
    candles: list[SRCandle],
    current_price: float,
    now_ts: Optional[float] = None,
) -> SupportResistanceState:
    """Calculate nearest S/R zones and a small UP/DOWN side bias.

    Args:
        config: Loaded `config.yaml` settings.
        candles: Aggregated OHLC candles.
        current_price: Latest BTC price.
        now_ts: Current strategy timestamp.
    """
    sr_config = config.get("support_resistance", {})
    candle_seconds = int(sr_config.get("candle_seconds", 15))
    lookbacks = [int(value) for value in sr_config.get("lookback_minutes", [30, 60])]
    if bool(sr_config.get("include_3h", False)) and 180 not in lookbacks:
        lookbacks.append(180)
    lookbacks = sorted(set(value for value in lookbacks if value > 0))
    zone_width = _zone_width(sr_config, current_price)
    pivots: list[dict] = []
    for minutes in lookbacks:
        needed = max(1, int((minutes * 60) / max(1, candle_seconds)))
        window_candles = candles[-needed:]
        pivots.extend(_swing_points(window_candles, config, minutes))

    zones = _cluster_zones(pivots, zone_width)
    all_support_zones = [zone for zone in zones if zone.side == "SUPPORT"]
    all_resistance_zones = [zone for zone in zones if zone.side == "RESISTANCE"]
    support_zones = sorted(
        [zone for zone in all_support_zones if zone.price <= current_price],
        key=lambda zone: current_price - zone.price,
    )
    resistance_zones = sorted(
        [zone for zone in all_resistance_zones if zone.price >= current_price],
        key=lambda zone: zone.price - current_price,
    )
    broken_support_zones = sorted(
        [zone for zone in all_support_zones if zone.price > current_price],
        key=lambda zone: zone.price - current_price,
    )
    broken_resistance_zones = sorted(
        [zone for zone in all_resistance_zones if zone.price < current_price],
        key=lambda zone: current_price - zone.price,
    )
    support = support_zones[0] if support_zones else None
    resistance = resistance_zones[0] if resistance_zones else None
    broken_support = broken_support_zones[0] if broken_support_zones else None
    broken_resistance = broken_resistance_zones[0] if broken_resistance_zones else None
    recent = candles[-max(2, int(sr_config.get("reaction_candles", 4))) :]
    bounce = _bounce_score(recent, support, zone_width)
    rejection = _rejection_score(recent, resistance, zone_width)
    # Breakout/retest checks use the nearest broken zone if price has already
    # crossed it, while the dashboard still shows nearest live support/resistance.
    breakout = _breakout_state(candles, support or broken_support, resistance or broken_resistance, zone_width, sr_config)
    retest = _retest_state(candles, support or broken_support, resistance or broken_resistance, zone_width, sr_config)
    nested = _nested_structure(support, resistance)
    bias_score, reason = _bias_score(
        current_price,
        support,
        resistance,
        bounce,
        rejection,
        breakout,
        retest,
        sr_config,
    )
    bias_side = "UP" if bias_score > 0.05 else "DOWN" if bias_score < -0.05 else "NEUTRAL"
    p_up_bias = max(0.0, min(1.0, 0.50 + (bias_score * 0.50)))
    support_distance = current_price - support.price if support else None
    resistance_distance = resistance.price - current_price if resistance else None

    return SupportResistanceState(
        enabled=True,
        updated_at=float(now_ts or time.time()),
        candle_seconds=candle_seconds,
        nearest_support=support.price if support else None,
        nearest_resistance=resistance.price if resistance else None,
        support_strength=support.strength if support else 0.0,
        resistance_strength=resistance.strength if resistance else 0.0,
        support_distance_usd=support_distance,
        resistance_distance_usd=resistance_distance,
        support_distance_bps=(support_distance / current_price * 10_000) if support_distance is not None else None,
        resistance_distance_bps=(resistance_distance / current_price * 10_000) if resistance_distance is not None else None,
        bounce_score=bounce,
        rejection_score=rejection,
        breakout_state=breakout,
        retest_state=retest,
        nested_structure=nested,
        bias_side=bias_side,
        bias_score=bias_score,
        p_up_bias=p_up_bias,
        p_down_bias=1.0 - p_up_bias,
        reason=reason,
        zones_count=len(zones),
    )


def apply_support_resistance_bias(
    config: dict,
    probability: ProbabilityState,
    sr_state: SupportResistanceState,
) -> ProbabilityState:
    """Apply a capped S/R side-information adjustment to existing probability."""
    if probability.p_up is None or probability.p_down is None:
        return probability
    sr_config = config.get("support_resistance", {})
    if not bool(sr_config.get("enabled", False)) or not bool(sr_config.get("apply_to_probability", True)):
        return probability
    weight = float(sr_config.get("probability_weight", 0.08))
    max_adjustment = abs(float(sr_config.get("max_probability_adjustment", 0.06)))
    delta = max(-max_adjustment, min(max_adjustment, sr_state.bias_score * weight))
    p_up = max(float(config["probability"]["clamp_min"]), min(float(config["probability"]["clamp_max"]), probability.p_up + delta))
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
        reason=f"{probability.reason}+sr_{sr_state.bias_side.lower()}",
        raw_score=probability.raw_score,
        distance_score=probability.distance_score,
        ma_score=probability.ma_score,
        slope_score=probability.slope_score,
        volume_score=probability.volume_score,
        imbalance_score=probability.imbalance_score,
        time_factor=probability.time_factor,
    )


def aggregate_candles(raw: list[tuple[int, float, float, float, float]], candle_seconds: int) -> list[SRCandle]:
    """Aggregate raw OHLC rows into the configured S/R candle timeframe."""
    if not raw:
        return []
    grouped: list[SRCandle] = []
    bucket = None
    current: Optional[SRCandle] = None
    for ts, open_price, high, low, close in sorted(raw, key=lambda row: row[0]):
        next_bucket = int(ts) - (int(ts) % max(1, candle_seconds))
        if bucket != next_bucket:
            if current is not None:
                grouped.append(current)
            bucket = next_bucket
            current = SRCandle(next_bucket, float(open_price), float(high), float(low), float(close))
        elif current is not None:
            current.high = max(current.high, float(high))
            current.low = min(current.low, float(low))
            current.close = float(close)
    if current is not None:
        grouped.append(current)
    return grouped


def _swing_points(candles: list[SRCandle], config: dict, lookback_minutes: int) -> list[dict]:
    left = int(_get(config, "support_resistance.swing_left", 2))
    right = int(_get(config, "support_resistance.swing_right", 2))
    if len(candles) < left + right + 1:
        return []
    pivots: list[dict] = []
    for index in range(left, len(candles) - right):
        candle = candles[index]
        neighborhood = candles[index - left : index + right + 1]
        if candle.high >= max(item.high for item in neighborhood):
            pivots.append({"side": "RESISTANCE", "price": candle.high, "ts": candle.ts, "lookback": lookback_minutes})
        if candle.low <= min(item.low for item in neighborhood):
            pivots.append({"side": "SUPPORT", "price": candle.low, "ts": candle.ts, "lookback": lookback_minutes})
    return pivots


def _cluster_zones(pivots: list[dict], zone_width: float) -> list[SRZone]:
    zones: list[SRZone] = []
    for pivot in sorted(pivots, key=lambda item: (item["side"], item["price"])):
        match = None
        for zone in zones:
            if zone.side == pivot["side"] and abs(zone.price - pivot["price"]) <= zone_width:
                match = zone
                break
        if match is None:
            zones.append(
                SRZone(
                    side=pivot["side"],
                    price=float(pivot["price"]),
                    lower=float(pivot["price"]) - zone_width,
                    upper=float(pivot["price"]) + zone_width,
                    strength=1.0,
                    touches=1,
                    windows=[int(pivot["lookback"])],
                    last_ts=int(pivot["ts"]),
                )
            )
            continue
        total = match.touches + 1
        match.price = ((match.price * match.touches) + float(pivot["price"])) / total
        match.lower = match.price - zone_width
        match.upper = match.price + zone_width
        match.touches = total
        if int(pivot["lookback"]) not in match.windows:
            match.windows.append(int(pivot["lookback"]))
        match.last_ts = max(match.last_ts, int(pivot["ts"]))
        match.strength = min(10.0, match.touches + (len(match.windows) * 1.5))
    return zones


def _bounce_score(candles: list[SRCandle], support: Optional[SRZone], zone_width: float) -> float:
    if support is None or not candles:
        return 0.0
    score = 0.0
    for candle in candles:
        touched = candle.low <= support.upper and candle.close > support.price
        rejected_lower = candle.close > candle.open and candle.close > support.upper
        if touched and rejected_lower:
            score += 1.0
        elif touched:
            score += 0.5
    return min(1.0, (score / max(1, len(candles))) * (1.0 + support.strength / 10.0))


def _rejection_score(candles: list[SRCandle], resistance: Optional[SRZone], zone_width: float) -> float:
    if resistance is None or not candles:
        return 0.0
    score = 0.0
    for candle in candles:
        touched = candle.high >= resistance.lower and candle.close < resistance.price
        rejected_upper = candle.close < candle.open and candle.close < resistance.lower
        if touched and rejected_upper:
            score += 1.0
        elif touched:
            score += 0.5
    return min(1.0, (score / max(1, len(candles))) * (1.0 + resistance.strength / 10.0))


def _breakout_state(candles: list[SRCandle], support: Optional[SRZone], resistance: Optional[SRZone], zone_width: float, config: dict) -> str:
    if not candles:
        return "NONE"
    close = candles[-1].close
    buffer = zone_width * float(config.get("breakout_buffer_zones", 1.0))
    if resistance and close > resistance.price + buffer:
        return "BREAKOUT_UP"
    if support and close < support.price - buffer:
        return "BREAKOUT_DOWN"
    return "NONE"


def _retest_state(candles: list[SRCandle], support: Optional[SRZone], resistance: Optional[SRZone], zone_width: float, config: dict) -> str:
    if len(candles) < 3:
        return "NONE"
    lookback = max(3, int(config.get("retest_candles", 6)))
    recent = candles[-lookback:]
    if resistance:
        broke_up = any(candle.close > resistance.price + zone_width for candle in recent[:-1])
        retested = recent[-1].low <= resistance.upper and recent[-1].close > resistance.price
        if broke_up and retested:
            return "RETEST_SUPPORT_AFTER_BREAKOUT"
    if support:
        broke_down = any(candle.close < support.price - zone_width for candle in recent[:-1])
        retested = recent[-1].high >= support.lower and recent[-1].close < support.price
        if broke_down and retested:
            return "RETEST_RESISTANCE_AFTER_BREAKDOWN"
    return "NONE"


def _nested_structure(support: Optional[SRZone], resistance: Optional[SRZone]) -> str:
    support_nested = support is not None and len(support.windows) > 1
    resistance_nested = resistance is not None and len(resistance.windows) > 1
    if support_nested and resistance_nested:
        return "BOTH_NESTED"
    if support_nested:
        return "SUPPORT_NESTED"
    if resistance_nested:
        return "RESISTANCE_NESTED"
    return "NONE"


def _bias_score(
    current_price: float,
    support: Optional[SRZone],
    resistance: Optional[SRZone],
    bounce: float,
    rejection: float,
    breakout: str,
    retest: str,
    config: dict,
) -> tuple[float, str]:
    zone_width = _zone_width({"zone_width_usd": config.get("zone_width_usd", 25.0), "zone_width_bps": config.get("zone_width_bps", 4.0)}, current_price)
    score = 0.0
    reasons: list[str] = []
    if support:
        support_distance = max(0.0, current_price - support.price)
        proximity = max(0.0, 1.0 - support_distance / max(zone_width * 4.0, 1.0))
        score += proximity * min(0.35, support.strength / 30.0)
        if proximity > 0:
            reasons.append("near_support")
    if resistance:
        resistance_distance = max(0.0, resistance.price - current_price)
        proximity = max(0.0, 1.0 - resistance_distance / max(zone_width * 4.0, 1.0))
        score -= proximity * min(0.35, resistance.strength / 30.0)
        if proximity > 0:
            reasons.append("near_resistance")
    if bounce > 0:
        score += bounce * 0.35
        reasons.append("support_bounce")
    if rejection > 0:
        score -= rejection * 0.35
        reasons.append("resistance_rejection")
    if breakout == "BREAKOUT_UP":
        score += 0.45
        reasons.append("breakout_up")
    elif breakout == "BREAKOUT_DOWN":
        score -= 0.45
        reasons.append("breakout_down")
    if retest == "RETEST_SUPPORT_AFTER_BREAKOUT":
        score += 0.35
        reasons.append("bullish_retest")
    elif retest == "RETEST_RESISTANCE_AFTER_BREAKDOWN":
        score -= 0.35
        reasons.append("bearish_retest")
    score = max(-1.0, min(1.0, score))
    return score, "+".join(reasons) if reasons else "between_zones"


def _zone_width(config: dict, price: float) -> float:
    return max(
        float(config.get("zone_width_usd", 25.0)),
        price * (float(config.get("zone_width_bps", 4.0)) / 10_000.0),
    )


def _close_bars_to_ohlc(bars: list[tuple[int, float]]) -> list[tuple[int, float, float, float, float]]:
    return [(int(ts), float(price), float(price), float(price), float(price)) for ts, price in bars]


def _get(config: dict, path: str, default):
    if hasattr(config, "get_path"):
        return config.get_path(path, default)
    current = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current
