"""Decision frequency on recorded feeds: a 1s decision loop (four phases) vs a 0.25s loop.

At each decision time tau (local receive clock) the spot is the latest recorded Binance aggTrade received
by tau; completed seconds come from the official 1s closes. The book is the recorded PM book at tau; the
order fills 0.5s later at the book ask if it is within 3c of the seen ask. One entry per window, C1 gates.
usage (from scratchpad): python -I research/scripts/rec_speed.py REC_DIR DIR [DIR ...]
"""
import glob, gzip, json, math, os, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rec_clock
from bisect import bisect_right
import numpy as np
from scipy.special import ndtr

FEE, TOL, EPS, FILL = 0.07, 0.03, 0.01, 0.5
BETA = (0.246, 0.088, 0.036)
MODELS = {"V1": (1, 1.25), "V4": (30, 0.85), "V5": (1, 1.075)}  # (vol horizon, k); vol window 900
GRIDS = {f"1s phase {p:.2f}": np.arange(230 + p, 281, 1.0) for p in (0.0, 0.25, 0.5, 0.75)}
GRIDS["0.25s"] = np.arange(230, 281, 0.25)

rec_dir, dirs = sys.argv[1], sys.argv[2:]
pm, bn = {}, []
paths = sorted(glob.glob(os.path.join(rec_dir, "*.jsonl.gz"))); offs = rec_clock.offsets(paths)
for path in paths:
    off = offs[path]  # local clock minus real (rec_clock.py)
    try:
        with gzip.open(path, "rt") as fh:
            for line in fh:
                if line.startswith('["pm"'):
                    try: r = json.loads(line)
                    except json.JSONDecodeError: continue
                    pm.setdefault(int(r[2]), []).append((r[1] - off, r[4], r[6]))
                elif line.startswith('["bn"'):
                    try: r = json.loads(line)
                    except json.JSONDecodeError: continue
                    bn.append((r[1] - off, r[3]))
    except (EOFError, OSError, zlib.error):
        pass
for w in pm: pm[w].sort()
pmt = {w: [r[0] for r in rows] for w, rows in pm.items()}
bn.sort(); bnt = [r[0] for r in bn]


def ask(w, at, side):
    i = bisect_right(pmt[w], at) - 1
    if i < 0 or at - pm[w][i][0] > 2.5: return None
    return pm[w][i][1 + side]


def spot(at):
    i = bisect_right(bnt, at) - 1
    return bn[i][1] if i >= 0 and at - bn[i][0] < 2.0 else None


res = {(g, m): [] for g in GRIDS for m in MODELS}; n_w = 0
for d in dirs:
    b = np.load(os.path.join(d, "binance_1s.npz")); sec0, c = int(b["sec"][0]), b["close"].astype(float)
    for e in map(json.loads, open(os.path.join(d, "events.jsonl"))):
        w = e["window_start"]
        if e.get("winner") not in ("UP", "DOWN") or w not in pm: continue
        wi = w - sec0
        if wi - 1000 < 0 or wi + 300 >= len(c) or spot(w + 230) is None or spot(w + 280) is None: continue
        n_w += 1; up_won = e["winner"] == "UP"
        o = c[wi - 59: wi + 1].mean()
        sig = {}
        for m, (h, k) in MODELS.items():
            x = c[wi + 230 - 900 + 1 - h: wi + 281] ; x = x[h:] - x[:-h]  # h-changes ending at seconds 230-900+1 .. 280
            sig[m] = [math.sqrt(max(x[t - 230: t - 230 + 900 - h + 1].var(), 1e-12) / h) for t in range(230, 281)]
        for g, taus in GRIDS.items():
            done = {m: False for m in MODELS}
            for off in taus:
                tau = w + off; s = int(off)
                sp = spot(tau)
                if sp is None: continue
                at = lambda t: sp if t == s else c[wi + t]
                sd_ = sp + BETA[0] * (sp - at(s - 2)) + BETA[1] * (at(s - 2) - at(s - 5)) + BETA[2] * (at(s - 5) - at(s - 10))
                if s < 240: ef = sd_; vmul = (240 - s) + 20
                else:
                    kk = s - 240; rem = 60 - kk
                    acc = (c[wi + 241: wi + s].sum() + sp) / kk if kk else sp
                    ef = (kk * acc + rem * sd_) / 60; vmul = rem ** 3 / (3 * 3600)
                for m, (h, k) in MODELS.items():
                    if done[m]: continue
                    var = sig[m][s - 230] ** 2 * vmul
                    pu = EPS + (1 - 2 * EPS) * float(ndtr((ef - o) / math.sqrt(var) / k))
                    side = 0 if pu >= 0.5 else 1; p = pu if side == 0 else 1 - pu
                    a = ask(w, tau, side)
                    if a is None or not (0.60 <= a <= 0.90 and p >= 0.90 and p - a - FEE * a * (1 - a) >= 0.02): continue
                    f = ask(w, tau + FILL, side)
                    if f is None or f > a + TOL + 1e-9: continue
                    won = (side == 0) == up_won
                    res[(g, m)].append((1 / f) * won - 1 - FEE * (1 - f)); done[m] = True
print(f"recorded windows: {n_w}")
for m in MODELS:
    row = []
    for g in GRIDS:
        r = np.array(res[(g, m)])
        row.append(f"{g}: n={len(r)} {r.sum():+.2f}")
    print(f"{m}: " + " | ".join(row))
