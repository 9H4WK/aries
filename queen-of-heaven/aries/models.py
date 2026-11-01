from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def to_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, dict):
        return obj
    raise TypeError(f"Cannot convert {type(obj)} to dict")


@dataclass
class MarketInfo:
    slug: str
    market_id: str
    up_token_id: str
    down_token_id: str
    window_start_ts: int
    window_end_ts: int
    question: str = ""
    target_price: Optional[float] = None
    target_source: str = "unknown"


@dataclass
class Quote:
    token_id: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    ts: Optional[float] = None
    lag: Optional[float] = None  # How late the latest book update arrived vs the fastest one this window (s).

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass
class BinanceState:
    price: Optional[float]
    best_bid: Optional[float]
    best_ask: Optional[float]
    best_bid_qty: Optional[float]
    best_ask_qty: Optional[float]
    ts: Optional[float]
    bars: list[tuple[int, float]] = field(default_factory=list)
    volume_bars: list[tuple[int, float]] = field(default_factory=list)
    ohlc_bars: list[tuple[int, float, float, float, float]] = field(default_factory=list)
    ofi_events: list[tuple[float, float]] = field(default_factory=list)
    cvd_events: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class IndicatorState:
    ma_fast: Optional[float]
    ma_mid: Optional[float]
    ma_slow: Optional[float]
    ma_anchor: Optional[float]
    pressure: str
    ma_fast_slope_bps: Optional[float]
    ma_mid_slope_bps: Optional[float]
    ma_anchor_slope_bps: Optional[float]
    up_stability_score: int
    down_stability_score: int
    velocity_5s_bps: Optional[float]
    velocity_15s_bps: Optional[float]
    volatility_60s: Optional[float]
    persistence_above_target_seconds: int
    persistence_below_target_seconds: int
    current_volume: Optional[float]
    volume_ma_5: Optional[float]
    volume_ma_10: Optional[float]
    volume_spike: Optional[float]
    orderbook_imbalance: Optional[float]


@dataclass
class ProbabilityState:
    p_up: Optional[float]
    p_down: Optional[float]
    confidence: float
    distance_from_target: Optional[float]
    distance_bps: Optional[float]
    inside_noise_band: bool
    reason: str
    raw_score: Optional[float] = None
    distance_score: Optional[float] = None
    ma_score: Optional[float] = None
    slope_score: Optional[float] = None
    volume_score: Optional[float] = None
    imbalance_score: Optional[float] = None
    time_factor: Optional[float] = None


@dataclass
class PositionShape:
    up_shares: float = 0.0
    down_shares: float = 0.0
    total_cost: float = 0.0
    pnl_if_up: float = 0.0
    pnl_if_down: float = 0.0
    worst_case_pnl: float = 0.0
    locked: bool = False


@dataclass
class Fill:
    ts: float
    market_id: str
    window_start_ts: int
    side: str  # UP or DOWN
    price: float
    notional_usdc: float
    shares: float
    reason: str
    action: str = "BUY"


@dataclass
class Candidate:
    side: str
    valid: bool
    reason: str
    action: str = "BUY"
    role: Optional[str] = None
    price: Optional[float] = None
    edge: Optional[float] = None
    notional_usdc: Optional[float] = None
    shares: Optional[float] = None
    ladder_step: Optional[int] = None
    shape_after: Optional[PositionShape] = None
    shape_improvement: float = 0.0
    would_lock: bool = False
    min_shares_check_passed: bool = False
    net_price: Optional[float] = None
    net_edge: Optional[float] = None
    full_kelly_fraction: float = 0.0
    applied_kelly_fraction: float = 0.0
    kelly_cap_usdc: float = 0.0
    repair_target_usdc: Optional[float] = None
    allowed_notional_usdc: Optional[float] = None
    account_scale: float = 1.0
    ladder_bankroll_ratio: Optional[float] = None
    time_kelly_multiplier: float = 1.0
    expected_log_growth_delta: Optional[float] = None
    growth_score: Optional[float] = None
    inventory_imbalance: Optional[float] = None
    stability_score: Optional[int] = None
    opposite_stability_score: Optional[int] = None
    target_tilt_shares: Optional[float] = None
    held_shares_before: Optional[float] = None
    sell_fraction: Optional[float] = None
    model_probability: Optional[float] = None
    opposite_probability: Optional[float] = None
    model_exit_edge: Optional[float] = None
    worst_case_deterioration: Optional[float] = None


@dataclass
class Decision:
    decision_id: str
    timestamp: float
    mode: str
    strategy_state: str
    window_start: Optional[int]
    window_end: Optional[int]
    market_id: Optional[str]
    market_slug: Optional[str]
    seconds_since_open: Optional[int]
    seconds_remaining: Optional[int]
    seconds_since_first_fill: Optional[float]
    average_entry_up: Optional[float]
    average_entry_down: Optional[float]
    account_starting_balance_usdc: float
    account_realized_pnl_usdc: float
    account_balance_usdc: float
    account_win_rate_percent: float
    stop_when_locked: bool
    allow_post_lock_adds: bool
    repair_start_after_first_entry_seconds: int
    preferred_repair_after_first_entry_seconds: int
    target_price: Optional[float]
    btc_price: Optional[float]
    ma_fast: Optional[float]
    ma_mid: Optional[float]
    ma_slow: Optional[float]
    ma_anchor: Optional[float]
    ma_pressure: Optional[str]
    ma_fast_slope_bps: Optional[float]
    ma_mid_slope_bps: Optional[float]
    ma_anchor_slope_bps: Optional[float]
    up_stability_score: Optional[int]
    down_stability_score: Optional[int]
    ma_velocity_5s_bps: Optional[float]
    ma_velocity_15s_bps: Optional[float]
    current_volume: Optional[float]
    volume_ma_5: Optional[float]
    volume_ma_10: Optional[float]
    volume_spike: Optional[float]
    orderbook_imbalance: Optional[float]
    persistence_above_target_seconds: Optional[int]
    persistence_below_target_seconds: Optional[int]
    p_up: Optional[float]
    p_down: Optional[float]
    confidence: Optional[float]
    distance_from_target: Optional[float]
    distance_bps: Optional[float]
    raw_score: Optional[float]
    distance_score: Optional[float]
    ma_score: Optional[float]
    slope_score: Optional[float]
    volume_score: Optional[float]
    imbalance_score: Optional[float]
    time_factor: Optional[float]
    spot_target_ratio: Optional[float]
    time_remaining_ratio: Optional[float]
    growth_sizing_enabled: bool
    high_conviction_same_side_enabled: bool
    high_conviction_min_probability: float
    high_conviction_max_window_notional_usdc: float
    up_bid: Optional[float]
    up_ask: Optional[float]
    down_bid: Optional[float]
    down_ask: Optional[float]
    up_spread: Optional[float]
    down_spread: Optional[float]
    up_quote_age_seconds: Optional[float]
    down_quote_age_seconds: Optional[float]
    edge_up: Optional[float]
    edge_down: Optional[float]
    position_before: dict[str, Any]
    candidate_up: dict[str, Any]
    candidate_down: dict[str, Any]
    sell_candidate: dict[str, Any]
    selected_side: Optional[str]
    decision: str
    selected_reason: str
    position_after: dict[str, Any]
    order_id: Optional[int] = None
    fill_id: Optional[int] = None
    order_status: Optional[str] = None
    order_reason: Optional[str] = None
    up_book_lag_seconds: Optional[float] = None
    down_book_lag_seconds: Optional[float] = None
    sell_probe: dict[str, Any] = field(default_factory=dict)
    support_resistance: dict[str, Any] = field(default_factory=dict)
    polymarket_flow: dict[str, Any] = field(default_factory=dict)
    binance_flow: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaperOrder:
    timestamp: float
    window_start: int
    window_end: int
    market_id: str
    token_id: str
    side: str
    action: str
    price: float
    notional_usdc: float
    shares: float
    status: str
    reason: str
    decision_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None


@dataclass
class PaperFill:
    order_id: int
    created_at: float
    window_start: int
    window_end: int
    market_id: str
    token_id: str
    side: str
    action: str
    price: float
    notional_usdc: float
    shares: float
    metadata: dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None


@dataclass
class WindowResult:
    window_start: int
    window_end: int
    market_id: str
    target_price: float
    final_btc_price: float
    winning_side: str
    resolved: bool
    resolved_at: float
    id: Optional[int] = None


@dataclass
class WindowPnl:
    window_start: int
    market_id: str
    up_shares: float
    down_shares: float
    total_cost: float
    pnl_if_up: float
    pnl_if_down: float
    realized_pnl: float
    winning_side: str
    locked: bool
    notes: str = ""
    id: Optional[int] = None
