from __future__ import annotations

import json
import time
import urllib.request
from typing import Optional

# Exchange-aligned wall clock. Market windows, Binance bar seconds and Polymarket
# timestamps are exchange time, so the bot reads the time through `now()`: the local
# clock plus an offset measured against Binance server time. With a drifting local
# clock (seen: 6.5s slow on a PC with Windows Time stopped) the TWAP model ignores
# the newest Binance bars, because they are stamped "in the future", and every
# entry and window rollover shifts by the same amount. All local timestamps go
# through this clock, so ages between them are unchanged.

_offset = 0.0


def now() -> float:
    return time.time() + _offset


def offset() -> float:
    return _offset


def measure(url: str, samples: int = 5, timeout: float = 5.0) -> Optional[tuple[float, float]]:
    """Return (offset, rtt) from the lowest-latency of `samples` server-time requests."""
    best: Optional[tuple[float, float]] = None
    for _ in range(samples):
        try:
            t0 = time.time()
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                server = float(json.load(resp)["serverTime"]) / 1000.0
            t1 = time.time()
        except Exception:
            continue
        if best is None or t1 - t0 < best[1]:
            best = (server - (t0 + t1) / 2.0, t1 - t0)
    return best


def sync(url: str) -> Optional[tuple[float, float]]:
    """Measure and apply the offset; keeps the previous one if every request fails."""
    global _offset
    result = measure(url)
    if result is not None:
        _offset = result[0]
    return result


def set_offset(value: float) -> None:
    global _offset
    _offset = float(value)
