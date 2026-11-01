"""Analyse recorded feeds: Chainlink vs Binance/Coinbase lag+basis, and PM book asks vs print-based backtest fills.

usage: python rec_analysis.py REC_DIR SCRIPTS_DIR [CACHE_DIR]
"""
import glob, gzip, json, math, os, sys, time, urllib.request, zlib
from bisect import bisect_right
from datetime import datetime
import numpy as np
sys.path.insert(0, sys.argv[2])
from twap_model import phi

rec_dir = sys.argv[1]
cache_dir = sys.argv[3] if len(sys.argv) > 3 else rec_dir
FEE, VW, K, EPS = 0.07, 900, 1.25, 0.01
CFG = dict(tmin=230, tmax=280, amin=0.60, amax=0.90, margin=0.02, pmin=0.90)  # C1


def get(url):
    for attempt in range(6):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))
        except Exception:
            time.sleep(2 + 3 * attempt)
    return None


def read_rows():
    rows = []
    for path in sorted(glob.glob(os.path.join(rec_dir, "*.jsonl.gz"))):
        try:
            with gzip.open(path, "rt") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except (EOFError, OSError, zlib.error):
            pass  # file still being written; keep what was readable
    return rows


rows = read_rows()
cl = {}; cl_lat = []
bn_t, bn_p, bn_lat = [], [], []
cb_t, cb_p, cb_lat = [], [], []
pm = {}
for r in rows:
    k = r[0]
    if k == "cl":
        cl[r[2]] = r[3]; cl_lat.append(r[1] - r[2])
    elif k == "bn":
        bn_t.append(r[2] / 1000); bn_p.append(r[3]); bn_lat.append(r[1] - r[2] / 1000)
    elif k == "cb":
        try:
            et = datetime.fromisoformat(r[2].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        cb_t.append(et); cb_p.append(r[3]); cb_lat.append(r[1] - et)
    elif k == "pm":
        pm.setdefault(r[2], []).append((r[1], r[3], r[4], r[5], r[6]))
order = np.argsort(bn_t); bn_t = np.array(bn_t)[order]; bn_p = np.array(bn_p)[order]
order = np.argsort(cb_t); cb_t = np.array(cb_t)[order]; cb_p = np.array(cb_p)[order]
first_s, last_s = int(min(cl)), int(max(cl))
print(f"recorded {datetime.fromtimestamp(first_s):%m-%d %H:%M} .. {datetime.fromtimestamp(last_s):%m-%d %H:%M}  rows={len(rows)} cl={len(cl)} bn={len(bn_t)} cb={len(cb_t)} pm_windows={len(pm)}")
q = lambda a: np.percentile(a, [50, 90, 99]).round(3).tolist() if len(a) else None
print(f"receive latency s (p50,p90,p99): chainlink(from obs second)={q(np.array(cl_lat))} binance={q(np.array(bn_lat))} coinbase={q(np.array(cb_lat))}")


def last_at(ts, ps, s):
    i = bisect_right(ts, s) - 1
    return ps[i] if i >= 0 else np.nan


secs = np.arange(first_s, last_s + 1)
CL = np.array([cl.get(s, np.nan) for s in secs])
# Chainlink's obs second s reflects prices up to about s; compare with exchange price at the end of second s+lag
for name, ts, ps in (("binance", bn_t, bn_p), ("coinbase", cb_t, cb_p)):
    ex = np.array([last_at(ts, ps, s + 1.0) for s in secs])
    basis = CL - ex
    ok = ~np.isnan(basis)
    b5 = []
    for s0 in range(0, len(secs) - 300, 60):
        a, b = basis[s0], basis[s0 + 300]
        if not (np.isnan(a) or np.isnan(b)): b5.append(abs(b - a))
    dcl = np.diff(CL)
    out = []
    for lag in range(-3, 4):
        exl = np.array([last_at(ts, ps, s + 1.0 + lag) for s in secs]); dex = np.diff(exl)
        m = ~np.isnan(dcl) & ~np.isnan(dex)
        out.append(f"{lag:+d}s:{np.corrcoef(dcl[m], dex[m])[0, 1]:.2f}")
    print(f"chainlink - {name}: basis mean={np.nanmean(basis):+.2f} std={np.nanstd(basis):.2f} |5min change| p50/p90/max={np.percentile(b5, 50):.2f}/{np.percentile(b5, 90):.2f}/{max(b5):.2f}")
    print(f"   corr(dChainlink[s], d{name}[s+lag]): " + " ".join(out))

# Resolution check per full window: Chainlink TWAP (the official rule) vs Binance-based TWAP
cl_secs = sorted(cl)
def cl_at(s):
    i = bisect_right(cl_secs, s) - 1
    return cl[cl_secs[i]] if i >= 0 else np.nan
windows = [w for w in range(first_s // 300 * 300 + 300, last_s - 300, 300)
           if sum(s in cl for s in range(w - 59, w + 301)) >= 0.95 * 360]
mism = 0; drift = []
for w in windows:
    o = np.mean([cl_at(s) for s in range(w - 59, w + 1)]); c = np.mean([cl_at(s) for s in range(w + 241, w + 301)])
    bo = np.mean([last_at(bn_t, bn_p, s) for s in range(w - 59, w + 1)]); bc = np.mean([last_at(bn_t, bn_p, s) for s in range(w + 241, w + 301)])
    mism += (c >= o) != (bc >= bo); drift.append((c - bc) - (o - bo))
dq = np.percentile(np.abs(drift), [50, 90]).round(2).tolist() if drift else None
print(f"windows with chainlink data: {len(windows)}; Binance-vs-Chainlink winner mismatches: {mism}; |basis change open->close TWAP| p50/p90: {dq}")

# ---- PM book vs prints, and print-based vs book-based strategy on the same windows ----
os.makedirs(cache_dir, exist_ok=True)
kl_path = os.path.join(cache_dir, "klines.json")
kl = json.load(open(kl_path)) if os.path.exists(kl_path) else {}
need_lo, need_hi = min(pm) - VW - 200, max(pm) + 300
have = sorted(int(s) for s in kl)
if not have or have[0] > need_lo or have[-1] < need_hi:
    cur = (have[-1] + 1 if have and have[0] <= need_lo else need_lo) * 1000  # extend an existing cache
    while cur < need_hi * 1000:
        k = get(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s&startTime={cur}&limit=1000") or []
        if not k: break
        for row in k: kl[str(int(row[0]) // 1000)] = float(row[4])
        cur = int(k[-1][0]) + 1000
    json.dump(kl, open(kl_path, "w"))
close = lambda s: kl.get(str(s)) if str(s) in kl else close(s - 1)

tr_path = os.path.join(cache_dir, "trades.json")
trades = json.load(open(tr_path)) if os.path.exists(tr_path) else {}
done_ws = [w for w in sorted(pm) if w + 420 < time.time()]
for w in done_ws:
    if str(w) in trades: continue
    ev = get(f"https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{w}")
    if not ev: continue
    m = ev[0]["markets"][0]; outs = json.loads(m["outcomes"]); pr = json.loads(m.get("outcomePrices") or "[]")
    winner = outs[pr.index("1")].upper() if "1" in pr else None
    tl, off = [], 0
    while True:
        page = get(f"https://data-api.polymarket.com/trades?market={m['conditionId']}&limit=500&offset={off}") or []
        tl += [(t["timestamp"] - w, t["outcomeIndex"], t["side"], t["price"], t["size"]) for t in page]
        if len(page) < 500: break
        off += 500
    trades[str(w)] = {"winner": winner, "t": tl}
json.dump(trades, open(tr_path, "w"))


def model_p(w, t):
    o = np.mean([close(s) for s in range(w - 59, w + 1)])
    d = np.diff([close(s) for s in range(w + t - VW, w + t + 1)]); sig = max(d.std(), 1e-9); s = close(w + t)
    if t < 240: ef, var = s, sig ** 2 * ((240 - t) + 20)
    else:
        k = t - 240; acc = np.mean([close(x) for x in range(w + 241, w + t + 1)]) if k else s; rem = 60 - k
        ef, var = (k * acc + rem * s) / 60, sig ** 2 * rem ** 3 / (3 * 3600)
    return EPS + (1 - 2 * EPS) * float(phi(((ef - o) / math.sqrt(var)) / K))


def book_ask(w, side, at):
    """Best ask for side (0=Up, 1=Down) as seen locally at time `at`."""
    rows_w = pm.get(w)
    if not rows_w: return np.nan
    i = bisect_right([r[0] for r in rows_w], at) - 1
    if i < 0 or at - rows_w[i][0] > 2.5: return np.nan  # no fresh book (recorder writes a 1s heartbeat)
    v = rows_w[i][2] if side == 0 else rows_w[i][4]
    return np.nan if v is None else v


gap_print = []  # book ask just before a BUY print minus the print price
results = {"print": [], "print_L3": [], "book_0.3s": [], "book_1.0s": []}
for w in done_ws:
    if str(w) not in trades or trades[str(w)]["winner"] is None: continue
    if str(w - VW - 100) not in kl: continue
    tw = trades[str(w)]; up_won = tw["winner"] == "UP"
    ask = np.full((2, 306), np.nan)
    for rel, oi, side, price, size in tw["t"]:
        if side == "BUY" and 0 <= rel <= 305:
            if np.isnan(ask[oi, rel]) or price < ask[oi, rel]: ask[oi, rel] = price
            if 230 <= rel <= 295:
                b = book_ask(w, oi, w + rel - 0.5)
                if not np.isnan(b): gap_print.append(b - price)
    P = {t: model_p(w, t) for t in range(CFG["tmin"], CFG["tmax"] + 3)}
    # print-based (as in the backtest), unshifted and with print times shifted by 3s
    for key, Ls in (("print", 0), ("print_L3", 3)):
        for t in range(CFG["tmin"], CFG["tmax"] + 1):
            pu = P[t]; sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
            seen = ask[sd, t - 2 + Ls: t + 1 + Ls]; seen = seen[~np.isnan(seen)]
            if not len(seen): continue
            a = seen[-1]
            if not (CFG["amin"] <= a <= CFG["amax"] and pf >= CFG["pmin"] and pf - a - FEE * a * (1 - a) >= CFG["margin"]): continue
            nx = ask[sd, t + 1 + Ls: t + 3 + Ls]; nx = nx[~np.isnan(nx) & (nx <= a + 0.01)]
            if not len(nx): continue
            f = nx[0]; won = (sd == 0) == up_won
            results[key].append((w, t, sd, f, won)); break
    # book-based: decide at end of second t (+0.2s) with the live book, fill at the book ask after order latency
    for key, lat in (("book_0.3s", 0.3), ("book_1.0s", 1.0)):
        for t in range(CFG["tmin"], CFG["tmax"] + 1):
            pu = P[t]; sd = 0 if pu >= 0.5 else 1; pf = pu if sd == 0 else 1 - pu
            dec = w + t + 1.2
            a = book_ask(w, sd, dec)
            if np.isnan(a) or not (CFG["amin"] <= a <= CFG["amax"] and pf >= CFG["pmin"] and pf - a - FEE * a * (1 - a) >= CFG["margin"]): continue
            f = book_ask(w, sd, dec + lat)
            if np.isnan(f) or f > a + 0.01: continue
            won = (sd == 0) == up_won
            results[key].append((w, t, sd, f, won)); break

if gap_print:
    g = np.array(gap_print)
    print(f"\nbook ask 0.5s before a BUY print minus print price (230-295s, n={len(g)}): p10/p50/p90={np.percentile(g, [10, 50, 90]).round(3).tolist()} mean={g.mean():+.3f}")
print(f"windows evaluated: {len([w for w in done_ws if str(w) in trades])}")
for key, rs in results.items():
    if not rs: print(f"  {key:10s} no trades"); continue
    pnl = [(1 / f) * won - 1 - (1 / f) * FEE * f * (1 - f) for _, _, _, f, won in rs]
    print(f"  {key:10s} trades={len(rs)} win={np.mean([r[4] for r in rs]):.0%} avg fill={np.mean([r[3] for r in rs]):.3f} pnl/$1={sum(pnl):+.2f}")
by_w = {}
for key, rs in results.items():
    for w, t, sd, f, won in rs: by_w.setdefault(w, {})[key] = f"{'UP' if sd == 0 else 'DN'}@{t}s {f:.2f} {'W' if won else 'L'}"
for w in sorted(by_w):
    print(f"   {datetime.fromtimestamp(w):%H:%M} " + " | ".join(f"{k}: {by_w[w].get(k, '-'):16s}" for k in results))
