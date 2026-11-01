import json, sys, math, pickle
import numpy as np
FEE = 0.07
W, PM, PB, A, F1, F2, WONUP, SIDEM, SIDEB = pickle.load(open(sys.argv[1], "rb"))
SPLIT = json.load(open(sys.argv[2]))["split"]
n = len(W); tt = np.arange(300)
PF = np.where(SIDEM == 0, PM, 1 - PM)
AF = np.take_along_axis(A, SIDEM[:, None, :], 1)[:, 0, :]; FF = np.take_along_axis(F1, SIDEM[:, None, :], 1)[:, 0, :]
WON = (SIDEM == 0) == WONUP[:, None]
def cond(cfg):
    tmin, tmax, amin, amax, margin, pmin = cfg
    with np.errstate(invalid="ignore"):
        return ((tt >= tmin) & (tt <= tmax))[None, :] & (AF >= amin) & (AF <= amax) & (PF >= pmin) & \
               (PF - AF - FEE * AF * (1 - AF) >= margin) & ~np.isnan(FF)
def trades(cfg, max_entries=1, spacing=10):
    c = cond(cfg); out = []
    for i in range(n):
        cols = np.nonzero(c[i])[0]; taken = []
        for t in cols:
            if len(taken) >= max_entries: break
            if taken and t - taken[-1] < spacing: continue
            taken.append(t)
            f = FF[i, t]; won = WON[i, t]
            out.append((i, t, f, won, (1 / f) * won - 1 - (1 / f) * FEE * f * (1 - f)))
    return out
def stats(rows, label):
    if not rows: print(label, "none"); return
    p = np.array([r[4] for r in rows]); te = np.array([W[r[0]] >= SPLIT for r in rows])
    days = {}
    for r in rows: days.setdefault(int(W[r[0]] // 86400), []).append(r[4])
    eq = np.cumsum(p)
    def t_(x): return x.mean() / (x.std() / math.sqrt(len(x)) + 1e-9) if len(x) > 1 else 0
    print(f"{label}: all n={len(p)} pnl {p.sum():+.1f} | TEST n={te.sum()} pnl {p[te].sum():+.1f} t={t_(p[te]):.2f} win {np.mean(p[te] > 0):.0%} | "
          f"days+ {sum(sum(v) > 0 for v in days.values())}/{len(days)} worst day {min(sum(v) for v in days.values()):+.1f} maxDD {float((eq - np.maximum.accumulate(eq)).min()):.1f}")
    return p
cfg = (240, 290, 0.50, 0.90, 0.03, 0.85)
rows = trades(cfg)
stats(rows, "CHOSEN 1 entry ")
print("per-day:", " ".join(f"{sum(r[4] for r in rows if int(W[r[0]] // 86400) == d):+.1f}" for d in sorted({int(W[r[0]] // 86400) for r in rows})))
print("by ask bucket:")
for lo, hi in [(.5, .6), (.6, .7), (.7, .8), (.8, .85), (.85, .9), (.9, .91)]:
    s = [r for r in rows if lo <= r[2] < hi]
    if s: p = np.array([r[4] for r in s]); print(f"  fill {lo:.2f}-{hi:.2f}: n={len(s)} pnl {p.sum():+.1f} per-trade {p.mean():+.3f} win {np.mean([r[3] for r in s]):.0%}")
print("by UTC hour block:")
for h0 in range(0, 24, 4):
    s = [r for r in rows if h0 <= (W[r[0]] % 86400) // 3600 < h0 + 4]
    if s: p = np.array([r[4] for r in s]); print(f"  {h0:02d}-{h0+4:02d}h: n={len(s)} pnl {p.sum():+.1f} win {np.mean([r[3] for r in s]):.0%}")
print("scale-in variants:")
for me, sp in ((2, 10), (3, 10), (3, 5)):
    stats(trades(cfg, me, sp), f"  up to {me} entries, >= {sp}s apart")
print("probability floor sweep (1 entry):")
for pmin in (0.80, 0.85, 0.88, 0.90, 0.93, 0.95):
    stats(trades((240, 290, 0.50, 0.90, 0.03, pmin)), f"  pmin {pmin:.2f}")
print("margin sweep at pmin .85:")
for m in (0.0, 0.02, 0.03, 0.05, 0.08, 0.10):
    stats(trades((240, 290, 0.50, 0.90, m, 0.85)), f"  margin {m:.2f}")
