from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Any, Optional

from .models import BinanceState


class ChainlinkPriceFeed:
    """Track Polymarket's official Chainlink BTC/USD price for the active window.

    BTC 5m markets resolve using the Chainlink BTC/USD TWAP data stream, so the
    reference "current price" must come from Chainlink, not Binance. Binance is
    kept as the high-frequency signal feed (MA pressure, order book, OFI/CVD),
    and its price levels are anchored to the Chainlink level with a rolling
    basis estimate between official updates.

    Reference hierarchy used for distance/actual-side/entry decisions:
    1. "chainlink"        - fresh official Chainlink price from Polymarket
    2. "binance_anchored" - Binance price shifted by the last Chainlink basis
    3. "binance_raw"      - unanchored Binance price (entries blocked by default)
    """

    def __init__(self, config: Any, api: Any):
        self.enabled = bool(config.get_path("chainlink_reference.enabled", True))
        self.refresh_seconds = max(0.5, float(config.get_path("chainlink_reference.refresh_seconds", 2.0)))
        self.max_price_age_seconds = max(1.0, float(config.get_path("chainlink_reference.max_price_age_seconds", 15.0)))
        self.require_for_entry = bool(config.get_path("chainlink_reference.require_for_entry", True))
        self.basis_ema_alpha = min(1.0, max(0.01, float(config.get_path("chainlink_reference.basis_ema_alpha", 0.3))))
        self.max_abs_basis_bps = max(1.0, float(config.get_path("chainlink_reference.max_abs_basis_bps", 50.0)))
        self.twap_lookback_seconds = max(1, int(config.get_path("polymarket.crypto_price_twap_lookback_seconds", 60)))
        self.max_carried_basis_age_seconds = max(
            0.0, float(config.get_path("chainlink_reference.max_carried_basis_age_seconds", 600.0))
        )
        self.api = api
        self._window_start: Optional[int] = None
        self._open_price: Optional[float] = None
        self._current_price: Optional[float] = None
        self._current_ts: float = 0.0
        self._latest_sample_ts: float = 0.0
        self._basis: Optional[float] = None
        self._basis_ts: float = 0.0
        self._basis_carried = False
        self._last_fetch_ts: float = 0.0
        self._samples: deque[tuple[float, float]] = deque(maxlen=600)

    async def update(self, market: Any, binance_price: Optional[float], now: float) -> None:
        """Poll the official Chainlink feeds for the active window.

        The open price ("price to beat") comes from the crypto-price endpoint;
        the live current price comes from the price-history sample series,
        which updates every few seconds during the window.
        """
        if not self.enabled or market is None:
            return
        window_start = int(getattr(market, "window_start_ts", 0))
        if window_start != self._window_start:
            self._window_start = window_start
            self._open_price = None
            self._current_price = None
            self._current_ts = 0.0
            self._latest_sample_ts = 0.0
            # The official price-history series only publishes about once a
            # minute, so a fresh window can go a minute without a sample. Keep
            # the previous window's basis for anchoring until the first new
            # sample replaces it, unless that basis is too old to trust.
            if self._basis is not None and now - self._basis_ts <= self.max_carried_basis_age_seconds:
                self._basis_carried = True
            else:
                self._basis = None
                self._basis_carried = False
            self._last_fetch_ts = 0.0
            self._samples.clear()
        if now - self._last_fetch_ts < self.refresh_seconds:
            return
        self._last_fetch_ts = now
        if self._open_price is None:
            snapshot = await self.api.fetch_crypto_price(window_start)
            if snapshot and snapshot.get("open_price") is not None:
                self._open_price = float(snapshot["open_price"])
        history = await self.api.fetch_price_history(window_start)
        if not history:
            return
        if self._open_price is None:
            first_ts, first_price = history[0]
            if first_ts <= window_start + 2:
                self._open_price = float(first_price)
        new_samples = [(ts, price) for ts, price in history if ts > self._latest_sample_ts]
        if not new_samples:
            return
        self._samples.extend(new_samples)
        sample_ts, sample_price = new_samples[-1]
        self._latest_sample_ts = sample_ts
        self._current_price = sample_price
        self._current_ts = now
        if binance_price is not None:
            self._update_basis(sample_price, float(binance_price), now)

    def _update_basis(self, chainlink_price: float, binance_price: float, now: float) -> None:
        """EMA-smooth the Chainlink-minus-Binance basis with outlier rejection.

        Binance typically leads the Chainlink aggregate, so a single paired
        observation during a fast move can be noisy; the EMA plus a sanity cap
        keeps the anchor stable without chasing spikes.
        """
        if binance_price <= 0:
            return
        sample = chainlink_price - binance_price
        cap = binance_price * self.max_abs_basis_bps / 10_000.0
        if abs(sample) > cap:
            return
        alpha = self.basis_ema_alpha
        if self._basis is None or self._basis_carried:
            self._basis = sample
        else:
            self._basis = alpha * sample + (1.0 - alpha) * self._basis
        self._basis_carried = False
        self._basis_ts = now

    def current_price(self, now: float) -> Optional[float]:
        """Return the official Chainlink price when it is fresh enough."""
        if self._current_price is None or self._current_ts <= 0:
            return None
        if now - self._current_ts > self.max_price_age_seconds:
            return None
        return self._current_price

    def age_seconds(self, now: float) -> Optional[float]:
        if self._current_ts <= 0:
            return None
        return max(0.0, now - self._current_ts)

    @property
    def basis(self) -> Optional[float]:
        return self._basis

    @property
    def open_price(self) -> Optional[float]:
        return self._open_price

    def anchor_price(self, price: Optional[float]) -> Optional[float]:
        """Shift a Binance price onto the Chainlink level using the basis."""
        if price is None:
            return None
        if self._basis is None:
            return None
        return float(price) + self._basis

    def anchor_state(self, state: BinanceState) -> BinanceState:
        """Return a copy of the Binance state with price levels Chainlink-anchored.

        Quantity-based signals (book sizes, OFI, CVD volumes) are unchanged;
        only price levels are shifted so distance/persistence/MA-versus-target
        comparisons line up with the resolution source.
        """
        if not self.enabled or self._basis is None:
            return state
        basis = self._basis

        def shift(value: Optional[float]) -> Optional[float]:
            return None if value is None else value + basis

        return replace(
            state,
            price=shift(state.price),
            best_bid=shift(state.best_bid),
            best_ask=shift(state.best_ask),
            bars=[(ts, price + basis) for ts, price in state.bars],
            ohlc_bars=[(ts, o + basis, h + basis, l + basis, c + basis) for ts, o, h, l, c in state.ohlc_bars],
        )

    def reference(self, anchored_price: Optional[float], now: float) -> tuple[Optional[float], str]:
        """Pick the best current-price reference and label its source."""
        chainlink = self.current_price(now)
        if chainlink is not None:
            return chainlink, "chainlink"
        if self.enabled and anchored_price is not None and self._basis is not None:
            return anchored_price, "binance_anchored"
        return anchored_price, "binance_raw"

    def entry_allowed(self, anchored_price: Optional[float], now: float) -> tuple[bool, str]:
        """Gate entries on having a Chainlink-grade reference price."""
        if not self.enabled or not self.require_for_entry:
            return True, "reference_gate_disabled"
        _, source = self.reference(anchored_price, now)
        if source in {"chainlink", "binance_anchored"}:
            return True, source
        return False, "chainlink_reference_unavailable"

    def twap_state(self, window_end_ts: Optional[int], now: float) -> dict[str, Any]:
        """Report how much of the resolution TWAP window is already accumulated.

        Resolution uses the Chainlink TWAP over the final `twap_lookback_seconds`
        of the window. This is diagnostic state for operators and models; it is
        computed as a step-function time-weighted average of observed samples.
        """
        lookback = float(self.twap_lookback_seconds)
        state: dict[str, Any] = {
            "lookback_seconds": int(lookback),
            "accumulated_seconds": 0.0,
            "locked_fraction": 0.0,
            "twap_so_far": None,
            "samples": len(self._samples),
        }
        if not self._samples or window_end_ts is None:
            return state
        range_start = float(window_end_ts) - lookback
        range_end = min(now, float(window_end_ts))
        if range_end <= range_start:
            return state
        samples = list(self._samples)
        weighted = 0.0
        covered = 0.0
        for index, (ts, price) in enumerate(samples):
            if ts > range_end:
                break
            hold_from = max(range_start, ts)
            next_ts = samples[index + 1][0] if index + 1 < len(samples) else range_end
            hold_to = min(range_end, next_ts)
            if index == 0 and ts < range_start and next_ts > range_start:
                hold_from = range_start
            duration = max(0.0, hold_to - hold_from)
            if duration > 0:
                weighted += price * duration
                covered += duration
        if covered > 0:
            state["accumulated_seconds"] = round(covered, 2)
            state["locked_fraction"] = round(min(1.0, covered / lookback), 4)
            state["twap_so_far"] = weighted / covered
        return state

    def snapshot(self, anchored_price: Optional[float], now: float, window_end_ts: Optional[int] = None) -> dict[str, Any]:
        """Compact feed state for decision logs and dashboards."""
        reference_price, reference_source = self.reference(anchored_price, now)
        return {
            "enabled": self.enabled,
            "chainlink_price": self._current_price,
            "chainlink_age_seconds": self.age_seconds(now),
            "open_price": self._open_price,
            "price_basis": self._basis,
            "reference_price": reference_price,
            "reference_source": reference_source,
            "twap": self.twap_state(window_end_ts, now),
        }
