import json, sys, math
import numpy as np
sys.path.insert(0, sys.argv[3])
from twap_model import phi
b = np.load(sys.argv[1]); sec, close = b["sec"], b["close"]
idx = {int(s): i for i, s in enumerate(sec)}
events = [json.loads(l) for l in open(sys.argv[2]) ]
events = [e for e in events if e["winner"] and e["window_start"] - 1000 in idx and e["window_start"] + 300 in idx]
split = events[0]["window_start"] + 10 * 86400

def features(vol_window):
    out = []
    for e in events:
        w = e["window_start"]; wi = idx[w]; up = e["winner"] == "UP"
        o = close[wi - 59: wi + 1].mean()
        rets = np.diff(close[wi - vol_window - 300: wi + 301])
        for t in range(90, 291, 5):
            now = wi + t
            seg = np.diff(close[now - vol_window: now + 1]); sigma = seg.std()
            s = close[now]
            if t < 240:
                ef, var = s, sigma**2 * ((240 - t) + 20)
            else:
                k = t - 240; acc = close[wi + 241: now + 1].mean() if k else s; rem = 60 - k
                ef, var = (k * acc + rem * s) / 60, sigma**2 * rem**3 / (3 * 3600)
            z = (ef - o) / math.sqrt(max(var, 1e-12))
            out.append((w, t, z, up))
    return np.array(out, dtype=float)

def loss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6); return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))

best = None
for vw in (120, 300, 900):
    F = features(vw); tr = F[:, 0] < split; te = ~tr
    z, y = F[:, 2], F[:, 3]
    res = []
    for k in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5):
        pz = phi(z / k)
        for eps in (0.0, 0.005, 0.01, 0.015, 0.02):
            p = eps + (1 - 2 * eps) * pz
            res.append((loss(p[tr], y[tr]), k, eps))
    l, k, eps = min(res)
    p = eps + (1 - 2 * eps) * phi(z / k)
    print(f"vol_window={vw}s: best k={k} eps={eps} train logloss={l:.4f} TEST logloss={loss(p[te], y[te]):.4f} test brier={np.mean((p[te]-y[te])**2):.4f}", flush=True)
    if best is None or l < best[0]: best = (l, vw, k, eps, F)
l, vw, k, eps, F = best
p = eps + (1 - 2 * eps) * phi(F[:, 2] / k); y = F[:, 3]; te = F[:, 0] >= split
pf = np.where(p >= .5, p, 1 - p); yf = np.where(p >= .5, y, 1 - y)
print(f"\nCHOSEN vol_window={vw} k={k} eps={eps}. TEST-set calibration (favoured side):")
for lo, hi in [(.8,.85),(.85,.9),(.9,.93),(.93,.95),(.95,.97),(.97,.98),(.98,1.01)]:
    m = te & (pf >= lo) & (pf < hi)
    if m.sum(): print(f"  calibrated {lo:.2f}-{hi:.2f}: n={int(m.sum()):6d} predicted={pf[m].mean():.3f} actual={yf[m].mean():.3f}")
json.dump({"vol_window": vw, "k": k, "eps": eps, "split": split}, open(sys.argv[4], "w"))
