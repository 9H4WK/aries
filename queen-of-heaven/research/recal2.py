"""Monotone binned recalibration of the favoured-side probability (fit on FIT_DIR, 230-280s), scored with C1.

usage (from scratchpad): python -I research/scripts/recal2.py FIT_DIR DIR [DIR ...]
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, sim

T0, T1 = 230, 281
KNOTS = np.array([0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.93, 0.95, 0.97, 0.98, 0.99, 1.0])


def fav_won(arr):
    W, PM, WONUP, SIDEM, ASK = arr
    p = PM[:, T0:T1]; fav = np.maximum(p, 1 - p)
    won = np.where(p >= 0.5, WONUP[:, None], ~WONUP[:, None])
    return fav, won


def fit(arr):
    fav, won = fav_won(arr)
    x, y = [], []
    for lo, hi in zip(KNOTS, KNOTS[1:]):
        m = (fav >= lo) & (fav < hi + (1e-9 if hi == 1.0 else 0))
        x.append(fav[m].mean()); y.append(won[m].mean())
    y = np.maximum.accumulate(np.array(y))
    return np.array(x), np.minimum(y, 0.995)


def apply(arr, x, y):
    W, PM, WONUP, SIDEM, ASK = arr
    p = PM[:, T0:T1]; fav = np.maximum(p, 1 - p)
    newfav = np.interp(fav, x, y)
    PM2 = PM.copy(); PM2[:, T0:T1] = np.where(p >= 0.5, newfav, 1 - newfav)
    return W, PM2, WONUP, SIDEM, ASK


if __name__ == "__main__":
    x, y = fit(arrays(sys.argv[1]))
    print("map (model -> realized):", " ".join(f"{a:.3f}->{b:.3f}" for a, b in zip(x, y)))
    for d in sys.argv[2:]:
        arr = arrays(d)
        for name, a in (("current", arr), ("recal", apply(arr, x, y))):
            for tol in (0.03,):
                r = sim(a, tol)[:, 0]
                print(f"{d.split('/')[-1]:6s} {name:8s} n={len(r):5d} pnl={r.sum():+7.1f} mean={r.mean():+.4f} win={(r > 0).mean():.3f} "
                      f"t={r.mean() / (r.std() / math.sqrt(len(r))):+.2f}", flush=True)
