from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Optional

import websockets

from .models import BinanceState


class BinanceStream:
    def __init__(self, config: dict):
        self.url = config["binance"]["websocket_url"]
        self.max_bars = int(config["binance"].get("max_bars", 1000))
        self.latest_price: Optional[float] = None
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None
        self.latest_ts: Optional[float] = None
        self._bars: deque[tuple[int, float]] = deque(maxlen=self.max_bars)
        self._last_bar_second: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

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
            ts=self.latest_ts,
            bars=list(self._bars),
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
            event_ts = float(data.get("E", int(now * 1000))) / 1000.0
            self.latest_price = price
            self.latest_ts = event_ts
            self._update_bar(int(event_ts), price)
            return

        if event_type == "bookTicker" or {"b", "a"}.issubset(data.keys()):
            try:
                self.best_bid = float(data["b"])
                self.best_ask = float(data["a"])
                self.latest_ts = now
            except (KeyError, TypeError, ValueError):
                return

    def _update_bar(self, second: int, price: float) -> None:
        if self._last_bar_second == second and self._bars:
            self._bars[-1] = (second, price)
        else:
            self._bars.append((second, price))
            self._last_bar_second = second
