"""C1 (L=3, 3c limit) PnL per week, to look for edge decay.

usage (from scratchpad): python -I research/scripts/weekly.py DIR [DIR ...]
"""
import math, os, sys, time
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, C1, FEE, L


def trades(arr, tol=0.03):
    W, PM, WONUP, SIDEM, ASK = arr
    tmin, tmax, amin, amax, margin, pmin = C1
    for i in range(len(W)):
        for t in range(tmin, tmax + 1):
            sd = SIDEM[i, t]; pf = PM[i, t] if sd == 0 else 1 - PM[i, t]
            if pf < pmin: continue
            seen = ASK[i, sd, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = float(seen[-1])
            if not (amin <= a <= amax and pf - a - FEE * a * (1 - a) >= margin): continue
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= a + tol + 1e-9)]
            if not len(nx): continue
            f = float(nx[0]); won = (sd == 0) == WONUP[i]
            yield int(W[i]), (1 / f) * won - 1 - (1 / f) * FEE * f * (1 - f), pf - a
            break


if __name__ == "__main__":
    by_week = defaultdict(list)
    for d in sys.argv[1:]:
        for w, pnl, edge in trades(arrays(d)):
            by_week[time.strftime("%Y-W%W", time.gmtime(w))].append((pnl, edge))
    print("week      trades  pnl    per-trade  model-edge  win")
    for wk in sorted(by_week):
        r = np.array(by_week[wk])
        print(f"{wk}  {len(r):5d} {r[:, 0].sum():+6.1f}   {r[:, 0].mean():+.3f}     {r[:, 1].mean():.3f}     {(r[:, 0] > 0).mean():.3f}")
