import random

import pytest
from types import SimpleNamespace

from aries.config import Config
from aries.models import ProbabilityState
from aries.queen_of_heaven import candidate_value_reject_reason, strategy_reject_reason
from aries.strategy import AriesBot
from aries.twap_probability import estimate

CONFIG = {"bot": {"interval_seconds": 300}, "queen_of_heaven": {"twap_vol_window_seconds": 120}}
WINDOW = 10_000


def bars_from(prices_by_second):
    return sorted(prices_by_second.items())


def noisy_walk(start, end, base=100.0, drift=0.0):
    prices, price = {}, base
    for second in range(start, end + 1):
        price += drift + (0.05 if second % 2 else -0.05)
        prices[second] = price
    return prices


def test_flat_market_is_a_coin_flip():
    prices = noisy_walk(WINDOW - 400, WINDOW + 200)
    result = estimate(CONFIG, bars_from(prices), WINDOW, WINDOW + 200)
    assert result is not None
    assert abs(result.p_up - 0.5) < 0.05


def test_price_far_above_open_is_confident_up():
    prices = noisy_walk(WINDOW - 400, WINDOW)
    prices.update(noisy_walk(WINDOW + 1, WINDOW + 230, base=prices[WINDOW] + 40.0))
    result = estimate(CONFIG, bars_from(prices), WINDOW, WINDOW + 230)
    assert result.p_up > 0.95
    assert result.p_up <= 0.99  # flip_rate keeps a resolution-mismatch floor


def test_locked_twap_portion_dominates_late_in_window():
    prices = noisy_walk(WINDOW - 400, WINDOW)
    # Spent the TWAP period well above the open, then dropped back to it at the very end.
    prices.update({s: prices[WINDOW] + 30.0 for s in range(WINDOW + 1, WINDOW + 295)})
    prices[WINDOW + 295] = prices[WINDOW]
    result = estimate(CONFIG, bars_from(prices), WINDOW, WINDOW + 295)
    assert result.expected_final > result.open_twap + 25.0
    assert result.p_up > 0.95


def test_missing_history_returns_none():
    prices = noisy_walk(WINDOW - 30, WINDOW + 100)
    assert estimate(CONFIG, bars_from(prices), WINDOW, WINDOW + 100) is None


def test_edge_after_fee_gate():
    config = {
        "polymarket": {"taker_fee_rate": 0.07},
        "queen_of_heaven": {
            "min_pm_ask": 0.80,
            "max_pm_ask": 0.95,
            "min_model_probability": 0.0,
            "min_edge": -1.0,
            "min_net_edge": -1.0,
            "min_win_profit_fraction": 0.0,
            "min_edge_after_fee": 0.02,
        },
    }
    fee = 0.07 * 0.88 * 0.12
    common = dict(price=0.88, notional_usdc=2.0, edge=0.0, net_edge=0.0)
    assert candidate_value_reject_reason(config, model_probability=0.88 + fee + 0.021, **common) is None
    assert (
        candidate_value_reject_reason(config, model_probability=0.88 + fee + 0.019, **common)
        == "queen_edge_after_fee_too_low"
    )


def _queen_bot(model):
    bot = object.__new__(AriesBot)
    bot.mode = "paper"
    bot.config = Config({"bot": {"interval_seconds": 300}, "queen_of_heaven": {
        "enabled": True, "modes": ["paper"], "probability_model": model, "twap_vol_window_seconds": 120}})
    return bot


def test_twap_mode_replaces_heuristic_probability():
    prices = noisy_walk(WINDOW - 400, WINDOW)
    prices.update(noisy_walk(WINDOW + 1, WINDOW + 250, base=prices[WINDOW] + 40.0))
    heuristic = ProbabilityState(0.6, 0.4, 0.2, 40.0, 4.0, True, "estimated")
    state = SimpleNamespace(bars=bars_from(prices))
    market = SimpleNamespace(window_start_ts=WINDOW)

    replaced = AriesBot._queen_twap_probability(_queen_bot("twap"), heuristic, state, market, WINDOW + 250)
    assert replaced.reason == "twap_calibrated"
    assert replaced.p_up > 0.95 and abs(replaced.p_up + replaced.p_down - 1.0) < 1e-12
    assert replaced.inside_noise_band is False
    kept = AriesBot._queen_twap_probability(_queen_bot("heuristic"), heuristic, state, market, WINDOW + 250)
    assert kept is heuristic


def test_heuristic_gates_can_be_disabled():
    config = {
        "queen_of_heaven": {
            "enabled": True, "modes": ["paper"], "min_pm_ask": 0.5, "max_pm_ask": 0.9, "min_model_probability": 0.85,
            "min_confidence": 0.7, "min_edge": 0.0, "min_net_edge": 0.0, "min_win_profit_fraction": 0.0,
            "min_seconds_since_open": 240, "max_seconds_since_open": 290, "min_seconds_remaining": 9,
            "require_ma_alignment": True,
        }
    }
    candidate = SimpleNamespace(valid=True, role="FIRST_ENTRY", side="UP", price=0.8, notional_usdc=2.0, edge=0.1, net_edge=0.1)
    prob = SimpleNamespace(p_up=0.92, p_down=0.08, confidence=0.84, distance_bps=1.0)
    indicators = SimpleNamespace(pressure="DOWN", ma_fast_slope_bps=-1.0, ma_mid_slope_bps=-1.0)
    kwargs = dict(mode="paper", candidate=candidate, prob=prob, indicators=indicators,
                  binance_state=SimpleNamespace(price=100.0), market=SimpleNamespace(target_price=99.0),
                  seconds_since_open=250, seconds_remaining=50)
    assert strategy_reject_reason(config, **kwargs) is not None
    config["queen_of_heaven"]["apply_heuristic_gates"] = False
    assert strategy_reject_reason(config, **kwargs) is None


def test_vol_horizon_measures_variance_over_longer_changes():
    # Alternating +/-0.05 steps: 1s changes look volatile, but 2s changes cancel out.
    prices = noisy_walk(WINDOW - 400, WINDOW + 230)
    one = estimate(CONFIG, bars_from(prices), WINDOW, WINDOW + 230)
    config = {**CONFIG, "queen_of_heaven": {**CONFIG["queen_of_heaven"], "twap_vol_horizon_seconds": 2}}
    two = estimate(config, bars_from(prices), WINDOW, WINDOW + 230)
    assert one.sigma_per_second == pytest.approx(0.05)
    assert two.sigma_per_second < 1e-5


def test_vol_horizon_matches_one_second_vol_on_a_random_walk():
    rng = random.Random(7)
    prices, price = {}, 100.0
    for second in range(WINDOW - 1000, WINDOW + 231):
        price += rng.gauss(0.0, 1.0)
        prices[second] = price
    base = {**CONFIG, "queen_of_heaven": {"twap_vol_window_seconds": 900}}
    one = estimate(base, bars_from(prices), WINDOW, WINDOW + 230)
    thirty = estimate({**base, "queen_of_heaven": {**base["queen_of_heaven"], "twap_vol_horizon_seconds": 30}},
                      bars_from(prices), WINDOW, WINDOW + 230)
    assert one.sigma_per_second == pytest.approx(1.0, rel=0.1)
    assert thirty.sigma_per_second == pytest.approx(one.sigma_per_second, rel=0.3)


def test_momentum_extends_the_latest_move():
    prices = noisy_walk(WINDOW - 400, WINDOW + 247)
    prices.update({WINDOW + 248: prices[WINDOW + 247] + 5.0, WINDOW + 249: prices[WINDOW + 247] + 10.0,
                   WINDOW + 250: prices[WINDOW + 247] + 10.0})
    plain = estimate(CONFIG, bars_from(prices), WINDOW, WINDOW + 250)
    config = {**CONFIG, "queen_of_heaven": {**CONFIG["queen_of_heaven"], "twap_momentum": [0.25, 0.0, 0.0]}}
    trending = estimate(config, bars_from(prices), WINDOW, WINDOW + 250)
    # The last 2s rose 5; each of the 50 remaining seconds is expected 0.25 * 5 above the spot.
    assert trending.expected_final - plain.expected_final == pytest.approx(50 * 0.25 * 5 / 60)
    assert trending.p_up > plain.p_up
