# ADX, VWAP, and MACD Side Information

Aries uses these indicators as bounded side information. They do not replace the
core BTC target-distance, MA, volume, order-book, S/R, and PM-flow model.

## ADX

ADX measures trend strength, not direction. Aries uses it as a regime filter:

- weak ADX pulls the raw probability score toward zero, which pulls p_up/p_down
  toward 50/50;
- strong and rising ADX boosts the MA-alignment component because trend-following
  signals are more trustworthy in a trending regime.

Important settings live under `technical_indicators`:

- `adx_period`
- `adx_weak_threshold`
- `adx_strong_threshold`
- `adx_weak_neutral_pull`
- `adx_strong_ma_boost`

## VWAP

VWAP is treated as intraday fair value. Aries uses rolling 5-minute VWAP by
default because the markets are BTC 5m windows.

- near VWAP, price above/below VWAP is mild directional confirmation;
- far above VWAP makes new UP buys smaller;
- far below VWAP makes new DOWN buys smaller;
- if profit trims are enabled, far VWAP extension can count as a weakening
  condition for trimming a profitable side.

Important settings:

- `vwap_lookback_seconds`
- `vwap_overextension_bps`
- `vwap_size_reduction_enabled`
- `vwap_size_reduction_bps`
- `vwap_size_reduction_max_fraction`
- `profit_trim_count_vwap_overextension_as_weakness`

## MACD Histogram Slope

MACD histogram slope is used as momentum confirmation:

- rising histogram nudges probability toward UP;
- falling histogram nudges probability toward DOWN;
- the nudge is capped and should be treated as confirmation, not a primary
  trading signal.

Important settings:

- `macd_fast`
- `macd_slow`
- `macd_signal`
- `macd_slope_lookback`
- `macd_histogram_slope_bps_scale`
- `macd_probability_weight`

## Terminal

Enable or disable the panel with:

```yaml
dashboard:
  panels:
    technicals: true
```
