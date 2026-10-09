from __future__ import annotations

import time
from typing import Optional

from .models import Quote, BinanceState
from .growth_sizing import account_scale


def quote_age_seconds(q: Quote) -> Optional[float]:
    if q.ts is None:
        return None
    return time.time() - q.ts


def quote_fresh(q: Quote, max_age_seconds: float) -> bool:
    age = quote_age_seconds(q)
    return age is not None and age <= max_age_seconds and q.ask is not None and q.bid is not None


def binance_fresh(state: BinanceState, max_age_seconds: float) -> bool:
    if state.ts is None:
        return False
    return time.time() - state.ts <= max_age_seconds


def valid_spread(spread: Optional[float], max_spread: float) -> bool:
    return spread is not None and spread >= 0 and spread <= max_spread


def scaled_risk_cap(config: dict, base_cap: float, current_balance: float) -> float:
    """Scale configured paper-risk caps with settled account growth when enabled."""
    if not bool(config.get_path("growth_sizing.enabled", False)):
        return base_cap
    starting_balance = float(config.get_path("account.balance_usdc", current_balance))
    return base_cap * account_scale(config, current_balance, starting_balance)


def window_risk_caps(
    config: dict,
    current_balance: float,
    high_conviction: bool = False,
    queen_of_heaven: bool = False,
) -> tuple[float, float]:
    """Return scaled total-cost and worst-loss caps for normal or profile orders."""
    if queen_of_heaven and bool(config.get_path("queen_of_heaven.enabled", False)):
        max_notional = float(config.get_path("queen_of_heaven.max_window_notional_usdc", 5.0))
        max_loss = float(config.get_path("queen_of_heaven.max_window_loss_usdc", 5.0))
        if not bool(config.get_path("queen_of_heaven.scale_risk_caps_with_account_balance", False)):
            return max_notional, max_loss
    if high_conviction and bool(config.get_path("high_conviction_same_side.enabled", False)):
        max_notional = float(config.get_path("high_conviction_same_side.max_window_notional_usdc", 7.0))
        max_loss = float(config.get_path("high_conviction_same_side.max_window_loss_usdc", 7.0))
        if not bool(config.get_path("high_conviction_same_side.scale_risk_caps_with_account_balance", False)):
            return max_notional, max_loss
    else:
        max_notional = float(config.get_path("risk.max_window_notional_usdc", 3.0))
        max_loss = float(config.get_path("risk.max_window_loss_usdc", 1.0))
    return (
        scaled_risk_cap(config, max_notional, current_balance),
        scaled_risk_cap(config, max_loss, current_balance),
    )


def account_equity_floor(config: dict, starting_balance: float) -> float:
    """Return the minimum allowed account equity after worst-case open PnL."""
    fraction = float(config.get_path("risk.min_account_equity_fraction", 0.0))
    return max(0.0, starting_balance) * max(0.0, min(1.0, fraction))


def account_equity_floor_passes(
    config: dict,
    current_balance: float,
    starting_balance: float,
    worst_case_pnl: float,
) -> bool:
    """Prevent a new order from making the account's worst case cross its floor."""
    if float(config.get_path("risk.min_account_equity_fraction", 0.0)) <= 0 or starting_balance <= 0:
        return True
    return current_balance + worst_case_pnl >= account_equity_floor(config, starting_balance) - 1e-9
