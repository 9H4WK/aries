"""V1 / V4 / V5 entries and the early-exit rule on recorded PM books (5m).

Decide at w+t+1.2 on the recorded book (the close of second t is known at t+1), fill 0.5s later at the
book ask if it is within 3c of the seen ask. Exit (V1 entries): from the second after the fill to 298s,
sell when the seen book bid after fee beats the model by MARGIN, filled DELAY later at the book bid
if it is at most 3c below the seen bid.
Binance closes and outcomes come from the forward data dirs (official 1s klines and events).
usage (from scratchpad): python -I research/scripts/rec_variants.py REC_DIR DIR [DIR ...] [--speed DECIDE FILL] [--model-limit FLOOR] [--tmax 280] [--interval 900]
--interval 900: the 15m markets (recorder rows "pm15" from Oct 23 09:00; times on the 5m axis, t + 600).
--model-limit: the BUY limit rises to the highest cent where the model still beats the price by FLOOR after fee.
"""
import glob, gzip, json, math, os, sys, zlib
from bisect import bisect_right
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import volfit_lib as V
import rec_clock

FEE, TOL = 0.07, 0.03
DECIDE, FILL = 1.2, 0.5  # seconds after second t at which the bot decides, then fills
if "--speed" in sys.argv:
    i = sys.argv.index("--speed"); DECIDE, FILL = float(sys.argv[i + 1]), float(sys.argv[i + 2]); del sys.argv[i:i + 3]
FLOOR = None
if "--model-limit" in sys.argv:
    i = sys.argv.index("--model-limit"); FLOOR = float(sys.argv[i + 1]); del sys.argv[i:i + 2]
TMAX, IV = 280, 300
if "--interval" in sys.argv:
    i = sys.argv.index("--interval"); IV = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
SH, TAG = IV - 300, '["pm"' if IV == 300 else '["pm15"'
if "--tmax" in sys.argv:
    i = sys.argv.index("--tmax"); TMAX = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
V.T = np.arange(230, 299)


def limit(a, p):
    lim = a + TOL
    if FLOOR is not None:
        q = np.arange(0.99, 0.0, -0.01); ok = p - q - FEE * q * (1 - q) >= FLOOR - 1e-9
        if ok.any(): lim = max(lim, float(q[ok][0]))
    return min(lim, 0.99)
MODELS = {"V1": (900, 1, 1.25), "V4": (900, 30, 0.85), "V5": (900, 1, 1.075)}
EXITS = [(m, dl) for m in (0.10, 0.05) for dl in (0.5, 1.0, 1.5)]

rec_dir, dirs = sys.argv[1], sys.argv[2:]
pm = {}
paths = sorted(glob.glob(os.path.join(rec_dir, "*.jsonl.gz"))); offs = rec_clock.offsets(paths)
for path in paths:
    off = offs[path]  # local clock minus real (rec_clock.py)
    try:
        with gzip.open(path, "rt") as fh:
            for line in fh:
                if not line.startswith(TAG): continue
                try: r = json.loads(line)
                except json.JSONDecodeError: continue
                pm.setdefault(int(r[2]), []).append((r[1] - off, r[3], r[4], r[5], r[6]))
    except (EOFError, OSError, zlib.error):
        pass
for w in pm: pm[w].sort()
times = {w: [r[0] for r in rows] for w, rows in pm.items()}


def book(w, at):
    i = bisect_right(times[w], at) - 1
    if i < 0 or at - pm[w][i][0] > 2.5: return None
    return pm[w][i]


def px(w, at, side, bid):
    r = book(w, at)
    if r is None: return None
    return r[(1 if side == 0 else 3) if bid else (2 if side == 0 else 4)]


res = {k: [] for k in MODELS}; ex = {k: [] for k in EXITS}; n_w = 0
for d in dirs:
    ev = [json.loads(l) for l in open(os.path.join(d, "events.jsonl"))]
    ev = [e for e in ev if e.get("winner") in ("UP", "DOWN") and e["window_start"] in pm]
    if not ev: continue
    W = np.array([e["window_start"] for e in ev]); up = {e["window_start"]: e["winner"] == "UP" for e in ev}
    P = {k: V.prob(V.zgrid(d, W, vw, h, IV), kk) for k, (vw, h, kk) in MODELS.items()}
    for n, w in enumerate(W):
        w = int(w)
        if np.isnan(P["V1"][n, 0]): continue
        n_w += 1
        for k in MODELS:
            for j, t in enumerate(range(230, TMAX + 1)):
                pu = P[k][n, j]; sd = 0 if pu >= 0.5 else 1; p = pu if sd == 0 else 1 - pu
                dec = w + SH + t + DECIDE; a = px(w, dec, sd, False)
                if a is None or not (0.60 <= a <= 0.90 and p >= 0.90 and p - a - FEE * a * (1 - a) >= 0.02): continue
                f = px(w, dec + FILL, sd, False)
                if f is None or f > limit(a, p) + 1e-9: continue
                won = (sd == 0) == up[w]; sh = 1 / f; hold = sh * won - 1 - FEE * (1 - f)
                res[k].append((hold, won, f, w, t))
                if k == "V1":
                    for m, dl in EXITS:
                        out = (hold, None)
                        for u in range(t + 1, 299):
                            q = P["V1"][n, u - 230]; q = q if sd == 0 else 1 - q
                            b = px(w, w + SH + u + 1.2, sd, True)
                            if b is None or b - FEE * b * (1 - b) < q + m: continue
                            s = px(w, w + SH + u + 1.2 + dl, sd, True)
                            if s is None or s < b - TOL - 1e-9: continue
                            out = (sh * (s - FEE * s * (1 - s)) - 1 - FEE * (1 - f), (u, b, s, q)); break
                        ex[(m, dl)].append((out[0] - hold, out[1]))
                break
print(f"recorded windows with Binance data and outcomes: {n_w}; decide at t+{DECIDE}s, fill {FILL}s later, model limit floor {FLOOR}, entries 230-{TMAX}s")
for k, rs in res.items():
    A = np.array([r[:3] for r in rs], dtype=float)
    se = A[:, 0].std() / math.sqrt(len(A))
    late = [r for r in rs if r[4] > 280]
    print(f"  {k}: n={len(A)} pnl/$1={A[:, 0].sum():+.2f} per={A[:, 0].mean():+.3f} (se {se:.3f}) win={A[:, 1].mean():.3f} fill={A[:, 2].mean():.3f}"
          + (f" | after 280s: n={len(late)} pnl={sum(r[0] for r in late):+.2f}" if late else ""))
print("early exit on V1 entries (gain vs holding):")
for (m, dl), rs in ex.items():
    g = np.array([r[0] for r in rs]); nx = sum(1 for r in rs if r[1])
    print(f"  margin {m:.2f} delay {dl:.1f}s: exits {nx} gain {g.sum():+.2f}")
