"""C1 at L=3 (momentum models V1, V5 and V4): flat limit allowances vs a limit set by the model.

Model limit: the highest price q with p_model - q - fee(q) >= FLOOR, never below seen + 3c, at most 0.99.
Fill: the first buy print at or below the limit within 2s after the decision (as tolerance.py), and the
fill must still clear the edge floor when one is set.
usage (from scratchpad): python -I research/scripts/tolerance3.py DIR [DIR ...] [--L 3] [--interval 300] [--v1only]
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, C1, L, FEE
from momentum import model
import volfit_lib as V

BETA = np.array([0.246, 0.088, 0.036])
IV, V1ONLY = 300, "--v1only" in sys.argv
if V1ONLY: sys.argv.remove("--v1only")
if "--L" in sys.argv:
    i = sys.argv.index("--L"); L = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
if "--interval" in sys.argv:
    i = sys.argv.index("--interval"); IV = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
VARIANTS = [("flat", 0.03), ("flat", 0.05), ("flat", 0.08), ("flat", 0.10),
            ("model", 0.0), ("model", 0.01), ("model", 0.02)]


def model_limit(p, floor):
    q = np.arange(0.99, 0.0, -0.01)
    ok = p - q - FEE * q * (1 - q) >= floor - 1e-9
    return float(q[ok][0]) if ok.any() else 0.0


def sim(W, P, WONUP, ASK, kind, x):
    tmin, tmax, amin, amax, margin, pmin = C1
    out = []
    for i in range(len(W)):
        for t in range(tmin, tmax + 1):
            pu = P[i, t]
            if np.isnan(pu): continue
            sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
            if pf < pmin: continue
            seen = ASK[i, sd, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = float(seen[-1])
            if not (amin <= a <= amax and pf - a - FEE * a * (1 - a) >= margin): continue
            lim = a + x if kind == "flat" else min(0.99, max(a + 0.03, model_limit(pf, x)))
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= lim + 1e-9)]
            if kind == "model":
                nx = nx[(nx <= a + 0.03 + 1e-9) | (pf - nx - FEE * nx * (1 - nx) >= x - 1e-9)]
            if not len(nx): continue
            f = float(nx[0]); won = (sd == 0) == WONUP[i]
            c = max(f, a)
            out.append(((1 / f) * won - 1 - FEE * (1 - f), f - a, (1 / c) * won - 1 - FEE * (1 - c))); break
    return np.array(out).reshape(-1, 3)


tot = {(m, v): [] for m in ("V1", "V5", "V4") for v in VARIANTS}
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P1 = model(d, W, BETA, (1.0,), IV)[1.0]
    models = [("V1", P1)]
    if not V1ONLY:
        P5 = np.full_like(P1, np.nan); P5[:, V.T[0]:V.T[-1] + 1] = V.prob(V.zgrid(d, W, 900, 1, IV), 1.075)
        models.append(("V5", P5))
        P4 = np.full_like(P1, np.nan); P4[:, V.T[0]:V.T[-1] + 1] = V.prob(V.zgrid(d, W, 900, 30, IV), 0.85)
        models.append(("V4", P4))
    for m, P in models:
        row = []
        for v in VARIANTS:
            r = sim(W, P, WONUP, ASK, *v); tot[(m, v)].append(r)
            row.append(f"{v[0]}{v[1]:.2f} n={len(r)} {r[:, 0].sum():+.1f} (capped {r[:, 2].sum():+.1f})")
        print(d.split("/")[-1], m, " | ".join(row), flush=True)
print("\nall periods: trades, PnL per $1, per trade, t, mean paid above seen")
for (m, v), rs in tot.items():
    if not rs: continue
    r = np.concatenate(rs); p = r[:, 0]
    print(f"{m} {v[0]:5s} {v[1]:.2f} n={len(p):5d} pnl={p.sum():+7.1f} mean={p.mean():+.4f} t={p.mean() / (p.std() / math.sqrt(len(p))):+.2f} paid={r[:, 1].mean():+.3f} "
          f"capped={r[:, 2].sum():+7.1f} t={r[:, 2].mean() / (r[:, 2].std() / math.sqrt(len(p))):+.2f}")
