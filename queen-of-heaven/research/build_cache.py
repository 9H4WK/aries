"""Build a model-only cache (same tuple layout as bt2_cache.pkl) without running any grid.

usage: python build_cache.py BINANCE_NPZ EVENTS TRADES1[,TRADES2] SCRIPTS_DIR CALIBRATION OUT.pkl
"""
import json, math, pickle, sys
import numpy as np
sys.path.insert(0, sys.argv[4])
from twap_model import phi

binance_path, events_path, trades_paths, _, calib_path, out = sys.argv[1:7]
cal = json.load(open(calib_path)); VW, K, EPS = cal["vol_window"], cal["k"], cal["eps"]
b = np.load(binance_path); sec, close = b["sec"], b["close"]
idx = {int(s): i for i, s in enumerate(sec)}
winners = {}
for line in open(events_path):
    e = json.loads(line); winners[e["window_start"]] = e["winner"]
have = set()
for path in trades_paths.split(","):
    for line in open(path):
        have.add(json.loads(line)["window_start"])
ws = sorted(w for w in have if w in idx and w - VW - 400 in idx and w + 300 in idx and winners.get(w))
n = len(ws); print("windows", n, flush=True)
W = np.array(ws); PM = np.full((n, 300), np.nan); WONUP = np.array([winners[w] == "UP" for w in ws])
for i, w in enumerate(ws):
    wi = idx[w]
    o = close[wi - 59: wi + 1].mean()
    d = np.diff(close[wi - VW - 1: wi + 301])
    csum = np.concatenate([[0], np.cumsum(d)]); csq = np.concatenate([[0], np.cumsum(d * d)])
    for t in range(210, 297):
        hi = VW + 1 + t; lo = hi - VW
        m = (csum[hi] - csum[lo]) / VW; sig = math.sqrt(max((csq[hi] - csq[lo]) / VW - m * m, 1e-12))
        s = close[wi + t]
        if t < 240: ef, var = s, sig ** 2 * ((240 - t) + 20)
        else:
            k = t - 240; acc = close[wi + 241: wi + t + 1].mean() if k else s; rem = 60 - k
            ef, var = (k * acc + rem * s) / 60, sig ** 2 * rem ** 3 / (3 * 3600)
        PM[i, t] = EPS + (1 - 2 * EPS) * float(phi(((ef - o) / math.sqrt(var)) / K))
    if i % 2000 == 0: print("model", i, n, flush=True)
SIDEM = np.where(PM >= 0.5, 0, 1)
pickle.dump((W, PM, None, None, None, None, WONUP, SIDEM, None), open(out, "wb"))
print("saved", out, flush=True)
