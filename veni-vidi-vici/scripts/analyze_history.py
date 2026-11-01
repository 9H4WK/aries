import json
import sqlite3
from collections import defaultdict

conn = sqlite3.connect("aries.db")
conn.row_factory = sqlite3.Row

tables = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
print("TABLES:", tables)

# window results joined with paper fills
windows = {}
for row in conn.execute(
    "select market_id, window_start, winning_side from window_results where resolved=1"
):
    windows[(row["market_id"], row["window_start"])] = dict(row)
print("resolved windows:", len(windows))

fills = []
for row in conn.execute(
        "select market_id, window_start, side, action, price, notional_usdc, shares, created_at, metadata_json from paper_fills"
):
    fills.append(dict(row))
print("paper fills:", len(fills))

def meta(row):
    try:
        return json.loads(row.get("metadata_json") or "{}")
    except Exception:
        return {}

# Per-fill outcome pnl (BUY: shares if won - cost; SELL: proceeds - value)
obs = []
for f in fills:
    key = (f["market_id"], f["window_start"])
    w = windows.get(key)
    if not w:
        continue
    won = f["side"] == w["winning_side"]
    action = str(f["action"]).upper()
    price = float(f["price"]) or 0.0
    notional = float(f["notional_usdc"]) or 0.0
    shares = float(f["shares"]) or 0.0
    if action == "BUY":
        pnl = (shares if won else 0.0) - notional
    else:
        pnl = notional - (shares if not won else 0.0)
    m = meta(f)
    cand = m.get("candidate", {}) or {}
    obs.append(
        {
            "pnl": pnl,
            "won": won,
            "action": action,
            "side": f["side"],
            "price": price,
            "notional": notional,
            "shares": shares,
            "role": cand.get("role") or "UNKNOWN",
            "reason": m.get("strategy_reason") or f.get("reason") or "UNKNOWN",
            "t": float(f["created_at"]) - int(f["window_start"]),
            "window_start": int(f["window_start"]),
        }
    )

buys = [o for o in obs if o["action"] == "BUY"]
sells = [o for o in obs if o["action"] == "SELL"]
print(f"joined buys: {len(buys)} sells: {len(sells)}")
print(f"buy pnl total: {sum(o['pnl'] for o in buys):+.2f}")
print(f"sell pnl total: {sum(o['pnl'] for o in sells):+.2f}")

def bucket_stats(rows, keyfn, label):
    groups = defaultdict(list)
    for r in rows:
        groups[keyfn(r)].append(r)
    print(f"\n== {label} ==")
    for k in sorted(groups):
        g = groups[k]
        pnl = sum(x["pnl"] for x in g)
        cost = sum(x["notional"] for x in g)
        wr = sum(1 for x in g if x["pnl"] > 0) / len(g)
        print(f"{k:>12} n={len(g):>4} pnl={pnl:+8.2f} roi={(pnl/cost*100 if cost else 0):+7.1f}% win={wr:.2f}")

bucket_stats(buys, lambda r: f"{int(r['t']//15)*15}s", "BUY by entry time since open")
bucket_stats(buys, lambda r: r["role"], "BUY by role")
bucket_stats(buys, lambda r: r["reason"], "BUY by reason")
bucket_stats(buys, lambda r: f"{min(0.9, int(r['price']*10)/10):.1f}-{min(0.9, int(r['price']*10)/10)+0.1:.1f}", "BUY by price bucket")
bucket_stats(sells, lambda r: r["reason"], "SELL by reason")

# Window-level: first-entry timing vs window pnl
first_by_window = {}
for o in sorted(buys, key=lambda x: (x["window_start"], x["t"])):
    first_by_window.setdefault(o["window_start"], o)
win_pnl = defaultdict(float)
for f in fills:
    key = (f["market_id"], f["window_start"])
    w = windows.get(key)
    if not w:
        continue
    won = f["side"] == w["winning_side"]
    action = str(f["action"]).upper()
    price = float(f["price"]) or 0.0
    notional = float(f["notional_usdc"]) or 0.0
    shares = float(f["shares"]) or 0.0
    pnl = ((shares if won else 0.0) - notional) if action == "BUY" else (notional - (shares if not won else 0.0))
    win_pnl[int(f["window_start"])] += pnl

groups = defaultdict(list)
for ws, o in first_by_window.items():
    b = int(o["t"] // 30) * 30
    groups[f"{b}-{b+29}s"].append(win_pnl.get(ws, 0.0))
print("\n== WINDOW PNL by FIRST-ENTRY time bucket ==")
for k in sorted(groups):
    g = groups[k]
    print(f"{k:>10} n={len(g):>4} total={sum(g):+8.2f} avg={sum(g)/len(g):+6.3f} winrate={sum(1 for x in g if x>0)/len(g):.2f}")

# Calibration of logged p_up at BUY decisions vs actual outcomes
dec_path = "logs/decisions.jsonl"
samples = []
with open(dec_path, "r", encoding="utf-8") as fh:
    for line in fh:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") != "DECISION":
            continue
        p = d.get("payload", d)
        if p.get("decision") not in {"BUY_UP", "BUY_DOWN"}:
            continue
        key = (str(p.get("market_id")), int(p.get("window_start")))
        w = windows.get(key)
        if not w:
            continue
        p_up = p.get("p_up")
        if p_up is None:
            continue
        samples.append((float(p_up), 1.0 if w["winning_side"] == "UP" else 0.0, p))
print(f"\ncalibration samples (first BUY decisions with settled outcome): {len(samples)}")
if samples:
    brier = sum((p - y) ** 2 for p, y, _ in samples) / len(samples)
    base = sum(y for _, y, _ in samples) / len(samples)
    print(f"Brier={brier:.4f} (base rate const-pred Brier={base*(1-base)*4/4 + 0:.4f} at p={base:.3f}: {((1-base)**2*base + base**2*(1-base)):.4f})")
    buckets = defaultdict(list)
    for p, y, _ in samples:
        buckets[min(9, int(p * 10))].append(y)
    for b in sorted(buckets):
        ys = buckets[b]
        print(f"p[{b/10:.1f}-{b/10+0.1:.1f}) n={len(ys):>4} mean_p~{(b/10+0.05):.2f} actual_up={sum(ys)/len(ys):.3f}")
