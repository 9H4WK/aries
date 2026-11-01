"""Backtest model-only vs model+market+momentum blend; finer grid with probability floor; stress tests on winners."""
import json, sys, math, itertools, pickle, os
import numpy as np
sys.path.insert(0, sys.argv[4])
from twap_model import phi
FEE = 0.07
binance_path, events_path, trades_path, _, calib_path, feat_path, cache = sys.argv[1:8]
cal = json.load(open(calib_path)); VW, K, EPS, SPLIT = cal["vol_window"], cal["k"], cal["eps"], cal["split"]
fw = np.array(json.load(open(feat_path))["w"])
if os.path.exists(cache):
    W, PM, PB, A, F1, F2, WONUP, SIDEM, SIDEB = pickle.load(open(cache, "rb"))
else:
    b = np.load(binance_path); sec, close = b["sec"], b["close"]
    idx = {int(s): i for i, s in enumerate(sec)}
    winners = {json.loads(l)["window_start"]: json.loads(l)["winner"] for l in open(events_path)}
    logit = lambda p: math.log(min(max(p, 1e-4), 1 - 1e-4) / (1 - min(max(p, 1e-4), 1 - 1e-4)))
    rows = []; seen = set()
    for path in trades_path.split(","):
        for line in open(path):
            r = json.loads(line); w = r["window_start"]
            if w in seen or w not in idx or w - VW - 400 not in idx or w + 300 not in idx or not winners.get(w): continue
            seen.add(w); rows.append((w, r["t"], winners[w] == "UP"))
    n = len(rows)
    W = np.array([r[0] for r in rows]); PM = np.full((n, 300), np.nan); PB = np.full((n, 300), np.nan)
    A = np.full((n, 2, 300), np.nan); F1 = np.full((n, 2, 300), np.nan); F2 = np.full((n, 2, 300), np.nan)
    WONUP = np.array([r[2] for r in rows])
    for i, (w, trades, up) in enumerate(rows):
        wi = idx[w]
        ask = np.full((2, 306), np.nan); mkt = np.full(306, np.nan)
        for t, oi, side, price, sz in trades:
            if side == 1 and 0 <= t <= 305:
                if np.isnan(ask[oi, t]) or price < ask[oi, t]: ask[oi, t] = price
                if t < 300: mkt[t] = price if oi == 0 else 1 - price
        o = close[wi - 59: wi + 1].mean(); last, la = np.nan, -999
        for t in range(300):
            if not np.isnan(mkt[t]): last, la = mkt[t], t
            if t < 210 or t > 296: continue
            now = wi + t; d = np.diff(close[now - VW: now + 1]); sig = max(d.std(), 1e-9); s = close[now]
            if t < 240: ef, var = s, sig**2 * ((240 - t) + 20)
            else:
                k = t - 240; acc = close[wi + 241: now + 1].mean() if k else s; rem = 60 - k
                ef, var = (k * acc + rem * s) / 60, sig**2 * rem**3 / (3 * 3600)
            p = EPS + (1 - 2 * EPS) * float(phi(((ef - o) / math.sqrt(var)) / K)); PM[i, t] = p
            r5 = (close[now] - close[now - 5]) / (sig * math.sqrt(5)); r30 = (close[now] - close[now - 30]) / (sig * math.sqrt(30))
            has_m = (t - la) <= 5
            x = np.array([1.0, logit(p), r5, r30, logit(last) if has_m else 0.0, 0.0 if has_m else 1.0])
            PB[i, t] = 1 / (1 + math.exp(-float(x @ fw)))
            for sd in (0, 1):
                sn = ask[sd, t - 2: t + 1]; sn = sn[~np.isnan(sn)]
                if not len(sn): continue
                a = sn[-1]; A[i, sd, t] = a
                for arr, lo, hi in ((F1, 1, 3), (F2, 2, 5)):
                    nx = ask[sd, t + lo: t + hi]; ok = np.nonzero(~np.isnan(nx) & (nx <= a + 0.01))[0]
                    if len(ok): arr[i, sd, t] = nx[ok[0]]
        if i % 500 == 0: print("precompute", i, n, flush=True)
    SIDEM = np.where(PM >= 0.5, 0, 1); SIDEB = np.where(PB >= 0.5, 0, 1)
    pickle.dump((W, PM, PB, A, F1, F2, WONUP, SIDEM, SIDEB), open(cache, "wb"))
n = len(W); rows_ix = np.arange(n)[:, None]; tt = np.arange(300)

def views(P, SIDE):
    PF = np.where(SIDE == 0, P, 1 - P)
    AF = np.take_along_axis(A, SIDE[:, None, :], 1)[:, 0, :]
    F1F = np.take_along_axis(F1, SIDE[:, None, :], 1)[:, 0, :]; F2F = np.take_along_axis(F2, SIDE[:, None, :], 1)[:, 0, :]
    WON = (SIDE == 0) == WONUP[:, None]
    return PF, AF, F1F, F2F, WON
VIEWS = {"model": views(PM, SIDEM), "blend": views(PB, SIDEB)}

def run(kind, cfg, mask, slow=False, slip=0.0, detail=False):
    tmin, tmax, amin, amax, margin, pmin = cfg
    PF, AF, F1F, F2F, WON = VIEWS[kind]; FF = F2F if slow else F1F
    with np.errstate(invalid="ignore"):
        c = ((tt >= tmin) & (tt <= tmax))[None, :] & (AF >= amin) & (AF <= amax) & (PF >= pmin) & \
            (PF - AF - FEE * AF * (1 - AF) >= margin) & ~np.isnan(FF) & mask[:, None]
    has = c.any(1); first = np.argmax(c, 1); r = np.nonzero(has)[0]; col = first[r]
    fl = np.minimum(FF[r, col] + slip, 0.999); won = WON[r, col]
    pnl = (1 / fl) * won - 1 - (1 / fl) * FEE * fl * (1 - fl)
    if detail: return r, col, fl, won, pnl
    if not len(pnl): return 0, 0.0, 0.0, 0.0
    return len(pnl), float(pnl.sum()), float((pnl > 0).mean()), float(pnl.mean() / (pnl.std() / math.sqrt(len(pnl)) + 1e-9))

tr = W < SPLIT; te = ~tr
grid = list(itertools.product((220, 230, 240, 250), (270, 280, 290), (0.50, 0.60, 0.70, 0.80), (0.90, 0.95, 0.97), (0.0, 0.01, 0.02, 0.03, 0.05, 0.07), (0.0, 0.85, 0.90)))
for kind in ("model", "blend"):
    res = []
    for g in grid:
        n_, tot, wr, ts = run(kind, g, tr)
        if n_ >= 60: res.append((ts, tot, n_, wr, g))
    res.sort(reverse=True); score = {r[4]: r[0] for r in res}
    def nb(g):
        opts = [(220, 230, 240, 250), (270, 280, 290), (0.50, 0.60, 0.70, 0.80), (0.90, 0.95, 0.97), (0.0, 0.01, 0.02, 0.03, 0.05, 0.07), (0.0, 0.85, 0.90)]
        out = []
        for j, v in enumerate(g):
            o = opts[j]; i = o.index(v)
            for k_ in (i - 1, i + 1):
                if 0 <= k_ < len(o): h = list(g); h[j] = o[k_]; out.append(score.get(tuple(h), 0.0))
        return float(np.median(out))
    robust = sorted(((nb(r[4]), r) for r in res[:150]), reverse=True)[:6]
    print(f"\n===== {kind.upper()}: most robust train configs (median t of one-step neighbours) and TEST =====")
    for med, (ts, tot, n_, wr, g) in robust:
        a = run(kind, g, te); s1 = run(kind, g, te, slip=0.01); s2 = run(kind, g, te, slow=True, slip=0.01)
        print(f"  t{g[0]}-{g[1]} ask {g[2]:.2f}-{g[3]:.2f} m{g[4]:.2f} pmin{g[5]:.2f} | nbhd {med:.2f} train n={n_} {tot:+.1f} t={ts:.2f} | TEST n={a[0]} {a[1]:+.1f} win {a[2]:.0%} t={a[3]:.2f} | +1c {s1[1]:+.1f} t={s1[3]:.2f} | slow+1c {s2[1]:+.1f} t={s2[3]:.2f}")
    best = robust[0][1][4]
    r, col, fl, won, pnl = run(kind, best, np.ones(n, bool), detail=True)
    days = {}
    for wi, p_ in zip(W[r], pnl): days.setdefault(int(wi // 86400), []).append(p_)
    eq = np.cumsum(pnl)
    print(f"  BEST {best}: all-14-day per-day pnl " + " ".join(f"{sum(v):+.1f}" for _, v in sorted(days.items())) +
          f" | positive {sum(sum(v) > 0 for v in days.values())}/{len(days)} | maxDD {float((eq - np.maximum.accumulate(eq)).min()):.2f} | trades/day {len(pnl)/len(days):.1f} | avg fill {fl.mean():.3f}")
