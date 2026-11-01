"""Validation on recorded PM books: are entries whose setup already stood before 230s weaker? (5m, model limit)

Same entry and fill as rec_variants.py (decide at w+t+DECIDE on the book, fill FILL later at the book ask within the
limit). cond(s) = the C1 condition at second s on the same side, judged on the book at w+s+DECIDE. A trade is "pre"
if the run of cond seconds that leads to it started before 230s. As in opp_age3.py, the run continues back
through seconds without cond (no book, ask out of band, low probability) while the previous same-side cond second
is at most GAP seconds earlier, and stops at a second with cond on the other side. GAP is fixed from the print
backtest (opp_age3.py, 10s), not tuned here: the recorded days are forward days.
usage (from scratchpad): python -I research/scripts/rec_pre.py REC_DIR DIR [DIR ...] [--gap 10] [--speed DECIDE FILL]
"""
import glob, gzip, json, math, os, sys, zlib
from bisect import bisect_right
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import volfit_lib as V
import rec_clock

FEE, TOL, FLOOR, T0, TMIN, TMAX = 0.07, 0.03, 0.01, 200, 230, 290
DECIDE, FILL, GAP = 1.2, 0.5, 10
if "--speed" in sys.argv:
    i = sys.argv.index("--speed"); DECIDE, FILL = float(sys.argv[i + 1]), float(sys.argv[i + 2]); del sys.argv[i:i + 3]
if "--gap" in sys.argv:
    i = sys.argv.index("--gap"); GAP = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
V.T = np.arange(T0, TMAX + 1)
MODELS = {"V1": (900, 1, 1.25), "V4": (900, 30, 0.85), "V5": (900, 1, 1.075)}


def limit(a, p):
    q = np.arange(0.99, 0.0, -0.01); ok = p - q - FEE * q * (1 - q) >= FLOOR - 1e-9
    return min(max(a + TOL, float(q[ok][0]) if ok.any() else 0.0), 0.99)


rec_dir, dirs = sys.argv[1], sys.argv[2:]
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
                pm.setdefault(int(r[2]), []).append((r[1] - off, r[3], r[4], r[5], r[6]))
    except (EOFError, OSError, zlib.error):
        pass
for w in pm: pm[w].sort()
times = {w: [r[0] for r in rows] for w, rows in pm.items()}


def ask(w, at, side):
    i = bisect_right(times[w], at) - 1
    if i < 0 or at - pm[w][i][0] > 2.5: return None
    return pm[w][i][2 if side == 0 else 4]


def cond(Pk, n, w, s):
    """(side, ask) if the C1 condition holds at second s; (side, None) if there is no book; (side, False) if not."""
    pu = Pk[n, s - T0]
    if np.isnan(pu): return -1, False
    sd = 0 if pu >= 0.5 else 1; p = pu if sd == 0 else 1 - pu
    a = ask(w, w + s + DECIDE, sd)
    if a is None: return sd, None
    ok = 0.60 <= a <= 0.90 and p >= 0.90 and p - a - FEE * a * (1 - a) >= 0.02
    return sd, (a if ok else False)


res = {(k, r): [] for k in MODELS for r in ("deployed", "no pre")}; n_w = 0
for d in dirs:
    ev = [json.loads(l) for l in open(os.path.join(d, "events.jsonl"))]
    ev = [e for e in ev if e.get("winner") in ("UP", "DOWN") and e["window_start"] in pm]
    if not ev: continue
    W = np.array([e["window_start"] for e in ev]); up = {e["window_start"]: e["winner"] == "UP" for e in ev}
    P = {k: V.prob(V.zgrid(d, W, vw, h, 300), kk) for k, (vw, h, kk) in MODELS.items()}
    for n, w in enumerate(W):
        w = int(w)
        if np.isnan(P["V1"][n, TMIN - T0]): continue
        n_w += 1
        for k in MODELS:
            for rule in ("deployed", "no pre"):
                for t in range(TMIN, TMAX + 1):
                    sd, a = cond(P[k], n, w, t)
                    if a is None or a is False: continue
                    s, last = t, t
                    while s - 1 >= T0:
                        sd2, a2 = cond(P[k], n, w, s - 1)
                        ok2 = a2 not in (None, False)
                        if ok2 and sd2 == sd: s -= 1; last = s
                        elif not ok2 and last - (s - 1) <= GAP: s -= 1
                        else: break
                    pre = last < TMIN
                    if rule == "no pre" and pre: continue
                    pu = P[k][n, t - T0]; p = pu if sd == 0 else 1 - pu
                    f = ask(w, w + t + DECIDE + FILL, sd)
                    if f is None or f > limit(a, p) + 1e-9: continue
                    won = (sd == 0) == up[w]
                    res[(k, rule)].append(((1 / f) * won - 1 - FEE * (1 - f), int(pre), w, t)); break
print(f"recorded windows: {n_w}; decide t+{DECIDE}s, fill {FILL}s later, model limit {FLOOR}, entries {TMIN}-{TMAX}s, run gap {GAP}s")
for (k, rule), rs in res.items():
    A = np.array(rs, dtype=float).reshape(-1, 4)
    line = f"  {k} {rule:8s}: n={len(A):3d} pnl/$1={A[:, 0].sum():+.2f} per={A[:, 0].mean() if len(A) else 0:+.3f}"
    if rule == "deployed":
        for c, name in ((1, "pre"), (0, "not pre")):
            y = A[A[:, 1] == c, 0]
            line += f" | {name} n={len(y)} {y.sum():+.2f} ({y.mean() if len(y) else 0:+.3f}/trade)"
    print(line)
