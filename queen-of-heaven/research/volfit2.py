"""Deployed model vs the two sharper candidates from volfit.py: t-stats, and the PnL of the extra windows.
usage: python -I research/scripts/volfit2.py DIR [DIR ...]
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import volfit_lib as V
from tolerance import arrays, C1, L, FEE

CANDS = {"deployed": (900, 1, 1.25), "k1.075": (900, 1, 1.075), "H30": (900, 30, 0.85)}


def trades(arr):
    W, PM, WONUP, SIDEM, ASK = arr
    tmin, tmax, amin, amax, margin, pmin = C1; out = {}
    for i in range(len(W)):
        for t in range(tmin, tmax + 1):
            pu = PM[i, t]
            if np.isnan(pu): continue
            sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
            if pf < pmin: continue
            seen = ASK[i, sd, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = float(seen[-1])
            if not (amin <= a <= amax and pf - a - FEE * a * (1 - a) >= margin): continue
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= a + 0.03 + 1e-9)]
            if not len(nx): continue
            f = float(nx[0]); won = (sd == 0) == WONUP[i]
            out[i] = (1 / f) * won - 1 - FEE * (1 - f); break
    return out


tst = lambda r: r.mean() / (r.std() / math.sqrt(len(r))) if len(r) > 1 else float("nan")
tot = {k: [] for k in CANDS}; extra = {k: [] for k in CANDS}
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d); res = {}
    for name, (vw, h, k) in CANDS.items():
        Z = V.zgrid(d, W, vw, h); m = ~np.isnan(Z[:, 0])
        PM2 = PM.copy(); PM2[:, 230:281] = V.prob(Z, k); PM2[~m] = np.nan
        res[name] = trades((W, PM2, WONUP, np.where(PM2 >= 0.5, 0, 1), ASK))
    base = res["deployed"]
    for name, r in res.items():
        v = np.array(list(r.values())); tot[name].append(v)
        ex = np.array([p for i, p in r.items() if i not in base]); extra[name].append(ex)
        print(f"{d.split('/')[-1]:6s} {name:9s} n={len(v):4d} pnl={v.sum():+6.1f} mean={v.mean():+.4f} t={tst(v):+.2f} | "
              f"windows the deployed model skips: n={len(ex):4d} pnl={ex.sum():+6.1f} mean={ex.mean() if len(ex) else 0:+.4f}", flush=True)
print()
for name in CANDS:
    v = np.concatenate(tot[name]); ex = np.concatenate(extra[name])
    print(f"all {name:9s} n={len(v):4d} pnl={v.sum():+6.1f} mean={v.mean():+.4f} t={tst(v):+.2f} | extra windows n={len(ex)} pnl={ex.sum():+.1f} "
          f"t={tst(ex) if len(ex) > 1 else float('nan'):+.2f}")
