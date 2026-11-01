"""Offline replay validation: old policy vs adaptive policies on settled history.

Source: logs/decisions.jsonl DECISION payloads joined to settled window_results.

Replays first-BUY decisions through:
  P0  baseline            - every deadline entry taken (what history did)
  P1  edge-gated deadline - deadline entries require net_edge >= 0.02, conf >= 0.12
  P2  P1 + longshot gates - min_ask 0.28 / max_ask 0.88
  P3  P2 + adaptive prob  - bridge/market-mid blend + Platt calibration
                            (fitted on the train split only)

Each simulated entry spends a fixed $2 at the logged ask.
"""

import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aries.adaptive import AdaptiveState, OnlineCalibrator, blend_logit  # noqa: E402
from aries.config import load_config  # noqa: E402


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def bridge_p_up(distance_bps, vol_60s, seconds_remaining, twap_lookback=60.0):
    if distance_bps is None or vol_60s is None or seconds_remaining is None:
        return None
    if vol_60s <= 0 or seconds_remaining <= 0:
        return None
    tau_eff = max(1.0, float(seconds_remaining)) + twap_lookback / 3.0
    sigma = vol_60s * 10_000.0 * math.sqrt(tau_eff)
    if sigma <= 0:
        return None
    return clamp(norm_cdf(clamp(distance_bps / sigma, -3.5, 3.5)), 0.01, 0.99)


def main():
    config = load_config("config.yaml")
    conn = sqlite3.connect(config.get_path("database.path", "aries.db"))
    conn.row_factory = sqlite3.Row
    results = {
        (r["market_id"], r["window_start"]): r["winning_side"]
        for r in conn.execute("SELECT market_id, window_start, winning_side FROM window_results WHERE resolved=1")
    }

    first = {}
    with open("logs/decisions.jsonl", "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") != "DECISION":
                continue
            p = event.get("payload", event)
            if p.get("decision") not in {"BUY_UP", "BUY_DOWN"}:
                continue
            key = (str(p.get("market_id")), int(p.get("window_start")))
            if key not in results or key in first:
                continue
            if p.get("p_up") is None or p.get("selected_side") is None:
                continue
            side = p["selected_side"]
            ask = p.get("up_ask") if side == "UP" else p.get("down_ask")
            if ask is None or ask <= 0 or ask >= 1:
                continue
            cand = p.get("candidate_up") if side == "UP" else p.get("candidate_down")
            mkt_mid_up = None
            if p.get("up_bid") is not None and p.get("up_ask"):
                mkt_mid_up = clamp((p["up_bid"] + p["up_ask"]) / 2.0, 0.01, 0.99)
            first[key] = {
                "window_start": key[1],
                "side": side,
                "ask": float(ask),
                "market_mid_up": mkt_mid_up,
                "p_model": float(p["p_up"]),
                "confidence": float(p.get("confidence") or 0.0),
                "distance_bps": p.get("distance_bps"),
                "vol_60s": p.get("volatility_60s"),
                "seconds_remaining": p.get("seconds_remaining"),
                "net_edge": (cand or {}).get("net_edge"),
                "edge": float((p.get("edge_up") if side == "UP" else p.get("edge_down")) or 0.0),
                "reason": p.get("selected_reason"),
            }

    obs = sorted(first.values(), key=lambda o: o["window_start"])
    print(f"replayable first-entry decisions: {len(obs)}")

    result_by_ws = {}
    for (market_id, ws), side in results.items():
        result_by_ws.setdefault(ws, side)
    for o in obs:
        winning = result_by_ws[o["window_start"]]
        o["won_side"] = winning == o["side"]
        o["won_up"] = winning == "UP"

    split = int(len(obs) * 0.7)
    train, test = obs[:split], obs[split:]
    print(f"train windows: {len(train)}  test windows: {len(test)}")

    calibrator = OnlineCalibrator(
        learning_rate=float(config.get_path("adaptive.calibration_learning_rate", 0.08)),
        l2=float(config.get_path("adaptive.calibration_l2", 0.001)),
    )
    state = AdaptiveState()
    n_fit = calibrator.batch_fit([(o["p_model"], o["won_up"]) for o in train], state, epochs=400)
    print(f"Platt warm-start fit on {n_fit} train samples -> a={state.a:.3f} b={state.b:.3f}")

    def adaptive_p(o):
        p_bridge = bridge_p_up(o["distance_bps"], o["vol_60s"], o["seconds_remaining"])
        blended_logit = blend_logit(
            [
                (o["p_model"], 0.55),
                (p_bridge, 0.30),
                (o["market_mid_up"], 0.15),
            ]
        )
        if blended_logit is None:
            return None
        return calibrator.apply(sigmoid(blended_logit), state)

    def simulate(policy, entries, prob_fn=None):
        pnl = wins = n = 0
        briers = []
        for o in entries:
            if not policy(o):
                continue
            won = o["won_side"]
            shares = 2.0 / o["ask"]
            pnl += (shares if won else 0.0) - 2.0
            wins += 1 if won else 0
            n += 1
            p_used = prob_fn(o) if prob_fn else o["p_model"]
            if p_used is not None:
                briers.append((p_used - (1.0 if o["won_up"] else 0.0)) ** 2)
        return pnl, wins, n, (sum(briers) / len(briers) if briers else None)

    def p0(_o):
        return True

    def p1(o):
        net = o["net_edge"] if o["net_edge"] is not None else o["edge"]
        return net >= 0.02 and o["confidence"] >= 0.12

    def p2(o):
        return p1(o) and 0.28 <= o["ask"] <= 0.88

    def p3(o):
        pa = adaptive_p(o)
        if pa is None:
            return False
        return (pa - o["ask"]) >= 0.02 and abs(pa - 0.5) * 2 >= 0.12 and 0.28 <= o["ask"] <= 0.88

    print()
    for label, fn, prob_fn in (
        ("P0 baseline(all deadline)", p0, None),
        ("P1 edge-gated", p1, None),
        ("P2 +longshot gates", p2, None),
        ("P3 +adaptive prob", p3, adaptive_p),
    ):
        tr = simulate(fn, train, prob_fn)
        te = simulate(fn, test, prob_fn)
        tr_s = f"train pnl={tr[0]:+8.2f} n={tr[2]:>4} win={tr[1] / max(1, tr[2]):.2f}"
        te_s = f"test pnl={te[0]:+8.2f} n={te[2]:>4} win={te[1] / max(1, te[2]):.2f}"
        b_s = f"brier={te[3]:.4f}" if te[3] is not None else ""
        print(f"{label:<26} {tr_s} | {te_s} {b_s}")

    old_b, new_b = [], []
    for o in test:
        y = 1.0 if o["won_up"] else 0.0
        old_b.append((o["p_model"] - y) ** 2)
        pa = adaptive_p(o)
        if pa is not None:
            new_b.append((pa - y) ** 2)
    print(f"\ntest-split Brier  old-model={sum(old_b) / len(old_b):.4f}  adaptive-blend={sum(new_b) / len(new_b):.4f}")


if __name__ == "__main__":
    main()
