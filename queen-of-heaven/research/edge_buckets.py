"""Does anything known at entry predict realized PnL under the deployed 5m rule with capped fills?

Deployed rule: C1, 230-290s, model limit floor 1c, fill no better than the seen ask (docs: Fill realism).
Buckets each trade by model edge after fee, model probability, seen ask and entry second (quintiles),
for V1/V5/V4 at L=3 and L=4, and scores edge-proportional stakes against a flat stake.
usage (from scratchpad): python -I research/scripts/edge_buckets.py DIR [DIR ...]
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
    """One row per trade: window, pnl per $1, model prob, seen ask, edge after fee at the seen ask, second, fill."""
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
            a = float(seen[-1]); edge = pf - a - FEE * a * (1 - a)
            if not (amin <= a <= amax and edge >= margin): continue
            lim = min(0.99, max(a + 0.03, model_limit(pf)))
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= lim + 1e-9)]
            if not len(nx): continue
            f = max(float(nx[0]), a); won = (sd == 0) == WONUP[i]
            out.append((int(W[i]), (1 / f) * won - 1 - FEE * (1 - f), pf, a, edge, t, f)); break
    return out


def tstat(x):
    return x.mean() / (x.std() / math.sqrt(len(x))) if len(x) > 1 and x.std() > 0 else 0.0


rows = defaultdict(list)  # (model, L) -> trade rows
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
    a = np.array(rs); pnl = a[:, 1]
    print(f"\n{m} L={L}: n={len(a)} pnl={pnl.sum():+.1f} per={pnl.mean():+.4f} t={tstat(pnl):+.2f}")
    for name, col in (("edge", 4), ("prob", 2), ("ask", 3), ("second", 5)):
        x = a[:, col]; qs = np.quantile(x, [0.2, 0.4, 0.6, 0.8]); b = np.searchsorted(qs, x, side="right")
        cells = []
        for k in range(5):
            y = pnl[b == k]
            if not len(y): continue  # tied quantiles (many entries at exactly 230s)
            cells.append(f"{x[b == k].min():.3g}-{x[b == k].max():.3g}: {y.mean():+.4f} (n {len(y)}, win {(y > 0).mean():.3f})")
        print(f"  {name:6s} " + " | ".join(cells))
    # Stakes proportional to the computed edge (and its square root), normalized to the same total stake.
    for name, w in (("flat", np.ones(len(a))), ("edge", a[:, 4]), ("sqrt edge", np.sqrt(a[:, 4])),
                    ("kelly", a[:, 4] / (1 - a[:, 3]))):
        w = w / w.mean(); y = w * pnl
        print(f"  stake {name:9s}: pnl={y.sum():+.1f} t={tstat(y):+.2f} max dd={np.max(np.maximum.accumulate(np.cumsum(y)) - np.cumsum(y)):.1f}")
