"""Cross-validated config search for Queen under realistic print timing (shift L).

Pick on period A, test on period B and vice versa; rank by the worse of the two.
usage: python cv_search.py L DIR_A DIR_B   (each DIR has bt2_cache.pkl, trades.jsonl, trades_rev.jsonl)
"""
import itertools, json, math, os, pickle, sys
import numpy as np

FEE = 0.07
T0, T1 = 210, 297  # columns kept
L = int(sys.argv[1]); dirs = sys.argv[2:4]


def load(d):
    arr_path = os.path.join(d, f"shift_L{L}.pkl")
    if os.path.exists(arr_path):
        return pickle.load(open(arr_path, "rb"))
    W, PM, PB, A0, F10, F20, WONUP, SIDEM, SIDEB = pickle.load(open(os.path.join(d, "bt2_cache.pkl"), "rb"))
    pos = {int(w): i for i, w in enumerate(W)}; n = len(W)
    ASK = np.full((n, 2, 312), np.nan, dtype=np.float32); seen = set()
    for name in ("trades.jsonl", "trades_rev.jsonl"):
        p = os.path.join(d, name)
        if not os.path.exists(p): continue
        for line in open(p):
            r = json.loads(line); w = r["window_start"]; i = pos.get(w)
            if i is None or w in seen: continue
            seen.add(w)
            for t, oi, side, price, sz in r["t"]:
                if side == 1 and 0 <= t < 312:
                    cur = ASK[i, oi, t]
                    if np.isnan(cur) or price < cur: ASK[i, oi, t] = price
    A = np.full((n, 2, T1 - T0), np.nan, dtype=np.float32); F = np.full_like(A, np.nan)
    for j, t in enumerate(range(T0, T1)):
        for k in range(t - 2, t + 1):
            v = ASK[:, :, k + L]; A[:, :, j] = np.where(np.isnan(v), A[:, :, j], v)
        for k in range(t + 2, t, -1):
            v = ASK[:, :, k + L]; ok = ~np.isnan(v) & (v <= A[:, :, j] + 0.01); F[:, :, j] = np.where(ok, v, F[:, :, j])
    SIDE = SIDEM[:, T0:T1]
    PF = np.where(SIDE == 0, PM[:, T0:T1], 1 - PM[:, T0:T1]).astype(np.float32)
    AF = np.take_along_axis(A, SIDE[:, None, :], 1)[:, 0, :]
    FF = np.take_along_axis(F, SIDE[:, None, :], 1)[:, 0, :]
    WON = (SIDE == 0) == WONUP[:, None]
    out = (W, PF, AF, FF, WON)
    pickle.dump(out, open(arr_path, "wb"))
    return out


def run(cfg, data, slip=0.0, detail=False):
    tmin, tmax, amin, amax, margin, pmin, maxgap = cfg
    W, PF, AF, FF, WON = data
    tt = np.arange(T0, T1)
    with np.errstate(invalid="ignore"):
        gap = PF - AF - FEE * AF * (1 - AF)
        c = ((tt >= tmin) & (tt <= tmax))[None, :] & (AF >= amin) & (AF <= amax) & (PF >= pmin) & \
            (gap >= margin) & (PF - AF <= maxgap) & ~np.isnan(FF)
    has = c.any(1); first = np.argmax(c, 1); r = np.nonzero(has)[0]; col = first[r]
    fl = np.minimum(FF[r, col].astype(float) + slip, 0.999); won = WON[r, col]
    pnl = (1 / fl) * won - 1 - (1 / fl) * FEE * fl * (1 - fl)
    if detail: return W[r], col + T0, fl, won, pnl
    if len(pnl) < 2: return len(pnl), float(pnl.sum()) if len(pnl) else 0.0, 0.0, 0.0
    return len(pnl), float(pnl.sum()), float((pnl > 0).mean()), float(pnl.mean() / (pnl.std() / math.sqrt(len(pnl)) + 1e-9))


if __name__ == "__main__":
    DA, DB = load(dirs[0]), load(dirs[1])
    print(f"L={L}  A={dirs[0]} ({len(DA[0])} windows)  B={dirs[1]} ({len(DB[0])} windows)", flush=True)
    opts = [(220, 230, 240, 250, 260), (270, 280, 290, 295), (0.30, 0.50, 0.60, 0.70, 0.80), (0.90, 0.95, 0.97),
            (0.0, 0.02, 0.03, 0.05, 0.08), (0.85, 0.90, 0.93, 0.95, 0.97), (0.15, 0.25, 1.0)]
    grid = [g for g in itertools.product(*opts) if g[0] < g[1] and g[2] < g[3]]
    res = {}
    for i, g in enumerate(grid):
        res[g] = (run(g, DA), run(g, DB))
        if i % 3000 == 0: print("grid", i, len(grid), flush=True)

    def nb_min(g, which):
        vals = []
        for j, v in enumerate(g):
            o = opts[j]; k = o.index(v)
            for kk in (k - 1, k + 1):
                if 0 <= kk < len(o):
                    h = list(g); h[j] = o[kk]; h = tuple(h)
                    if h in res: vals.append(res[h][which][3] if res[h][which][0] >= 30 else 0.0)
        return float(np.median(vals)) if vals else 0.0

    fmt = lambda r: f"n={r[0]:3d} pnl={r[1]:+6.2f} win={r[2]:.1%} t={r[3]:.2f}"
    for pick, test, label in ((0, 1, "pick on A -> test on B"), (1, 0, "pick on B -> test on A")):
        cand = [(nb_min(g, pick), g) for g, r in res.items() if r[pick][0] >= 60]
        cand.sort(reverse=True)
        print(f"\n{label} (ranked by median neighbour t on the pick period):")
        for med, g in cand[:8]:
            print(f"  {g} | pick nbhd t={med:.2f} {fmt(res[g][pick])} || TEST {fmt(res[g][test])}")
    both = sorted(((min(r[0][3], r[1][3]), g) for g, r in res.items() if r[0][0] >= 60 and r[1][0] >= 60), reverse=True)
    print("\nBest by the worse of the two periods (in-sample on both, for reference):")
    for m, g in both[:10]:
        print(f"  {g} | A {fmt(res[g][0])} | B {fmt(res[g][1])}")
    frozen = (240, 290, 0.50, 0.90, 0.03, 0.85, 1.0)
    print(f"\nfrozen {frozen}: A {fmt(run(frozen, DA))} | B {fmt(run(frozen, DB))}")
    json.dump({str(g): r for g, r in res.items()}, open(os.path.join(dirs[0], f"cv_L{L}.json"), "w"))
