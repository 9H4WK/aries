"""Probe one Polymarket hourly BTC up/down market: metadata and late trades."""
import json, sys, urllib.request
from datetime import datetime, timezone
def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))
slug = sys.argv[1]
ev = get(f"https://gamma-api.polymarket.com/events?slug={slug}")[0]
m = ev["markets"][0]
for k in ("question", "conditionId", "startDate", "endDate", "orderPriceMinTickSize", "orderMinSize", "feeType", "feeSchedule", "takerBaseFee", "makerBaseFee", "outcomes", "outcomePrices", "volume", "clobTokenIds", "eventStartTime"):
    print(k, "=", m.get(k))
end = int(datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")).timestamp())
trades, off = [], 0
while off <= 3000:
    page = get(f"https://data-api.polymarket.com/trades?market={m['conditionId']}&limit=500&offset={off}")
    trades += page
    if len(page) < 500: break
    off += 500
print("trades fetched", len(trades), "oldest", min(t["timestamp"] for t in trades) - end, "newest", max(t["timestamp"] for t in trades) - end)
for lo in (-600, -300, -120, -60, -30):
    sel = [t for t in trades if lo <= t["timestamp"] - end < 0]
    print(f"last {-lo}s: {len(sel)} trades, ${sum(t['size']*t['price'] for t in sel):.0f}")
for t in sorted(trades, key=lambda t: t["timestamp"])[-15:]:
    print(t["timestamp"] - end, t["outcome"], t["side"], t["price"], round(t["size"], 2))
