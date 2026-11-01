"""TWAP model plus short-term Binance momentum, scored with C1 at L=3 and a 3c limit.

usage (from scratchpad): python -I research/scripts/momentum.py FIT_DIR DIR [DIR ...] [--interval 900]
Momentum coefficients come from price data only (FIT_DIR's Binance 1s closes): OLS of the next H-second
change on the changes over the last 0-2s, 2-5s and 5-10s. The expected final average adds that drift to
every remaining second.
"""
import json, math, os, pickle, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, sim
from twap_model import phi

T0, T1, H = 230, 281, 15
LAGS = ((0, 2), (2, 5), (5, 10))


def legs(c, i):
    return np.stack([c[i - a] - c[i - b] for a, b in LAGS], axis=-1)


def fit(d):
    c = np.load(os.path.join(d, "binance_1s.npz"))["close"]
    i = np.arange(10, len(c) - H)
    X = legs(c, i); y = c[i + H] - c[i]
    return np.linalg.lstsq(X, y, rcond=None)[0]


def model(d, W, beta, scale=(1.0,), interval=300, basis=0.0):
    VW, K, EPS = 900, 1.25, 0.01  # deployed calibration
    b = np.load(os.path.join(d, "binance_1s.npz")); sec, close = b["sec"], b["close"]
    idx = {int(s): i for i, s in enumerate(sec)}
    out = {}
    for sc in scale:
        out[sc] = np.full((len(W), 300), np.nan)
    shift, lock = interval - 300, interval - 60  # caches for longer windows use the 5m time axis
    for n, w in enumerate(W):
        wi = idx[int(w)]; o = close[wi - 59: wi + 1].mean()
        d1 = np.diff(close[wi + shift - VW - 1: wi + shift + 301])
        csum = np.concatenate([[0], np.cumsum(d1)]); csq = np.concatenate([[0], np.cumsum(d1 * d1)])
        for t in range(T0, T1):
            hi = VW + 1 + t; lo = hi - VW
            m = (csum[hi] - csum[lo]) / VW; sig = math.sqrt(max((csq[hi] - csq[lo]) / VW - m * m, 1e-12))
            s = close[wi + shift + t]; drift = float(legs(close, wi + shift + t) @ beta)
            for sc in scale:
                sd = s + sc * drift
                if t + shift < lock: ef, var = sd, sig ** 2 * ((lock - t - shift) + 20)
                else:
                    k = t + shift - lock; acc = close[wi + lock + 1: wi + shift + t + 1].mean() if k else s; rem = 60 - k
                    ef, var = (k * acc + rem * sd) / 60, sig ** 2 * rem ** 3 / (3 * 3600)
                # basis: sd ($) of the Chainlink-Binance basis change, added outside the fat-tail factor K
                out[sc][n, t] = EPS + (1 - 2 * EPS) * float(phi((ef - o) / math.sqrt(K * K * var + basis * basis)))
    return out


def loss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6); return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))


if __name__ == "__main__":
    interval = 300
    if "--interval" in sys.argv:
        i = sys.argv.index("--interval"); interval = int(sys.argv[i + 1]); del sys.argv[i:i + 2]
    beta = fit(sys.argv[1])
    print("momentum coefficients (0-2s, 2-5s, 5-10s), H=%ds:" % H, np.round(beta, 3), flush=True)
    SCALES = (0.0, 0.5, 1.0, 1.5)
    for d in sys.argv[2:]:
        W, PM, WONUP, SIDEM, ASK = arr = arrays(d)
        pms = model(d, W, beta, SCALES, interval)
        y = np.repeat(WONUP[:, None], T1 - T0, axis=1)
        print(f"\n{d.split('/')[-1]} (windows {len(W)}; scale 0 = current model)")
        for sc, P in pms.items():
            P2 = PM.copy(); P2[:, T0:T1] = P[:, T0:T1]
            r = sim((W, P2, WONUP, np.where(P2 >= 0.5, 0, 1), ASK), 0.03)[:, 0]
            print(f"  scale {sc:.1f}: logloss={loss(P[:, T0:T1], y):.4f} C1 3c n={len(r)} pnl={r.sum():+.1f} mean={r.mean():+.4f} "
                  f"win={(r > 0).mean():.3f} t={r.mean() / (r.std() / math.sqrt(len(r))):+.2f}", flush=True)
