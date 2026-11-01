"""Record the price feeds that matter for BTC 5m and 15m markets, for offline research.

Writes gzip JSON lines per hour into OUT_DIR (local receive time first):
  ["cl", recv, obs_second, value]                 Chainlink BTC/USD (Polymarket RTDS), the resolution source
  ["cb", recv, exch_time, price, bid, ask]        Coinbase BTC-USD ticker
  ["bn", recv, trade_ms, price]                   Binance BTCUSDT aggTrade, at most one per 100ms
  ["pm", recv, window, up_bid, up_ask, dn_bid, dn_ask]  Polymarket 5m best quotes when they change
  ["pm15", recv, window, up_bid, up_ask, dn_bid, dn_ask]  the same for the 15m markets

Usage: python research/record_feeds.py OUT_DIR [HOURS]
"""
import asyncio
import gzip
import json
import os
import sys
import time

import aiohttp
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from aries.polymarket_ws import PolymarketQuoteStore  # noqa: E402

RTDS_URL = "wss://ws-live-data.polymarket.com"
COINBASE_URL = "wss://ws-feed.exchange.coinbase.com"
BINANCE_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
PM_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com/events?slug=btc-updown-{}-{}"
MARKETS = ((300, "5m", "pm"), (900, "15m", "pm15"))  # (interval, slug label, row tag)


class Writer:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.hour = None
        self.fh = None
        self.flushed = 0.0
        os.makedirs(out_dir, exist_ok=True)

    def write(self, row):
        hour = time.strftime("%Y%m%d_%H", time.gmtime(row[1]))
        if hour != self.hour:
            if self.fh:
                self.fh.close()
            self.fh = gzip.open(os.path.join(self.out_dir, f"feeds_{hour}_{os.getpid()}.jsonl.gz"), "at")
            self.hour = hour
        self.fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        if row[1] - self.flushed > 30:  # keep the file readable if the process dies
            self.fh.flush()
            self.flushed = row[1]

    def close(self):
        if self.fh:
            self.fh.close()


async def forever(name, fn, stop):
    while not stop.is_set():
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"{name} reconnecting after error: {exc}", flush=True)
            await asyncio.sleep(2)


async def chainlink(out, stop):
    async with websockets.connect(RTDS_URL, ping_interval=None) as ws:
        await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": "{\"symbol\":\"btc/usd\"}"}]}))
        last_ping = last_data = time.time()
        while not stop.is_set():
            if time.time() - last_ping > 5:
                await ws.send("PING")
                last_ping = time.time()
            if time.time() - last_data > 10:
                raise ConnectionError("chainlink stream stalled")
            try:
                raw = await asyncio.wait_for(ws.recv(), 5)
            except asyncio.TimeoutError:
                continue
            if not raw or raw == "PONG":
                continue
            recv = last_data = time.time()
            try:
                payload = json.loads(raw).get("payload") or {}
            except (json.JSONDecodeError, AttributeError):
                continue
            points = payload.get("data") or ([payload] if "value" in payload else [])
            for point in points:
                out.write(["cl", recv, int(point["timestamp"]) // 1000, float(point["value"])])


async def coinbase(out, stop):
    async with websockets.connect(COINBASE_URL, ping_interval=20) as ws:
        await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}))
        async for raw in ws:
            if stop.is_set():
                return
            msg = json.loads(raw)
            if msg.get("type") == "ticker":
                out.write(["cb", time.time(), msg.get("time"), float(msg["price"]),
                           float(msg.get("best_bid") or 0), float(msg.get("best_ask") or 0)])


async def binance(out, stop):
    async with websockets.connect(BINANCE_URL, ping_interval=20) as ws:
        last = 0.0
        async for raw in ws:
            if stop.is_set():
                return
            recv = time.time()
            if recv - last < 0.1:
                continue
            last = recv
            msg = json.loads(raw)
            out.write(["bn", recv, int(msg["T"]), float(msg["p"])])


async def window_tokens(session, window, label="5m"):
    async with session.get(GAMMA_URL.format(label, window), timeout=aiohttp.ClientTimeout(total=10)) as resp:
        events = await resp.json()
    market = events[0]["markets"][0]
    outcomes = [o.upper() for o in json.loads(market["outcomes"])]
    tokens = json.loads(market["clobTokenIds"])
    return tokens[outcomes.index("UP")], tokens[outcomes.index("DOWN")]


async def polymarket(out, stop, interval=300, label="5m", tag="pm"):
    async with aiohttp.ClientSession() as session:
        while not stop.is_set():
            window = int(time.time()) // interval * interval
            try:
                up, down = await window_tokens(session, window, label)
            except Exception as exc:
                print(f"PM {label} token lookup failed for {window}: {exc}", flush=True)
                await asyncio.sleep(3)
                continue
            store = PolymarketQuoteStore()
            last = None
            try:
                async with websockets.connect(PM_URL, ping_interval=None, max_size=None) as ws:
                    await ws.send(json.dumps({"assets_ids": [up, down], "type": "market", "custom_feature_enabled": True}))
                    last_ping = last_data = last_row = time.time()
                    while not stop.is_set() and int(time.time()) // interval * interval == window:
                        if time.time() - last_ping > 10:
                            await ws.send("PING")
                            last_ping = time.time()
                        if time.time() - last_data > 8:
                            raise ConnectionError("polymarket stream stalled")
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 1)
                        except asyncio.TimeoutError:
                            raw = None
                        if raw is not None and raw != "PONG":
                            try:
                                store.apply_message(json.loads(raw))
                                last_data = time.time()
                            except json.JSONDecodeError:
                                pass
                        uq, dq = store.quote(up), store.quote(down)
                        row = (uq.bid, uq.ask, dq.bid, dq.ask)
                        # Write on change, plus a 1s heartbeat so gaps in the file mean a dead feed.
                        if row != last or time.time() - last_row >= 1.0:
                            last, last_row = row, time.time()
                            out.write([tag, last_row, window, *row])
            except Exception as exc:
                print(f"POLYMARKET_WS {label} reconnecting after error: {exc}", flush=True)
                await asyncio.sleep(2)


async def main():
    out_dir = sys.argv[1]
    hours = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0
    out, stop = Writer(out_dir), asyncio.Event()
    feeds = [("CHAINLINK", chainlink), ("COINBASE", coinbase), ("BINANCE", binance)]
    tasks = [asyncio.create_task(forever(name, lambda f=fn: f(out, stop), stop)) for name, fn in feeds]
    tasks += [asyncio.create_task(polymarket(out, stop, *m)) for m in MARKETS]
    try:
        await asyncio.sleep(hours * 3600)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        out.close()


if __name__ == "__main__":
    asyncio.run(main())
