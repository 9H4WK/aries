"""Fetch all Polymarket trades for each BTC 5m window (resumable). One compact line per window."""
import json, sys, time, urllib.request, urllib.error

def get(url):
    for attempt in range(8):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504): time.sleep(3 * (attempt + 1)); continue
            return None
        except Exception:
            time.sleep(3)
    return None

events_path, out_path = sys.argv[1], sys.argv[2]
done = set()
try:
    for line in open(out_path):
        done.add(json.loads(line)["window_start"])
except FileNotFoundError:
    pass
events = [json.loads(l) for l in open(events_path)]
todo = [e for e in events if e["window_start"] not in done]
print(f"{len(done)} done, {len(todo)} to fetch", flush=True)
with open(out_path, "a") as out:
    for n, e in enumerate(todo, 1):
        trades, offset, ok = [], 0, True
        while True:
            page = get(f"https://data-api.polymarket.com/trades?market={e['condition_id']}&limit=500&offset={offset}")
            if page is None:
                ok = False; break
            trades += page
            if len(page) < 500: break
            offset += 500
            if offset > 10000: break  # data-api caps offset at 10000 (seen Oct 24); pages are newest first, so a busy window keeps its last ~10500 prints
        if not ok:
            print("failed", e["window_start"], flush=True); continue
        w = e["window_start"]
        compact = sorted((t["timestamp"] - w, t["outcomeIndex"], 1 if t["side"] == "BUY" else -1, t["price"], round(t["size"], 4)) for t in trades)
        row = {"window_start": w, "n": len(compact), "t": compact}
        if offset > 10000: row["truncated_before"] = compact[0][0]
        out.write(json.dumps(row) + "\n")
        out.flush()
        if n % 50 == 0: print(f"{n}/{len(todo)}", flush=True)
print("done", flush=True)
