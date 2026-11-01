import sys, math, pickle
import numpy as np
FEE = 0.07; CFG = (240, 290, 0.50, 0.90, 0.03, 0.85)
for label, path in zip(("Oct 5-18 ", "Sep21-Oct4"), sys.argv[1:3]):
    W, PM, PB, A, F1, F2, WONUP, SIDEM, SIDEB = pickle.load(open(path, "rb"))
    tt = np.arange(300); PF = np.where(SIDEM == 0, PM, 1 - PM)
    AF = np.take_along_axis(A, SIDEM[:, None, :], 1)[:, 0, :]
    WON = (SIDEM == 0) == WONUP[:, None]
    for fname, FF in (("fast 1-2s", np.take_along_axis(F1, SIDEM[:, None, :], 1)[:, 0, :]), ("slow 2-4s", np.take_along_axis(F2, SIDEM[:, None, :], 1)[:, 0, :])):
        for slip in (0.0, 0.01, 0.02):
            tmin, tmax, amin, amax, m, pmin = CFG
            with np.errstate(invalid="ignore"):
                c = ((tt >= tmin) & (tt <= tmax))[None, :] & (AF >= amin) & (AF <= amax) & (PF >= pmin) & (PF - AF - FEE * AF * (1 - AF) >= m) & ~np.isnan(FF)
            has = c.any(1); col = np.argmax(c, 1)[has]; r = np.nonzero(has)[0]
            fl = np.minimum(FF[r, col] + slip, 0.999); won = WON[r, col]
            pnl = (1 / fl) * won - 1 - (1 / fl) * FEE * fl * (1 - fl)
            t = pnl.mean() / (pnl.std() / math.sqrt(len(pnl)))
            print(f"{label} {fname} +{slip:.2f}: n={len(pnl)} pnl={pnl.sum():+6.1f} per-trade={pnl.mean():+.3f} win={won.mean():.1%} t={t:.2f}")
