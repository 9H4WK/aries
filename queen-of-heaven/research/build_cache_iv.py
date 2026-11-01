"""Model cache for longer windows (e.g. 15m), on the 5m time axis: axis t = window t - (INTERVAL - 300).

usage: python build_cache_iv.py INTERVAL BINANCE_NPZ EVENTS TRADES CALIBRATION OUT_DIR
Writes OUT_DIR/bt2_cache.pkl and OUT_DIR/trades.jsonl (trade times shifted to the axis), so the
5m backtest scripts (tolerance.py, forward2.py V0) run unchanged: axis 230-280s = the same 70-20s before close.
"""
import json, math, os, pickle, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twap_model import phi

I, binance_path, events_path, trades_path, calib_path, out = int(sys.argv[1]), *sys.argv[2:7]
SHIFT = I - 300
cal = json.load(open(calib_path)); VW, K, EPS = cal["vol_window"], cal["k"], cal["eps"]
b = np.load(binance_path); sec, close = b["sec"], b["close"]
idx = {int(s): i for i, s in enumerate(sec)}
winners = {json.loads(l)["window_start"]: json.loads(l)["winner"] for l in open(events_path)}
os.makedirs(out, exist_ok=True)
have = []
with open(os.path.join(out, "trades.jsonl"), "w") as f:
    for line in open(trades_path):
        r = json.loads(line); w = r["window_start"]
        r["t"] = [[t - SHIFT, *rest] for t, *rest in r["t"] if t >= SHIFT]
        f.write(json.dumps(r) + "\n"); have.append(w)
ws = sorted(w for w in have if w in idx and w - VW - 400 in idx and w + I in idx and winners.get(w))
n = len(ws); print("windows", n, flush=True)
W = np.array(ws); PM = np.full((n, 300), np.nan); WONUP = np.array([winners[w] == "UP" for w in ws])
lock = I - 60
for i, w in enumerate(ws):
    wi = idx[w]; o = close[wi - 59: wi + 1].mean()
    for ta in range(210, 297):
        t = ta + SHIFT; now = wi + t
        d = np.diff(close[now - VW: now + 1]); sig = max(float(d.std()), 1e-6)
        s = close[now]
        if t < lock: ef, var = s, sig ** 2 * ((lock - t) + 20)
        else:
            k = t - lock; acc = close[wi + lock + 1: now + 1].mean() if k else s; rem = 60 - k
            ef, var = (k * acc + rem * s) / 60, sig ** 2 * rem ** 3 / (3 * 3600)
        PM[i, ta] = EPS + (1 - 2 * EPS) * float(phi(((ef - o) / math.sqrt(var)) / K))
SIDEM = np.where(PM >= 0.5, 0, 1)
pickle.dump((W, PM, None, None, None, None, WONUP, SIDEM, None), open(os.path.join(out, "bt2_cache.pkl"), "wb"))
print("saved", out, flush=True)
