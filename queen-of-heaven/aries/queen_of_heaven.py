from __future__ import annotations

from typing import Any, Optional


ENTRY_REASON = "queen_of_heaven_entry"
ENTRY_ROLE = "QUEEN_OF_HEAVEN_ENTRY"


def _get(config: dict, path: str, default: Any = None) -> Any:
    """Read a dotted config value from either Config or a plain dict."""
    if hasattr(config, "get_path"):
        return config.get_path(path, default)
    value: Any = config
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def enabled_for_mode(config: dict, mode: str) -> bool:
    """Return true when the profile is enabled for the current bot mode."""
    if not bool(_get(config, "queen_of_heaven.enabled", False)):
        return False
    modes = {str(value) for value in _get(config, "queen_of_heaven.modes", ["paper", "live"])}
    return mode in modes


def max_resolution_return(price: Optional[float]) -> Optional[float]:
    """Return max possible stake return if a binary share bought at `price` settles at 1."""
    if price is None or price <= 0:
        return None
    return (1.0 - float(price)) / float(price)


def stake_return(entry_price: Optional[float], exit_price: Optional[float]) -> Optional[float]:
    """Return realized stake return from buying at entry and selling at exit."""
    if entry_price is None or exit_price is None or entry_price <= 0:
        return None
    return (float(exit_price) - float(entry_price)) / float(entry_price)


def elapsed_fraction(seconds_since_open: Optional[int], seconds_remaining: Optional[int]) -> Optional[float]:
    """Return how much of the current window has elapsed, from 0.0 to 1.0."""
    if seconds_since_open is None or seconds_remaining is None:
        return None
    total = int(seconds_since_open) + int(seconds_remaining)
    if total <= 0:
        return None
    return max(0.0, min(1.0, float(seconds_since_open) / float(total)))


def timing_reject_reason(
    config: dict,
    *,
    seconds_since_open: Optional[int],
    seconds_remaining: Optional[int],
) -> Optional[str]:
    """Apply absolute and fractional timing gates for the high-conviction profile."""
    if seconds_since_open is None or seconds_remaining is None:
        return "queen_timing_missing"
    if seconds_since_open < int(_get(config, "queen_of_heaven.min_seconds_since_open", 20)):
        return "queen_too_early"
    if seconds_since_open > int(_get(config, "queen_of_heaven.max_seconds_since_open", 210)):
        return "queen_after_time_ceiling"
    if seconds_remaining < int(_get(config, "queen_of_heaven.min_seconds_remaining", 35)):
        return "queen_too_late"
    if not bool(_get(config, "queen_of_heaven.time_preference_enabled", False)):
        return None

    elapsed = elapsed_fraction(seconds_since_open, seconds_remaining)
    if elapsed is None:
        return "queen_timing_missing"
    if elapsed < float(_get(config, "queen_of_heaven.min_elapsed_fraction", 0.50)):
        return "queen_before_half_window"
    max_elapsed = float(_get(config, "queen_of_heaven.max_elapsed_fraction", 1.0))
    if max_elapsed < 1.0 and elapsed > max_elapsed:
        return "queen_after_time_preference_ceiling"
    return None


def time_kelly_multiplier(
    config: dict,
    *,
    role: str,
    seconds_since_open: Optional[int],
    seconds_remaining: Optional[int],
) -> float:
    """Return a Queen-only Kelly multiplier that rises as expiry gets closer."""
    if role != "FIRST_ENTRY" or not bool(_get(config, "queen_of_heaven.enabled", False)):
        return 1.0
    if not bool(_get(config, "queen_of_heaven.time_kelly_enabled", False)):
        return 1.0

    elapsed = elapsed_fraction(seconds_since_open, seconds_remaining)
    if elapsed is None:
        return 1.0
    half = float(_get(config, "queen_of_heaven.time_kelly_half_elapsed_fraction", 0.50))
    third = float(_get(config, "queen_of_heaven.time_kelly_third_quarter_elapsed_fraction", 0.75))
    late = float(_get(config, "queen_of_heaven.time_kelly_late_elapsed_fraction", 0.88))
    half_mult = float(_get(config, "queen_of_heaven.time_kelly_half_multiplier", 0.70))
    third_mult = float(_get(config, "queen_of_heaven.time_kelly_third_quarter_multiplier", 1.00))
    late_mult = float(_get(config, "queen_of_heaven.time_kelly_late_multiplier", 1.35))

    if elapsed <= half:
        return half_mult
    if elapsed <= third:
        return _lerp(half_mult, third_mult, (elapsed - half) / max(1e-9, third - half))
    if elapsed <= late:
        return _lerp(third_mult, late_mult, (elapsed - third) / max(1e-9, late - third))
    return late_mult


def candidate_value_reject_reason(
    config: dict,
    *,
    price: Optional[float],
    notional_usdc: Optional[float],
    model_probability: Optional[float],
    edge: Optional[float],
    net_edge: Optional[float],
) -> Optional[str]:
    """Check price, edge, probability, stake, and max-payout gates for the profile."""
    if price is None:
        return "queen_price_missing"
    min_ask = float(_get(config, "queen_of_heaven.min_pm_ask", 0.85))
    max_ask = float(_get(config, "queen_of_heaven.max_pm_ask", 0.90))
    if price < min_ask:
        return "queen_ask_below_min"
    if price > max_ask:
        return "queen_ask_above_max"

    if model_probability is None or model_probability < float(
        _get(config, "queen_of_heaven.min_model_probability", 0.92)
    ):
        return "queen_probability_too_low"
    if edge is None or edge < float(_get(config, "queen_of_heaven.min_edge", 0.03)):
        return "queen_edge_too_low"
    if net_edge is None or net_edge < float(_get(config, "queen_of_heaven.min_net_edge", 0.02)):
        return "queen_net_edge_too_low"
    min_edge_after_fee = _get(config, "queen_of_heaven.min_edge_after_fee", None)
    if min_edge_after_fee is not None:
        fee_per_share = float(_get(config, "polymarket.taker_fee_rate", 0.0)) * price * (1.0 - price)
        if model_probability - price - fee_per_share < float(min_edge_after_fee):
            return "queen_edge_after_fee_too_low"

    max_return = max_resolution_return(price)
    if max_return is None or max_return < float(_get(config, "queen_of_heaven.min_win_profit_fraction", 0.10)):
        return "queen_max_profit_below_target"
    max_win_profit = float(_get(config, "queen_of_heaven.max_win_profit_fraction", 0.0))
    if max_win_profit > 0 and max_return > max_win_profit:
        return "queen_max_profit_above_profile"

    if notional_usdc is None:
        return "queen_notional_missing"
    if notional_usdc < float(_get(config, "queen_of_heaven.min_stake_usdc", 1.0)):
        return "queen_stake_below_min"
    if notional_usdc > float(_get(config, "queen_of_heaven.max_stake_usdc", 5.0)):
        return "queen_stake_above_max"
    return None


def setup_holds(config: dict, *, price: Optional[float], model_probability: Optional[float]) -> bool:
    """True when the ask band, probability floor and edge-after-fee gates pass, at any time in the window.

    This is the backtest's entry condition, used to see how long a setup has stood (no stake or timing gates).
    """
    if price is None or model_probability is None:
        return False
    if not float(_get(config, "queen_of_heaven.min_pm_ask", 0.85)) <= price <= float(_get(config, "queen_of_heaven.max_pm_ask", 0.90)):
        return False
    if model_probability < float(_get(config, "queen_of_heaven.min_model_probability", 0.92)):
        return False
    fee_per_share = float(_get(config, "polymarket.taker_fee_rate", 0.0)) * price * (1.0 - price)
    return model_probability - price - fee_per_share >= float(_get(config, "queen_of_heaven.min_edge_after_fee", 0.0) or 0.0)


def setup_started_before_window(
    setup_seconds: dict[str, set[int]],
    side: str,
    second: int,
    first_second: int,
    gap_seconds: int,
) -> bool:
    """True when the setup on `side` at `second` belongs to a run that already stood before `first_second`.

    The run walks back through seconds without the setup while the previous same-side setup second is at most
    `gap_seconds` earlier, and stops at a second with the setup on the other side. Such entries made about
    nothing on Aug 14-Oct 18 (docs/queen_of_heaven_v2.md, setups standing before the entry window).
    """
    own = setup_seconds.get(side, set())
    other = set().union(*(seconds for key, seconds in setup_seconds.items() if key != side))
    last, s = second, second - 1
    while s >= 0 and last >= first_second:
        if s in own:
            last = s
        elif s in other or last - s > gap_seconds:
            break
        s -= 1
    return last < first_second


def buy_limit_price(
    config: dict,
    *,
    price: Optional[float],
    model_probability: Optional[float],
    tolerance: float,
) -> Optional[float]:
    """BUY limit: the seen ask plus `tolerance`, raised to the highest cent at which the model
    still beats the price by `queen_of_heaven.limit_edge_floor` after the taker fee (when set).

    At most 0.99. Orders a flat limit misses are mostly ones where the ask jumped because the
    market agreed with the model (docs/queen_of_heaven_v2.md, limit price).
    """
    if price is None:
        return None
    limit = price + tolerance
    floor = _get(config, "queen_of_heaven.limit_edge_floor", None)
    if floor is not None and model_probability is not None:
        fee_rate = float(_get(config, "polymarket.taker_fee_rate", 0.0))
        for cents in range(99, 0, -1):
            q = cents / 100.0
            if model_probability - q - fee_rate * q * (1.0 - q) >= float(floor) - 1e-9:
                limit = max(limit, q)
                break
    return min(limit, 0.99)


def order_reject_reason(
    config: dict,
    *,
    mode: str,
    action: str,
    price: Optional[float],
    notional_usdc: float,
    reason: str,
    metadata: dict[str, Any],
) -> Optional[str]:
    """Repeat the profile's hard BUY gates inside paper/live execution."""
    if reason != ENTRY_REASON:
        return None
    if not enabled_for_mode(config, mode):
        return "queen_profile_disabled"
    if action != "BUY":
        return "queen_only_buy_orders"

    candidate = metadata.get("candidate") if isinstance(metadata.get("candidate"), dict) else metadata
    value_reason = candidate_value_reject_reason(
        config,
        price=price,
        notional_usdc=notional_usdc,
        model_probability=_as_float(candidate.get("model_probability")),
        edge=_as_float(candidate.get("edge")),
        net_edge=_as_float(candidate.get("net_edge")),
    )
    if value_reason:
        return value_reason
    if bool(_get(config, "queen_of_heaven.time_preference_enabled", False)):
        return timing_reject_reason(
            config,
            seconds_since_open=_as_int(metadata.get("seconds_since_open") or candidate.get("seconds_since_open")),
            seconds_remaining=_as_int(metadata.get("seconds_remaining") or candidate.get("seconds_remaining")),
        )
    return None


def strategy_reject_reason(
    config: dict,
    *,
    mode: str,
    candidate,
    prob,
    indicators,
    binance_state,
    market,
    seconds_since_open: int,
    seconds_remaining: int,
) -> Optional[str]:
    """Return the first strategy gate blocking a Queen first entry, or None."""
    if not enabled_for_mode(config, mode):
        return "queen_of_heaven_disabled"
    if not getattr(candidate, "valid", False):
        return getattr(candidate, "reason", "queen_candidate_invalid")
    if bool(_get(config, "queen_of_heaven.first_entry_only", True)) and getattr(candidate, "role", None) != "FIRST_ENTRY":
        return "queen_first_entry_only"

    side = str(getattr(candidate, "side", ""))
    side_probability = getattr(prob, "p_up", None) if side == "UP" else getattr(prob, "p_down", None)
    value_reason = candidate_value_reject_reason(
        config,
        price=getattr(candidate, "price", None),
        notional_usdc=getattr(candidate, "notional_usdc", None),
        model_probability=side_probability,
        edge=getattr(candidate, "edge", None),
        net_edge=getattr(candidate, "net_edge", None),
    )
    if value_reason:
        return value_reason
    if getattr(prob, "confidence", 0.0) < float(_get(config, "queen_of_heaven.min_confidence", 0.70)):
        return "queen_confidence_too_low"
    timing_reason = timing_reject_reason(
        config,
        seconds_since_open=seconds_since_open,
        seconds_remaining=seconds_remaining,
    )
    if timing_reason:
        return timing_reason

    if market is None or indicators is None or binance_state is None:
        return "queen_state_missing"
    # The calibrated TWAP model already prices target distance, volatility, and
    # time left; the hand-tuned Binance gates below were not part of its backtest.
    if not bool(_get(config, "queen_of_heaven.apply_heuristic_gates", True)):
        return None
    sign = 1 if side == "UP" else -1
    target = getattr(market, "target_price", None)
    spot = getattr(binance_state, "price", None)
    if target is None or spot is None:
        return "queen_target_or_spot_missing"

    if bool(_get(config, "queen_of_heaven.require_price_on_target_side", True)):
        if sign * (float(spot) - float(target)) <= 0:
            return "queen_price_not_on_target_side"
    distance_bps = getattr(prob, "distance_bps", None)
    if distance_bps is None or sign * float(distance_bps) < float(_get(config, "queen_of_heaven.min_distance_bps", 4.0)):
        return "queen_distance_too_small"
    if bool(_get(config, "queen_of_heaven.require_ma_alignment", True)) and getattr(indicators, "pressure", None) != side:
        return "queen_ma_not_aligned"
    if sign * float(getattr(indicators, "ma_fast_slope_bps", 0.0) or 0.0) < float(
        _get(config, "queen_of_heaven.min_fast_slope_bps", 0.0)
    ):
        return "queen_fast_slope_not_aligned"
    if sign * float(getattr(indicators, "ma_mid_slope_bps", 0.0) or 0.0) < float(
        _get(config, "queen_of_heaven.min_mid_slope_bps", -0.05)
    ):
        return "queen_mid_slope_not_aligned"

    persistence = (
        getattr(indicators, "persistence_above_target_seconds", 0)
        if side == "UP"
        else getattr(indicators, "persistence_below_target_seconds", 0)
    )
    if persistence < int(_get(config, "queen_of_heaven.min_persistence_seconds", 6)):
        return "queen_persistence_too_low"
    if bool(_get(config, "queen_of_heaven.require_orderbook_alignment", False)):
        if sign * float(getattr(indicators, "orderbook_imbalance", 0.0) or 0.0) < float(
            _get(config, "queen_of_heaven.min_orderbook_imbalance", 0.05)
        ):
            return "queen_orderbook_not_aligned"
    return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lerp(start: float, end: float, fraction: float) -> float:
    """Linearly interpolate between two timing multipliers."""
    bounded = max(0.0, min(1.0, fraction))
    return start + ((end - start) * bounded)
