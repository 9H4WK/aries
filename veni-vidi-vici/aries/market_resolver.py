from __future__ import annotations

import time
from typing import Any, Optional

from .models import MarketInfo, BinanceState
from .polymarket_api import PolymarketApi, current_window_start


class MarketResolver:
    def __init__(self, config: dict, api: PolymarketApi):
        self.config = config
        self.api = api
        self.current: Optional[MarketInfo] = None
        self.last_refresh_ts: float = 0.0
        self.last_target_fetch_ts: float = 0.0
        self.crypto_price_by_window: dict[int, dict[str, Any]] = {}

    async def resolve(self, binance: BinanceState) -> Optional[MarketInfo]:
        now = int(time.time())
        interval = int(self.config["bot"]["interval_seconds"])
        start = current_window_start(now, interval)

        if self.current and self.current.window_start_ts == start:
            await self._maybe_fetch_polymarket_target(self.current, now)
            self._maybe_set_target(self.current, binance, now)
            return self.current

        if time.time() - self.last_refresh_ts < float(self.config["polymarket"].get("market_refresh_seconds", 10)):
            return self.current

        self.last_refresh_ts = time.time()
        market = await self.api.resolve_current_market(now)
        if market:
            self.current = market
            await self._maybe_fetch_polymarket_target(market, now)
            self._maybe_set_target(market, binance, now)
        return self.current

    async def _maybe_fetch_polymarket_target(self, market: MarketInfo, now_ts: int) -> None:
        """Fetch the official Chainlink "price to beat" from Polymarket.

        BTC 5m markets resolve via the Chainlink BTC/USD TWAP stream, so the
        target must come from Polymarket, not from Binance. Binance prices are
        signal inputs only. The latest snapshot is cached per window so
        settlement can reuse the official close price.
        """
        if market.target_price is None:
            throttle = max(1.0, float(self.config["polymarket"].get("target_refresh_seconds", 2)))
            if time.time() - self.last_target_fetch_ts >= throttle:
                self.last_target_fetch_ts = time.time()
                snapshot = await self.api.fetch_crypto_price(market.window_start_ts)
                if snapshot:
                    self.crypto_price_by_window[market.window_start_ts] = snapshot
                    if len(self.crypto_price_by_window) > 8:
                        for key in sorted(self.crypto_price_by_window)[:-8]:
                            self.crypto_price_by_window.pop(key, None)
                    if snapshot.get("open_price") is not None:
                        market.target_price = float(snapshot["open_price"])
                        market.target_source = "polymarket_chainlink_open"
                if market.target_price is None:
                    # The crypto-price openPrice can lag window open by tens of
                    # seconds; the price-history series starts with the exact
                    # Chainlink open sample and is available immediately.
                    history = await self.api.fetch_price_history(market.window_start_ts)
                    if history:
                        first_ts, first_price = history[0]
                        if first_ts <= market.window_start_ts + 2:
                            market.target_price = float(first_price)
                            market.target_source = "polymarket_chainlink_open_sample"
        elif market.window_start_ts in self.crypto_price_by_window:
            throttle = max(5.0, float(self.config["polymarket"].get("target_refresh_seconds", 2)) * 5)
            if time.time() - self.last_target_fetch_ts >= throttle:
                self.last_target_fetch_ts = time.time()
                snapshot = await self.api.fetch_crypto_price(market.window_start_ts)
                if snapshot:
                    self.crypto_price_by_window[market.window_start_ts] = snapshot

    def _maybe_set_target(self, market: MarketInfo, binance: BinanceState, now_ts: int) -> None:
        """Optional emergency fallback: use Binance only if explicitly enabled.

        Disabled by default because Binance BTCUSDT is not the resolution
        source; Polymarket's Chainlink open price is the real price to beat.
        """
        if market.target_price is not None:
            return
        allow = bool(self.config["polymarket"].get("allow_target_from_first_seen_after_open", False))
        max_delay = int(self.config["polymarket"].get("max_target_set_delay_seconds", 3))
        if not allow or binance.price is None:
            return
        delay = now_ts - market.window_start_ts
        if 0 <= delay <= max_delay:
            market.target_price = binance.price
            market.target_source = "binance_first_seen_after_window_open"
