"""Volatility estimator variants for the TWAP model (momentum drift kept), 5m windows.

sigma^2 per second = variance of overlapping H-second Binance changes over the last VW seconds / H.
H=1, VW=900 is the deployed model. For each variant, k (fat-tail factor) is fit by log loss on the
TUNE dirs (230-280s, every second); then log loss and C1 trading (L=3, 3c) are reported per dir.
Windows without 3660s of history are dropped for every variant.
usage: python -I research/scripts/volfit.py TUNE_DIR,TUNE_DIR DIR [DIR ...]
"""
import math, os, sys
import numpy as np
from scipy.special import ndtr
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, sim

EPS, BETA = 0.01, (0.246, 0.088, 0.036)
VARIANTS = [(900, 1, 1.25)] + [(vw, h, None) for vw in (900, 1800, 3600) for h in (1, 5, 15, 30)]  # first: deployed
KS = np.arange(0.70, 2.01, 0.025)
T = np.arange(230, 281)


def zgrid(d, W, vw, h, interval=300):
    b = np.load(os.path.join(d, "binance_1s.npz")); sec, c = b["sec"].astype(np.int64), b["close"].astype(float)
    assert sec[-1] - sec[0] + 1 == len(sec), "binance seconds not contiguous"
    wi = W.astype(np.int64) - sec[0]
    ok = (wi - 3660 - 60 >= 0) & (wi + interval < len(c))
    wi = np.where(ok, wi, 4000)
    x = np.zeros(len(c)); x[h:] = c[h:] - c[:-h]
    cx = np.concatenate([[0], np.cumsum(x)]); cx2 = np.concatenate([[0], np.cumsum(x * x)]); cc = np.concatenate([[0], np.cumsum(c)])
    sh = interval - 300                                # longer windows use the 5m time axis (t = seconds since open - sh)
    I = wi[:, None] + sh + T[None, :]                 # global index of "now"
    n = vw - h + 1                                     # H-changes ending in the last VW seconds
    s1 = cx[I + 1] - cx[I + 1 - n]; s2 = cx2[I + 1] - cx2[I + 1 - n]
    var1 = np.maximum(s2 / n - (s1 / n) ** 2, 1e-12) / h
    o = (cc[wi + 1] - cc[wi - 59]) / 60
    s = c[I]; drift = BETA[0] * (c[I] - c[I - 2]) + BETA[1] * (c[I - 2] - c[I - 5]) + BETA[2] * (c[I - 5] - c[I - 10])
    sd = s + drift
    k = np.maximum(T - 240, 0)[None, :]; rem = 60 - k
    acc = np.where(k > 0, (cc[I + 1] - cc[wi[:, None] + sh + 241]) / np.maximum(k, 1), s)
    pre = T[None, :] < 240
    ef = np.where(pre, sd, (k * acc + rem * sd) / 60)
    var = np.where(pre, var1 * ((240 - T[None, :]) + 20), var1 * rem ** 3 / (3 * 3600))
    Z = (ef - o[:, None]) / np.sqrt(var)
    Z[~ok] = np.nan
    return Z


def prob(Z, k):
    return EPS + (1 - 2 * EPS) * ndtr(Z / k)


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6); return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


