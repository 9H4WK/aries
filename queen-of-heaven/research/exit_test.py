"""Model-based early exit for C1 (V1 momentum model, L=3, 3c entry limit).

After a C1 fill, at each later second up to 298 the position is sold if the seen bid on our side,
after the taker fee, beats the model probability by MARGIN. The bid comes from prints: taker SELLs
of our outcome, or 1 - taker BUYs of the other outcome (a sell of ours can merge against that ask).
The exit fills at the first bid print >= seen bid - 3c in the next 2s (shifted L), at that print.

usage (from scratchpad): python -I research/scripts/exit_test.py DIR [DIR ...] [--interval 900]
"""
import json, math, os, pickle, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from momentum import legs
from twap_model import phi

FEE, L, TOL = 0.07, 3, 0.03
C1 = (230, 280, 0.60, 0.90, 0.02, 0.90)
BETA = np.array([0.246, 0.088, 0.036])
MARGINS = (None, 0.10, 0.05, 0.02, 0.0)
TMAX = 298
CAP_AT_SEEN = False   # robustness: never sell above the seen bid
SELL_PRINTS_ONLY = False  # robustness: bids only from taker SELLs of our outcome
EXIT_L = L  # robustness: extra print lag for the exit


def load(d):
    W, PM, _, _, _, _, WONUP, SIDEM, _ = pickle.load(open(os.path.join(d, "bt2_cache.pkl"), "rb"))
    pos = {int(w): i for i, w in enumerate(W)}
    ASK = np.full((len(W), 2, 312), np.nan, dtype=np.float32)
    BID = np.full((len(W), 2, 312), np.nan, dtype=np.float32); seen = set()
    for name in ("trades.jsonl", "trades_rev.jsonl"):
        p = os.path.join(d, name)
        if not os.path.exists(p): continue
        for line in open(p):
            r = json.loads(line); w = r["window_start"]; i = pos.get(w)
            if i is None or w in seen: continue
            seen.add(w)
            for t, oi, side, price, sz in r["t"]:
                if not 0 <= t < 312: continue
                if side == 1:
                    if np.isnan(ASK[i, oi, t]) or price < ASK[i, oi, t]: ASK[i, oi, t] = price
                    if SELL_PRINTS_ONLY: continue
                    b, o = 1 - price, 1 - oi
                else:
                    b, o = price, oi
                if np.isnan(BID[i, o, t]) or b > BID[i, o, t]: BID[i, o, t] = b
    return W, WONUP, ASK, BID


def model(d, W, t0=230, t1=TMAX + 1, shift=0):
    # 15m caches use the 5m time axis: second t is t + shift after the window start
    VW, K, EPS = 900, 1.25, 0.01
    b = np.load(os.path.join(d, "binance_1s.npz")); sec, close = b["sec"], b["close"]
    idx = {int(s): i for i, s in enumerate(sec)}
    out = np.full((len(W), 300), np.nan); lock = 240
    for n, w in enumerate(W):
        wi = idx[int(w)]; o = close[wi - 59: wi + 1].mean()
        d1 = np.diff(close[wi + shift - VW - 1: wi + shift + 301])
        csum = np.concatenate([[0], np.cumsum(d1)]); csq = np.concatenate([[0], np.cumsum(d1 * d1)])
        for t in range(t0, t1):
            hi = VW + 1 + t; lo = hi - VW
            m = (csum[hi] - csum[lo]) / VW; sig = math.sqrt(max((csq[hi] - csq[lo]) / VW - m * m, 1e-12))
            s = close[wi + shift + t] + float(legs(close, wi + shift + t) @ BETA)
            if t < lock: ef, var = s, sig ** 2 * ((lock - t) + 20)
            else:
                k = t - lock; acc = close[wi + shift + lock + 1: wi + shift + t + 1].mean() if k else close[wi + shift + t]; rem = 60 - k
                ef, var = (k * acc + rem * s) / 60, sig ** 2 * rem ** 3 / (3 * 3600)
            out[n, t] = EPS + (1 - 2 * EPS) * float(phi((ef - o) / math.sqrt(K * K * var)))
    return out


def latest(a, lo, hi):
    v = a[lo:hi]; v = v[~np.isnan(v)]
    return float(v[-1]) if len(v) else None


def run(W, WONUP, ASK, BID, P):
    tmin, tmax, amin, amax, margin, pmin = C1
    res = {m: [] for m in MARGINS}; exits = {m: [] for m in MARGINS}
    for i in range(len(W)):
        for t in range(tmin, tmax + 1):
            pu = P[i, t]; sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
            if pf < pmin: continue
            a = latest(ASK[i, sd], t - 2 + L, t + 1 + L)
            if a is None or not (amin <= a <= amax and pf - a - FEE * a * (1 - a) >= margin): continue
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= a + TOL + 1e-9)]
            if not len(nx): continue
            f = float(nx[0]); sh = 1 / f; won = (sd == 0) == WONUP[i]
            hold = sh * won - 1 - sh * FEE * f * (1 - f)
            for m in MARGINS:
                pnl, ex = hold, None
                if m is not None:
                    for u in range(t + 3, TMAX + 1):
                        p = P[i, u] if sd == 0 else 1 - P[i, u]
                        b = latest(BID[i, sd], u - 2 + EXIT_L, u + 1 + EXIT_L)
                        if b is None or b - FEE * b * (1 - b) < p + m: continue
                        nb = BID[i, sd, u + 1 + EXIT_L: u + 3 + EXIT_L]; nb = nb[~np.isnan(nb) & (nb >= b - TOL - 1e-9)]
                        if not len(nb): continue
                        s = min(float(nb[0]), b) if CAP_AT_SEEN else float(nb[0]); pnl = sh * (s - FEE * s * (1 - s)) - 1 - sh * FEE * f * (1 - f)
                        ex = (u, s, p, pnl - hold); break
                res[m].append(pnl); exits[m].append(ex)
            break
    return res, exits


if __name__ == "__main__":
    shift = 0
    if "--interval" in sys.argv:
        i = sys.argv.index("--interval"); shift = int(sys.argv[i + 1]) - 300; del sys.argv[i:i + 2]
    tot = {m: [] for m in MARGINS}; allex = {m: [] for m in MARGINS}
    for d in sys.argv[1:]:
        W, WONUP, ASK, BID = load(d); P = model(d, W, shift=shift)
        res, exits = run(W, WONUP, ASK, BID, P)
        row = []
        for m in MARGINS:
            r = np.array(res[m]); tot[m].append(r); ex = [e for e in exits[m] if e]; allex[m] += ex
            row.append(f"{'hold' if m is None else f'm={m:.2f}'} {r.sum():+6.2f} (exits {len(ex)}, gain {sum(e[3] for e in ex):+.2f})")
        print(f"{d.split('/')[-1]:6s} n={len(res[None])}", " | ".join(row), flush=True)
    print("\nall")
    base = np.concatenate(tot[None])
    for m in MARGINS:
        r = np.concatenate(tot[m]); ex = allex[m]
        diff = r - base
        print(f"  {'hold' if m is None else f'm={m:.2f}'}: n={len(r)} pnl={r.sum():+.2f} t={r.mean() / (r.std() / math.sqrt(len(r))):+.2f} "
              f"exits={len(ex)} gain={diff.sum():+.2f} t_gain={diff.mean() / (diff.std() / math.sqrt(len(diff)) + 1e-12):+.2f}")
