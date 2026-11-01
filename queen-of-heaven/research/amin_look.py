"""What the ask floor 0.50 (vs C1's 0.60) adds under the model limit: the windows it changes, by period and model.

Also scores every variant with fills no better than the seen ask (guards against stray low prints).
usage (from scratchpad): python -I research/scripts/amin_look.py DIR [DIR ...] [--L 3]
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
NAMES = ("tmin", "tmax", "amin", "amax", "margin", "pmin")


def model_limit(p):
    q = np.arange(0.99, 0.0, -0.01)
    ok = p - q - FEE * q * (1 - q) >= FLOOR - 1e-9
    return float(q[ok][0]) if ok.any() else 0.0


def sim(W, P, WONUP, ASK, gates):
    tmin, tmax, amin, amax, margin, pmin = gates
    out = {}
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
            out[i] = (t, sd, pf, a, f, bool(won)); break
    return out


def pnl(f, won):
    return (1 / f) * won - 1 - FEE * (1 - f)


lo = tuple(0.50 if n == "amin" else c for n, c in zip(NAMES, C1))
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P1 = model(d, W, BETA, (1.0,), 300)[1.0]
    P5 = np.full_like(P1, np.nan); P5[:, V.T[0]:V.T[-1] + 1] = V.prob(V.zgrid(d, W, 900, 1, 300), 1.075)
    for m, P in (("V1", P1), ("V5", P5)):
        b, x = sim(W, P, WONUP, ASK, C1), sim(W, P, WONUP, ASK, lo)
        rb = sum(pnl(v[4], v[5]) for v in b.values()); rx = sum(pnl(v[4], v[5]) for v in x.values())
        cb = sum(pnl(max(v[4], v[3]), v[5]) for v in b.values()); cx = sum(pnl(max(v[4], v[3]), v[5]) for v in x.values())
        low_b = sum(1 for v in b.values() if v[4] < v[3] - 0.05)
        print(f"\n{d} L={L} {m}: base n={len(b)} {rb:+.1f} (fill>=seen {cb:+.1f}, fills >5c under seen: {low_b}) | amin .50 n={len(x)} {rx:+.1f} (fill>=seen {cx:+.1f})")
        ch = sorted(set(b) ^ set(x) | {i for i in set(b) & set(x) if b[i] != x[i]})
        add = 0.0
        for i in ch:
            ob, ox = b.get(i), x.get(i)
            d_ = (pnl(ox[4], ox[5]) if ox else 0) - (pnl(ob[4], ob[5]) if ob else 0); add += d_
            fmt = lambda v: f"t={v[0]} p={v[2]:.3f} seen={v[3]:.2f} fill={v[4]:.2f} {'W' if v[5] else 'L'}" if v else "-"
            print(f"  w{i:5d} base[{fmt(ob)}] lo[{fmt(ox)}] delta {d_:+.2f}")
        print(f"  changed windows {len(ch)}, delta {add:+.1f}")
