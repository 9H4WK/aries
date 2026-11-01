"""One-time holdout evaluation of pre-registered finalists on data3 (plus data/data2 for reference)."""
import importlib, sys, math
import numpy as np
dirs = ["research/data", "research/data2", "research/data3"]
C = {"C0 frozen   ": (240, 290, 0.50, 0.90, 0.03, 0.85, 1.0),
     "C1 L3-joint ": (230, 280, 0.60, 0.90, 0.02, 0.90, 1.0),
     "C2 L2-joint ": (230, 290, 0.30, 0.97, 0.02, 0.85, 1.0),
     "C3 L3-pickA ": (240, 295, 0.30, 0.90, 0.02, 0.90, 1.0)}
fmt = lambda r: f"n={r[0]:4d} pnl={r[1]:+7.2f} win={r[2]:.1%} t={r[3]:+.2f}"
for L in (3, 2):
    sys.argv = ["cv", str(L)] + dirs[:2]
    cv = importlib.import_module("cv_search") if "cv_search" not in sys.modules else importlib.reload(sys.modules["cv_search"])
    data = [cv.load(d) for d in dirs]
    print(f"\n=== print-time shift L={L} ===")
    for name, g in C.items():
        rs = [cv.run(g, d) for d in data]
        print(f"{name} Oct5-18 {fmt(rs[0])} | Sep21-Oct4 {fmt(rs[1])} | HOLDOUT Aug14-Sep20 {fmt(rs[2])}")
    for name, g in C.items():
        W, t, fl, won, pnl = cv.run(g, data[2], detail=True)
        weeks = {}
        for w, p in zip(W, pnl): weeks.setdefault(int((w - 1786665600) // 604800), []).append(p)
        print(f"  {name} holdout by week: " + " ".join(f"w{k}:{sum(v):+.1f}({len(v)})" for k, v in sorted(weeks.items())))
