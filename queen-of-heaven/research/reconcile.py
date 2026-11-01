"""Paper orders vs the backtest's C1 trades (momentum model, L=3, 3c) in the same windows of one forward set.

usage: python -I research/scripts/reconcile.py DIR BOT_DB [INTERVAL] [--v4]
--v4 scores the backtest with the V4 model (30s-change vol, k 0.85) to match the V4 shadow bot.
Lists every window where either the paper bot ordered or the backtest traded.
"""
import json, math, os, sqlite3, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, C1, L, FEE
from momentum import model
import volfit_lib as V

v4 = "--v4" in sys.argv
if v4: sys.argv.remove("--v4")
d, db = sys.argv[1], sys.argv[2]; interval = int(sys.argv[3]) if len(sys.argv) > 3 else 300
shift = interval - 300
W, PM, WONUP, SIDEM, ASK = arrays(d)
P = model(d, W, np.array([0.246, 0.088, 0.036]), (1.0,), interval)[1.0]
if v4:
    P = np.full_like(P, np.nan); P[:, V.T[0]:V.T[-1] + 1] = V.prob(V.zgrid(d, W, 900, 30, interval), 0.85)
tmin, tmax, amin, amax, margin, pmin = C1
bt = {}
for i, w in enumerate(W):
    for t in range(tmin, tmax + 1):
        pu = P[i, t]
        if np.isnan(pu): continue
        sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
        if pf < pmin: continue
        seen = ASK[i, sd, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
        if not len(seen): continue
        a = float(seen[-1])
        if not (amin <= a <= amax and pf - a - FEE * a * (1 - a) >= margin): continue
        nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= a + 0.03 + 1e-9)]
        if not len(nx): continue
        f = float(nx[0]); won = (sd == 0) == WONUP[i]
        bt[int(w)] = (t + shift, "UP" if sd == 0 else "DOWN", a, f, won, (1 / f) * won - 1 - FEE * (1 - f)); break
lo, hi = int(W.min()), int(W.max())
c = sqlite3.connect(db)
paper = {}
for ws, ca, side, lim, status, meta in c.execute(
        "select window_start, created_at, side, price, status, metadata_json from paper_orders where window_start between ? and ? order by id", (lo, hi)):
    m = json.loads(meta or "{}")
    paper.setdefault(ws, []).append((round(ca - ws, 1), side, lim, status, m.get("fill_price") or m.get("book_ask_at_fill")))
win = {int(w): ("UP" if WONUP[i] else "DOWN") for i, w in enumerate(W)}
both = sorted(set(bt) | set(paper))
hm = lambda w: time.strftime("%m-%d %H:%M", time.localtime(w))
nb = np_ = nboth = 0; pp = bp = 0.0
for w in both:
    b = bt.get(w); p = paper.get(w, [])
    bs = f"bt {b[1]:4s}@{b[0]}s seen={b[2]:.2f} fill={b[3]:.2f} {'W' if b[4] else 'L'} {b[5]:+.2f}" if b else "bt -" + " " * 40
    ps = " ; ".join(f"paper {st} {side}@{t}s lim={lim:.2f} px={px}" for t, side, lim, st, px in p) if p else "paper -"
    print(f"{hm(w)} winner={win.get(w, '?'):4s} | {bs:52s} | {ps}")
    nb += b is not None; np_ += any(x[3] == "FILLED" for x in p); nboth += b is not None and any(x[3] == "FILLED" for x in p)
print(f"\nwindows {hm(lo)}..{hm(hi)}: backtest trades {nb} ({sum(v[5] for v in bt.values()):+.2f} per $1), "
      f"paper fills {np_}, both {nboth}")
