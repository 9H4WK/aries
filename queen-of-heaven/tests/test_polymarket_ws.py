import asyncio
import json

import pytest

from aries.polymarket_ws import PolymarketQuoteStore, PolymarketStream


class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(3600)  # silent but never closes, like a dead TCP connection


def make_stream(stall_seconds):
    config = {"polymarket": {"websocket_url": "wss://example", "stream_stall_seconds": stall_seconds}}
    return PolymarketStream(config, ["up", "down"], PolymarketQuoteStore())


def test_read_applies_messages_then_raises_on_stall():
    stream = make_stream(0.05)
    book = {"event_type": "book", "asset_id": "up", "bids": [{"price": "0.6", "size": "10"}],
            "asks": [{"price": "0.62", "size": "5"}]}
    ws = FakeSocket([json.dumps(book), "PONG", "not json"])

    with pytest.raises(ConnectionError, match="stalled"):
        asyncio.run(stream._read(ws))

    quote = stream.quote_store.quote("up")
    assert quote.bid == 0.6
    assert quote.ask == 0.62


def test_pongs_do_not_count_as_book_data():
    class PongSocket:
        async def recv(self):
            await asyncio.sleep(0.01)
            return "PONG"

    with pytest.raises(ConnectionError, match="stalled"):
        asyncio.run(asyncio.wait_for(make_stream(0.05)._read(PongSocket()), 2))


def test_lag_is_delivery_delay_beyond_the_fastest_update(monkeypatch):
    store = PolymarketQuoteStore()
    now = [1_792_512_000.0]
    monkeypatch.setattr("aries.clock.time.time", lambda: now[0])
    book = {"event_type": "book", "asset_id": "up", "timestamp": "1792511999700",  # sent 0.3s ago
            "bids": [{"price": "0.6", "size": "10"}], "asks": [{"price": "0.62", "size": "5"}]}
    store.apply_message(book)
    assert store.quote("up").lag == 0.0

    # A backed-up stream: the update is applied now but was sent 12.3s ago.
    now[0] = 1_792_512_015.0
    change = {"event_type": "price_change", "timestamp": "1792512002700",
              "price_changes": [{"asset_id": "up", "side": "SELL", "price": "0.61", "size": "4"}]}
    store.apply_message(change)
    quote = store.quote("up")
    assert quote.ask == 0.61
    assert quote.lag == pytest.approx(12.0)
    assert quote.ts == now[0]  # the quote still looks fresh by receive time

    # A faster update than any before becomes the new baseline.
    store.apply_message({"event_type": "best_bid_ask", "asset_id": "up", "timestamp": "1792512014800",
                         "best_bid": "0.6", "best_ask": "0.61"})
    assert store.quote("up").lag == 0.0
    assert store.min_delay["up"] == pytest.approx(0.2)


def test_lag_unknown_without_exchange_timestamp():
    store = PolymarketQuoteStore()
    store.apply_message({"event_type": "book", "asset_id": "up", "bids": [], "asks": [{"price": "0.5", "size": "1"}]})
    assert store.quote("up").lag is None
