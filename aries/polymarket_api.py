from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import aiohttp

from .models import MarketInfo


def current_window_start(now_ts: Optional[int] = None, interval_seconds: int = 300) -> int:
    now_ts = now_ts or int(time.time())
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

    async def resolve_current_market(self, now_ts: Optional[int] = None) -> Optional[MarketInfo]:
        start = current_window_start(now_ts, self.interval_seconds)
        slug = self.expected_slug(start)
        return await self.fetch_market_by_slug(slug, start)
