"""TWAP-aware Brownian probability that a BTC 5m window resolves UP."""
import math
import numpy as np

WINDOW, LOOKBACK = 300, 60
_erf = np.vectorize(math.erf)

def phi(z):
    return 0.5 * (1.0 + _erf(np.asarray(z) / math.sqrt(2.0)))

def twap_prob_up(prices, w_idx, t, vol_window=300, vol_floor=0.0):
    """prices: 1s close array; w_idx: index of window start second; t: seconds since open.
    Returns (p_up, z, sigma_per_sec, expected_final, open_twap)."""
    open_twap = prices[w_idx - LOOKBACK + 1: w_idx + 1].mean()
    now = w_idx + t
    seg = prices[now - vol_window: now + 1]
    sigma = max(float(np.std(np.diff(seg))), vol_floor)
    s_t = prices[now]
    tw_start = WINDOW - LOOKBACK  # 240
    if t < tw_start:
        exp_final = s_t
        var = sigma ** 2 * ((tw_start - t) + LOOKBACK / 3.0)
    else:
        k = t - tw_start
        acc = prices[w_idx + tw_start + 1: now + 1].mean() if k > 0 else s_t
        rem = LOOKBACK - k
        exp_final = (k * acc + rem * s_t) / LOOKBACK
        var = sigma ** 2 * (rem ** 3) / (3.0 * LOOKBACK ** 2)
    sd = math.sqrt(max(var, 1e-12))
    z = (exp_final - open_twap) / sd
    return float(phi(z)), z, sigma, exp_final, open_twap
