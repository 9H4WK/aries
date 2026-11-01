"""C1 entry time band under the model-based BUY limit (floor 1c), momentum models V1/V5/V4.

The model arrays are extended to 200-290s (the other scripts compute 230-280 only). Other gates as C1.
Fill as tolerance3.py (model 0.01). Also reports the entry-second histogram for the widest early band.
usage (from scratchpad): python -I research/scripts/time_band.py DIR [DIR ...] [--L 3] [--interval 300] [--flat] [--v1only]
--flat: flat 3c limit instead of the model limit (the 15m bot). Times are on the 5m axis (t + interval - 300).
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, C1, L, FEE
import momentum
import volfit_lib as V

BETA = np.array([0.246, 0.088, 0.036])
if "--L" in sys.argv:
    i = sys.argv.index("--L"); L = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
FLOOR, IV = 0.01, 300
if "--interval" in sys.argv:
    i = sys.argv.index("--interval"); IV = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
FLAT, V1ONLY = "--flat" in sys.argv, "--v1only" in sys.argv
sys.argv = [a for a in sys.argv if a not in ("--flat", "--v1only")]
momentum.T0, momentum.T1 = 200, 291
V.T = np.arange(200, 291)
BANDS = [(230, 280), (200, 280), (210, 280), (220, 280), (225, 280), (230, 285), (230, 290), (220, 285), (215, 270)]


def model_limit(p):
    q = np.arange(0.99, 0.0, -0.01)
    ok = p - q - FEE * q * (1 - q) >= FLOOR - 1e-9
    return float(q[ok][0]) if ok.any() else 0.0


def sim(W, P, WONUP, ASK, tmin, tmax):
    _, _, amin, amax, margin, pmin = C1
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
            lim = a + 0.03 if FLAT else min(0.99, max(a + 0.03, model_limit(pf)))
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= lim + 1e-9)]
            if not FLAT: nx = nx[(nx <= a + 0.03 + 1e-9) | (pf - nx - FEE * nx * (1 - nx) >= FLOOR - 1e-9)]
            if not len(nx): continue
            f = float(nx[0]); won = (sd == 0) == WONUP[i]
            out.append((t, (1 / f) * won - 1 - FEE * (1 - f), (1 / max(f, a)) * won - 1 - FEE * (1 - max(f, a)))); break
    return np.array(out).reshape(-1, 3)


res = {}
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P1 = momentum.model(d, W, BETA, (1.0,), IV)[1.0]; models = [("V1", P1)]
    if not V1ONLY:
        P5 = np.full_like(P1, np.nan); P5[:, V.T[0]:V.T[-1] + 1] = V.prob(V.zgrid(d, W, 900, 1, IV), 1.075)
        P4 = np.full_like(P1, np.nan); P4[:, V.T[0]:V.T[-1] + 1] = V.prob(V.zgrid(d, W, 900, 30, IV), 0.85)
        models += [("V5", P5), ("V4", P4)]
    for m, P in models:
        for b in BANDS:
            res.setdefault((m, b), []).append(sim(W, P, WONUP, ASK, *b))
    print("done", d, flush=True)

dirs = [d.rstrip("/").split("/")[-1] for d in sys.argv[1:]]
print(f"\nL={L} interval={IV} {'flat 3c' if FLAT else 'model limit'}; per period PnL per $1 (" + ", ".join(dirs)
      + "), total, trades, per trade, t, total with fills no better than seen; * = beats base in every period")
for m in (("V1",) if V1ONLY else ("V1", "V5", "V4")):
    base = [r[:, 1].sum() for r in res[(m, BANDS[0])]]
    for b in BANDS:
        rs = res[(m, b)]; r = np.concatenate(rs); p = r[:, 1]; per = [x[:, 1].sum() for x in rs]
        star = "*" if b != BANDS[0] and all(x > y for x, y in zip(per, base)) else " "
        print(f"{m} {b[0]}-{b[1]}{star} " + " ".join(f"{x:+6.1f}" for x in per)
              + f" | {p.sum():+7.1f} n={len(p):5d} {p.mean():+.4f} t={p.mean() / (p.std() / math.sqrt(len(p))):+.2f} | seen-capped {r[:, 2].sum():+7.1f}")
    r = np.concatenate(res[(m, (200, 280))])
    h = [(lo, ((r[:, 0] >= lo) & (r[:, 0] < lo + 10)).sum(), r[(r[:, 0] >= lo) & (r[:, 0] < lo + 10), 1].sum()) for lo in range(200, 290, 10)]
    print(f"{m} 200-280 entries by 10s bucket (n, pnl): " + " ".join(f"{lo}:{n}/{x:+.1f}" for lo, n, x in h))
