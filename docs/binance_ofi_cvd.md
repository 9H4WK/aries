# Binance OFI + CVD Probability Nudges

Aries keeps its base probability model in `aries/probability.py`. Binance OFI
and CVD are side-information only. They cannot force trades and they do not
bypass quote freshness, edge, sizing, sell, or risk checks.

## OFI

Order-flow imbalance estimates short-term liquidity pressure from top-of-book
changes. Positive OFI means bid-side demand increased or ask-side supply was
removed. Negative OFI means the opposite.

Implemented from Binance `bookTicker` updates in `aries/binance_ws.py`:

```text
ofi =
  + new_bid_qty if new_bid_price >= old_bid_price
  - old_bid_qty if new_bid_price <= old_bid_price
  - new_ask_qty if new_ask_price <= old_ask_price
  + old_ask_qty if new_ask_price >= old_ask_price
```

The rolling OFI signal is:

```text
ofi_signal = sum(ofi_events) / sum(abs(ofi_events))
```

So it stays between `-1` and `+1`.

## CVD

Cumulative volume delta estimates aggressive taker flow. Binance trade payload
field `m` says whether the buyer was maker:

- `m = false`: buyer was taker, so signed volume is positive.
- `m = true`: seller was taker, so signed volume is negative.

The rolling CVD signal is:

```text
cvd_signal = (taker_buy_volume - taker_sell_volume)
             / (taker_buy_volume + taker_sell_volume)
```

So it also stays between `-1` and `+1`.

## Probability Use

`aries/binance_flow.py` blends OFI and CVD into one `signal`.

```text
delta = signal * binance_flow.probability_weight
delta is capped by binance_flow.max_probability_adjustment
p_up = clamp(p_up + delta)
p_down = 1 - p_up
```

If Aries already leans UP and Binance flow is positive, `p_up` rises slightly.
If Aries leans UP and Binance flow is negative, `p_up` falls and sizing/sell
urgency use that lower probability. DOWN works the same in reverse.

Main tuning lives in `config.yaml`:

- `binance_flow.lookback_seconds`
- `binance_flow.ofi_weight`
- `binance_flow.cvd_weight`
- `binance_flow.probability_weight`
- `binance_flow.max_probability_adjustment`
- `binance_flow.min_trade_volume_btc`
- `binance_flow.min_book_events`
