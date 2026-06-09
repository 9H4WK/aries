from __future__ import annotations


class LiveExecutor:
    def __init__(self, config: dict):
        self.config = config

    def place_order(self, *args, **kwargs):
        self._guard()
        # TODO: Implement real Polymarket live order placement after paper trading is validated.
        raise RuntimeError("Live execution is disabled. Paper trading only.")

    def cancel_order(self, *args, **kwargs):
        self._guard()
        # TODO: Implement real Polymarket live order cancellation after paper trading is validated.
        raise RuntimeError("Live execution is disabled. Paper trading only.")

    def redeem(self, *args, **kwargs):
        self._guard()
        # TODO: Implement real Polymarket redemption flow after paper trading is validated.
        raise RuntimeError("Live execution is disabled. Paper trading only.")

    def _guard(self) -> None:
        enabled = bool(self.config.get_path("live.enabled", False))
        allow_real_orders = bool(self.config.get_path("live.allow_real_orders", False))
        if enabled and allow_real_orders:
            raise RuntimeError("Live execution is disabled. Paper trading only.")
        raise RuntimeError("Live execution is disabled. Paper trading only.")
