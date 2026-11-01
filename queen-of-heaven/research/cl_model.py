"""Binance-only TWAP model vs a Chainlink-anchored variant, on recorded windows.

Chainlink-anchored: the open is the recorded Chainlink TWAP (the official price to
beat), the locked part of the final average uses Chainlink values already received
by the decision time, and anything not yet received is Binance + the live basis
(median Chainlink[s] - Binance close[s-1] over the last 40s).

Run rec_analysis.py first: it fills CACHE_DIR/klines.json and trades.json (winners).
usage: python cl_model.py REC_DIR SCRIPTS_DIR CACHE_DIR
"""
import glob, gzip, json, math, os, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rec_clock
from bisect import bisect_right
from datetime import datetime
import numpy as np
sys.path.insert(0, sys.argv[2])
from twap_model import phi

rec_dir, cache_dir = sys.argv[1], sys.argv[3]
FEE, VW, K = 0.07, 900, 1.25
CFG = dict(tmin=230, tmax=280, amin=0.60, amax=0.90, margin=0.02, pmin=0.90)  # C1

cl_val, cl_recv, pm = {}, {}, {}
paths = sorted(glob.glob(os.path.join(rec_dir, "*.jsonl.gz"))); offs = rec_clock.offsets(paths)
for path in paths:
    off = offs[path]  # local clock minus real (rec_clock.py)
    try:
        with gzip.open(path, "rt") as fh:
            for line in fh:
                try: r = json.loads(line)
                except json.JSONDecodeError: continue
                if r[0] == "cl":
                    cl_val[r[2]] = r[3]
                    cl_recv[r[2]] = min(r[1] - off, cl_recv.get(r[2], math.inf))  # first receipt (reconnect backfill arrives late)
                elif r[0] == "pm":
                    pm.setdefault(r[2], []).append((r[1] - off, r[3], r[4], r[5], r[6]))
    except (EOFError, OSError, zlib.error):
        pass
for w in pm: pm[w].sort()
kl = json.load(open(os.path.join(cache_dir, "klines.json")))
trades = json.load(open(os.path.join(cache_dir, "trades.json")))
cl_secs = sorted(cl_val)


def close(s):
    while str(s) not in kl: s -= 1
    return kl[str(s)]


def cl_ff(s):
    i = bisect_right(cl_secs, s) - 1
    return cl_val[cl_secs[i]] if i >= 0 else np.nan


def book_ask(w, side, at):
    rows_w = pm.get(w)
    if not rows_w: return np.nan
    i = bisect_right([r[0] for r in rows_w], at) - 1
    if i < 0 or at - rows_w[i][0] > 2.5: return np.nan
    v = rows_w[i][2] if side == 0 else rows_w[i][4]
    return np.nan if v is None else v


def probs(w, t, eps_b=0.01, eps_c=0.01):
    """(p_up Binance model, p_up Chainlink-anchored) for a decision at w+t+1.2s."""
    dec = w + t + 1.2
    d = np.diff([close(s) for s in range(w + t - VW, w + t + 1)]); sig = max(d.std(), 1e-9)
    s_bn = close(w + t)
    known = lambda s: s in cl_recv and cl_recv[s] <= dec
    pts = [cl_val[s] - close(s - 1) for s in range(w + t - 40, w + t + 1) if known(s)]
    if len(pts) < 10: return None
    b = float(np.median(pts))
    out = []
    for o, spot, path, eps in (
            (np.mean([close(s) for s in range(w - 59, w + 1)]), s_bn, lambda s: close(s), eps_b),
            (np.mean([cl_ff(s) for s in range(w - 59, w + 1)]), s_bn + b,
             lambda s: cl_val[s] if known(s) else close(s - 1) + b, eps_c)):
        if t < 240: ef, var = spot, sig ** 2 * ((240 - t) + 20)
        else:
            k = t - 240; acc = np.mean([path(x) for x in range(w + 241, w + t + 1)]) if k else spot; rem = 60 - k
            ef, var = (k * acc + rem * spot) / 60, sig ** 2 * rem ** 3 / (3 * 3600)
        out.append(eps + (1 - 2 * eps) * float(phi(((ef - o) / math.sqrt(var)) / K)))
    return out


wins = [w for w in sorted(pm) if str(w) in trades and trades[str(w)]["winner"]
        and str(w - VW - 100) in kl and str(w + 300) in kl
        and sum(s in cl_val for s in range(w - 59, w + 301)) >= 0.95 * 360]
print(f"windows: {len(wins)}  ({datetime.fromtimestamp(wins[0]):%m-%d %H:%M} .. {datetime.fromtimestamp(wins[-1]):%m-%d %H:%M})")
rows = []  # (w, t, pB, pC, up_won)
for w in wins:
    up = trades[str(w)]["winner"] == "UP"
    for t in range(CFG["tmin"], CFG["tmax"] + 1):
        p = probs(w, t)
        if p: rows.append((w, t, p[0], p[1], up))
R = np.array([(r[2], r[3], r[4]) for r in rows])
y = R[:, 2]
for name, p in (("binance", R[:, 0]), ("chainlink", R[:, 1])):
    ll = -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)); br = np.mean((p - y) ** 2)
    conf = np.maximum(p, 1 - p) >= 0.90; right = (p >= 0.5) == (y == 1)
    print(f"  {name:9s} rows={len(p)} logloss={ll:.4f} brier={br:.4f} | p_side>=0.90: n={conf.sum()} "
          f"mean p={np.maximum(p, 1 - p)[conf].mean():.3f} hit={right[conf].mean():.3f}")
dis = (R[:, 0] >= 0.5) != (R[:, 1] >= 0.5)
print(f"  side disagreements: {dis.sum()} rows; chainlink side right in {((R[dis, 1] >= 0.5) == (y[dis] == 1)).sum()} of them")

# C1 trades with the book (decision at t+1.2s, fill 1.0s later) for each model
for name, j in (("binance", 0), ("chainlink", 1)):
    res = []
    for w in wins:
        up = trades[str(w)]["winner"] == "UP"
        for r in (r for r in rows if r[0] == w):
            pu = r[2 + j]; sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu; dec = w + r[1] + 1.2
            a = book_ask(w, sd, dec)
            if np.isnan(a) or not (CFG["amin"] <= a <= CFG["amax"] and pf >= CFG["pmin"] and pf - a - FEE * a * (1 - a) >= CFG["margin"]): continue
            f = book_ask(w, sd, dec + 1.0)
            if np.isnan(f) or f > a + 0.01: continue
            res.append((w, r[1], sd, f, (sd == 0) == up)); break
    pnl = [(1 / f) * won - 1 - (1 / f) * FEE * f * (1 - f) for *_, f, won in res]
    print(f"  book_1.0s {name:9s} trades={len(res)} win={np.mean([r[4] for r in res]) if res else 0:.0%} pnl/$1={sum(pnl):+.2f}  "
          + " ".join(f"{datetime.fromtimestamp(w):%H:%M}{'U' if sd == 0 else 'D'}@{t}:{f:.2f}{'W' if won else 'L'}" for w, t, sd, f, won in res))


# Spread gate (edge.max_spread 0.06 in the bot; prints-only backtests cannot apply it):
# binance-model C1 trades with no spread gate, marking the ones the gate would have blocked.
def book_row(w, at):
    rows_w = pm.get(w)
    if not rows_w: return None
    i = bisect_right([r[0] for r in rows_w], at) - 1
    return None if i < 0 or at - rows_w[i][0] > 2.5 else rows_w[i]


blocked, kept = [], []
for w in wins:
    up = trades[str(w)]["winner"] == "UP"
    for r in (r for r in rows if r[0] == w):
        pu = r[2]; sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu; dec = w + r[1] + 1.2
        a = book_ask(w, sd, dec)
        if np.isnan(a) or not (CFG["amin"] <= a <= CFG["amax"] and pf >= CFG["pmin"] and pf - a - FEE * a * (1 - a) >= CFG["margin"]): continue
        f = book_ask(w, sd, dec + 0.5)
        if np.isnan(f) or f > a + 0.03: continue
        b = book_row(w, dec)[1 + 2 * sd]
        wide = b is None or a - b > 0.06 + 1e-9
        pnl = (1 / f) * ((sd == 0) == up) - 1 - (1 / f) * FEE * f * (1 - f)
        (blocked if wide else kept).append((w, r[1], sd, a, b, f, pnl)); break
for name, res in (("spread<=0.06", kept), ("spread>0.06", blocked)):
    print(f"  3c/0.5s {name:12s} trades={len(res)} pnl/$1={sum(x[-1] for x in res):+.2f}  "
          + " ".join(f"{datetime.fromtimestamp(w):%H:%M}{'U' if sd == 0 else 'D'}@{t} {b}/{a}:{p:+.2f}" for w, t, sd, a, b, f, p in res if name != "spread<=0.06"))
