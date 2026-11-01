from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Optional

import aiohttp
import websockets

from .models import BinanceState


class BinanceStream:
    def __init__(self, config: dict):
        self.config = config
        self.url = config["binance"]["websocket_url"]
        sr_config = config.get("support_resistance", {})
        lookback_minutes = [int(value) for value in sr_config.get("lookback_minutes", [30, 60])]
        if sr_config.get("include_3h", False) and 180 not in lookback_minutes:
            lookback_minutes.append(180)
        required_sr_bars = (max(lookback_minutes or [30]) * 60) + 600
        self.max_bars = max(int(config["binance"].get("max_bars", 1000)), required_sr_bars)
        self.latest_price: Optional[float] = None
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None
        self.best_bid_qty: Optional[float] = None
        self.best_ask_qty: Optional[float] = None
        self.latest_ts: Optional[float] = None
        self._bars: deque[tuple[int, float]] = deque(maxlen=self.max_bars)
        self._volume_bars: deque[tuple[int, float]] = deque(maxlen=self.max_bars)
        self._ohlc_bars: deque[tuple[int, float, float, float, float]] = deque(maxlen=self.max_bars)
        max_flow_events = max(100, int(config.get("binance_flow", {}).get("max_events", 5000)))
        self._ofi_events: deque[tuple[float, float]] = deque(maxlen=max_flow_events)
        self._cvd_events: deque[tuple[float, float]] = deque(maxlen=max_flow_events)
        self._last_bar_second: Optional[int] = None
        self._last_volume_second: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def bootstrap_history(self) -> None:
        """Seed price bars from Binance REST so indicators are ready at startup."""
        if not bool(self.config.get("support_resistance", {}).get("enabled", False)):
            return
        if not bool(self.config.get("binance", {}).get("bootstrap_history", True)):
            return
        interval = str(self.config.get("support_resistance", {}).get("bootstrap_interval", "1s"))
        if not await self._fetch_klines(interval):
            fallback = str(self.config.get("support_resistance", {}).get("bootstrap_fallback_interval", "1m"))
            if fallback != interval:
                await self._fetch_klines(fallback)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def snapshot(self) -> BinanceState:
        return BinanceState(
            price=self.latest_price,
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            best_bid_qty=self.best_bid_qty,
            best_ask_qty=self.best_ask_qty,
            ts=self.latest_ts,
            bars=list(self._bars),
            volume_bars=list(self._volume_bars),
            ohlc_bars=list(self._ohlc_bars),
            ofi_events=list(self._ofi_events),
            cvd_events=list(self._cvd_events),
        )

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20) as ws:
                    async for raw in ws:
                        self._handle_message(raw)
                        if self._stop.is_set():
                            break
            except Exception as exc:
                print(f"BINANCE_WS reconnecting after error: {exc}", flush=True)
                await asyncio.sleep(2)

    def _handle_message(self, raw: str) -> None:
        msg = json.loads(raw)
        data = msg.get("data", msg)
        event_type = data.get("e")
        now = time.time()

        if event_type == "trade":
            price = float(data["p"])
            quantity = float(data.get("q", 0.0))
            event_ts = float(data.get("T", data.get("E", int(now * 1000)))) / 1000.0
            self.latest_price = price
            self.latest_ts = event_ts
            self._update_bar(int(event_ts), price)
            self._update_volume_bar(int(event_ts), quantity)
            self._record_cvd(event_ts, quantity, data.get("m"))
            return

        if event_type == "bookTicker" or {"b", "a"}.issubset(data.keys()):
            try:
                previous_bid = self.best_bid
                previous_ask = self.best_ask
                previous_bid_qty = self.best_bid_qty
                previous_ask_qty = self.best_ask_qty
                next_bid = float(data["b"])
                next_ask = float(data["a"])
                next_bid_qty = float(data.get("B")) if data.get("B") is not None else None
                next_ask_qty = float(data.get("A")) if data.get("A") is not None else None
                event_ts = float(data.get("E", int(now * 1000))) / 1000.0
                self.best_bid = next_bid
                self.best_ask = next_ask
                self.best_bid_qty = next_bid_qty
                self.best_ask_qty = next_ask_qty
                self.latest_ts = now
                self._record_ofi(
                    event_ts,
                    previous_bid,
                    previous_bid_qty,
                    previous_ask,
                    previous_ask_qty,
                    next_bid,
                    next_bid_qty,
                    next_ask,
                    next_ask_qty,
                )
            except (KeyError, TypeError, ValueError):
                return

    def _record_cvd(self, event_ts: float, quantity: float, buyer_maker: object) -> None:
        """Store signed taker volume for rolling CVD.

        Binance `m=True` means the buyer was maker, so the taker was selling.
        Positive values are taker-buy pressure; negative values are taker-sell.
        """
        qty = max(0.0, float(quantity))
        if qty <= 0:
            return
        is_buyer_maker = (
            buyer_maker.strip().lower() == "true"
            if isinstance(buyer_maker, str)
            else bool(buyer_maker)
        )
        signed_qty = -qty if is_buyer_maker else qty
        self._cvd_events.append((event_ts, signed_qty))

    def _record_ofi(
        self,
        event_ts: float,
        previous_bid: Optional[float],
        previous_bid_qty: Optional[float],
        previous_ask: Optional[float],
        previous_ask_qty: Optional[float],
        next_bid: float,
        next_bid_qty: Optional[float],
        next_ask: float,
        next_ask_qty: Optional[float],
    ) -> None:
        """Store best-level OFI from bid/ask price and size changes.

        This follows the Cont-Kukanov-Stoikov best-quote event formula. Positive
        OFI means demand added or supply removed at the top of book.
        """
        if None in (previous_bid, previous_bid_qty, previous_ask, previous_ask_qty, next_bid_qty, next_ask_qty):
            return
        ofi = 0.0
        if next_bid >= previous_bid:
            ofi += float(next_bid_qty)
        if next_bid <= previous_bid:
            ofi -= float(previous_bid_qty)
        if next_ask <= previous_ask:
            ofi -= float(next_ask_qty)
        if next_ask >= previous_ask:
            ofi += float(previous_ask_qty)
        if ofi:
            self._ofi_events.append((event_ts, ofi))

    def _update_bar(self, second: int, price: float) -> None:
        if self._last_bar_second == second and self._bars:
            self._bars[-1] = (second, price)
        else:
            self._bars.append((second, price))
            self._last_bar_second = second
        self._update_ohlc_bar(second, price)

    def _update_ohlc_bar(self, second: int, price: float) -> None:
        if self._ohlc_bars and self._ohlc_bars[-1][0] == second:
            ts, open_price, high, low, _ = self._ohlc_bars[-1]
            self._ohlc_bars[-1] = (ts, open_price, max(high, price), min(low, price), price)
        else:
            self._ohlc_bars.append((second, price, price, price, price))

    def _update_volume_bar(self, second: int, quantity: float) -> None:
        if self._last_volume_second == second and self._volume_bars:
            current_second, current_volume = self._volume_bars[-1]
            self._volume_bars[-1] = (current_second, current_volume + max(0.0, quantity))
        else:
            self._volume_bars.append((second, max(0.0, quantity)))
            self._last_volume_second = second

    async def _fetch_klines(self, interval: str) -> bool:
        symbol = str(self.config.get("bot", {}).get("symbol", "BTCUSDT")).upper()
        base_url = str(self.config.get("binance", {}).get("rest_base_url", "https://api.binance.com"))
        end_ms = int(time.time() * 1000)
        lookback_seconds = min(self.max_bars, int(self.config.get("support_resistance", {}).get("bootstrap_lookback_seconds", self.max_bars)))
        start_ms = end_ms - (lookback_seconds * 1000)
        interval_seconds = _interval_seconds(interval)
        rows: list[list] = []
        next_start = start_ms
        try:
            async with aiohttp.ClientSession() as session:
                while next_start < end_ms:
                    params = {
                        "symbol": symbol,
                        "interval": interval,
                        "startTime": next_start,
                        "endTime": end_ms,
                        "limit": 1000,
                    }
                    async with session.get(f"{base_url}/api/v3/klines", params=params, timeout=10) as response:
                        if response.status != 200:
                            return False
                        chunk = await response.json()
                    if not chunk:
                        break
                    rows.extend(chunk)
                    last_open = int(chunk[-1][0])
                    next_start = last_open + (interval_seconds * 1000)
                    if len(chunk) < 1000:
                        break
        except Exception:
            return False

        if not rows:
            return False
        self._seed_klines(rows)
        return True

    def _seed_klines(self, rows: list[list]) -> None:
        self._bars.clear()
        self._volume_bars.clear()
        self._ohlc_bars.clear()
        for row in rows[-self.max_bars :]:
            second = int(row[0]) // 1000
            open_price = float(row[1])
            high = float(row[2])
            low = float(row[3])
            close = float(row[4])
            volume = float(row[5]) if len(row) > 5 else 0.0
            self._bars.append((second, close))
            self._volume_bars.append((second, max(0.0, volume)))
            self._ohlc_bars.append((second, open_price, high, low, close))
            self.latest_price = close
            self.latest_ts = float(second)
        if self._bars:
            self._last_bar_second = self._bars[-1][0]
        if self._volume_bars:
            self._last_volume_second = self._volume_bars[-1][0]


def _interval_seconds(interval: str) -> int:
    unit = interval[-1]
    value = int(interval[:-1])
    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 3600
    return 60
