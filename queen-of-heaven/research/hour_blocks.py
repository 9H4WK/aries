"""Does the edge depend on the time of day or the weekday? (5m, deployed rule from Oct 23 12:00, capped fills)

Deployed rule: C1, 230-290s, model limit floor 1c, skip setups that stood before 230s (gap 10s).
Six 4-hour UTC blocks and weekday/weekend, per period, V1/V5/V4 at L=3 and L=4. Only a block that is weak in every
period for every model would justify a filter (6 blocks x 3 periods is many cells: expect some noise).
usage (from scratchpad): python -I research/scripts/hour_blocks.py DIR [DIR ...]
"""
import os, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays
import momentum
import volfit_lib as V
from opp_age import conds, fill, BETA, T0, TMIN, TMAX, tstat

GAP = 10


def run(W, P, WONUP, ASK, L, period):
    side, seen_a = conds(P, ASK, L)
    out = []
    for i in range(len(W)):
        for t in range(TMIN, TMAX + 1):
            sd = side[i, t]
            if sd < 0: continue
            s, last = t, t
            while s - 1 >= T0:
                if side[i, s - 1] == sd: s -= 1; last = s
                elif side[i, s - 1] == -1 and last - (s - 1) <= GAP: s -= 1
                else: break
            if last < TMIN: continue
            p = fill(i, t, sd, P, ASK, WONUP, seen_a[i, t], L)
            if p is None: continue
            w = int(W[i]); out.append((p, period, (w // 3600) % 24 // 4, int((w // 86400 + 3) % 7 >= 5))); break  # epoch day 0 = Thursday
    return out


rows = defaultdict(list)
for n, d in enumerate(sys.argv[1:]):
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P1 = momentum.model(d, W, BETA, (1.0,), 300)[1.0]
    P5 = np.full_like(P1, np.nan); P5[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 1, 300), 1.075)
    P4 = np.full_like(P1, np.nan); P4[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 30, 300), 0.85)
    for m, P in (("V1", P1), ("V5", P5), ("V4", P4)):
        for L in (3, 4):
            rows[(m, L)] += run(W, P, WONUP, ASK, L, n)
    print("done", d, flush=True)

dirs = [d.rstrip("/").split("/")[-1] for d in sys.argv[1:]]
print("\ncells: per-trade (n) [period sums " + ",".join(dirs) + "]")
for (m, L), rs in rows.items():
    A = np.array(rs, dtype=float); pnl = A[:, 0]
    print(f"\n{m} L={L}: n={len(A)} pnl={pnl.sum():+.1f} per={pnl.mean():+.4f}")
    groups = [(f"UTC {4 * b:02d}-{4 * b + 4:02d}", A[:, 2] == b) for b in range(6)] + [("weekday", A[:, 3] == 0), ("weekend", A[:, 3] == 1)]
    for name, g in groups:
        y = pnl[g]; per = [y[A[g, 1] == q].sum() for q in range(len(dirs))]
        print(f"  {name:10s} {y.mean():+.4f} ({len(y):4d}) t={tstat(y):+.2f} [" + " ".join(f"{x:+.1f}" for x in per) + "]")
