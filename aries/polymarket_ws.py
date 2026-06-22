from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from typing import Any, Optional

import websockets # type: ignore

from .models import Quote


class PolymarketQuoteStore:
    def __init__(self, max_trade_events: int = 2000):
        """Keep live L2 books and recent matched trades for active tokens."""
        self.books: dict[str, dict[str, dict[float, float]]] = defaultdict(lambda: {"BUY": {}, "SELL": {}})
        self.best_bid: dict[str, float] = {}
        self.best_ask: dict[str, float] = {}
        self.last: dict[str, float] = {}
        self.ts: dict[str, float] = {}
        self.trades: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=max(100, int(max_trade_events)))
        )

    def quote(self, token_id: str) -> Quote:
        book = self.books[token_id]
        bids = [p for p, size in book["BUY"].items() if size > 0]
        asks = [p for p, size in book["SELL"].items() if size > 0]
        return Quote(
            token_id=token_id,
            bid=self.best_bid.get(token_id, max(bids) if bids else None),
            ask=self.best_ask.get(token_id, min(asks) if asks else None),
            last=self.last.get(token_id),
            ts=self.ts.get(token_id),
        )

    def book_levels(self, token_id: str, max_levels: int = 5) -> dict[str, Any]:
        """Return nearest real bid/ask levels; sizes are shares, not people."""
        book = self.books[token_id]
        bids = sorted(
            ((price, size) for price, size in book["BUY"].items() if size > 0),
            key=lambda level: level[0],
            reverse=True,
        )[: max(1, int(max_levels))]
        asks = sorted(
            ((price, size) for price, size in book["SELL"].items() if size > 0),
            key=lambda level: level[0],
        )[: max(1, int(max_levels))]
        return {"bids": bids, "asks": asks, "ts": self.ts.get(token_id)}

    def recent_trades(self, token_id: str, since_ts: float) -> list[dict[str, Any]]:
        """Return matched trades received after `since_ts`."""
        return [trade for trade in self.trades.get(token_id, ()) if trade["received_at"] >= since_ts]

    def retain_tokens(self, token_ids: list[str]) -> None:
        """Drop completed-window books/trades so overnight memory stays bounded."""
        keep = set(token_ids)
        for mapping in (self.books, self.best_bid, self.best_ask, self.last, self.ts, self.trades):
            for token_id in list(mapping.keys()):
                if token_id not in keep:
                    mapping.pop(token_id, None)

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
                    if price is not None and size is not None and size > 0:
                        self.books[token_id][side_name][price] = size
            self._refresh_best(token_id)
            self.ts[token_id] = now
            return

        if event_type == "price_change":
            # Official messages use `price_changes`; keep `changes` for older feeds.
            changes = msg.get("price_changes") or msg.get("changes") or [msg]
            changed_tokens: set[str] = set()
            for ch in changes:
                token_id = str(ch.get("asset_id") or ch.get("token_id") or msg.get("asset_id") or "")
                side = str(ch.get("side") or ch.get("change_side") or "").upper()
                price = _first_level_float(ch, "price", "change_price")
                size = _first_level_float(ch, "size", "change_size")
                if token_id and side in ("BUY", "SELL") and price is not None and size is not None:
                    if size <= 0:
                        self.books[token_id][side].pop(price, None)
                    else:
                        self.books[token_id][side][price] = size
                    self.ts[token_id] = now
                    changed_tokens.add(token_id)
            for token_id in changed_tokens:
                self._refresh_best(token_id)
            return

        if event_type == "best_bid_ask":
            token_id = str(msg.get("asset_id") or msg.get("token_id") or "")
            if not token_id:
                return
            bid = _first_level_float(msg, "bid", "best_bid")
            ask = _first_level_float(msg, "ask", "best_ask")
            if bid is not None:
                self.best_bid[token_id] = bid
            if ask is not None:
                self.best_ask[token_id] = ask
            self.ts[token_id] = now
            return

        if event_type == "last_trade_price":
            token_id = str(msg.get("asset_id") or msg.get("token_id") or "")
            price = _level_float(msg, "price")
            if token_id and price is not None:
                self.last[token_id] = price
                self.ts[token_id] = now
                side = str(msg.get("side") or "").upper()
                size = _level_float(msg, "size")
                if side in {"BUY", "SELL"} and size is not None and size > 0:
                    self.trades[token_id].append(
                        {
                            "received_at": now,
                            "exchange_ts": _message_timestamp(msg.get("timestamp"), now),
                            "side": side,
                            "price": price,
                            "size": size,
                        }
                    )

    def _refresh_best(self, token_id: str) -> None:
        bids = [price for price, size in self.books[token_id]["BUY"].items() if size > 0]
        asks = [price for price, size in self.books[token_id]["SELL"].items() if size > 0]
        if bids:
            self.best_bid[token_id] = max(bids)
        else:
            self.best_bid.pop(token_id, None)
        if asks:
            self.best_ask[token_id] = min(asks)
        else:
            self.best_ask.pop(token_id, None)


def _level_float(obj: Any, key: str) -> Optional[float]:
    if not isinstance(obj, dict) or obj.get(key) is None:
        return None
    try:
        return float(obj[key])
    except (TypeError, ValueError):
        return None


def _first_level_float(obj: Any, *keys: str) -> Optional[float]:
    for key in keys:
        value = _level_float(obj, key)
        if value is not None:
            return value
    return None


def _message_timestamp(value: Any, default: float) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return default
    return timestamp / 1000.0 if timestamp > 10_000_000_000 else timestamp


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
