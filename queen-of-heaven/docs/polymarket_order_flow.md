# Polymarket Order-Book and Trade Flow

Aries uses Polymarket flow as bounded side information. It does not replace the
Binance target/MA model, and it never bypasses quote, timing, sizing, or risk
checks.

## What The Feed Can Measure

The public Polymarket market channel provides:

- L2 bid/ask price levels and resting share sizes.
- order placement/cancellation updates through `price_change`.
- matched trades through `last_trade_price`, including reported side and size.

It does **not** provide the number of unique people at a price. One participant
can split orders, and resting orders can be cancelled. Aries therefore labels
all book and trade quantities as shares, not people.

## Calculations

For each UP and DOWN token, best-level queue imbalance is:

```text
queue_imbalance = (best_bid_size - best_ask_size)
                  / (best_bid_size + best_ask_size)
```

Multi-level depth imbalance uses the same formula over the nearest configured
levels:

```text
depth_imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
```

The queue-weighted microprice is:

```text
microprice = (ask * bid_size + bid * ask_size)
             / (bid_size + ask_size)
```

Because buying DOWN is bearish for UP, cross-outcome signals are calculated as:

```text
cross_queue = (UP_queue - DOWN_queue) / 2
cross_depth = (UP_depth - DOWN_depth) / 2
```

Matched-trade pressure uses reported trade side and share size:

```text
bullish_shares = UP_BUY + DOWN_SELL
bearish_shares = UP_SELL + DOWN_BUY

trade_imbalance = (bullish_shares - bearish_shares)
                  / (bullish_shares + bearish_shares)
```

Available components are combined with configurable weights. The raw signal is
smoothed using fast and slow exponential moving averages. Missing recent trades
do not count as neutral trades; the remaining book components are reweighted.

## Probability Integration

The final flow signal is between `-1` and `+1`:

```text
delta = clamp(flow_signal * probability_weight,
              -max_probability_adjustment,
              +max_probability_adjustment)

p_up = clamp(existing_p_up + delta)
p_down = 1 - p_up
```

The default maximum adjustment is four percentage points. The adjusted
probabilities then flow through the existing:

- first-entry edge and confidence checks
- repair and lock candidate comparisons
- Kelly/growth sizing
- high-conviction and sold-side recovery checks
- dominant, weak-side, profit, and stop sell plans

There are no PM-flow-only BUY or SELL triggers.

## Configuration

```yaml
polymarket_flow:
  enabled: true
  levels: 5
  trade_lookback_seconds: 30
  fast_ema_seconds: 5
  slow_ema_seconds: 20
  fast_ema_weight: 0.70
  max_age_seconds: 3
  min_depth_shares: 5.0
  min_trade_volume_shares: 1.0
  queue_weight: 0.30
  depth_weight: 0.25
  microprice_weight: 0.15
  trade_weight: 0.30
  apply_to_probability: true
  probability_weight: 0.06
  max_probability_adjustment: 0.04
```

Set `apply_to_probability: false` to collect and display the information without
letting it affect decisions.

## Research Basis And Limitations

- [Polymarket Market Channel](https://docs.polymarket.com/market-data/websocket/market-channel)
  documents L2 snapshots, order changes, and matched-trade events.
- [Cont, Kukanov, and Stoikov, *The Price Impact of Order Book Events*](https://arxiv.org/abs/1011.6402)
  finds short-horizon price changes are strongly related to order-flow imbalance
  and market depth.
- [Gould and Bonart, *Queue Imbalance as a One-Tick-Ahead Price Predictor*](https://arxiv.org/abs/1512.03492)
  finds statistically significant predictive information in best-queue imbalance.
- [Stoikov, *The Micro-Price*](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2970694)
  develops queue-sensitive price estimation from limit-order-book state.

These results support the feature construction, not Aries' default weights or
profitability on Polymarket BTC five-minute markets. Resting liquidity can be
cancelled or spoofed, prediction-market token books are linked, and model
weights still require out-of-sample paper calibration.
