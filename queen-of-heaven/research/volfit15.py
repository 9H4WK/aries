"""V1 (deployed) vs V4 (30s-change vol, k 0.85) vs V5 (k 1.075) on 15m caches, C1 830-880s, 3c, L=3.
usage: python -I research/scripts/volfit15.py DIR [DIR ...] [--interval 900]  (14400 for the 4h caches)
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import volfit_lib as V
from tolerance import arrays, sim

IV = 900
if "--interval" in sys.argv:
    i = sys.argv.index("--interval"); IV = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
CANDS = {"V1": (900, 1, 1.25), "V5": (900, 1, 1.075), "V4": (900, 30, 0.85)}
tot = {k: [] for k in CANDS}
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d); row = []
    for name, (vw, h, k) in CANDS.items():
        Z = V.zgrid(d, W, vw, h, IV); m = ~np.isnan(Z[:, 0])
        PM2 = PM.copy(); PM2[:, 230:281] = V.prob(Z, k); PM2[~m] = np.nan
        r = sim((W, PM2, WONUP, np.where(PM2 >= 0.5, 0, 1), ASK), 0.03)[:, 0]; tot[name].append(r)
        row.append(f"{name} n={len(r)} {r.sum():+.1f}")
    print(d.split("/")[-1], " | ".join(row), flush=True)
for name, rs in tot.items():
    r = np.concatenate(rs); c = np.minimum(r, 1.5)
    print(f"all {name}: n={len(r)} pnl={r.sum():+.1f} t={r.mean() / (r.std() / math.sqrt(len(r))):+.2f} capped={c.sum():+.1f} (t={c.mean() / (c.std() / math.sqrt(len(c))):+.2f})")
