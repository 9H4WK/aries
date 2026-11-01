"""15m: are setups that already stood before the entry window weak there too? (capped fills, model limit)

15m caches use the 5m time axis (axis second = real second - 600), so the 830-880s entries are axis 230-280 and the
setup is tracked from real 800s (axis 200). Same rule as opp_age3.py ("no pre", run bridges up to 10s without the
condition, stops at the other side's condition). V1/V5/V4 with interval 900, L=3 and L=4.
usage (from scratchpad): python -I research/scripts/opp_age15.py DIR [DIR ...]
"""
import os, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, C1, FEE
import momentum
import volfit_lib as V
from opp_age import model_limit, tstat

BETA = np.array([0.246, 0.088, 0.036])
T0, TMIN, TMAX, GAP, IV = 200, 230, 280, 10, 900
momentum.T0, momentum.T1 = T0, TMAX + 1
V.T = np.arange(T0, TMAX + 1)


def run(W, P, WONUP, ASK, L, nopre):
    _, _, amin, amax, margin, pmin = C1
    side = np.full((len(W), TMAX + 1), -1, dtype=np.int8); seen_a = np.full((len(W), TMAX + 1), np.nan)
    for i in range(len(W)):
        for t in range(T0, TMAX + 1):
            pu = P[i, t]
            if np.isnan(pu): continue
            sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
            if pf < pmin: continue
            s = ASK[i, sd, t - 2 + L: t + 1 + L]; s = s[~np.isnan(s)]
            if not len(s): continue
            a = float(s[-1])
            if amin <= a <= amax and pf - a - FEE * a * (1 - a) >= margin: side[i, t] = sd; seen_a[i, t] = a
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
            if nopre and last < TMIN: continue
            pu = P[i, t]; pf = pu if sd == 0 else 1 - pu; a = seen_a[i, t]
            lim = min(0.99, max(a + 0.03, model_limit(pf)))
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= lim + 1e-9)]
            if not len(nx): continue
            f = max(float(nx[0]), a); won = (sd == 0) == WONUP[i]
            out.append(((1 / f) * won - 1 - FEE * (1 - f), int(last < TMIN))); break
    return np.array(out).reshape(-1, 2)


res = defaultdict(list)
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P1 = momentum.model(d, W, BETA, (1.0,), IV)[1.0]
    P5 = np.full_like(P1, np.nan); P5[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 1, IV), 1.075)
    P4 = np.full_like(P1, np.nan); P4[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 30, IV), 0.85)
    for m, P in (("V1", P1), ("V5", P5), ("V4", P4)):
        for L in (3, 4):
            for nopre in (False, True):
                res[(m, L, nopre)].append(run(W, P, WONUP, ASK, L, nopre))
    print("done", d, flush=True)

dirs = [d.rstrip("/").split("/")[-1] for d in sys.argv[1:]]
print("\nper period (" + ",".join(dirs) + ") | total n per-trade t")
for m in ("V1", "V5", "V4"):
    for L in (3, 4):
        base = res[(m, L, False)]; a = np.concatenate(base)
        print(f"\n{m} L={L}")
        for c, name in ((0, "fresh"), (1, "pre")):
            per = [r[r[:, 1] == c, 0] for r in base]; y = np.concatenate(per)
            print(f"  cat {name:5s}: n={len(y):4d} per={y.mean() if len(y) else 0:+.4f} t={tstat(y):+.2f} periods " + " ".join(f"{p.sum():+.1f}({len(p)})" for p in per))
        for nopre, name in ((False, "deployed"), (True, "no pre")):
            rs = res[(m, L, nopre)]; y = np.concatenate(rs)[:, 0]
            print(f"  rule {name:8s} " + " ".join(f"{r[:, 0].sum():+6.1f}" for r in rs) + f" | {y.sum():+6.1f} n={len(y):4d} per={y.mean():+.4f} t={tstat(y):+.2f}")
