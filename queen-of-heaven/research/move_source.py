"""Which move made the setup: Binance (model probability jumped) or Polymarket (ask dropped)? (5m, capped fills)

Deployed rule from Oct 23 12:00: C1, 230-290s, model limit floor 1c, skip setups that stood before 230s (gap 10s).
For each trade: dp = favoured-side model probability now minus K seconds earlier, da = seen ask now minus the
seen ask K seconds earlier (latest print in the 3s lookback, as the entry), K = 3 and 6.
Hypothesis: setups made by a Binance move the market has not caught up with (dp > 0, ask flat) are the edge;
setups made by the ask falling while the model is flat may be informed selling.
usage (from scratchpad): python -I research/scripts/move_source.py DIR [DIR ...]
"""
import os, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays
import momentum
import volfit_lib as V
from opp_age import conds, fill, BETA, T0, TMIN, TMAX, tstat

GAP, KS = 10, (3, 6)


def seen_ask(ASK, i, sd, t, L):
    s = ASK[i, sd, t - 2 + L: t + 1 + L]; s = s[~np.isnan(s)]
    return float(s[-1]) if len(s) else np.nan


def run(W, P, WONUP, ASK, L, period):
    side, seen_a = conds(P, ASK, L)
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
            if last < TMIN: continue
            p = fill(i, t, sd, P, ASK, WONUP, seen_a[i, t], L)
            if p is None: continue
            fav = lambda u: P[i, u] if sd == 0 else 1 - P[i, u]
            row = [p, period]
            for k in KS:
                row += [fav(t) - fav(t - k), seen_a[i, t] - seen_ask(ASK, i, sd, t - k, L)]
            out.append(row); break
    return out


rows = defaultdict(list)
for n, d in enumerate(sys.argv[1:]):
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P1 = momentum.model(d, W, BETA, (1.0,), 300)[1.0]
    P5 = np.full_like(P1, np.nan); P5[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 1, 300), 1.075)
    P4 = np.full_like(P1, np.nan); P4[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 30, 300), 0.85)
    for m, P in (("V1", P1), ("V5", P5), ("V4", P4)):
        for L in (3, 4):
            rows[(m, L)] += run(W, P, WONUP, ASK, L, n)
    print("done", d, flush=True)

dirs = [d.rstrip("/").split("/")[-1] for d in sys.argv[1:]]
print("\ncells: per-trade (n) [period sums " + ",".join(dirs) + "]")
for (m, L), rs in rows.items():
    A = np.array(rs, dtype=float); pnl = A[:, 0]
    print(f"\n{m} L={L}: n={len(A)} pnl={pnl.sum():+.1f} per={pnl.mean():+.4f} t={tstat(pnl):+.2f}")
    for j, k in enumerate(KS):
        dp, da = A[:, 2 + 2 * j], A[:, 3 + 2 * j]
        groups = {
            f"dp<=1pt": dp <= 0.01, f"dp 1-5pt": (dp > 0.01) & (dp <= 0.05), f"dp>5pt": dp > 0.05,
            f"ask fell>=3c": da <= -0.03 + 1e-9, f"ask -2..+2c": (da > -0.03 + 1e-9) & (da < 0.03 - 1e-9),
            f"ask rose>=3c": da >= 0.03 - 1e-9, f"ask unknown": np.isnan(da),
            f"model flat & ask fell": (dp <= 0.01) & (da <= -0.03 + 1e-9),
            f"model up & ask not fell": (dp > 0.01) & ~(da <= -0.03 + 1e-9),
        }
        print(f"  K={k}s")
        for name, g in groups.items():
            y = pnl[g]
            if not len(y): continue
            per = [y[A[g, 1] == q].sum() for q in range(len(dirs))]
            print(f"    {name:24s} {y.mean():+.4f} ({len(y):4d}) t={tstat(y):+.2f} [" + " ".join(f"{x:+.1f}" for x in per) + "]")
