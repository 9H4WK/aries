#!/usr/bin/env python3
"""
Run a small grid search over a few config knobs and report best-performing combos
using the advisory StrategyOptimizer (uses settled paper/live history from the
SQLite database). This does not change on-disk config.yaml.
"""
import copy
import json
from itertools import product
from pathlib import Path

import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from aries.config import load_config
from aries.database import Database
from aries.optimizer import StrategyOptimizer


def evaluate_config(base_cfg, db, tweaks: dict):
    cfg = copy.deepcopy(base_cfg)
    # Apply tweaks (dotted path keys)
    for path, value in tweaks.items():
        parts = path.split(".")
        cur = cfg
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = value

    optimizer = StrategyOptimizer(cfg, db)
    report = optimizer.analyze()
    overall = report.get("overall", {})
    return {
        "tweaks": tweaks,
        "samples": report.get("settled_traded_windows"),
        "expectancy": overall.get("expectancy"),
        "profit_factor": overall.get("profit_factor"),
        "payoff_ratio": overall.get("payoff_ratio"),
        "breakeven_win_rate": overall.get("breakeven_win_rate"),
    }


def main():
    base_cfg = load_config("config.yaml")
    db = Database(base_cfg.get_path("database.path", "aries.db"), float(base_cfg.get_path("storage.max_database_mb", 50.0)))

    fractional_kelly = [0.02, 0.05, 0.10]
    apply_kelly_to_repairs = [False, True]
    preferred_first_entry_seconds = [18, 30, 42]
    min_edge = [0.03, 0.05, 0.07]
    use_growth_score = [True, False]

    results = []
    total = 0
    for fk, akr, pref, me, ugs in product(fractional_kelly, apply_kelly_to_repairs, preferred_first_entry_seconds, min_edge, use_growth_score):
        tweaks = {
            "growth_sizing.fractional_kelly": fk,
            "growth_sizing.apply_kelly_to_repairs": akr,
            "timing.preferred_first_entry_seconds": pref,
            "edge.min_edge": me,
            "growth_sizing.use_growth_score_for_first_entry": ugs,
        }
        try:
            res = evaluate_config(base_cfg, db, tweaks)
            results.append(res)
        except Exception as e:
            results.append({"tweaks": tweaks, "error": str(e)})
        total += 1

    # sort by expectancy desc, fallback profit_factor
    good = [r for r in results if r.get("expectancy") is not None]
    good.sort(key=lambda r: (r.get("expectancy", -9999.0), r.get("profit_factor", -9999.0)), reverse=True)

    out = {
        "tried": total,
        "top_10": good[:10],
    }

    dest = Path("reports/param_sweep.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2))
    print(f"Wrote {dest.resolve()}")


if __name__ == "__main__":
    main()
