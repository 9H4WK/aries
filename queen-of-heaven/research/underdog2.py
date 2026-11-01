"""Robustness of the underdog result: PnL by fill price, at the seen price, and with capped winners.
usage: python -I research/scripts/underdog2.py DIR [DIR ...]
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, L, FEE
from momentum import model

BETA = np.array([0.246, 0.088, 0.036])
AMAX, MARGIN = 0.40, 0.10
rows = []
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P = model(d, W, BETA, (1.0,))[1.0]
    for i in range(len(W)):
        for t in range(230, 281):
            pu = P[i, t]
            if np.isnan(pu): continue
            ud = 1 if pu >= 0.5 else 0; p = 1 - pu if ud == 1 else pu
            seen = ASK[i, ud, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = float(seen[-1])
            if not (0.02 <= a <= AMAX) or p - a - FEE * a * (1 - a) < MARGIN: continue
            nx = ASK[i, ud, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= a + 0.03 + 1e-9)]
            if not len(nx): continue
            f = float(nx[0]); won = (ud == 0) == WONUP[i]
            rows.append((a, f, won, p, d.split("/")[-1])); break
A = np.array([r[:4] for r in rows], dtype=float); a, f, won, p = A.T
pnl = lambda x: (1 / x) * won - 1 - FEE * (1 - x)
pf, pa = pnl(f), pnl(np.maximum(a, f))
t = lambda r: r.mean() / (r.std() / math.sqrt(len(r)))
print(f"n={len(A)} at fill: {pf.sum():+.1f} (t={t(pf):+.2f}) | at max(seen, fill): {pa.sum():+.1f} (t={t(pa):+.2f}) | "
      f"winners capped at +3: {np.minimum(pf, 3).sum():+.1f} (t={t(np.minimum(pf, 3)):+.2f})")
print("by fill price: n, win rate, mean fill, mean model p, pnl, pnl per trade")
for lo, hi in ((0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.45)):
    m = (f >= lo) & (f < hi)
    if m.any():
        print(f"  [{lo:.2f},{hi:.2f}): n={m.sum():5d} win={won[m].mean():.3f} fill={f[m].mean():.3f} model={p[m].mean():.3f} "
              f"pnl={pf[m].sum():+7.1f} per={pf[m].mean():+.3f}")
print("fill below seen by >= 2c:", int((f <= a - 0.02).sum()), "pnl", round(float(pf[f <= a - 0.02].sum()), 1),
      "| others pnl", round(float(pf[f > a - 0.02].sum()), 1))
