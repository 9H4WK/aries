# Intra-Window Inventory Sells

## Conclusion

There is no proven universal MA-based sell rule for BTC 5-minute prediction
markets. The research-supported principle is narrower:

1. Treat open shares as risky inventory.
2. Value any immediate exit at the executable bid, not the midpoint or ask.
3. Reduce inventory more urgently as the finite trading horizon approaches.
4. Require several aligned signals before acting because one fast crossover is
   noisy.
5. Separate risk-reducing inventory trims from directional exits. A directional
   exit is justified only when its executable bid exceeds the model value and
   its added downside is explicitly capped.

Aries implements two optional paper-only sell modes. Neither is a profit-taking
model and neither blindly sells a side because its MA crossed.

## Research Basis

- Avellaneda and Stoikov model finite-horizon market making with an explicit
  inventory-risk penalty. The practical implication for Aries is that inventory
  must affect decisions; the paper does **not** prove Aries should sell more
  near a binary-market resolution.
  <https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf>
- Almgren and Chriss frame execution as a trade-off between transaction cost
  and the risk of continuing to hold inventory. It supports staged execution,
  but Aries' exact late-window fractions remain an unproven strategy setting.
  <https://www.smallake.kr/wp-content/uploads/2016/03/optliq.pdf>
- Cont, Kukanov, and Stoikov find short-horizon price changes are strongly
  related to order-flow imbalance and market depth. Aries keeps order-book
  imbalance as supporting information, but does not let one book observation
  independently trigger a sell.
  <https://arxiv.org/abs/1011.6402>
- Research on stop-loss rules does not establish reliable return improvement;
  the defensible benefit is primarily risk reduction. Aries therefore requires
  position-risk improvement and does not describe these sells as a source of
  guaranteed growth.
  <https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1214737>
- Polymarket documents that orders are limit orders, immediate orders execute
  against the book, SELL orders require conditional-token inventory/allowance,
  and taker fees can apply. Paper sells therefore use the current bid and live
  sells remain blocked.
  <https://docs.polymarket.com/concepts/order-lifecycle>
  <https://docs.polymarket.com/trading/orders/create>
  <https://docs.polymarket.com/trading/fees>
- Research on Kelly bettors in prediction markets shows that market prices can
  be strong aggregate forecasts and supports fractional-risk treatment when
  model beliefs differ from market prices. It does not prove Aries' probability
  model is better than PM, so directional exits stay tightly capped.
  <https://arxiv.org/abs/1201.6655>
  <https://arxiv.org/abs/2412.14144>

## Binary Position Accounting

Aries keeps `total_cost` as **net cash invested**:

```text
net_cost = gross_buy_cost - gross_sell_proceeds
pnl_if_up = remaining_up_shares - net_cost
pnl_if_down = remaining_down_shares - net_cost
worst_case_pnl = min(pnl_if_up, pnl_if_down)
```

Selling `q` UP shares at executable bid `b` changes the outcome PnL by:

```text
change_in_pnl_if_up = -(1 - b) * q
change_in_pnl_if_down = b * q
```

This means selling oversized UP inventory transfers PnL from the UP outcome to
the DOWN outcome. It is a valid rebalance while it improves the weaker outcome
and worst-case PnL.

Selling the smaller side does the opposite: it deliberately increases the
position tilt. For one binary share with Aries probability `p` and executable
bid `b`, the immediate model expected-value change is:

```text
expected_value_change_per_share = b - p
added_loss_if_sold_side_wins = 1 - b
```

The directional mode therefore requires `b - p` to be positive, strong
opposite-side probability/stability, weak sold-side stability, and hard caps on
the added worst-case loss. This is a directional hypothesis, not a repair.

## Stability Score

The MA anchor is used only for exits. With the current Binance stream, these
are one-second close bars, so MA144 needs roughly 144 seconds of history.

UP stability receives one point for each:

```text
fast > mid
mid > slow
spot > anchor
fast slope > 0
mid slope > 0
anchor slope >= 0
```

DOWN stability uses the reversed comparisons.

```text
score >= 4: stable
score 2-3: weak
score <= 1: unstable
opposite score >= 4 with own score <= 1: confirmed flip
```

The score must remain actionable for `confirmation_cycles` before a paper sell
is submitted.

## Sell Selection

### Inventory Trim

Aries considers the dominant side and requires all of:

- Paper sell feature enabled for the current mode.
- Fresh quote and executable bid.
- Anchor MA ready.
- Position is not locked, unless explicitly allowed.
- Composite stability weakness or a confirmed opposite flip.
- Position worst-case loss or late-window urgency.
- Bid/spread/probability-sacrifice gates pass.
- Shares and proceeds pass configured minimums.
- Simulated sale improves worst-case PnL.
- Sell cooldown and per-window action caps pass.

Partial trims can be followed by the existing buy/repair logic. A complete
final-window exit is treated as `FLAT_AFTER_EXIT`; Aries does not churn back
into the same window after intentionally selling all shares.

Permitted sell size increases with urgency:

```text
middle window: up to 25%
last 90 seconds: up to 50%
confirmed flip: up to 50%
last 45 seconds with weak stability: up to 100%
```

These are research hypotheses, not calibrated optimal values.

### Directional Weak-Side Exit

When `directional_weak_side_exit_enabled` is true, Aries may sell the
model-weak side of a balanced or already tilted box. It requires all of:

- The sold side has no more shares than the retained side.
- `p_weak` is at or below the configured weak-side ceiling.
- `p_retained` and retained-side stability exceed their configured floors.
- The probability gap is large enough and weak-side stability is low.
- The executable weak-side bid exceeds `p_weak` by
  `directional_min_model_exit_edge`.
- The order remains above minimum shares/proceeds after sizing.
- Added worst-case loss and post-sell worst-case loss remain under their caps.
- The normal window-loss and account-equity floors still pass.

This mode can act throughout the window after its configured minimum time, but
it still requires repeated confirmation cycles. It is disabled by default. It
also refuses to break a locked box unless `directional_allow_sell_locked` is
explicitly enabled.

Core paper-test settings:

```yaml
intra_window_sells:
  enabled: true
  directional_weak_side_exit_enabled: true
  directional_max_weak_side_probability: 0.25
  directional_min_retained_side_probability: 0.75
  directional_min_probability_gap: 0.50
  directional_min_model_exit_edge: 0.01
  directional_max_sell_fraction: 0.25
  directional_late_max_sell_fraction: 0.50
  directional_final_max_sell_fraction: 1.00
  directional_max_worst_case_deterioration_usdc: 3.00
  directional_max_post_sell_worst_case_loss_usdc: 3.00
```

`enforce_time_based_buy_limits` is a separate disabled-by-default experiment.
When enabled, ordinary same-side adds are blocked in the last 90 seconds and
the final 45 seconds permit only a BUY that immediately locks the structure.
This restriction does not activate merely because inventory sells are enabled.

## Important Limitations

- Paper sells fill immediately at the current best bid regardless of displayed
  depth. Large orders would receive worse execution in reality.
- The default paper sell minimum is five shares to match the currently observed
  CLOB minimum; query current market parameters before any future live support.
- Paper fills do not yet apply Polymarket's dynamic crypto taker-fee curve.
- Queue position, partial fills, latency, and bid movement during submission
  are not simulated.
- Stability, probability, and directional-exit thresholds have not been proven
  on Aries out-of-sample data.
- Live SELL submission is hard blocked with `live_sell_orders_disabled`.

For these reasons, leave `intra_window_sells.enabled: false` until running a
controlled paper comparison.

## Validation Protocol

Run enough settled paper windows with sells off and on, then compare:

- Total and average realized PnL.
- Worst-case PnL immediately before and after each sell.
- PnL by seconds-remaining bucket.
- Gross sell proceeds and number of sells.
- Outcome PnL sacrificed on the sold side.
- Percentage of sells followed by continued adverse price movement.
- Results after estimating taker fees and realistic depth slippage.

Do not promote the model to live execution from win rate alone. Require better
out-of-sample expectancy, smaller drawdown, and acceptable execution costs.
