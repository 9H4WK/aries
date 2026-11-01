from __future__ import annotations

import time
from typing import Any

from .polymarket_api import PolymarketApi, current_window_start

OFFICIAL_CRYPTO_PRICE_MARKER = "polymarket.com/api/crypto/crypto-price"
OFFICIAL_PRICE_HISTORY_MARKER = "polymarket.com/api/crypto/price-history"

_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_RESET = "\033[0m"


def _get(config: Any, path: str, default: Any = None) -> Any:
    getter = getattr(config, "get_path", None)
    if callable(getter):
        return getter(path, default)
    cur: Any = config
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


async def verify_chainlink_settlement(config: Any, api: PolymarketApi) -> dict[str, Any]:
    """Verify that settlement and targets will use Polymarket's official Chainlink data.

    BTC 5m markets pay out on the Chainlink BTC/USD TWAP, so both the price to
    beat and the settling price must come from Polymarket's official endpoints.
    Binance is a signal feed only; settling from Binance close can disagree with
    the actual payout side. Returns a report dict with `ok`, `warnings`, and
    `probe` details. Never raises.
    """
    warnings: list[str] = []

    if bool(_get(config, "polymarket.allow_target_from_first_seen_after_open", False)):
        warnings.append(
            "binance_target_fallback_enabled: polymarket.allow_target_from_first_seen_after_open "
            "is true, the price-to-beat may come from Binance instead of Chainlink"
        )

    crypto_url = str(_get(config, "polymarket.crypto_price_url", OFFICIAL_CRYPTO_PRICE_MARKER))
    if OFFICIAL_CRYPTO_PRICE_MARKER not in crypto_url:
        warnings.append(f"unexpected_crypto_price_url: {crypto_url}")
    history_url = str(_get(config, "polymarket.crypto_price_history_url", OFFICIAL_PRICE_HISTORY_MARKER))
    if OFFICIAL_PRICE_HISTORY_MARKER not in history_url:
        warnings.append(f"unexpected_crypto_price_history_url: {history_url}")

    if not bool(_get(config, "chainlink_reference.enabled", True)):
        warnings.append("chainlink_reference_disabled: current-price reference falls back to raw Binance")
    elif not bool(_get(config, "chainlink_reference.require_for_entry", True)):
        warnings.append("chainlink_entry_gate_disabled: entries allowed without a Chainlink-grade reference")

    interval = int(_get(config, "bot.interval_seconds", 300))
    probe_window = current_window_start(int(time.time()), interval) - interval * 2
    probe: dict[str, Any] = {
        "window_start_ts": probe_window,
        "crypto_price_reachable": False,
        "open_price": None,
        "close_price": None,
        "completed": False,
        "price_history_reachable": False,
    }

    snapshot = await api.fetch_crypto_price(probe_window)
    if snapshot and snapshot.get("open_price") is not None:
        probe["crypto_price_reachable"] = True
        probe["open_price"] = snapshot.get("open_price")
        probe["close_price"] = snapshot.get("close_price")
        probe["completed"] = bool(snapshot.get("completed", False))
    if not probe["crypto_price_reachable"]:
        warnings.append(
            "official_endpoint_unreachable: no Chainlink openPrice for the last completed window, "
            "settlement will fall back to Binance close and may not match Polymarket payouts"
        )
    elif probe["close_price"] is None or not probe["completed"]:
        warnings.append(
            "official_close_price_missing: endpoint responded but the completed-window TWAP "
            "was not returned, settlement may fall back to Binance close"
        )

    history = await api.fetch_price_history(probe_window)
    probe["price_history_reachable"] = bool(history)
    if not history:
        warnings.append(
            "price_history_unreachable: open-price lag fallback unavailable, the price-to-beat "
            "may be delayed at window open"
        )

    return {"ok": not warnings, "warnings": warnings, "probe": probe}


def format_settlement_report(report: dict[str, Any]) -> str:
    """Render the settlement-source check as a console banner."""
    probe = report.get("probe", {})
    bar = "=" * 74
    if report.get("ok"):
        header = f"{_GREEN} SETTLEMENT SOURCE CHECK: OK - official Chainlink source active{_RESET}"
        body = [
            f" probe window {probe.get('window_start_ts')} | open {probe.get('open_price')} "
            f"| close {probe.get('close_price')} | completed {probe.get('completed')}",
            " settlement will use settled_from_polymarket_chainlink_twap",
        ]
    else:
        header = f"{_RED} SETTLEMENT SOURCE CHECK: WARNING - settlement may NOT use Chainlink{_RESET}"
        body = [f" {_YELLOW}{warning}{_RESET}" for warning in report.get("warnings", [])]
        body.append(
            " recorded PnL may not match Polymarket payouts; Binance-close settlement is only"
        )
        body.append(" an approximation. Fix connectivity/config before running live.")
    lines = [bar, header, *[f" {line}" for line in body], bar]
    return "\n".join(lines)
