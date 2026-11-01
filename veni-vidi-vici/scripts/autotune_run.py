#!/usr/bin/env python3
"""
Supervisor to run the bot in paper mode for a fixed duration and perform
periodic optimizer/param-sweep steps every N windows (default 50). It will
restart the bot after applying conservative config tweaks discovered by the
local optimizer against settled history.

Usage: run this via PowerShell Start-Process so it keeps running in background.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aries.config import load_config
from aries.database import Database
from aries.optimizer import StrategyOptimizer


def backup_config(dst_dir: Path) -> Path:
    cfg = ROOT / "config.yaml"
    dst_dir.mkdir(parents=True, exist_ok=True)
    idx = int(time.time())
    dst = dst_dir / f"config_backup_{idx}.yaml"
    shutil.copy2(cfg, dst)
    return dst


def run_optimizer(cfg):
    db = Database(cfg.get_path("database.path", "aries.db"), float(cfg.get_path("storage.max_database_mb", 50.0)))
    opt = StrategyOptimizer(cfg, db)
    return opt.analyze()


def choose_tweak_from_optimizer(cfg):
    # Simple conservative search over a small set (same as earlier sweep)
    db = Database(cfg.get_path("database.path", "aries.db"), float(cfg.get_path("storage.max_database_mb", 50.0)))
    base = cfg
    candidates = []
    for fk in (0.02, 0.05, 0.10):
        for apply_rep in (False,):
            for pref in (18, 30):
                for me in (0.03, 0.05):
                    tweaks = {
                        "growth_sizing.fractional_kelly": fk,
                        "growth_sizing.apply_kelly_to_repairs": apply_rep,
                        "timing.preferred_first_entry_seconds": pref,
                        "edge.min_edge": me,
                    }
                    # apply tweak into a temp config object
                    cfg2 = deepcopy(base)
                    for path, val in tweaks.items():
                        parts = path.split(".")
                        cur = cfg2
                        for p in parts[:-1]:
                            if p not in cur or not isinstance(cur[p], dict):
                                cur[p] = {}
                            cur = cur[p]
                        cur[parts[-1]] = val
                    try:
                        opt = StrategyOptimizer(cfg2, db)
                        report = opt.analyze()
                        exp = report.get("overall", {}).get("expectancy")
                        pf = report.get("overall", {}).get("profit_factor")
                        candidates.append((exp or -9999.0, pf or -9999.0, tweaks))
                    except Exception:
                        continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]


def apply_tweaks_to_disk(tweaks):
    cfg_path = ROOT / "config.yaml"
    cfg = load_config(cfg_path)
    for path, val in tweaks.items():
        parts = path.split(".")
        cur = cfg
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = val
    # write back
    with cfg_path.open("w", encoding="utf-8") as f:
        import yaml

        yaml.safe_dump(dict(cfg), f, sort_keys=False)


def count_settled_windows(cfg):
    db = Database(cfg.get_path("database.path", "aries.db"), float(cfg.get_path("storage.max_database_mb", 50.0)))
    rows = db.list_window_report_rows()
    return len(rows)


def main():
    cfg = load_config(ROOT / "config.yaml")
    interval = int(cfg.get_path("bot.interval_seconds", 300))
    total_seconds = 9 * 3600
    end_time = time.time() + total_seconds
    target_batch = 50

    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    backups = ROOT / "config_backups"

    # Starting baseline count
    baseline_settled = count_settled_windows(cfg)
    cycle = 0
    supervisor_report = []

    while time.time() < end_time:
        cycle += 1
        print(f"[autotune] cycle {cycle} start: launching bot")
        backup = backup_config(backups)

        stdout_path = logs_dir / f"autotune_bot_stdout_cycle{cycle}.log"
        stderr_path = logs_dir / f"autotune_bot_stderr_cycle{cycle}.log"

        # Start the bot subprocess in paper mode
        bot_proc = subprocess.Popen([sys.executable, "main.py"], cwd=str(ROOT), stdout=open(stdout_path, "ab"), stderr=open(stderr_path, "ab"))
        try:
            deadline = min(end_time, time.time() + target_batch * interval + 300)
            while time.time() < deadline:
                time.sleep(30)
                cfg_now = load_config(ROOT / "config.yaml")
                settled = count_settled_windows(cfg_now)
                got = settled - baseline_settled
                print(f"[autotune] cycle {cycle}: settled windows delta={got}")
                if got >= target_batch:
                    print(f"[autotune] cycle {cycle}: reached {got} settled windows")
                    break
                if time.time() >= end_time:
                    break

            # Run optimizer and pick a conservative tweak
            cfg_now = load_config(ROOT / "config.yaml")
            opt_report = run_optimizer(cfg_now)
            tweak = choose_tweak_from_optimizer(cfg_now)
            supervisor_report.append({
                "cycle": cycle,
                "baseline_backup": str(backup),
                "settled_delta": got,
                "optimizer_overall": opt_report.get("overall", {}),
                "chosen_tweak": tweak,
            })

            # Apply tweak conservatively if found
            if tweak:
                print(f"[autotune] cycle {cycle}: applying tweak {tweak}")
                apply_tweaks_to_disk(tweak)
            else:
                print(f"[autotune] cycle {cycle}: no tweak selected")

        finally:
            # Stop bot
            try:
                bot_proc.send_signal(signal.SIGTERM)
                time.sleep(2)
                if bot_proc.poll() is None:
                    bot_proc.kill()
            except Exception:
                pass

        # refresh baseline
        cfg = load_config(ROOT / "config.yaml")
        baseline_settled = count_settled_windows(cfg)

    # Write supervisor report
    out = ROOT / "reports" / "autotune_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(supervisor_report, indent=2))
    print(f"[autotune] finished; report written to {out}")


if __name__ == "__main__":
    main()
