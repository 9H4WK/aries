"""Exit rule robustness: capped exit price, SELL prints only, longer exit lag.

usage (from scratchpad): python -I research/scripts/exit_robust.py [--interval 900] DIR [DIR ...]
"""
import sys, os, math
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exit_test as X

shift = 0
if "--interval" in sys.argv:
    i = sys.argv.index("--interval"); shift = int(sys.argv[i + 1]) - 300; del sys.argv[i:i + 2]
X.MARGINS = (None, 0.10, 0.05)
data = []
for d in sys.argv[1:]:
    W, WONUP, ASK, BID = X.load(d); P = X.model(d, W, shift=shift); data.append((d, W, WONUP, ASK, BID, P))
X.SELL_PRINTS_ONLY = True
sell_only = [(d, W, WONUP, ASK, X.load(d)[3], P) for d, W, WONUP, ASK, BID, P in data]
X.SELL_PRINTS_ONLY = False
for name, cap, el, sets in (("base", False, 3, data), ("cap at seen", True, 3, data), ("exit L=4", False, 4, data),
                            ("exit L=5", False, 5, data), ("sell prints only", False, 3, sell_only)):
    X.CAP_AT_SEEN, X.EXIT_L = cap, el
    per = {m: [] for m in X.MARGINS}
    for d, W, WONUP, ASK, BID, P in sets:
        res, _ = X.run(W, WONUP, ASK, BID, P)
        for m in X.MARGINS: per[m].append(np.array(res[m]))
    base = [r for r in per[None]]
    out = []
    for m in X.MARGINS[1:]:
        g = [r - b for r, b in zip(per[m], base)]
        gg = np.concatenate(g)
        out.append(f"m={m:.2f} gain by period " + " ".join(f"{x.sum():+.2f}" for x in g) + f" total {gg.sum():+.2f} t={gg.mean() / (gg.std() / math.sqrt(len(gg))):+.2f}")
    print(f"{name:16s}", " | ".join(out), flush=True)
