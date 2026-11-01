from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _logit(p: float) -> float:
    p = _clamp(p, 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class AdaptiveState:
    """Persisted self-improvement state for calibration and risk scaling."""

    a: float = 1.0
    b: float = 0.0
    samples: int = 0
    brier_ema: float = 0.25
    loss_streak_mult: float = 1.0
    wins: int = 0
    losses: int = 0


def brownian_bridge_p_up(
    distance_bps: Optional[float],
    vol_1s_bps: Optional[float],
    seconds_remaining: Optional[float],
    twap_lookback_seconds: float = 60.0,
) -> Optional[float]:
    """Structural UP probability from distance, volatility, and time left.

    Models the Chainlink TWAP close as a driftless bridge from the current
    reference price: P(UP) = Phi(d / (sigma * sqrt(tau_eff))). The final
    resolution price is a trailing TWAP average, which damps variance; the
    `twap_lookback_seconds / 3` term is the standard variance reduction of an
    averaged Brownian segment.
    """
    if distance_bps is None or vol_1s_bps is None or seconds_remaining is None:
        return None
    if vol_1s_bps <= 0 or seconds_remaining <= 0:
        return None
    tau_eff = max(1.0, float(seconds_remaining)) + max(0.0, float(twap_lookback_seconds)) / 3.0
    sigma = float(vol_1s_bps) * math.sqrt(tau_eff)
    if sigma <= 0:
        return None
    z = _clamp(float(distance_bps) / sigma, -3.5, 3.5)
    return _clamp(normal_cdf(z), 0.01, 0.99)


class OnlineCalibrator:
    """Platt scaling: calibrated_p = sigmoid(a * logit(p_raw) + b).

    Fit online with SGD on settled window outcomes so probabilities (and the
    Kelly sizes derived from them) stay honest as market regime shifts.
    """

    def __init__(self, learning_rate: float = 0.08, l2: float = 0.001):
        self.learning_rate = max(1e-4, float(learning_rate))
        self.l2 = max(0.0, float(l2))

    def apply(self, p_raw: float, state: AdaptiveState) -> float:
        if state.samples < 1:
            return _clamp(p_raw, 0.01, 0.99)
        z = _clamp(state.a * _logit(p_raw) + state.b, -8.0, 8.0)
        return _clamp(_sigmoid(z), 0.01, 0.99)

    def observe(self, p_raw: float, won_up: bool, state: AdaptiveState) -> None:
        y = 1.0 if won_up else 0.0
        z = state.a * _logit(p_raw) + state.b
        p = _sigmoid(_clamp(z, -8.0, 8.0))
        err = p - y
        lz = _clamp(_logit(p_raw), -8.0, 8.0)
        state.a = _clamp(state.a - self.learning_rate * (err * lz + 2.0 * self.l2 * state.a), 0.05, 10.0)
        state.b = _clamp(state.b - self.learning_rate * err, -4.0, 4.0)
        state.samples += 1

    def batch_fit(self, samples: list[tuple[float, bool]], state: AdaptiveState, epochs: int = 400) -> int:
        """Bulk warm-start fit via regularized Newton-Raphson (Platt's method).

        Keeps the online SGD state so continued per-window learning works.
        """
        usable = [(float(p), 1.0 if w else 0.0) for p, w in samples if p is not None and 0.0 < p < 1.0]
        if len(usable) < 10:
            return 0
        lam = max(1e-4, self.l2 * 10.0)
        a, b = state.a, state.b
        # Both classes must be present for a meaningful fit.
        ys = [y for _, y in usable]
        if min(ys) == max(ys):
            return 0
        for _ in range(max(10, int(epochs))):
            ga = gb = h_aa = h_ab = h_bb = 0.0
            for p_raw, y in usable:
                z = _clamp(a * _logit(p_raw) + b, -8.0, 8.0)
                p = _sigmoid(z)
                err = p - y
                lz = _clamp(_logit(p_raw), -8.0, 8.0)
                w_pq = max(1e-6, p * (1.0 - p))
                ga += err * lz
                gb += err
                h_aa += w_pq * lz * lz
                h_ab += w_pq * lz
                h_bb += w_pq
            ga += lam * a
            h_aa += lam
            h_bb += lam
            det = h_aa * h_bb - h_ab * h_ab
            if abs(det) < 1e-12:
                break
            da = (h_bb * ga - h_ab * gb) / det
            db = (h_aa * gb - h_ab * ga) / det
            new_a = _clamp(a - da, 0.05, 10.0)
            new_b = _clamp(b - db, -4.0, 4.0)
            if abs(new_a - a) < 1e-7 and abs(new_b - b) < 1e-7:
                a, b = new_a, new_b
                break
            a, b = new_a, new_b
        state.a, state.b = a, b
        state.samples += len(usable)
        return len(usable)


def blend_logit(
    components: list[tuple[Optional[float], float]],
) -> Optional[float]:
    """Weighted logit-space blend; missing components drop out with renormalized weights."""
    total_weight = 0.0
    acc = 0.0
    for value, weight in components:
        if value is None or weight <= 0:
            continue
        acc += weight * _clamp(_logit(value), -8.0, 8.0)
        total_weight += weight
    if total_weight <= 0:
        return None
    return acc / total_weight


@dataclass
class ProbabilityComponents:
    p_model: Optional[float] = None
    p_bridge: Optional[float] = None
    p_market_mid: Optional[float] = None
    blended: Optional[float] = None
    calibrated: bool = False


class AdaptiveEngine:
    """Self-improving probability calibration and Kelly risk scaling.

    - Blends the feature model with a Brownian-bridge prior and the Polymarket
      mid-price in logit space.
    - Calibrates the blend with online Platt scaling learned from settled windows.
    - Scales fractional-Kelly sizing by measured calibration quality (Brier)
      and throttles after losing streaks.
    """

    STATE_KEY = "adaptive_state_v1"

    def __init__(self, config_get_path, db=None):
        self._get = config_get_path
        self.db = db
        self.state = AdaptiveState()
        self.calibrator = OnlineCalibrator(
            learning_rate=float(self._get("adaptive.calibration_learning_rate", 0.08)),
            l2=float(self._get("adaptive.calibration_l2", 0.001)),
        )
        self.last_warm_start_samples: int = 0

    # ---- persistence -------------------------------------------------
    def load(self) -> None:
        if self.db is None:
            return
        getter = getattr(self.db, "get_adaptive_state", None)
        raw = getter(self.STATE_KEY) if getter else None
        if not raw:
            return
        try:
            payload = json.loads(raw)
            self.state = AdaptiveState(
                a=float(payload.get("a", 1.0)),
                b=float(payload.get("b", 0.0)),
                samples=int(payload.get("samples", 0)),
                brier_ema=float(payload.get("brier_ema", 0.25)),
                loss_streak_mult=float(payload.get("loss_streak_mult", 1.0)),
                wins=int(payload.get("wins", 0)),
                losses=int(payload.get("losses", 0)),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            self.state = AdaptiveState()

    def save(self) -> None:
        if self.db is None:
            return
        setter = getattr(self.db, "set_adaptive_state", None)
        if setter:
            setter(self.STATE_KEY, json.dumps(asdict(self.state)))

    def warm_start_from_db(self) -> int:
        """Fit the calibrator on first-BUY decisions joined to settled outcomes.

        Mines both the SQLite decisions table and the JSONL decisions log
        (the log retains more history than the pruned table) and trains on
        the union of windows found.
        """
        if self.db is None or not bool(self._get("adaptive.warm_start_on_boot", True)):
            return 0
        list_results = getattr(self.db, "list_window_results", None)
        list_decisions = getattr(self.db, "list_decisions", None)
        if list_results is None:
            return 0
        results = {
            (str(row["market_id"]), int(row["window_start"])): str(row["winning_side"]).upper()
            for row in list_results()
        }
        first: dict[tuple[str, int], float] = {}
        if list_decisions is not None:
            for row in list_decisions():
                payload = row.get("payload") or {}
                self._collect_warm_sample(payload, results, first)
        for payload in self._jsonl_decision_payloads():
            self._collect_warm_sample(payload, results, first)
        samples = [
            (p, results[key] == "UP")
            for key, p in first.items()
        ]
        fitted = self.calibrator.batch_fit(samples, self.state, epochs=int(self._get("adaptive.calibration_epochs", 400)))
        self.last_warm_start_samples = fitted
        if fitted:
            self.save()
        return fitted

    def _collect_warm_sample(self, payload: dict, results: dict, first: dict) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("decision") not in {"BUY_UP", "BUY_DOWN"}:
            return
        market_id = payload.get("market_id")
        window_start = payload.get("window_start")
        if market_id is None or window_start is None or payload.get("p_up") is None:
            return
        key = (str(market_id), int(window_start))
        if key not in results:
            return
        try:
            p_up = float(payload["p_up"])
        except (TypeError, ValueError):
            return
        first.setdefault(key, p_up)

    def _jsonl_decision_payloads(self):
        import json as _json
        from pathlib import Path as _Path

        path = self._get("logging.decisions_log", None)
        if not path or not _Path(path).exists():
            return []
        payloads = []
        try:
            with _Path(path).open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    if event.get("event") != "DECISION":
                        continue
                    payloads.append(event.get("payload", event))
        except OSError:
            return []
        return payloads

    # ---- probability --------------------------------------------------
    def enabled(self) -> bool:
        return bool(self._get("adaptive.enabled", True))

    def augment_probability(
        self,
        prob,
        indicators=None,
        up_quote=None,
        down_quote=None,
        seconds_remaining: Optional[int] = None,
    ):
        """Blend model/bridge/market-mid and calibrate in place on a ProbabilityState."""
        if not self.enabled() or prob is None or prob.p_up is None:
            return prob, ProbabilityComponents()

        comps = ProbabilityComponents(p_model=prob.p_up)
        comps.p_bridge = brownian_bridge_p_up(
            prob.distance_bps,
            (indicators.volatility_60s * 10_000.0) if indicators and indicators.volatility_60s else None,
            seconds_remaining,
            float(self._get("adaptive.twap_lookback_seconds", 60.0)),
        )
        if up_quote is not None and down_quote is not None:
            up_mid = up_quote.mid
            down_mid = down_quote.mid
            if up_mid is not None and down_mid is not None and 0.0 < up_mid < 1.0 and 0.0 < down_mid < 1.0:
                comps.p_market_mid = _clamp((up_mid + (1.0 - down_mid)) / 2.0, 0.01, 0.99)

        blended_logit = blend_logit(
            [
                (comps.p_model, float(self._get("adaptive.model_weight", 0.55))),
                (comps.p_bridge, float(self._get("adaptive.bridge_weight", 0.30))),
                (comps.p_market_mid, float(self._get("adaptive.market_mid_weight", 0.15))),
            ]
        )
        if blended_logit is None:
            return prob, comps
        blended = _clamp(_sigmoid(blended_logit), 0.01, 0.99)
        comps.blended = blended

        if bool(self._get("adaptive.calibration_enabled", True)):
            calibrated = self.calibrator.apply(blended, self.state)
            comps.calibrated = self.state.samples >= int(self._get("adaptive.min_calibration_samples", 40))
        else:
            calibrated = blended

        prob.p_up = calibrated
        prob.p_down = 1.0 - calibrated
        prob.confidence = min(1.0, abs(calibrated - 0.5) * 2.0)
        if prob.inside_noise_band:
            prob.confidence *= 0.25
        return prob, comps

    # ---- outcome feedback ---------------------------------------------
    def observe_outcome(self, p_up: Optional[float], won_up: bool) -> None:
        """One settled-window observation: calibrate + Brier EMA + streak throttle."""
        if not self.enabled() or p_up is None:
            return
        alpha = _clamp(float(self._get("adaptive.brier_ema_alpha", 0.03)), 0.001, 1.0)
        outcome = 1.0 if won_up else 0.0
        brier = (p_up - outcome) ** 2
        self.state.brier_ema = (1.0 - alpha) * self.state.brier_ema + alpha * brier
        if bool(self._get("adaptive.calibration_enabled", True)):
            self.calibrator.observe(p_up, won_up, self.state)
        if won_up:
            self.state.wins += 1
        else:
            self.state.losses += 1
        if bool(self._get("adaptive.drawdown_throttle_enabled", True)):
            if won_up:
                recovery = float(self._get("adaptive.win_recovery", 1.05))
                self.state.loss_streak_mult = min(1.0, self.state.loss_streak_mult * recovery)
            else:
                decay = float(self._get("adaptive.loss_streak_decay", 0.85))
                floor = float(self._get("adaptive.loss_streak_floor", 0.4))
                self.state.loss_streak_mult = max(floor, self.state.loss_streak_mult * decay)
        self.save()

    # ---- sizing ---------------------------------------------------------
    def kelly_multiplier(self) -> float:
        """Scale fractional Kelly by calibration skill and drawdown throttle."""
        if not self.enabled():
            return 1.0
        mult = 1.0
        if (
            bool(self._get("adaptive.kelly_calibration_enabled", True))
            and self.state.samples >= int(self._get("adaptive.min_calibration_samples", 40))
        ):
            baseline = max(0.01, float(self._get("adaptive.brier_baseline", 0.25)))
            ratio = baseline / max(1e-6, self.state.brier_ema)
            mult *= _clamp(ratio, float(self._get("adaptive.kelly_min_multiplier", 0.6)), float(self._get("adaptive.kelly_max_multiplier", 1.25)))
        if bool(self._get("adaptive.drawdown_throttle_enabled", True)):
            mult *= self.state.loss_streak_mult
        return _clamp(mult, 0.1, 1.5)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "a": round(self.state.a, 4),
            "b": round(self.state.b, 4),
            "samples": self.state.samples,
            "brier_ema": round(self.state.brier_ema, 4),
            "kelly_multiplier": round(self.kelly_multiplier(), 3),
            "wins": self.state.wins,
            "losses": self.state.losses,
            "warm_start_samples": self.last_warm_start_samples,
        }
