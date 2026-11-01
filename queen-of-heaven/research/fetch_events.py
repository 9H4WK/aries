"""Fetch official 5m up/down event metadata (winner, condition id, token ids) for a date range.

usage: python fetch_events.py OUT START_DAY END_DAY [ASSET=btc] [INTERVAL_SECONDS=300]
"""
import json, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

def get(url):
    for attempt in range(6):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(5 * (attempt + 1)); continue
            return None
        except Exception:
            time.sleep(3)
    return None

out_path, start_day, end_day = sys.argv[1], sys.argv[2], sys.argv[3]
asset = sys.argv[4] if len(sys.argv) > 4 else "btc"
interval = int(sys.argv[5]) if len(sys.argv) > 5 else 300
start = int(datetime.strptime(start_day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
end = int(datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86400
windows = list(range(start, end, interval))
rows = {}
for i in range(0, len(windows), 50):
    chunk = windows[i:i + 50]
    label = f"{interval // 3600}h" if interval % 3600 == 0 else f"{interval // 60}m"
    q = "&".join(f"slug={asset}-updown-{label}-{w}" for w in chunk)
    ev = get(f"https://gamma-api.polymarket.com/events?{q}&limit=100") or []
    for e in ev:
        for m in e.get("markets", []):
            w = int(m["slug"].rsplit("-", 1)[1])
            outcomes, prices = json.loads(m["outcomes"]), json.loads(m.get("outcomePrices") or "[]")
            winner = outcomes[prices.index("1")].upper() if "1" in prices else None
            rows[w] = {"window_start": w, "condition_id": m["conditionId"], "token_ids": json.loads(m["clobTokenIds"]),
                       "outcomes": outcomes, "winner": winner, "volume": float(m.get("volumeNum") or m.get("volume") or 0)}
    time.sleep(0.4)
    print(f"{i + len(chunk)}/{len(windows)} windows, {len(rows)} found", flush=True)
with open(out_path, "w") as f:
    for w in sorted(rows):
        f.write(json.dumps(rows[w]) + "\n")
print("done", len(rows))
