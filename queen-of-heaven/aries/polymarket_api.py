from __future__ import annotations

import json
import re
import time
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

from .models import MarketInfo
from . import clock

# polymarket.com sends a Content-Security-Policy header larger than aiohttp's
# 8190-byte default, which makes every crypto-price request fail to parse.
POLYMARKET_WEB_MAX_FIELD_SIZE = 65536


def current_window_start(now_ts: Optional[int] = None, interval_seconds: int = 300) -> int:
    now_ts = now_ts or int(clock.now())
    return (now_ts // interval_seconds) * interval_seconds


def _loads_maybe(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _token_ids_from_market(market: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    # Most Gamma markets expose clobTokenIds as a JSON string/list in outcome order.
    token_ids = _loads_maybe(market.get("clobTokenIds") or market.get("clob_token_ids"))
    outcomes = _loads_maybe(market.get("outcomes"))

    if isinstance(token_ids, list) and len(token_ids) >= 2:
        token_ids = [str(x) for x in token_ids]
        if isinstance(outcomes, list) and len(outcomes) == len(token_ids):
            mapped = {str(out).strip().lower(): token_ids[i] for i, out in enumerate(outcomes)}
            up = mapped.get("up") or mapped.get("yes") or mapped.get("higher")
            down = mapped.get("down") or mapped.get("no") or mapped.get("lower")
            if up and down:
                return up, down
        return token_ids[0], token_ids[1]

    tokens = market.get("tokens") or market.get("outcomeTokens") or []
    if isinstance(tokens, list) and len(tokens) >= 2:
        pairs = []
        for t in tokens:
            outcome = str(t.get("outcome") or t.get("name") or "").lower()
            token_id = str(t.get("token_id") or t.get("tokenId") or t.get("id") or "")
            pairs.append((outcome, token_id))
        up = next((tid for out, tid in pairs if out in ("up", "yes", "higher")), None)
        down = next((tid for out, tid in pairs if out in ("down", "no", "lower")), None)
        if up and down:
            return up, down
        return pairs[0][1], pairs[1][1]

    return None, None


def parse_crypto_price(data: Any, event_start_ts: int) -> Optional[dict[str, Any]]:
    """Normalize the official Polymarket crypto-price endpoint response."""
    if not isinstance(data, dict):
        return None
    open_price = data.get("openPrice")
    close_price = data.get("closePrice")
    return {
        "window_start_ts": int(event_start_ts),
        "open_price": float(open_price) if open_price is not None else None,
        "close_price": float(close_price) if close_price is not None else None,
        "completed": bool(data.get("completed", False)),
    }


def parse_market(market: dict[str, Any], slug: str, window_start_ts: int, interval_seconds: int) -> Optional[MarketInfo]:
    up_token, down_token = _token_ids_from_market(market)
    market_id = (
        market.get("conditionId")
        or market.get("condition_id")
        or market.get("market_id")
        or market.get("id")
    )
    if not market_id or not up_token or not down_token:
        return None
    return MarketInfo(
        slug=slug,
        market_id=str(market_id),
        up_token_id=str(up_token),
        down_token_id=str(down_token),
        window_start_ts=window_start_ts,
        window_end_ts=window_start_ts + interval_seconds,
        question=str(market.get("question") or market.get("title") or ""),
    )


class PolymarketApi:
    def __init__(self, config: dict):
        self.base_url = config["polymarket"].get("gamma_base_url", "https://gamma-api.polymarket.com").rstrip("/")
        self.slug_template = config["polymarket"]["expected_slug_template"]
        self.interval_seconds = int(config["bot"]["interval_seconds"])
        self.crypto_price_url = str(
            config["polymarket"].get("crypto_price_url", "https://polymarket.com/api/crypto/crypto-price")
        )
        self.crypto_price_symbol = str(config["polymarket"].get("crypto_price_symbol", "BTC"))
        self.crypto_price_variant = str(config["polymarket"].get("crypto_price_variant", "fiveminute"))
        self.crypto_price_twap_enabled = bool(config["polymarket"].get("crypto_price_twap_enabled", True))
        self.crypto_price_twap_lookback_seconds = int(
            config["polymarket"].get("crypto_price_twap_lookback_seconds", 60)
        )

    def expected_slug(self, window_start_ts: int) -> str:
        return self.slug_template.format(window_start_ts=window_start_ts)

    async def fetch_market_by_slug(self, slug: str, window_start_ts: int) -> Optional[MarketInfo]:
        async with aiohttp.ClientSession() as session:
            for url in (
                f"{self.base_url}/markets/slug/{slug}",
                f"{self.base_url}/markets?slug={slug}",
            ):
                try:
                    async with session.get(url, timeout=10) as resp:
                        if resp.status >= 400:
                            continue
                        data = await resp.json()
                except Exception:
                    continue

                market = None
                if isinstance(data, dict) and data.get("id"):
                    market = data
                elif isinstance(data, list) and data:
                    market = data[0]
                elif isinstance(data, dict) and isinstance(data.get("markets"), list) and data["markets"]:
                    market = data["markets"][0]

                if market:
                    parsed = parse_market(market, slug, window_start_ts, self.interval_seconds)
                    if parsed:
                        return parsed
        return None

    async def fetch_crypto_price(self, event_start_ts: int, symbol: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Fetch Polymarket's official Chainlink open/close prices for one window.

        BTC 5m markets resolve on the Chainlink BTC/USD TWAP, so these prices
        (not Binance) decide the winning side. `completed` marks a final close.
        """
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(event_start_ts)))
        end_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(event_start_ts) + self.interval_seconds)
        )
        params: dict[str, str] = {
            "symbol": symbol or self.crypto_price_symbol,
            "eventStartTime": start_iso,
            "variant": self.crypto_price_variant,
            "endDate": end_iso,
        }
        if self.crypto_price_twap_enabled:
            params["twapEnabled"] = "true"
            params["twapLookbackSeconds"] = str(self.crypto_price_twap_lookback_seconds)
        url = f"{self.crypto_price_url}?{urlencode(params)}"
        try:
            async with aiohttp.ClientSession(max_field_size=POLYMARKET_WEB_MAX_FIELD_SIZE) as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status >= 400:
                        return None
                    data = await resp.json()
        except Exception:
            return None
        return parse_crypto_price(data, event_start_ts)

    async def resolve_current_market(self, now_ts: Optional[int] = None) -> Optional[MarketInfo]:
        start = current_window_start(now_ts, self.interval_seconds)
        slug = self.expected_slug(start)
        return await self.fetch_market_by_slug(slug, start)
