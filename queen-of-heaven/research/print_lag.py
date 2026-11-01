"""Estimate how far data-api trade timestamps lag the live order book (recorded via websocket)."""
import gzip, glob, json, sys, zlib
from bisect import bisect_right
import numpy as np
rec_dir, trades_path = sys.argv[1], sys.argv[2]
pm = {}
for p in sorted(glob.glob(rec_dir + "/*.gz")):
    try:
        for l in gzip.open(p, "rt"):
            r = json.loads(l)
            if r[0] == "pm": pm.setdefault(r[2], []).append(r)
    except (EOFError, OSError, zlib.error):
        pass
trades = json.load(open(trades_path))
deltas = np.arange(-2.0, 6.01, 0.25)
hits = np.zeros(len(deltas)); tot = 0; moving = 0
for w, tw in trades.items():
    w = int(w); rows = pm.get(w)
    if not rows: continue
    ts = [r[1] for r in rows]
    def ask(side, at):
        i = bisect_right(ts, at) - 1
        if i < 0: return None
        return rows[i][4] if side == 0 else rows[i][6]
    for rel, oi, side, price, size in tw["t"]:
        if side != "BUY" or not (5 <= rel <= 295): continue
        vals = [ask(oi, w + rel + 0.5 - d) for d in deltas]
        if any(v is None for v in vals): continue
        if max(vals) - min(vals) < 0.03: continue  # book not moving: uninformative
        moving += 1
        hits += np.array([abs(v - price) <= 0.011 for v in vals])
print(f"informative prints (book moved >=3c across the lag range): {moving}")
for d, h in zip(deltas, hits):
    print(f"  assume print happened {d:+.2f}s before its timestamp: book ask matches print in {h / max(moving, 1):.1%}")
