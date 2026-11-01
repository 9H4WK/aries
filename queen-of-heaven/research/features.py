"""Which extra signals improve the calibrated TWAP probability in the late window? (logistic regression, train vs test)."""
import json, sys, math
import numpy as np
sys.path.insert(0, sys.argv[4])
from twap_model import phi
binance_path, events_path, trades_path, _, calib_path, out_path = sys.argv[1:7]
cal = json.load(open(calib_path)); VW, K, EPS, SPLIT = cal["vol_window"], cal["k"], cal["eps"], cal["split"]
b = np.load(binance_path); sec, close, vol, tbv = b["sec"], b["close"], b["vol"], b["tbv"]
idx = {int(s): i for i, s in enumerate(sec)}
winners = {json.loads(l)["window_start"]: json.loads(l)["winner"] for l in open(events_path)}
logit = lambda p: np.log(np.clip(p, 1e-4, 1 - 1e-4) / (1 - np.clip(p, 1e-4, 1 - 1e-4)))
X, Y, Wn, T = [], [], [], []
seen = set()
for path in trades_path.split(","):
    for line in open(path):
        r = json.loads(line); w = r["window_start"]
        if w in seen or w not in idx or w - VW - 400 not in idx or w + 300 not in idx or not winners.get(w): continue
        seen.add(w); wi = idx[w]; up = winners[w] == "UP"
        # last traded Up-probability per second from prints (Up BUY at p, Down BUY at q -> Up = 1-q)
        mkt = np.full(300, np.nan)
        for t, oi, side, price, sz in r["t"]:
            if side == 1 and 0 <= t < 300: mkt[t] = price if oi == 0 else 1 - price
        last = np.nan; lastm = np.full(300, np.nan); age = np.full(300, 999.0); la = -999
        for t in range(300):
            if not np.isnan(mkt[t]): last = mkt[t]; la = t
            lastm[t] = last; age[t] = t - la
        o = close[wi - 59: wi + 1].mean()
        for t in range(230, 293, 2):
            now = wi + t; d = np.diff(close[now - VW: now + 1]); sig = max(d.std(), 1e-9); s = close[now]
            if t < 240: ef, var = s, sig**2 * ((240 - t) + 20)
            else:
                k = t - 240; acc = close[wi + 241: now + 1].mean() if k else s; rem = 60 - k
                ef, var = (k * acc + rem * s) / 60, sig**2 * rem**3 / (3 * 3600)
            p = EPS + (1 - 2 * EPS) * float(phi(((ef - o) / math.sqrt(var)) / K))
            r5 = (close[now] - close[now - 5]) / (sig * math.sqrt(5)); r30 = (close[now] - close[now - 30]) / (sig * math.sqrt(30))
            v30 = vol[now - 29: now + 1].sum(); imb = (2 * tbv[now - 29: now + 1].sum() / v30 - 1) if v30 > 0 else 0.0
            m = lastm[t] if age[t] <= 5 else np.nan
            X.append([logit(p), r5, r30, imb, math.log(sig), logit(m) if not np.isnan(m) else 0.0, 0.0 if not np.isnan(m) else 1.0, (t - 240) / 60])
            Y.append(up); Wn.append(w); T.append(t)
X, Y, Wn, T = np.array(X), np.array(Y, float), np.array(Wn), np.array(T)
print("samples", len(Y), flush=True)

def fit(Xs, y, l2=1e-3, iters=50):
    Xb = np.column_stack([np.ones(len(Xs)), Xs]); w = np.zeros(Xb.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xb @ w)); g = Xb.T @ (p - y) / len(y) + l2 * w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb / len(y) + l2 * np.eye(len(w)); w -= np.linalg.solve(H, g)
    return w
def ll(Xs, y, w):
    p = 1 / (1 + np.exp(-np.column_stack([np.ones(len(Xs)), Xs]) @ w)); p = np.clip(p, 1e-6, 1 - 1e-6)
    return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))
tr, te = Wn < SPLIT, Wn >= SPLIT
names = ["model", "ret5s", "ret30s", "taker_imb30", "log_vol", "market", "market_missing", "time"]
sets = {"model only": [0], "+momentum": [0, 1, 2], "+taker imbalance": [0, 3], "+vol regime": [0, 4],
        "+market price": [0, 5, 6], "+market+momentum": [0, 1, 2, 5, 6], "all": list(range(8))}
base = None
for nm, cols in sets.items():
    w = fit(X[tr][:, cols], Y[tr]); l_tr, l_te = ll(X[tr][:, cols], Y[tr], w), ll(X[te][:, cols], Y[te], w)
    base = base or l_te
    print(f"{nm:18s} train {l_tr:.4f} TEST {l_te:.4f} ({(l_te - base) / base:+.1%})  coefs " + " ".join(f"{names[c]}={v:+.2f}" for c, v in zip(cols, w[1:])))
w_all = fit(X[tr][:, [0, 1, 2, 5, 6]], Y[tr])
json.dump({"cols": ["model", "ret5s", "ret30s", "market", "market_missing"], "w": w_all.tolist()}, open(out_path, "w"))
