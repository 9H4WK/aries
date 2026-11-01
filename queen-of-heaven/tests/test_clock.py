import time

from aries import clock


def test_now_applies_offset():
    try:
        clock.set_offset(6.5)
        assert abs(clock.now() - (time.time() + 6.5)) < 0.05
    finally:
        clock.set_offset(0.0)
    assert abs(clock.now() - time.time()) < 0.05


def test_sync_keeps_offset_when_server_unreachable(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("aries.clock.urllib.request.urlopen", fail)
    try:
        clock.set_offset(1.25)
        assert clock.sync("https://example.invalid/time") is None
        assert clock.offset() == 1.25
    finally:
        clock.set_offset(0.0)


def test_measure_uses_lowest_latency_sample(monkeypatch):
    import io
    import json

    # Two requests: the first slow (server says +10.0s at its midpoint), the second fast (+6.5s).
    ticks = iter([100.0, 102.0, 200.0, 200.2])
    servers = iter([111.0, 206.6])
    monkeypatch.setattr("aries.clock.time.time", lambda: next(ticks))
    monkeypatch.setattr(
        "aries.clock.urllib.request.urlopen",
        lambda url, timeout: io.BytesIO(json.dumps({"serverTime": next(servers) * 1000}).encode()),
    )
    offset, rtt = clock.measure("https://example.invalid/time", samples=2)
    assert abs(rtt - 0.2) < 1e-9
    assert abs(offset - 6.5) < 1e-9
