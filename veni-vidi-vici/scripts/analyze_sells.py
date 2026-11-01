import json
import sqlite3
from collections import defaultdict

conn = sqlite3.connect("aries.db")
conn.row_factory = sqlite3.Row

windows = {}
for row in conn.execute("select market_id, window_start, winning_side from window_results where resolved=1"):
    windows[(row["market_id"], row["window_start"])] = dict(row)

fills = [
    dict(r)
    for r in conn.execute(
        "select market_id, window_start, side, action, price, notional_usdc, shares, created_at, metadata_json from paper_fills"
    )
]


def meta(row):
    try:
        return json.loads(row.get("metadata_json") or "{}")
    except Exception:
        return {}


obs = []
for f in fills:
    w = windows.get((f["market_id"], f["window_start"]))
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
        pnl = notional - (shares if won else 0.0)  # CORRECT sell formula
    m = meta(f)
    cand = m.get("candidate", {}) or {}
    obs.append(
        {
            "pnl": pnl,
            "action": action,
            "side": f["side"],
            "price": price,
            "notional": notional,
            "role": cand.get("role") or "?",
            "reason": m.get("strategy_reason") or "?",
            "t": float(f["created_at"]) - int(f["window_start"]),
            "won": won,
            "window_start": int(f["window_start"]),
        }
    )

buys = [o for o in obs if o["action"] == "BUY"]
sells = [o for o in obs if o["action"] == "SELL"]
print(f"buys total {sum(o['pnl'] for o in buys):+.2f} | sells total {sum(o['pnl'] for o in sells):+.2f}")
print("\nSELL detail:")
for o in sells:
    print(
        f"  t={o['t']:.0f}s side={o['side']} won={o['won']} px={o['price']:.2f} "
        f"notional={o['notional']:.2f} pnl={o['pnl']:+.2f} reason={o['reason']}"
    )

wp = defaultdict(float)
for f in fills:
    w = windows.get((f["market_id"], f["window_start"]))
    if not w:
        continue
    won = f["side"] == w["winning_side"]
    a = str(f["action"]).upper()
    n = float(f["notional_usdc"]) or 0
    s = float(f["shares"]) or 0
    wp[int(f["window_start"])] += ((s if won else 0) - n) if a == "BUY" else (n - (s if won else 0))
tot = sum(wp.values())
print(f"\nreconciled total realized: {tot:+.2f} over {len(wp)} windows")

skip = sum(o["pnl"] for o in buys if o["reason"] == "deadline_best_mispriced_side")
print(f"pnl from deadline-forced buys: {skip:+.2f}; without them total would be {tot - skip:+.2f}")
dl = [o for o in buys if o["reason"] == "deadline_best_mispriced_side"]
hi = [o for o in dl if o["price"] >= 0.5]
lo = [o for o in dl if o["price"] < 0.5]
print(f"deadline buys ask>=0.50: pnl={sum(o['pnl'] for o in hi):+.2f} n={len(hi)} win={sum(1 for o in hi if o['pnl']>0)/max(1,len(hi)):.2f}")
print(f"deadline buys ask<0.50 : pnl={sum(o['pnl'] for o in lo):+.2f} n={len(lo)} win={sum(1 for o in lo if o['pnl']>0)/max(1,len(lo)):.2f}")

# Simulate longshot floor on ALL buys
for floor in (0.20, 0.25, 0.30):
    kept = [o for o in buys if o["price"] >= floor]
    print(f"floor ask>={floor}: buy pnl={sum(o['pnl'] for o in kept):+.2f} (n={len(kept)} vs {len(buys)})")

# Repair-only mode: keep repairs/locks/recovery, drop all first entries entirely
rep = [o for o in buys if o["role"] != "FIRST_ENTRY"]
print(f"repairs+locks only: pnl={sum(o['pnl'] for o in rep):+.2f} n={len(rep)}")
