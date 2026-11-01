#!/usr/bin/env python3
import json
import os
import sys

# Ensure repo root is on sys.path when executed from scripts/ directory
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from aries.config import load_config
from aries.database import Database
from aries.optimizer import StrategyOptimizer


def main():
    config = load_config("config.yaml")
    db_path = config.get_path("database.path", "aries.db")
    db = Database(db_path, float(config.get_path("storage.max_database_mb", 50.0)))
    optimizer = StrategyOptimizer(config, db)
    report = optimizer.analyze()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
