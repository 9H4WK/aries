from __future__ import annotations

import time
from typing import Optional

from .models import MarketInfo, BinanceState
from .polymarket_api import PolymarketApi, current_window_start


class MarketResolver:
    def __init__(self, config: dict, api: PolymarketApi):
        self.config = config
        self.api = api
        self.current: Optional[MarketInfo] = None
        self.last_refresh_ts: float = 0.0

    async def resolve(self, binance: BinanceState) -> Optional[MarketInfo]:
        now = int(time.time())
        interval = int(self.config["bot"]["interval_seconds"])
        start = current_window_start(now, interval)

        if self.current and self.current.window_start_ts == start:
            self._maybe_set_target(self.current, binance, now)
            return self.current

        if time.time() - self.last_refresh_ts < float(self.config["polymarket"].get("market_refresh_seconds", 10)):
            return self.current

        self.last_refresh_ts = time.time()
        market = await self.api.resolve_current_market(now)
        if market:
            self.current = market
            self._maybe_set_target(market, binance, now)
        return self.current

    def _maybe_set_target(self, market: MarketInfo, binance: BinanceState, now_ts: int) -> None:
        if market.target_price is not None:
            return
        allow = bool(self.config["polymarket"].get("allow_target_from_first_seen_after_open", True))
        max_delay = int(self.config["polymarket"].get("max_target_set_delay_seconds", 3))
        if not allow or binance.price is None:
            return
        delay = now_ts - market.window_start_ts
        if 0 <= delay <= max_delay:
            market.target_price = binance.price
            market.target_source = "binance_first_seen_after_window_open"
