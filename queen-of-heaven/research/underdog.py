"""Buy the underdog when the model gives it more than the market does (the reverse of C1).

Same fill model as tolerance.py (L=3, first print <= seen + 3c within 2s), deployed momentum model.
For each window, the first second in TMIN-TMAX where the underdog's (model p < 0.5) print price a
satisfies AMIN <= a <= AMAX and p_u - a - fee >= MARGIN. Also prints calibration: realized win rate
of the underdog vs model and market when the market is more confident than the model.
usage: python -I research/scripts/underdog.py DIR [DIR ...]
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, L, FEE
from momentum import model

BETA = np.array([0.246, 0.088, 0.036])
GRID = [(tmin, amax, margin) for tmin in (230, 150) for amax in (0.15, 0.40) for margin in (0.02, 0.05, 0.10)]
res = {g: [] for g in GRID}; cal = []
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P = model(d, W, BETA, (1.0,))[1.0]
    per = {g: [] for g in GRID}
    for i in range(len(W)):
        done = set()
        for t in range(150, 281):
            pu = P[i, t]
            if np.isnan(pu): continue
            ud = 1 if pu >= 0.5 else 0; p = 1 - pu if ud == 1 else pu   # underdog side and its model p
            seen = ASK[i, ud, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = float(seen[-1]); won = (ud == 0) == WONUP[i]
            if t >= 230 and t % 5 == 0 and a < 0.5: cal.append((p, a, won))
            nx = ASK[i, ud, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= a + 0.03 + 1e-9)]
            for g in GRID:
                tmin, amax, margin = g
                if g in done or t < tmin or not (0.02 <= a <= amax) or p - a - FEE * a * (1 - a) < margin or not len(nx): continue
                f = float(nx[0]); done.add(g)
                per[g].append(((1 / f) * won - 1 - (1 / f) * FEE * f * (1 - f), won, p, f))
    for g in GRID:
        res[g].append(per[g])
    print(d.split("/")[-1], " | ".join(f"{g}: n={len(per[g])} pnl={sum(x[0] for x in per[g]):+.1f}" for g in GRID[:6]), flush=True)

print("\nunderdog trades, all dirs (per $1)")
for g in GRID:
    rows = [np.array(r, dtype=float).reshape(-1, 4) for r in res[g]]
    A = np.concatenate(rows); r = A[:, 0]
    t = r.mean() / (r.std() / math.sqrt(len(r))) if len(r) > 1 and r.std() > 0 else 0
    per = " ".join(f"{x[:, 0].sum():+6.1f}/{len(x)}" for x in rows)
    print(f"  tmin={g[0]} amax={g[1]:.2f} margin={g[2]:.2f}: n={len(r):4d} pnl={r.sum():+7.2f} t={t:+.2f} win={A[:, 1].mean():.3f} "
          f"model_p={A[:, 2].mean():.3f} fill={A[:, 3].mean():.3f} | per dir {per}")

C = np.array(cal, dtype=float)
print("\nunderdog calibration (230-280s, every 5th second, underdog price < 0.5), by model p minus market price")
for lo, hi in ((-1, -0.10), (-0.10, -0.03), (-0.03, 0.0), (0.0, 0.03), (0.03, 0.10), (0.10, 1)):
    m = (C[:, 0] - C[:, 1] >= lo) & (C[:, 0] - C[:, 1] < hi)
    if m.sum() < 30: continue
    print(f"  gap [{lo:+.2f},{hi:+.2f}): n={m.sum():6d} model={C[m, 0].mean():.4f} market={C[m, 1].mean():.4f} realized={C[m, 2].mean():.4f}")
