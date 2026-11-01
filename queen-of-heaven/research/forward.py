"""Forward test: pre-registered configs on data after Oct 18 (never used for selection).

usage (from scratchpad): python -I research/scripts/forward.py DIR [DIR ...]
"""
import importlib, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

dirs = sys.argv[1:]
C = {"C1 deployed": (230, 280, 0.60, 0.90, 0.02, 0.90, 1.0),
     "C0 frozen  ": (240, 290, 0.50, 0.90, 0.03, 0.85, 1.0),
     "C2         ": (230, 290, 0.30, 0.97, 0.02, 0.85, 1.0),
     "C3         ": (240, 295, 0.30, 0.90, 0.02, 0.90, 1.0)}
fmt = lambda r: f"n={r[0]:4d} pnl={r[1]:+7.2f} win={r[2]:.1%} t={r[3]:+.2f}"
for L in (3, 2):
    sys.argv = ["cv", str(L)] + dirs[:1] * 2
    cv = importlib.import_module("cv_search") if "cv_search" not in sys.modules else importlib.reload(sys.modules["cv_search"])
    data = [cv.load(d) for d in dirs]
    print(f"\n=== print-time shift L={L} ===")
    for name, g in C.items():
        print(name, " | ".join(f"{d.split('/')[-1]} {fmt(cv.run(g, x))}" for d, x in zip(dirs, data)))
        for slip in (0.01, 0.02):
            print(f"   slip {slip:.2f}:", " | ".join(fmt(cv.run(g, x, slip=slip)) for x in data))
    W, t, fl, won, pnl = cv.run(C["C1 deployed"], data[-1], detail=True)
    print("  C1 trades in", dirs[-1])
    for w, tt, f, wn, p in zip(W, t, fl, won, pnl):
        print(f"   {time.strftime('%m-%d %H:%M', time.localtime(int(w)))} t={int(tt)} fill={f:.2f} {'W' if wn else 'L'} {p:+.3f}")
