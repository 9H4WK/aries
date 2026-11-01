"""Are setups that already held before the entry second weaker than fresh ones? (5m, deployed rule, capped fills)

edge_buckets.py found entries at 230-238s make <1c per trade and later ones 5-7c. Hypothesis: a setup that has
held for a while is the market disagreeing with the model on purpose, a fresh one is the market lagging Binance.
Model arrays are extended to 200-290s so the setup can be checked before 230s. cond(s) = the C1 entry condition
at second s on the same side (p >= 0.90, seen ask 0.60-0.90, edge after fee >= 2c).
Part 1: deployed-rule trades bucketed by age (consecutive earlier seconds with cond) and by cond anywhere in the
previous 30s. Part 2: rule "enter only if cond did not hold in any of the previous K seconds" per period.
usage (from scratchpad): python -I research/scripts/opp_age.py DIR [DIR ...]
"""
import math, os, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, C1, FEE
import momentum
import volfit_lib as V

BETA = np.array([0.246, 0.088, 0.036])
FLOOR, T0, TMIN, TMAX = 0.01, 200, 230, 290
momentum.T0, momentum.T1 = T0, TMAX + 1
V.T = np.arange(T0, TMAX + 1)
KS = (1, 3, 5, 10, 20, 30)
AGE_BINS = ((0, 0), (1, 2), (3, 5), (6, 10), (11, 99))


def model_limit(p):
    q = np.arange(0.99, 0.0, -0.01)
    ok = p - q - FEE * q * (1 - q) >= FLOOR - 1e-9
    return float(q[ok][0]) if ok.any() else 0.0


def conds(P, ASK, L):
    """Per window and second T0..TMAX: side (0 up, 1 down) if the C1 condition holds there, else -1; and the seen ask."""
    _, _, amin, amax, margin, pmin = C1
    n = P.shape[0]; side = np.full((n, TMAX + 1), -1, dtype=np.int8); seen_a = np.full((n, TMAX + 1), np.nan)
    for i in range(n):
        for t in range(T0, TMAX + 1):
            pu = P[i, t]
            if np.isnan(pu): continue
            sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
            if pf < pmin: continue
            seen = ASK[i, sd, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = float(seen[-1])
            if amin <= a <= amax and pf - a - FEE * a * (1 - a) >= margin:
                side[i, t] = sd; seen_a[i, t] = a
    return side, seen_a


def fill(i, t, sd, P, ASK, WONUP, a, L):
    pu = P[i, t]; pf = pu if sd == 0 else 1 - pu
    lim = min(0.99, max(a + 0.03, model_limit(pf)))
    nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= lim + 1e-9)]
    if not len(nx): return None
    f = max(float(nx[0]), a); won = (sd == 0) == WONUP[i]
    return (1 / f) * won - 1 - FEE * (1 - f)


def run(W, P, WONUP, ASK, L, K):
    """K None: deployed rule, rows (pnl, age, held in previous 30s). Else: skip a second if cond held in the previous K."""
    side, seen_a = conds(P, ASK, L)
    out = []
    for i in range(len(W)):
        for t in range(TMIN, TMAX + 1):
            sd = side[i, t]
            if sd < 0: continue
            prev = side[i, t - 30:t] == sd
            if K is not None and prev[-K:].any(): continue
            p = fill(i, t, sd, P, ASK, WONUP, seen_a[i, t], L)
            if p is None: continue
            age = 0
            while age < 30 and prev[-1 - age]: age += 1
            out.append((p, age, prev.any())); break
    return np.array(out).reshape(-1, 3)


def tstat(x):
    return x.mean() / (x.std() / math.sqrt(len(x))) if len(x) > 1 and x.std() > 0 else 0.0


def main():
    res = defaultdict(list)  # (model, L, K) -> per-dir arrays
    for d in sys.argv[1:]:
        W, PM, WONUP, SIDEM, ASK = arrays(d)
        P1 = momentum.model(d, W, BETA, (1.0,), 300)[1.0]
        P5 = np.full_like(P1, np.nan); P5[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 1, 300), 1.075)
        P4 = np.full_like(P1, np.nan); P4[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 30, 300), 0.85)
        for m, P in (("V1", P1), ("V5", P5), ("V4", P4)):
            for L in (3, 4):
                for K in (None,) + KS:
                    res[(m, L, K)].append(run(W, P, WONUP, ASK, L, K))
        print("done", d, flush=True)

    dirs = [d.rstrip("/").split("/")[-1] for d in sys.argv[1:]]
    for m in ("V1", "V5", "V4"):
        for L in (3, 4):
            base = res[(m, L, None)]; a = np.concatenate(base)
            print(f"\n{m} L={L} deployed: n={len(a)} pnl={a[:, 0].sum():+.1f} per={a[:, 0].mean():+.4f} t={tstat(a[:, 0]):+.2f}")
            cells = []
            for lo, hi in AGE_BINS:
                y = a[(a[:, 1] >= lo) & (a[:, 1] <= hi), 0]
                cells.append(f"age {lo}-{hi}: {y.mean() if len(y) else 0:+.4f} n {len(y)}")
            print("  " + " | ".join(cells))
            for flag, name in ((0, "fresh in 30s"), (1, "held in 30s")):
                per = [r[r[:, 2] == flag, 0] for r in base]; y = np.concatenate(per)
                print(f"  {name}: n={len(y)} per={y.mean() if len(y) else 0:+.4f} t={tstat(y):+.2f} periods " + " ".join(f"{p.sum():+.1f}({len(p)})" for p in per))
            bper = [r[:, 0].sum() for r in base]
            print("  rule (" + ",".join(dirs) + "): base " + " ".join(f"{x:+.1f}" for x in bper))
            for K in KS:
                rs = res[(m, L, K)]; y = np.concatenate(rs)[:, 0]; per = [r[:, 0].sum() for r in rs]
                star = "*" if all(x > b for x, b in zip(per, bper)) else " "
                print(f"  skip if held in prev {K:2d}s{star} " + " ".join(f"{x:+.1f}" for x in per) + f" | {y.sum():+.1f} n={len(y)} per={y.mean():+.4f} t={tstat(y):+.2f}")


if __name__ == "__main__":
    main()
