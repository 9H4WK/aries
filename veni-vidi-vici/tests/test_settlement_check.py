import asyncio

from aries.config import Config
from aries.settlement_check import format_settlement_report, verify_chainlink_settlement


class StubApi:
    def __init__(self, crypto_price=None, price_history=None):
        self.crypto_price = crypto_price
        self.price_history = price_history or []
        self.crypto_price_calls = []
        self.price_history_calls = []

    async def fetch_crypto_price(self, event_start_ts, symbol=None):
        self.crypto_price_calls.append(event_start_ts)
        return self.crypto_price

    async def fetch_price_history(self, event_start_ts, symbol=None):
        self.price_history_calls.append(event_start_ts)
        return self.price_history


def make_config(**polymarket_overrides):
    polymarket = {}
    polymarket.update(polymarket_overrides)
    return Config(
        {
            "bot": {"interval_seconds": 300},
            "polymarket": polymarket,
            "chainlink_reference": {"enabled": True, "require_for_entry": True},
        }
    )


def completed_snapshot(window_start_ts):
    return {
        "window_start_ts": window_start_ts,
        "open_price": 64_231.79,
        "close_price": 64_228.30,
        "completed": True,
    }


def run_check(config, api):
    return asyncio.run(verify_chainlink_settlement(config, api))


def test_ok_when_official_source_reachable():
    api = StubApi(crypto_price=completed_snapshot(1_787_100_000), price_history=[(1_787_100_000.0, 64_231.79)])
    report = run_check(make_config(), api)
    assert report["ok"] is True
    assert report["warnings"] == []
    assert report["probe"]["crypto_price_reachable"] is True
    assert report["probe"]["open_price"] == 64_231.79
    assert report["probe"]["close_price"] == 64_228.30
    assert report["probe"]["completed"] is True
    assert report["probe"]["price_history_reachable"] is True
    assert len(api.crypto_price_calls) == 1
    assert api.crypto_price_calls[0] == api.price_history_calls[0]


def test_probe_window_is_last_completed_window():
    api = StubApi(crypto_price=completed_snapshot(0), price_history=[(0.0, 1.0)])
    report = run_check(make_config(), api)
    probe_ts = report["probe"]["window_start_ts"]
    assert probe_ts % 300 == 0
    assert api.crypto_price_calls[0] == probe_ts


def test_warns_when_official_endpoint_unreachable():
    report = run_check(make_config(), StubApi(crypto_price=None))
    assert report["ok"] is False
    assert any(w.startswith("official_endpoint_unreachable") for w in report["warnings"])


def test_warns_when_open_price_missing():
    api = StubApi(
        crypto_price={"window_start_ts": 1, "open_price": None, "close_price": None, "completed": False},
        price_history=[(1.0, 2.0)],
    )
    report = run_check(make_config(), api)
    assert report["ok"] is False
    assert any(w.startswith("official_endpoint_unreachable") for w in report["warnings"])


def test_warns_when_close_price_missing():
    api = StubApi(
        crypto_price={"window_start_ts": 1, "open_price": 64_000.0, "close_price": None, "completed": False},
        price_history=[(1.0, 64_000.0)],
    )
    report = run_check(make_config(), api)
    assert report["ok"] is False
    assert any(w.startswith("official_close_price_missing") for w in report["warnings"])


def test_warns_when_price_history_unreachable():
    api = StubApi(crypto_price=completed_snapshot(1), price_history=[])
    report = run_check(make_config(), api)
    assert report["ok"] is False
    assert any(w.startswith("price_history_unreachable") for w in report["warnings"])


def test_warns_when_binance_target_fallback_enabled():
    api = StubApi(crypto_price=completed_snapshot(1), price_history=[(1.0, 2.0)])
    config = make_config(allow_target_from_first_seen_after_open=True)
    report = run_check(config, api)
    assert report["ok"] is False
    assert any(w.startswith("binance_target_fallback_enabled") for w in report["warnings"])


def test_warns_when_chainlink_reference_disabled():
    api = StubApi(crypto_price=completed_snapshot(1), price_history=[(1.0, 2.0)])
    config = make_config()
    config["chainlink_reference"]["enabled"] = False
    report = run_check(config, api)
    assert report["ok"] is False
    assert any(w.startswith("chainlink_reference_disabled") for w in report["warnings"])


def test_warns_when_entry_gate_disabled():
    api = StubApi(crypto_price=completed_snapshot(1), price_history=[(1.0, 2.0)])
    config = make_config()
    config["chainlink_reference"]["require_for_entry"] = False
    report = run_check(config, api)
    assert report["ok"] is False
    assert any(w.startswith("chainlink_entry_gate_disabled") for w in report["warnings"])


def test_warns_on_unexpected_endpoint_urls():
    api = StubApi(crypto_price=completed_snapshot(1), price_history=[(1.0, 2.0)])
    config = make_config(
        crypto_price_url="https://example.com/wrong",
        crypto_price_history_url="https://example.com/wrong-history",
    )
    report = run_check(config, api)
    assert report["ok"] is False
    assert any(w.startswith("unexpected_crypto_price_url") for w in report["warnings"])
    assert any(w.startswith("unexpected_crypto_price_history_url") for w in report["warnings"])


def test_accepts_plain_dict_config():
    api = StubApi(crypto_price=completed_snapshot(1), price_history=[(1.0, 2.0)])
    config = {"bot": {"interval_seconds": 300}, "polymarket": {}, "chainlink_reference": {"enabled": True}}
    report = run_check(config, api)
    assert report["ok"] is True


def test_format_report_ok_banner():
    api = StubApi(crypto_price=completed_snapshot(1_787_100_000), price_history=[(1_787_100_000.0, 64_231.79)])
    report = run_check(make_config(), api)
    banner = format_settlement_report(report)
    assert "SETTLEMENT SOURCE CHECK: OK" in banner
    assert "settled_from_polymarket_chainlink_twap" in banner


def test_format_report_warning_banner_lists_warnings():
    report = run_check(make_config(), StubApi(crypto_price=None))
    banner = format_settlement_report(report)
    assert "SETTLEMENT SOURCE CHECK: WARNING" in banner
    assert "official_endpoint_unreachable" in banner
    assert "Polymarket payouts" in banner
