"""Forward scoring of variants fixed on Oct 21 before any later data (L=3, 3c limit).

usage (from scratchpad):
  python -I research/scripts/forward2.py --freeze      # fit the recalibration maps on research/data, write variants.json next to this script
  python -I research/scripts/forward2.py DIR [DIR ...]  # score the frozen variants
Variants: V0 C1 old model | V1 C1 + momentum (deployed Oct 21) | V2 V0 + recal map | V3 V1 + recal map.
V4/V5 (frozen Oct 22 04:40, before any forward day was scored for them): sharper vol models, see variants.json.
D1 (frozen Oct 21 evening, forward from Oct 22 only): buy the underdog (see variants.json), shadow only.
V1L/V4L/V5L (frozen Oct 23 07:00 EAT, forward from Oct 24 only): the same models with the model-based BUY limit
  (variants.json LIMIT; the 5m paper bots use it from Oct 23 07:00). Entries 230-290s (LIMIT seconds; amended
  Oct 23 10:00 EAT, before any forward day was scored for them, from Aug 14-Oct 18 prints, research/time_band.py).
  Scored at the print fill and, more conservatively, at max(seen, fill).
V1N/V4N/V5N (frozen Oct 23 12:00 EAT, forward from Oct 24 only): V1L/V4L/V5L, but an entry is skipped when its setup
  already stood before 230s (variants.json NOPRE; chosen on Aug 14-Oct 18 prints, research/opp_age3.py). Model columns
  run from 200s for this; the other lines only read 230s and later.
"""
import json, math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tolerance import arrays, sim, L, FEE
from momentum import model as momentum_model
import momentum
import recal2
import volfit_lib

momentum.T0, momentum.T1 = 200, 291  # model columns 200-290s for the L/N variants; the C1 lines still use 230-280
volfit_lib.T = np.arange(200, 291)

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = os.path.join(HERE, "variants.json")
BETA = np.array([0.246, 0.088, 0.036])


def with_momentum(d, arr):
    W, PM, WONUP, SIDEM, ASK = arr
    P = momentum_model(d, W, BETA, (1.0,))[1.0]
    PM2 = PM.copy(); PM2[:, 200:291] = P[:, 200:291]
    return W, PM2, WONUP, np.where(PM2 >= 0.5, 0, 1), ASK


if sys.argv[1] == "--freeze":
    base = arrays("research/data")
    x0, y0 = recal2.fit(base)
    x1, y1 = recal2.fit(with_momentum("research/data", base))
    json.dump({"V2": [x0.tolist(), y0.tolist()], "V3": [x1.tolist(), y1.tolist()], "beta": BETA.tolist()}, open(FROZEN, "w"))
    print("frozen", FROZEN); sys.exit()

def dog_sim(arr, cfg):
    """D1 underdog rule on a momentum array: PnL per $1 at the print fill and at max(seen, fill)."""
    W, PM, WONUP, SIDEM, ASK = arr
    (t0, t1), (amin, amax), margin, tol = cfg["seconds"], cfg["ask"], cfg["margin"], cfg["limit"]
    out = []
    for i in range(len(W)):
        for t in range(t0, t1 + 1):
            pu = PM[i, t]
            if np.isnan(pu): continue
            ud = 1 if pu >= 0.5 else 0; p = 1 - pu if ud == 1 else pu
            seen = ASK[i, ud, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = float(seen[-1])
            if not (amin <= a <= amax) or p - a - FEE * a * (1 - a) < margin: continue
            nx = ASK[i, ud, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= a + tol + 1e-9)]
            if not len(nx): continue
            f = float(nx[0]); won = (ud == 0) == WONUP[i]
            out.append([(1 / x) * won - 1 - FEE * (1 - x) for x in (f, max(a, f))]); break
    o = np.array(out).reshape(-1, 2)
    return o[:, 0], o[:, 1]


def limit_sim(arr, cfg, nopre_gap=None):
    """C1 with the BUY limit raised to the highest cent where the model still clears cfg["floor"] after fee,
    entries cfg["seconds"]. Returns PnL per $1 at the print fill and at max(seen, fill).
    nopre_gap: skip an entry whose setup run started before the first entry second (NOPRE in variants.json)."""
    W, PM, WONUP, SIDEM, ASK = arr
    q = np.arange(0.99, 0.0, -0.01); t0, t1 = cfg.get("seconds", (230, 280))

    def cond(i, t):
        """(favoured side, seen ask) if the C1 condition holds at second t, else (side or -1, None)."""
        pu = PM[i, t]
        if np.isnan(pu): return -1, None
        sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
        if pf < 0.90: return sd, None
        seen = ASK[i, sd, t - 2 + L: t + 1 + L]; seen = seen[~np.isnan(seen)]
        if not len(seen): return sd, None
        a = float(seen[-1])
        return sd, (a if 0.60 <= a <= 0.90 and pf - a - FEE * a * (1 - a) >= 0.02 else None)

    out = []
    for i in range(len(W)):
        for t in range(t0, t1 + 1):
            sd, a = cond(i, t)
            if a is None: continue
            if nopre_gap is not None:
                s, last = t, t  # walk back through the setup run (bridging up to nopre_gap s without it)
                while s - 1 >= 200:
                    sd2, a2 = cond(i, s - 1)
                    if a2 is not None and sd2 == sd: last = s - 1
                    elif a2 is not None or last - (s - 1) > nopre_gap: break
                    s -= 1
                if last < t0: continue
            pu = PM[i, t]; pf = pu if sd == 0 else 1 - pu
            ok = pf - q - FEE * q * (1 - q) >= cfg["floor"] - 1e-9
            lim = min(0.99, max(a + cfg["base_tol"], float(q[ok][0]) if ok.any() else 0.0))
            nx = ASK[i, sd, t + 1 + L: t + 3 + L]; nx = nx[~np.isnan(nx) & (nx <= lim + 1e-9)]
            if not len(nx): continue
            f = float(nx[0]); won = (sd == 0) == WONUP[i]
            out.append([(1 / x) * won - 1 - FEE * (1 - x) for x in (f, max(a, f))]); break
    return np.array(out).reshape(-1, 2)


def vol_variant(d, arr, cfg):
    W, PM, WONUP, SIDEM, ASK = arr
    Z = volfit_lib.zgrid(d, W, cfg["vol_window"], cfg["vol_horizon"]); m = ~np.isnan(Z[:, 30])  # column 30 = 230s
    PM2 = PM.copy(); PM2[:, 200:291] = volfit_lib.prob(Z, cfg["k"]); PM2[~m] = PM[~m]  # short history: fall back to V0
    return W, PM2, WONUP, np.where(PM2 >= 0.5, 0, 1), ASK


maps = json.load(open(FROZEN))
tot = {v: [] for v in ("V0", "V1", "V2", "V3", "V4", "V5", "V1L", "V4L", "V5L", "V1N", "V4N", "V5N",
                       "V1@seen", "V4@seen", "V5@seen", "V1L@seen", "V4L@seen", "V5L@seen", "V1N@seen", "V4N@seen", "V5N@seen")}
FLAT3 = {"floor": 9.0, "base_tol": 0.03, "seconds": (230, 280)}  # limit_sim at a flat 3c = C1, for the capped score
LIMIT_FROM = 0  # INFORMAL copy: score the L/N variants on every day (not pre-registered)
NOPRE_FROM = 0  # INFORMAL copy
dog = {"fill": [], "seen": []}
for d in sys.argv[1:]:
    arr = arrays(d); mom = with_momentum(d, arr)
    variants = {"V0": arr, "V1": mom, "V2": recal2.apply(arr, *map(np.array, maps["V2"])),
                "V3": recal2.apply(mom, *map(np.array, maps["V3"])),
                "V4": vol_variant(d, arr, maps["V4"]), "V5": vol_variant(d, arr, maps["V5"])}
    row = []
    for v, a in variants.items():
        r = sim(a, 0.03)[:, 0]; tot[v].append(r)
        if v + "@seen" in tot: tot[v + "@seen"].append(limit_sim(a, FLAT3)[:, 1])
        row.append(f"{v} n={len(r):3d} {r.sum():+6.2f}")
    if int(arr[0].min()) >= LIMIT_FROM:
        for v in ("V1", "V4", "V5"):
            r = limit_sim(variants[v], maps["LIMIT"]); tot[v + "L"].append(r[:, 0]); tot[v + "L@seen"].append(r[:, 1])
            row.append(f"{v}L n={len(r):3d} {r[:, 0].sum():+6.2f} (seen {r[:, 1].sum():+6.2f})")
    if int(arr[0].min()) >= NOPRE_FROM:
        for v in ("V1", "V4", "V5"):
            r = limit_sim(variants[v], maps["LIMIT"], maps["NOPRE"]["gap"]); tot[v + "N"].append(r[:, 0]); tot[v + "N@seen"].append(r[:, 1])
            row.append(f"{v}N n={len(r):3d} {r[:, 0].sum():+6.2f} (seen {r[:, 1].sum():+6.2f})")
    r1, r2 = dog_sim(mom, maps["D1"]); dog["fill"].append(r1); dog["seen"].append(r2)
    row.append(f"D1 n={len(r1):3d} {r1.sum():+6.2f} (seen {r2.sum():+6.2f})")
    print(f"{d.split('/')[-1]:7s}", " | ".join(row), flush=True)
print("\ntotal")
for v, rs in tot.items():
    if not rs: continue
    r = np.concatenate(rs)
    print(f"  {v}: n={len(r)} pnl={r.sum():+.2f} mean={r.mean():+.4f} win={(r > 0).mean():.3f} t={r.mean() / (r.std() / math.sqrt(len(r))):+.2f}")
for k, rs in dog.items():
    r = np.concatenate(rs)
    if len(r) > 1:
        print(f"  D1 at {k}: n={len(r)} pnl={r.sum():+.2f} mean={r.mean():+.4f} win={(r > 0).mean():.3f} t={r.mean() / (r.std() / math.sqrt(len(r))):+.2f}")
