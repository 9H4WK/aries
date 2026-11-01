"""Underdog buys (and C1 for comparison) on recorded PM books, with the deployed momentum model.

Decide at w+t+1.2 on the recorded book ask, fill 0.5s later at the book ask if it is within 3c.
Also: calibration of the book's underdog ask/mid vs realized outcomes, by model-minus-market gap.
Run rec_analysis.py first (fills CACHE_DIR/klines.json and trades.json).
usage: python -I rec_underdog.py REC_DIR SCRIPTS_DIR CACHE_DIR
"""
import glob, gzip, json, math, os, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rec_clock
from bisect import bisect_right
import numpy as np
sys.path.insert(0, sys.argv[2])
from twap_model import phi

rec_dir, cache_dir = sys.argv[1], sys.argv[3]
FEE, VW, K, EPS = 0.07, 900, 1.25, 0.01
BETA = (0.246, 0.088, 0.036); LAGS = ((0, 2), (2, 5), (5, 10))
pm = {}
paths = sorted(glob.glob(os.path.join(rec_dir, "*.jsonl.gz"))); offs = rec_clock.offsets(paths)
for path in paths:
    off = offs[path]  # local clock minus real (rec_clock.py)
    try:
        with gzip.open(path, "rt") as fh:
            for line in fh:
                if not line.startswith('["pm"'): continue
                try: r = json.loads(line)
                except json.JSONDecodeError: continue
                pm.setdefault(r[2], []).append((r[1] - off, r[3], r[4], r[5], r[6]))
    except (EOFError, OSError, zlib.error):
        pass
for w in pm: pm[w].sort()
kl = json.load(open(os.path.join(cache_dir, "klines.json")))
trades = json.load(open(os.path.join(cache_dir, "trades.json")))


def close(s):
    while str(s) not in kl: s -= 1
    return kl[str(s)]


def model_p(w, t):
    o = np.mean([close(s) for s in range(w - 59, w + 1)])
    c = [close(s) for s in range(w + t - VW, w + t + 1)]
    sig = max(np.diff(c).std(), 1e-9); s = c[-1]
    drift = sum(b * (c[-1 - a] - c[-1 - bb]) for b, (a, bb) in zip(BETA, LAGS)); sd = s + drift
    if t < 240: ef, var = sd, sig ** 2 * ((240 - t) + 20)
    else:
        k = t - 240; acc = np.mean([close(x) for x in range(w + 241, w + t + 1)]) if k else s; rem = 60 - k
        ef, var = (k * acc + rem * sd) / 60, sig ** 2 * rem ** 3 / (3 * 3600)
    return EPS + (1 - 2 * EPS) * float(phi((ef - o) / math.sqrt(K * K * var)))


def book(w, at):
    rows_w = pm.get(w)
    if not rows_w: return None
    i = bisect_right([r[0] for r in rows_w], at) - 1
    if i < 0 or at - rows_w[i][0] > 2.5: return None
    return rows_w[i]


def ask(w, side, at):
    r = book(w, at); v = None if r is None else (r[2] if side == 0 else r[4])
    return np.nan if v is None else v


RULES = {"C1": ("fav", 0.60, 0.90, 0.02, 0.90)} | {f"dog a<={amax} m>={m}": ("dog", 0.02, amax, m, 0.0)
                                                  for amax in (0.15, 0.40) for m in (0.05, 0.10)}
res = {k: [] for k in RULES}; cal = []; n_w = 0
for w in sorted(pm):
    tw = trades.get(str(w))
    if not tw or tw["winner"] is None or str(w - VW - 100) not in kl: continue
    n_w += 1; up_won = tw["winner"] == "UP"
    P = {t: model_p(w, t) for t in range(230, 281)}
    for name, (kind, amin, amax, margin, pmin) in RULES.items():
        for t in range(230, 281):
            pu = P[t]; fav = 0 if pu >= 0.5 else 1
            sd = fav if kind == "fav" else 1 - fav
            p = pu if sd == 0 else 1 - pu
            dec = w + t + 1.2; a = ask(w, sd, dec)
            if np.isnan(a) or not (amin <= a <= amax and p >= pmin and p - a - FEE * a * (1 - a) >= margin): continue
            f = ask(w, sd, dec + 0.5)
            if np.isnan(f) or f > a + 0.03 + 1e-9: continue
            won = (sd == 0) == up_won
            res[name].append(((1 / f) * won - 1 - FEE * (1 - f), won, p, f, w)); break
    for t in range(230, 281, 5):
        r = book(w, w + t + 1.2)
        if r is None or None in r[1:]: continue
        pu = P[t]; dog = 1 if pu >= 0.5 else 0; p = 1 - pu if dog == 1 else pu
        a_d = r[2] if dog == 0 else r[4]; b_d = r[1] if dog == 0 else r[3]
        cal.append((p, a_d, (a_d + b_d) / 2, (dog == 0) == up_won))

print(f"recorded windows with outcomes: {n_w}")
for name, rs in res.items():
    if not rs: print(f"  {name:18s} no trades"); continue
    A = np.array([r[:4] for r in rs], dtype=float)
    se = A[:, 0].std() / math.sqrt(len(A)) if len(A) > 1 else float("nan")
    print(f"  {name:18s} n={len(A):4d} pnl/$1={A[:, 0].sum():+7.2f} per={A[:, 0].mean():+.3f} (se {se:.3f}) win={A[:, 1].mean():.3f} model_p={A[:, 2].mean():.3f} fill={A[:, 3].mean():.3f}")
C = np.array(cal, dtype=float)
print("\nunderdog by model p minus book ask (every 5th second 230-280s)")
for lo, hi in ((-1, -0.10), (-0.10, -0.03), (-0.03, 0.0), (0.0, 0.03), (0.03, 0.10), (0.10, 1)):
    m = (C[:, 0] - C[:, 1] >= lo) & (C[:, 0] - C[:, 1] < hi)
    if m.sum() < 20: continue
    print(f"  gap [{lo:+.2f},{hi:+.2f}): n={m.sum():5d} model={C[m, 0].mean():.4f} ask={C[m, 1].mean():.4f} mid={C[m, 2].mean():.4f} realized={C[m, 3].mean():.4f}")
