# Support/Resistance Side Information

Aries uses support/resistance as side information only. It nudges the existing
UP/DOWN probability and therefore affects entry, repair, sizing, and sell plans,
but it does not replace the MA/volume/order-book model.

## Research Basis

- Local extrema are a standard way to make technical patterns algorithmic rather
  than visual. Lo, Mamaysky, and Wang use automated local-extrema pattern
  extraction to test whether technical-analysis shapes contain incremental
  information.
- Osler's intraday FX work finds that published support/resistance levels can
  predict trend interruptions around those levels. Aries treats zones as
  probabilistic context, not guaranteed reversal points.

## Main Flow

1. `BinanceStream.bootstrap_history()` seeds recent Binance candles at startup.
2. Live ticks keep one-second OHLC bars in `BinanceState.ohlc_bars`.
3. `SupportResistanceEngine.update()` aggregates those bars into configured
   S/R candles, default 15 seconds.
4. `calculate_support_resistance()` finds swing highs/lows using fractal
   left/right candles, clusters nearby pivots into horizontal zones, and scores
   the nearest support/resistance.
5. `apply_support_resistance_bias()` applies a capped probability nudge.
6. `JsonlLogger.terminal_status()` shows the S/R panel when enabled.

## Important Settings

```yaml
support_resistance:
  enabled: true
  candle_seconds: 15
  lookback_minutes: [30, 60]
  include_3h: false
  zone_width_usd: 25.0
  zone_width_bps: 4.0
  refresh_seconds: 5
  swing_left: 2
  swing_right: 2
  reaction_candles: 4
  retest_candles: 6
  probability_weight: 0.08
  max_probability_adjustment: 0.06
```

- `lookback_minutes`: separate windows used to build nested structure.
- `include_3h`: adds a 180-minute higher-timeframe map.
- `zone_width_usd` / `zone_width_bps`: clustering width for nearby levels.
- `swing_left` / `swing_right`: how many candles must confirm a pivot.
- `reaction_candles`: recent candles checked for bounce or rejection.
- `retest_candles`: recent candles checked for breakout retests.
- `probability_weight`: how strongly S/R affects p_up/p_down.
- `max_probability_adjustment`: hard cap on the probability nudge.

## State Fields

- `nearest_support` / `nearest_resistance`: closest active zones around BTC.
- `support_strength` / `resistance_strength`: touch count plus nested-window
  confirmation.
- `support_distance_bps` / `resistance_distance_bps`: distance from spot.
- `bounce_score`: recent candles touched support and closed back above it.
- `rejection_score`: recent candles touched resistance and closed back below it.
- `breakout_state`: `BREAKOUT_UP`, `BREAKOUT_DOWN`, or `NONE`.
- `retest_state`: successful retest after a breakout/breakdown.
- `nested_structure`: whether the zone appears in more than one lookback.
- `bias_side`: S/R-only side read, `UP`, `DOWN`, or `NEUTRAL`.
- `bias_score`: signed score from -1 to +1.

## Dashboard

Panel visibility is configured under:

```yaml
dashboard:
  panels:
    support_resistance: true
    polymarket: false
    flow_scores: false
    execution: false
    risk_timing: false
    optimization: false
```

Turn a panel back on by setting it to `true`.
