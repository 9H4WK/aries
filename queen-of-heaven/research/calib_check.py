"""Model calibration per probability bin: predicted vs realized win rate of the favoured side, 230-280s.

usage (from scratchpad): python -I research/scripts/calib_check.py DIR [DIR ...]
"""
import os, pickle, sys
import numpy as np

BINS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99, 1.0)
for d in sys.argv[1:]:
    W, PM, _, _, _, _, WONUP, SIDEM, _ = pickle.load(open(os.path.join(d, "bt2_cache.pkl"), "rb"))
    p = PM[:, 230:281]; fav = np.maximum(p, 1 - p); won = np.where(p >= 0.5, WONUP[:, None], ~WONUP[:, None])
    ok = ~np.isnan(fav); fav, won = fav[ok], won[ok]
    print(f"{d.split('/')[-1]}: windows={len(W)} up-rate={WONUP.mean():.3f}")
    for lo, hi in zip(BINS, BINS[1:]):
        m = (fav >= lo) & (fav < hi)
        if m.sum(): print(f"  p {lo:.2f}-{hi:.2f}: n={m.sum():6d} predicted={fav[m].mean():.4f} realized={won[m].mean():.4f} gap={won[m].mean() - fav[m].mean():+.4f}")
