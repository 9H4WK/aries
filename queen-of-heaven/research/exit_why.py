"""Who is right when the bid beats the model? Exits at margin 0.10: model p, exit price, realised win rate.

usage (from scratchpad): python -I research/scripts/exit_why.py [--interval 900] DIR [DIR ...]
"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exit_test as X

shift = 0
if "--interval" in sys.argv:
    i = sys.argv.index("--interval"); shift = int(sys.argv[i + 1]) - 300; del sys.argv[i:i + 2]
X.MARGINS = (None, 0.10)
rows = []
for d in sys.argv[1:]:
    W, WONUP, ASK, BID = X.load(d); P = X.model(d, W, shift=shift)
    res, exits = X.run(W, WONUP, ASK, BID, P)
    # re-derive side/outcome per trade: hold pnl > 0 means the held side won
    for h, e in zip(res[None], exits[0.10]):
        if e: rows.append((e[0], e[1], e[2], 1.0 if h > 0 else 0.0, e[3]))
r = np.array(rows)
print(f"exits {len(r)}: mean model p {r[:, 2].mean():.3f}, mean exit price {r[:, 1].mean():.3f}, held side won {r[:, 3].mean():.3f}, gain {r[:, 4].sum():+.2f}")
for lo, hi in ((0, 0.3), (0.3, 0.6), (0.6, 0.8), (0.8, 1.01)):
    s = r[(r[:, 2] >= lo) & (r[:, 2] < hi)]
    if len(s): print(f"  model p {lo:.1f}-{hi:.1f}: n={len(s):3d} p={s[:, 2].mean():.3f} exit={s[:, 1].mean():.3f} won={s[:, 3].mean():.3f} gain={s[:, 4].sum():+.2f}")
for lo, hi in ((230, 260), (260, 280), (280, 290), (290, 299)):
    s = r[(r[:, 0] >= lo) & (r[:, 0] < hi)]
    if len(s): print(f"  second {lo}-{hi}: n={len(s):3d} p={s[:, 2].mean():.3f} exit={s[:, 1].mean():.3f} won={s[:, 3].mean():.3f} gain={s[:, 4].sum():+.2f}")
