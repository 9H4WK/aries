from __future__ import annotations

import time
from typing import Optional

from .models import Quote, BinanceState


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
