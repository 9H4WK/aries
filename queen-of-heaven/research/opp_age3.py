"""Week by week: setups that already stood before 230s ("pre", opp_age2.py) and the "no pre" rule (5m, capped fills).

Run definition: cond(s) as opp_age.py (seen ask = latest print within 3s). On prints a quiet spell breaks a run,
while the bot's book keeps the ask, so the rule is also scored with the run held across print gaps of up to
GAP seconds ("no pre gapN": a second with no print in its 3s lookback keeps the run if the last cond second on the
same side is at most N seconds back). Entries themselves still use the 3s seen ask.
usage (from scratchpad): python -I research/scripts/opp_age3.py DIR [DIR ...]
"""
import os, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays
import momentum
import volfit_lib as V
from opp_age import conds, fill, BETA, T0, TMIN, TMAX, tstat

GAPS = (0, 5, 10)


def run(W, P, WONUP, ASK, L, gap):
    """gap None: deployed rule. Else "no pre" with runs bridging print gaps up to gap seconds.
    Rows: window, pnl, category of the deployed trade (0 fresh/retry, 1 pre)."""
    side, seen_a = conds(P, ASK, L)
    out = []
    for i in range(len(W)):
        for t in range(TMIN, TMAX + 1):
            sd = side[i, t]
            if sd < 0: continue
            s, last = t, t  # walk back through the run; `last` = most recent cond second seen while walking
            while s - 1 >= T0:
                if side[i, s - 1] == sd: s -= 1; last = s
                elif gap and side[i, s - 1] == -1 and last - (s - 1) <= gap: s -= 1
                else: break
            pre = last < TMIN
            if gap is not None and pre: continue
            p = fill(i, t, sd, P, ASK, WONUP, seen_a[i, t], L)
            if p is None: continue
            out.append((int(W[i]), p, int(pre))); break
    return out


rows = defaultdict(list)
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P1 = momentum.model(d, W, BETA, (1.0,), 300)[1.0]
    P5 = np.full_like(P1, np.nan); P5[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 1, 300), 1.075)
    P4 = np.full_like(P1, np.nan); P4[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 30, 300), 0.85)
    for m, P in (("V1", P1), ("V5", P5), ("V4", P4)):
        for L in (3, 4):
            for g in (None,) + GAPS:
                rows[(m, L, g)] += run(W, P, WONUP, ASK, L, g)
    print("done", d, flush=True)


def weekly(rs):
    wk = defaultdict(float)
    for w, p, *_ in rs: wk[str(np.datetime64(w, "s").astype("datetime64[W]"))] += p
    return wk


for m in ("V1", "V5", "V4"):
    for L in (3, 4):
        base = rows[(m, L, None)]; bw = weekly(base); weeks = sorted(bw)
        pre = weekly([r for r in base if r[2] == 1])
        print(f"\n{m} L={L} weeks: " + " ".join(k[5:] for k in weeks))
        print(f"  deployed     {sum(bw.values()):+7.1f} | " + " ".join(f"{bw[k]:+5.1f}" for k in weeks))
        print(f"  pre trades   {sum(pre.values()):+7.1f} | " + " ".join(f"{pre[k]:+5.1f}" for k in weeks))
        for g in GAPS:
            rs = rows[(m, L, g)]; ww = weekly(rs); y = np.array([r[1] for r in rs])
            better = sum(ww[k] > bw[k] for k in weeks)
            print(f"  no pre gap{g:<2d} {y.sum():+7.1f} | " + " ".join(f"{ww[k] - bw[k]:+5.1f}" for k in weeks)
                  + f" | diff>0 in {better}/{len(weeks)} weeks, n={len(y)} per={y.mean():+.4f} t={tstat(y):+.2f}")
