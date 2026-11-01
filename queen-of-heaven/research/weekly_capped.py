"""Week-by-week PnL of the deployed 5m rule (C1, 230-290s, model limit floor 1c) with fills capped at the seen ask.

Capped fills are the realistic print score (docs: Fill realism). Models V1/V5/V4, timing L=3 and L=4.
usage (from scratchpad): python -I research/scripts/weekly_capped.py DIR [DIR ...]
"""
import math, os, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, C1, FEE
import momentum
import volfit_lib as V

BETA = np.array([0.246, 0.088, 0.036])
FLOOR, TMAX = 0.01, 290
momentum.T1 = TMAX + 1
V.T = np.arange(230, TMAX + 1)


def model_limit(p):
    q = np.arange(0.99, 0.0, -0.01)
    ok = p - q - FEE * q * (1 - q) >= FLOOR - 1e-9
    return float(q[ok][0]) if ok.any() else 0.0


def sim(W, P, WONUP, ASK, L):
    tmin, _, amin, amax, margin, pmin = C1
    out = []
    for i in range(len(W)):
        for t in range(tmin, TMAX + 1):
            pu = P[i, t]
            if np.isnan(pu): continue
            sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
            if pf < pmin: continue
            seen = ASK[i, sd, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = float(seen[-1])
            if not (amin <= a <= amax and pf - a - FEE * a * (1 - a) >= margin): continue
            lim = min(0.99, max(a + 0.03, model_limit(pf)))
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= lim + 1e-9)]
            if not len(nx): continue
            f = max(float(nx[0]), a); won = (sd == 0) == WONUP[i]
            out.append((int(W[i]), (1 / f) * won - 1 - FEE * (1 - f))); break
    return out


rows = defaultdict(list)  # (model, L) -> [(window, pnl)]
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P1 = momentum.model(d, W, BETA, (1.0,), 300)[1.0]
    P5 = np.full_like(P1, np.nan); P5[:, V.T[0]:V.T[-1] + 1] = V.prob(V.zgrid(d, W, 900, 1, 300), 1.075)
    P4 = np.full_like(P1, np.nan); P4[:, V.T[0]:V.T[-1] + 1] = V.prob(V.zgrid(d, W, 900, 30, 300), 0.85)
    for m, P in (("V1", P1), ("V5", P5), ("V4", P4)):
        for L in (3, 4):
            rows[(m, L)] += sim(W, P, WONUP, ASK, L)
    print("done", d, flush=True)

for (m, L), rs in rows.items():
    wk = defaultdict(list)
    for w, p in rs:
        wk[str(np.datetime64(w, "s").astype("datetime64[W]"))].append(p)
    tot = np.array([p for _, p in rs]); sums = {k: sum(v) for k, v in sorted(wk.items())}
    pos = sum(1 for v in sums.values() if v > 0)
    print(f"\n{m} L={L}: n={len(tot)} pnl={tot.sum():+.1f} per={tot.mean():+.4f} t={tot.mean() / (tot.std() / math.sqrt(len(tot))):+.2f}"
          f" | weeks positive {pos}/{len(sums)}")
    print("  " + " ".join(f"{k[5:]}:{v:+.1f}({len(wk[k])})" for k, v in sums.items()))
