"""C1 at L=3 with different limit allowances above the seen ask (fill at the first print <= limit within 2s).

usage (from scratchpad): python -I research/scripts/tolerance.py DIR [DIR ...]
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


def sim(arr, tol):
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
            if not len(nx): continue
            f = float(nx[0]); won = (sd == 0) == WONUP[i]
            out.append(((1 / f) * won - 1 - (1 / f) * FEE * f * (1 - f), f - a)); break
    return np.array(out).reshape(-1, 2)


if __name__ == "__main__":
    tot = {tol: [] for tol in TOLS}
    for d in sys.argv[1:]:
        arr = arrays(d); row = []
        for tol in TOLS:
            r = sim(arr, tol); tot[tol].append(r); p = r[:, 0]
            row.append(f"tol={tol:.2f} n={len(p):4d} pnl={p.sum():+6.2f} t={p.mean() / (p.std() / math.sqrt(len(p))):+.2f} paid={r[:, 1].mean():+.3f}")
        print(d.split("/")[-1], " | ".join(row), flush=True)
    print("all", " | ".join(f"tol={tol:.2f} n={len(np.concatenate(v)):4d} pnl={np.concatenate(v)[:, 0].sum():+6.2f} "
                            f"t={np.concatenate(v)[:, 0].mean() / (np.concatenate(v)[:, 0].std() / math.sqrt(len(np.concatenate(v)))):+.2f}" for tol, v in tot.items()))
