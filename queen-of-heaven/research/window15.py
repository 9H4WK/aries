"""15m entry-window grid on the 5m-axis caches (t = seconds since open - 600), momentum model, 3c, L=3.
usage: python -I research/scripts/window15.py DIR [DIR ...]
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tolerance, momentum
from tolerance import arrays, sim

momentum.T0, momentum.T1 = 170, 296
BETA = np.array([0.246, 0.088, 0.036])
GRID = [(a, b) for a in (170, 200, 215, 230) for b in (270, 280, 290)]
tot = {g: [] for g in GRID}
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P = momentum.model(d, W, BETA, (1.0,), 900)[1.0]
    PM2 = PM.copy(); PM2[:, 170:296] = P[:, 170:296]
    arr = (W, PM2, WONUP, np.where(PM2 >= 0.5, 0, 1), ASK)
    row = []
    for g in GRID:
        tolerance.C1 = (g[0], g[1], 0.60, 0.90, 0.02, 0.90)
        r = sim(arr, 0.03)[:, 0]; tot[g].append(r)
        row.append(f"{g[0] + 600}-{g[1] + 600}: {len(r)} {r.sum():+.1f}")
    print(d.split("/")[-1], " | ".join(row), flush=True)
print()
for g, rs in tot.items():
    r = np.concatenate(rs); c = np.minimum(r, 1.5)
    print(f"{g[0] + 600}-{g[1] + 600}s: n={len(r):4d} pnl={r.sum():+6.1f} t={r.mean() / (r.std() / math.sqrt(len(r))):+.2f} "
          f"capped={c.sum():+6.1f} (t={c.mean() / (c.std() / math.sqrt(len(c))):+.2f}) per dir " + " ".join(f"{x.sum():+5.1f}/{len(x)}" for x in rs))
