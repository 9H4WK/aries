"""C1 (L=3, 3c) with an additive Chainlink-Binance basis variance in the TWAP model (momentum on).

usage (from scratchpad): python -I research/scripts/basis_test.py [--interval 900] DIR [DIR ...]
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, sim
from momentum import model

BETA = np.array([0.246, 0.088, 0.036]); BASES = (0.0, 1.0, 2.0, 3.0)
interval = 300
if "--interval" in sys.argv:
    i = sys.argv.index("--interval"); interval = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
tot = {b: [] for b in BASES}
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d); row = []
    for b in BASES:
        P = model(d, W, BETA, (1.0,), interval, b)[1.0]
        P2 = PM.copy(); P2[:, 230:281] = P[:, 230:281]
        r = sim((W, P2, WONUP, np.where(P2 >= 0.5, 0, 1), ASK), 0.03)[:, 0]; tot[b].append(r)
        row.append(f"B={b:.0f} n={len(r)} {r.sum():+.1f}")
    print(d.split("/")[-1], " | ".join(row), flush=True)
for b, rs in tot.items():
    r = np.concatenate(rs)
    print(f"all B=${b:.0f}: n={len(r)} pnl={r.sum():+.1f} mean={r.mean():+.4f} win={(r > 0).mean():.3f} t={r.mean() / (r.std() / math.sqrt(len(r))):+.2f}")
