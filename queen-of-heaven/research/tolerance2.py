"""C1 at L=3: flat limit allowance vs an allowance capped where the edge after fee drops below a floor.

usage (from scratchpad): python -I research/scripts/tolerance2.py DIR [DIR ...]
"""

import json, math, os, pickle, sys
import numpy as np
FEE, L = 0.07, 3
C1 = (230, 280, 0.60, 0.90, 0.02, 0.90)
TOLS = (0.0, 0.01, 0.02, 0.03, 0.05)


def arrays(d):
    W, PM, _, _, _, _, WONUP, SIDEM, _ = pickle.load(open(os.path.join(d, "bt2_cache.pkl"), "rb"))
    pos = {int(w): i for i, w in enumerate(W)}
    ASK = np.full((len(W), 2, 312), np.nan, dtype=np.float32); seen = set()
    for name in ("trades.jsonl", "trades_rev.jsonl"):
        p = os.path.join(d, name)
        if not os.path.exists(p): continue
        for line in open(p):
            r = json.loads(line); w = r["window_start"]; i = pos.get(w)
            if i is None or w in seen: continue
            seen.add(w)
            for t, oi, side, price, sz in r["t"]:
                if side == 1 and 0 <= t < 312 and (np.isnan(ASK[i, oi, t]) or price < ASK[i, oi, t]): ASK[i, oi, t] = price
    return W, PM, WONUP, SIDEM, ASK



VARIANTS = [(tol, floor) for tol in (0.0, 0.02, 0.03, 0.05) for floor in (None, 0.0, 0.01)]


def sim(arr, tol, floor):
    W, PM, WONUP, SIDEM, ASK = arr
    tmin, tmax, amin, amax, margin, pmin = C1
    out = []
    for i in range(len(W)):
        for t in range(tmin, tmax + 1):
            sd = SIDEM[i, t]; pf = PM[i, t] if sd == 0 else 1 - PM[i, t]
            if pf < pmin: continue
            seen = ASK[i, sd, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = float(seen[-1])
            if not (amin <= a <= amax and pf - a - FEE * a * (1 - a) >= margin): continue
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= a + tol + 1e-9)]
            if floor is not None:
                nx = nx[pf - nx - FEE * nx * (1 - nx) >= floor - 1e-9]
            if not len(nx): continue
            f = float(nx[0]); won = (sd == 0) == WONUP[i]
            out.append((1 / f) * won - 1 - (1 / f) * FEE * f * (1 - f)); break
    return np.array(out)


tot = {v: [] for v in VARIANTS}
for d in sys.argv[1:]:
    arr = arrays(d)
    for v in VARIANTS:
        tot[v].append(sim(arr, *v))
    print(d.split("/")[-1], " | ".join(f"{v[0]:.2f}/{v[1]} n={len(tot[v][-1])} {tot[v][-1].sum():+.1f}" for v in VARIANTS), flush=True)
print("\nall periods (tol / edge floor): trades, PnL per $1, per trade, t")
for v, rs in tot.items():
    p = np.concatenate(rs)
    print(f"tol={v[0]:.2f} floor={str(v[1]):5s} n={len(p):5d} pnl={p.sum():+7.1f} mean={p.mean():+.4f} t={p.mean() / (p.std() / math.sqrt(len(p))):+.2f}")
