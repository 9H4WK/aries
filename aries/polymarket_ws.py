from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from typing import Any, Optional

import websockets # type: ignore

from .models import Quote


class PolymarketQuoteStore:
    def __init__(self):
        self.books: dict[str, dict[str, dict[float, float]]] = defaultdict(lambda: {"BUY": {}, "SELL": {}})
        self.last: dict[str, float] = {}
        self.ts: dict[str, float] = {}

    def quote(self, token_id: str) -> Quote:
        book = self.books[token_id]
        bids = [p for p, size in book["BUY"].items() if size > 0]
        asks = [p for p, size in book["SELL"].items() if size > 0]
        return Quote(
            token_id=token_id,
            bid=max(bids) if bids else None,
            ask=min(asks) if asks else None,
            last=self.last.get(token_id),
            ts=self.ts.get(token_id),
        )

    def apply_message(self, msg: Any) -> None:
        if isinstance(msg, list):
            for item in msg:
                self.apply_message(item)
            return
        if not isinstance(msg, dict):
            return

        event_type = msg.get("event_type") or msg.get("type")
        now = time.time()

        if event_type == "book":
            token_id = str(msg.get("asset_id") or msg.get("token_id") or "")
            if not token_id:
                return
            self.books[token_id] = {"BUY": {}, "SELL": {}}
            for side_name, key in (("BUY", "bids"), ("SELL", "asks")):
                for level in msg.get(key, []) or []:
                    price = _level_float(level, "price")
                    size = _level_float(level, "size")
                    if price is not None and size is not None:
                        self.books[token_id][side_name][price] = size
            self.ts[token_id] = now
            return

        if event_type == "price_change":
            changes = msg.get("changes") or [msg]
            for ch in changes:
                token_id = str(ch.get("asset_id") or ch.get("token_id") or msg.get("asset_id") or "")
                side = str(ch.get("side") or ch.get("change_side") or "").upper()
                price = _level_float(ch, "price") or _level_float(ch, "change_price")
                size = _level_float(ch, "size") or _level_float(ch, "change_size")
                if token_id and side in ("BUY", "SELL") and price is not None and size is not None:
                    self.books[token_id][side][price] = size
                    self.ts[token_id] = now
            return

        if event_type == "best_bid_ask":
            token_id = str(msg.get("asset_id") or msg.get("token_id") or "")
            if not token_id:
                return
            bid = _level_float(msg, "bid") or _level_float(msg, "best_bid")
            ask = _level_float(msg, "ask") or _level_float(msg, "best_ask")
            if bid is not None:
                self.books[token_id]["BUY"][bid] = max(self.books[token_id]["BUY"].get(bid, 0), 1.0)
            if ask is not None:
                self.books[token_id]["SELL"][ask] = max(self.books[token_id]["SELL"].get(ask, 0), 1.0)
            self.ts[token_id] = now
            return

        if event_type == "last_trade_price":
            token_id = str(msg.get("asset_id") or msg.get("token_id") or "")
            price = _level_float(msg, "price")
            if token_id and price is not None:
                self.last[token_id] = price
                self.ts[token_id] = now


def _level_float(obj: Any, key: str) -> Optional[float]:
    if not isinstance(obj, dict) or obj.get(key) is None:
        return None
    try:
        return float(obj[key])
    except (TypeError, ValueError):
        return None


class PolymarketStream:
    def __init__(self, config: dict, token_ids: list[str], quote_store: PolymarketQuoteStore):
        self.url = config["polymarket"]["websocket_url"]
        self.token_ids = token_ids
        self.quote_store = quote_store
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=None) as ws:
                    await ws.send(json.dumps({
                        "assets_ids": self.token_ids,
                        "type": "market",
                        "custom_feature_enabled": True,
                    }))
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            if raw == "PONG":
                                continue
                            try:
                                self.quote_store.apply_message(json.loads(raw))
                            except json.JSONDecodeError:
                                continue
                            if self._stop.is_set():
                                break
                    finally:
                        ping_task.cancel()
            except Exception as exc:
                print(f"POLYMARKET_WS reconnecting after error: {exc}", flush=True)
                await asyncio.sleep(2)

    async def _ping_loop(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(10)
            try:
                await ws.send("PING")
            except Exception:
                return
