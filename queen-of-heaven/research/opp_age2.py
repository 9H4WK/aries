"""Split opp_age.py's weak "held" trades into pre-window setups and in-window retries (5m, deployed rule, capped).

Categories of deployed-rule trades: fresh (cond not held in the previous 30s), pre (cond held only before 230s,
i.e. the setup already stood when entries opened), retry (cond held at an earlier second >= 230s, i.e. an earlier
attempt in this window did not fill). Rules: "no pre" skips a window's entries while the setup has held without a
break since before 230s; "one try" allows one fill attempt per window (the bot retries after a cancel today);
"both" applies both.
usage (from scratchpad): python -I research/scripts/opp_age2.py DIR [DIR ...]
"""
import math, os, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays
import momentum
import volfit_lib as V
from opp_age import conds, fill, BETA, T0, TMIN, TMAX, tstat  # opp_age has no top-level run when imported

RULES = ("deployed", "no pre", "one try", "both")


def run(W, P, WONUP, ASK, L, rule):
    side, seen_a = conds(P, ASK, L)
    out = []
    for i in range(len(W)):
        tried = False
        for t in range(TMIN, TMAX + 1):
            sd = side[i, t]
            if sd < 0: continue
            run_start = t  # first second of the unbroken same-side run that includes t
            while run_start - 1 >= T0 and side[i, run_start - 1] == sd: run_start -= 1
            if rule in ("no pre", "both") and run_start < TMIN: continue
            if rule in ("one try", "both") and tried: break
            tried = True
            p = fill(i, t, sd, P, ASK, WONUP, seen_a[i, t], L)
            if p is None: continue
            prev = side[i, t - 30:t] == sd
            pre_only = prev.any() and not (side[i, TMIN:t] == sd).any()
            cat = 0 if not prev.any() else (1 if pre_only else 2)
            out.append((p, cat)); break
    return np.array(out).reshape(-1, 2)


res = defaultdict(list)
for d in sys.argv[1:]:
    W, PM, WONUP, SIDEM, ASK = arrays(d)
    P1 = momentum.model(d, W, BETA, (1.0,), 300)[1.0]
    P5 = np.full_like(P1, np.nan); P5[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 1, 300), 1.075)
    P4 = np.full_like(P1, np.nan); P4[:, T0:TMAX + 1] = V.prob(V.zgrid(d, W, 900, 30, 300), 0.85)
    for m, P in (("V1", P1), ("V5", P5), ("V4", P4)):
        for L in (3, 4):
            for r in RULES:
                res[(m, L, r)].append(run(W, P, WONUP, ASK, L, r))
    print("done", d, flush=True)

dirs = [d.rstrip("/").split("/")[-1] for d in sys.argv[1:]]
print("\nper period (" + ",".join(dirs) + ") | total n per-trade t; * = beats deployed in every period")
for m in ("V1", "V5", "V4"):
    for L in (3, 4):
        base = res[(m, L, "deployed")]; a = np.concatenate(base)
        print(f"\n{m} L={L}")
        for c, name in ((0, "fresh"), (1, "pre"), (2, "retry")):
            per = [r[r[:, 1] == c, 0] for r in base]; y = np.concatenate(per)
            print(f"  cat {name:6s}: n={len(y):5d} per={y.mean() if len(y) else 0:+.4f} t={tstat(y):+.2f} periods " + " ".join(f"{p.sum():+.1f}({len(p)})" for p in per))
        bper = [r[:, 0].sum() for r in base]
        for r in RULES:
            rs = res[(m, L, r)]; y = np.concatenate(rs)[:, 0]; per = [x[:, 0].sum() for x in rs]
            star = "*" if r != "deployed" and all(x > b for x, b in zip(per, bper)) else " "
            print(f"  rule {r:8s}{star} " + " ".join(f"{x:+6.1f}" for x in per) + f" | {y.sum():+7.1f} n={len(y):5d} per={y.mean():+.4f} t={tstat(y):+.2f}")
