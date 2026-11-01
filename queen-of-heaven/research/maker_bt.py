"""Maker (resting bid) variant of Queen under realistic print timing.

At each second t in [tmin, tmax] with model favoured-side prob pf >= pmin, rest a bid at
b = floor((pf - margin) * 100) / 100 (bmin <= b <= bmax). The bid is live from t+1.2s to
t+2.1s real time, i.e. prints labelled t+L+1 (labels lag ~2.25-2.75s). It fills only if
someone trades THROUGH it: a taker SELL of our outcome at <= b - 0.01, or a taker BUY of
the other outcome at >= 1 - b + 0.01. Makers pay no fee. One fill per window.

usage: python maker_bt.py L DIR_A DIR_B
"""
import itertools, json, math, os, pickle, sys
import numpy as np

L = int(sys.argv[1]); dirs = sys.argv[2:4]
T0, T1 = 210, 297


def load(d):
    path = os.path.join(d, "maker_arrays.pkl")
    if os.path.exists(path):
        return pickle.load(open(path, "rb"))
    W, PM, PB, A0, F10, F20, WONUP, SIDEM, SIDEB = pickle.load(open(os.path.join(d, "bt2_cache.pkl"), "rb"))
    pos = {int(w): i for i, w in enumerate(W)}; n = len(W)
    MINSELL = np.full((n, 2, 312), np.nan, dtype=np.float32); MAXBUY = np.full((n, 2, 312), np.nan, dtype=np.float32)
    seen = set()
    for name in ("trades.jsonl", "trades_rev.jsonl"):
        p = os.path.join(d, name)
        if not os.path.exists(p): continue
        for line in open(p):
            r = json.loads(line); w = r["window_start"]; i = pos.get(w)
            if i is None or w in seen: continue
            seen.add(w)
            for t, oi, side, price, sz in r["t"]:
                if not 0 <= t < 312: continue
                if side == -1:
                    cur = MINSELL[i, oi, t]
                    if np.isnan(cur) or price < cur: MINSELL[i, oi, t] = price
                else:
                    cur = MAXBUY[i, oi, t]
                    if np.isnan(cur) or price > cur: MAXBUY[i, oi, t] = price
    out = (W, PM, WONUP, SIDEM, MINSELL, MAXBUY)
    pickle.dump(out, open(path, "wb"))
    return out


def run(cfg, data, detail=False):
    tmin, tmax, margin, pmin, bmin, bmax = cfg
    W, PM, WONUP, SIDE, MINSELL, MAXBUY = data
    n = len(W)
    filled = np.zeros(n, bool); price = np.full(n, np.nan); won = np.zeros(n, bool); tfill = np.zeros(n, int)
    rows = np.arange(n)
    for t in range(max(tmin, T0), min(tmax, T1 - 1) + 1):
        sd = SIDE[:, t]; pu = PM[:, t]
        pf = np.where(sd == 0, pu, 1 - pu)
        b = np.floor((pf - margin) * 100 + 1e-9) / 100
        ok = ~filled & (pf >= pmin) & (b >= bmin) & (b <= bmax)
        s = t + L + 1
        if s >= 312: continue
        ms = MINSELL[rows, sd, s]; mb = MAXBUY[rows, 1 - sd, s]
        with np.errstate(invalid="ignore"):
            hit = ok & ((ms <= b - 0.01 + 1e-9) | (mb >= 1 - b + 0.01 - 1e-9))
        filled |= hit; price = np.where(hit, b, price); won = np.where(hit, (sd == 0) == WONUP, won); tfill = np.where(hit, t, tfill)
    r = np.nonzero(filled)[0]
    fl = price[r]; wn = won[r]
    pnl = (1 / fl) * wn - 1
    if detail: return W[r], tfill[r], fl, wn, pnl
    if len(pnl) < 2: return len(pnl), float(pnl.sum()) if len(pnl) else 0.0, 0.0, 0.0
    return len(pnl), float(pnl.sum()), float(wn.mean()), float(pnl.mean() / (pnl.std() / math.sqrt(len(pnl)) + 1e-9))


if __name__ == "__main__":
    DA, DB = load(dirs[0]), load(dirs[1])
    opts = [(210, 220, 230, 240, 250), (260, 270, 280, 290, 295), (0.01, 0.02, 0.03, 0.05, 0.08, 0.12),
            (0.70, 0.80, 0.85, 0.90, 0.95), (0.30, 0.50, 0.70, 0.80), (0.90, 0.95, 0.98)]
    grid = [g for g in itertools.product(*opts) if g[0] < g[1] and g[4] < g[5]]
    res = {g: (run(g, DA), run(g, DB)) for g in grid}
    fmt = lambda r: f"n={r[0]:4d} pnl={r[1]:+7.2f} win={r[2]:.1%} t={r[3]:.2f}"

    def nb(g, which):
        vals = []
        for j, v in enumerate(g):
            o = opts[j]; k = o.index(v)
            for kk in (k - 1, k + 1):
                if 0 <= kk < len(o):
                    h = list(g); h[j] = o[kk]; h = tuple(h)
                    if h in res: vals.append(res[h][which][3] if res[h][which][0] >= 30 else 0.0)
        return float(np.median(vals)) if vals else 0.0
    print(f"maker backtest L={L}")
    for pick, test, label in ((0, 1, "pick on A -> test on B"), (1, 0, "pick on B -> test on A")):
        cand = sorted(((nb(g, pick), g) for g, r in res.items() if r[pick][0] >= 60), reverse=True)
        print(f"\n{label}:")
        for med, g in cand[:8]:
            print(f"  {g} | pick nbhd t={med:.2f} {fmt(res[g][pick])} || TEST {fmt(res[g][test])}")
    both = sorted(((min(r[0][3], r[1][3]), g) for g, r in res.items() if r[0][0] >= 60 and r[1][0] >= 60), reverse=True)
    print("\nBest by the worse of the two periods (in-sample on both):")
    for m, g in both[:10]:
        print(f"  {g} | A {fmt(res[g][0])} | B {fmt(res[g][1])}")
    json.dump({str(g): r for g, r in res.items()}, open(os.path.join(dirs[0], f"maker_L{L}.json"), "w"))
