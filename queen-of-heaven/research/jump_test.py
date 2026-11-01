"""Does a sharp Binance move just before entry partly reverse? (Oct 21 13:25 paper loss)

For C1 (3c limit, L=3) trades under the deployed momentum model, and for every second with
p_side >= 0.90 in 230-280s, measure the move over the last 15s toward the favoured side in units
of sigma*sqrt(15) (z15) and the largest 2s move in the last 15s in sigma units (j2). Compare
predicted vs realized win rates and trade PnL by bucket.
usage: python -I research/scripts/jump_test.py DIR [DIR ...]
"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, C1, L, FEE
from momentum import model

BETA = np.array([0.246, 0.088, 0.036])
VW = 900


def feats(close, wi, t, fav_up):
    d = np.diff(close[wi + t - VW: wi + t + 1]); sig = max(d.std(), 1e-9)
    sgn = 1 if fav_up else -1
    z15 = sgn * (close[wi + t] - close[wi + t - 15]) / (sig * math.sqrt(15))
    j2 = np.max(np.abs(close[wi + t - 13: wi + t + 1] - close[wi + t - 15: wi + t - 1])) / sig
    # the largest 2s move, signed toward the favoured side
    m2 = close[wi + t - 13: wi + t + 1] - close[wi + t - 15: wi + t - 1]
    j2s = sgn * m2[np.argmax(np.abs(m2))] / sig
    return z15, j2, j2s


ZB = [-99, -1, 0, 1, 2, 3, 99]
JB = [0, 3, 5, 8, 12, 999]
cal = {"z15": [], "j2s": []}; trades = []
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P = model(d, W, BETA, (1.0,))[1.0]
    b = np.load(os.path.join(d, "binance_1s.npz")); sec, close = b["sec"], b["close"]
    idx = {int(s): i for i, s in enumerate(sec)}
    tmin, tmax, amin, amax, margin, pmin = C1
    n0 = len(trades)
    for i, w in enumerate(W):
        wi = idx[int(w)]; took = False
        for t in range(tmin, tmax + 1):
            pu = P[i, t]
            if np.isnan(pu): continue
            sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
            if pf < pmin: continue
            won = (sd == 0) == WONUP[i]
            z15, j2, j2s = feats(close, wi, t, sd == 0)
            if t % 5 == 0:  # thin the calibration rows (neighbouring seconds are nearly identical)
                cal["z15"].append((z15, pf, won)); cal["j2s"].append((j2s, pf, won))
            if took: continue
            seen = ASK[i, sd, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = float(seen[-1])
            if not (amin <= a <= amax and pf - a - FEE * a * (1 - a) >= margin): continue
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= a + 0.03 + 1e-9)]
            if not len(nx): continue
            f = float(nx[0]); took = True
            trades.append((z15, j2, j2s, pf, f, won, (1 / f) * won - 1 - (1 / f) * FEE * f * (1 - f), d))
    r = np.array([x[6] for x in trades[n0:]])
    print(f"{d.split('/')[-1]}: trades={len(r)} pnl={r.sum():+.1f}", flush=True)

for key, bins in (("z15", ZB), ("j2s", [-99, -8, -4, 0, 4, 8, 99])):
    A = np.array(cal[key], dtype=float)
    print(f"\ncalibration rows p_side>=0.90 by {key} (toward favoured side)")
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (A[:, 0] >= lo) & (A[:, 0] < hi)
        if m.sum() < 30: continue
        print(f"  {key} [{lo:>4},{hi:>4}): n={m.sum():6d} predicted={A[m, 1].mean():.4f} realized={A[m, 2].mean():.4f} gap={A[m, 2].mean() - A[m, 1].mean():+.4f}")

T = np.array([x[:7] for x in trades], dtype=float)
print(f"\nC1 trades (momentum model, 3c, L=3): n={len(T)} pnl={T[:, 6].sum():+.1f}")
for name, col, bins in (("z15", 0, ZB), ("j2 (abs)", 1, JB), ("j2s", 2, [-99, -8, -4, 0, 4, 8, 99])):
    print(f"  by {name}")
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (T[:, col] >= lo) & (T[:, col] < hi)
        if not m.any(): continue
        r = T[m, 6]
        print(f"    [{lo:>4},{hi:>4}): n={m.sum():5d} pnl={r.sum():+7.2f} mean={r.mean():+.4f} win={T[m, 5].mean():.3f} p={T[m, 3].mean():.3f} fill={T[m, 4].mean():.3f}")
