"""Print backtest vs recorded books on the same 5m windows: how much of the print edge is fills below the seen ask?

For the windows the recorder holds (clock-corrected, as rec_variants.py), C1 with the V1 model at a flat 3c limit:
  books: decide at t+DECIDE on the recorded ask, fill FILL later at the book ask if within 3c (rec_variants.py)
  prints: L=3 print simulation (tolerance.py), fill as printed and fill capped at the seen ask
Also reports how often the book ask FILL seconds after a decision sits below the decision ask.
usage (from scratchpad): python -I research/scripts/rec_vs_print.py REC_DIR DIR [DIR ...] [--speed DECIDE FILL] [--model-limit FLOOR]
"""
import glob, gzip, json, math, os, sys, zlib
from bisect import bisect_right
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import volfit_lib as V
import rec_clock
from tolerance import arrays, L

FEE, TOL = 0.07, 0.03
DECIDE, FILL = 1.2, 0.5
if "--speed" in sys.argv:
    i = sys.argv.index("--speed"); DECIDE, FILL = float(sys.argv[i + 1]), float(sys.argv[i + 2]); del sys.argv[i:i + 3]
FLOOR = None
if "--model-limit" in sys.argv:
    i = sys.argv.index("--model-limit"); FLOOR = float(sys.argv[i + 1]); del sys.argv[i:i + 2]
V.T = np.arange(230, 281)


def limit(a, p):
    lim = a + TOL
    if FLOOR is not None:
        q = np.arange(0.99, 0.0, -0.01); ok = p - q - FEE * q * (1 - q) >= FLOOR - 1e-9
        if ok.any(): lim = max(lim, float(q[ok][0]))
    return min(lim, 0.99)


rec_dir, dirs = sys.argv[1], sys.argv[2:]
pm = {}
paths = sorted(glob.glob(os.path.join(rec_dir, "*.jsonl.gz"))); offs = rec_clock.offsets(paths)
for path in paths:
    off = offs[path]
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


def px(w, at, side):
    i = bisect_right(times[w], at) - 1
    if i < 0 or at - pm[w][i][0] > 2.5: return None
    return pm[w][i][2 if side == 0 else 4]


book, prints, drops = [], [], []
for d in dirs:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P = V.prob(V.zgrid(d, W, 900, 1), 1.25)  # V1 sigma and k (momentum drift included in zgrid)
    for n, w in enumerate(W):
        w = int(w)
        if w not in pm or np.isnan(P[n, 0]): continue
        bk = pr = None
        for j, t in enumerate(range(230, 281)):
            pu = P[n, j]; sd = 0 if pu >= 0.5 else 1; p = pu if sd == 0 else 1 - pu
            if p < 0.90: continue
            if bk is None:
                a = px(w, w + t + DECIDE, sd)
                if a is not None and 0.60 <= a <= 0.90 and p - a - FEE * a * (1 - a) >= 0.02:
                    f = px(w, w + t + DECIDE + FILL, sd)
                    if f is not None:
                        drops.append(f - a)
                        if f <= limit(a, p) + 1e-9:
                            won = (sd == 0) == WONUP[n]; bk = ((1 / f) * won - 1 - FEE * (1 - f), f - a)
            if pr is None:
                seen = ASK[n, sd, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
                if len(seen):
                    a = float(seen[-1])
                    if 0.60 <= a <= 0.90 and p - a - FEE * a * (1 - a) >= 0.02:
                        nx = ASK[n, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= limit(a, p) + 1e-9)]
                        if len(nx):
                            f = float(nx[0]); won = (sd == 0) == WONUP[n]
                            pr = tuple((1 / x) * won - 1 - FEE * (1 - x) for x in (f, max(f, a))) + (f - a,)
            if bk is not None and pr is not None: break
        if bk: book.append(bk)
        if pr: prints.append(pr)
B, Pr, D = np.array(book), np.array(prints), np.array(drops)
print(f"recorded 5m windows also in the data dirs: decide t+{DECIDE}s fill +{FILL}s, limit floor {FLOOR}")
print(f"  books : n={len(B)} pnl/$1={B[:, 0].sum():+.2f} per={B[:, 0].mean():+.3f} fill-seen mean={B[:, 1].mean():+.3f} below seen {np.mean(B[:, 1] < -0.005):.0%}")
print(f"  prints: n={len(Pr)} pnl/$1={Pr[:, 0].sum():+.2f} per={Pr[:, 0].mean():+.3f} | capped at seen {Pr[:, 1].sum():+.2f} per={Pr[:, 1].mean():+.3f} | fill-seen mean={Pr[:, 2].mean():+.3f} below seen {np.mean(Pr[:, 2] < -0.005):.0%} (>5c below {np.mean(Pr[:, 2] < -0.05):.0%})")
print(f"  book ask {FILL}s after every qualifying decision: n={len(D)} below decision ask {np.mean(D < -0.005):.0%}, >5c below {np.mean(D < -0.05):.0%}, above {np.mean(D > 0.005):.0%}")
