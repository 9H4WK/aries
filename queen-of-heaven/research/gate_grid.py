"""C1 gates re-checked one at a time under the model-based BUY limit (floor 1c), 5m, momentum models V1/V5/V4.

The gates were tuned with a 1c fill allowance; the model limit fills more of the runaway entries, so the best
time band, ask band, probability and edge gates may have moved. Fill as tolerance3.py (model 0.01).
usage (from scratchpad): python -I research/scripts/gate_grid.py DIR [DIR ...] [--L 3] [--capped]
--capped: fills no better than the seen ask (closer to the recorded books, docs: Fill realism).
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, C1, L, FEE
from momentum import model
import volfit_lib as V

BETA = np.array([0.246, 0.088, 0.036])
if "--L" in sys.argv:
    i = sys.argv.index("--L"); L = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
FLOOR = 0.01
CAPPED = "--capped" in sys.argv
if CAPPED: sys.argv.remove("--capped")
NAMES = ("tmin", "tmax", "amin", "amax", "margin", "pmin")
CHANGES = [("tmin", 200), ("tmin", 215), ("tmin", 245), ("tmax", 270), ("tmax", 285), ("tmax", 290),
           ("amin", 0.50), ("amin", 0.70), ("amax", 0.85), ("amax", 0.93), ("amax", 0.95),
           ("margin", 0.01), ("margin", 0.03), ("margin", 0.04),
           ("pmin", 0.85), ("pmin", 0.88), ("pmin", 0.92), ("pmin", 0.94)]
GATES = [("base", C1)] + [(f"{k}={v}", tuple(v if n == k else c for n, c in zip(NAMES, C1))) for k, v in CHANGES]


def model_limit(p):
    q = np.arange(0.99, 0.0, -0.01)
    ok = p - q - FEE * q * (1 - q) >= FLOOR - 1e-9
    return float(q[ok][0]) if ok.any() else 0.0


def sim(W, P, WONUP, ASK, gates):
    tmin, tmax, amin, amax, margin, pmin = gates
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
            lim = min(0.99, max(a + 0.03, model_limit(pf)))
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= lim + 1e-9)]
            nx = nx[(nx <= a + 0.03 + 1e-9) | (pf - nx - FEE * nx * (1 - nx) >= FLOOR - 1e-9)]
            if not len(nx): continue
            f = float(nx[0]); won = (sd == 0) == WONUP[i]
            if CAPPED: f = max(f, a)
            out.append((1 / f) * won - 1 - FEE * (1 - f)); break
    return np.array(out)


res = {}  # (model, gate) -> list of per-dir arrays
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P1 = model(d, W, BETA, (1.0,), 300)[1.0]
    P5 = np.full_like(P1, np.nan); P5[:, V.T[0]:V.T[-1] + 1] = V.prob(V.zgrid(d, W, 900, 1, 300), 1.075)
    P4 = np.full_like(P1, np.nan); P4[:, V.T[0]:V.T[-1] + 1] = V.prob(V.zgrid(d, W, 900, 30, 300), 0.85)
    for m, P in (("V1", P1), ("V5", P5), ("V4", P4)):
        for g, gates in GATES:
            res.setdefault((m, g), []).append(sim(W, P, WONUP, ASK, gates))
    print("done", d, flush=True)

dirs = [d.rstrip("/").split("/")[-1] for d in sys.argv[1:]]
print(f"\nL={L}; per period PnL per $1 (" + ", ".join(dirs) + "), total, trades, per trade, t; * = beats base in every period")
for m in ("V1", "V5", "V4"):
    base = [r.sum() for r in res[(m, "base")]]
    for g, _ in GATES:
        rs = res[(m, g)]; p = np.concatenate(rs); per = [r.sum() for r in rs]
        star = "*" if g != "base" and all(a > b for a, b in zip(per, base)) else " "
        t = p.mean() / (p.std() / math.sqrt(len(p))) if len(p) > 1 else 0
        print(f"{m} {g:12s}{star} " + " ".join(f"{x:+6.1f}" for x in per) + f" | {p.sum():+7.1f} n={len(p):5d} {p.mean():+.4f} t={t:+.2f}")
