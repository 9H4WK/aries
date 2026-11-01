"""Local-clock correction for recorder files (research/record_feeds.py stamps rows with local receive time).

The home PC clock drifted: about -0.1s on Oct 20 01:00 UTC, +0.75s by Oct 21 15:00, then -7.1s from
Oct 22 (Windows Time stopped). Each file's offset (local minus real) is estimated from its Binance
rows, offset = p05(recv - trade_time) - BN_LATENCY, where BN_LATENCY is the one-way delay measured
on Oct 23 against Binance server time (0.176s at p05). Real receive time = recv - offset.
Files with too few Binance rows take the previous file's offset.
"""
import gzip, json, zlib
import numpy as np

BN_LATENCY = 0.18
_cache = {}


def _estimate(path):
    lat = []
    try:
        with gzip.open(path, "rt") as fh:
            for line in fh:
                if line.startswith('["bn"'):
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    lat.append(r[1] - r[2] / 1000.0)
    except (EOFError, OSError, zlib.error):
        pass
    return float(np.percentile(lat, 5)) - BN_LATENCY if len(lat) >= 50 else None


def offsets(paths):
    """Map each path (in the given order) to its local-minus-real clock offset."""
    out, last = {}, 0.0
    for p in paths:
        if p not in _cache:
            _cache[p] = _estimate(p)
        last = _cache[p] if _cache[p] is not None else last
        out[p] = last
    return out
