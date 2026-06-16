# Aries Strategy Code Guide

This is a short map of the important strategy functions, variables, and
computations. Start with `AriesBot._decide()` when tracing any decision.

## Main Decision Flow

`aries/strategy.py`

- `_cycle()`: Resolves data, calls `_decide()`, executes the approved candidate,
  then logs and stores the result.
- `_decide()`: Builds one complete decision. It reads quotes/indicators,
  estimates probabilities, builds both BUY candidates, selects a BUY, then
  checks whether a SELL should override it.
- `_build_candidate(side, ...)`: Simulates buying one side at its PM ask.
  It assigns `FIRST_ENTRY`, `REPAIR`, or `SAME_SIDE_ADD`, sizes the order, and
  computes the resulting position.
- `_select_candidate(...)`: Applies rules shared by every BUY, including data
  freshness, timing, spread, locked state, and order count.

Important values:

- `p_side`: Aries' estimated probability that this side wins.
- `price`: Current executable PM ask for a BUY.
- `edge = p_side - price`: Positive means Aries values the side above its ask.
- `net_edge = p_side - net_price`: Edge after configured fee/slippage buffer.
- `shape_after`: Simulated position after the candidate fills.
- `shape_improvement`: Change in `worst_case_pnl`; positive means safer.
- `would_lock`: True when both possible resolution outcomes are profitable.

## Finding The First Side To Buy

`aries/strategy.py`

- `_select_first_entry()`: Chooses between the valid UP and DOWN candidates.
  Before the forced-entry deadline it requires edge/confidence; at the deadline
  it chooses the valid side with the best net edge.
- `_first_entry_deadline_seconds()`: Reads the configured forced-entry time.
- `_risk_caps_pass()`: Final account/window risk check before selection.

Main settings:

```yaml
timing:
  first_entry_start_seconds: 8
  preferred_first_entry_seconds: 18

entry:
  early_min_edge: 0.12
  early_min_confidence: 0.50
  force_entry_deadline_seconds: 42
  force_best_side_after_preferred: true

edge:
  min_edge: 0.07
  min_confidence: 0.10
  max_spread: 0.06
```

## Probability Calculation

`aries/probability.py`

- `estimate_probability()`: Combines target distance, velocity, MA alignment,
  MA slopes, volume spike, Binance order-book imbalance, persistence, and time.
- `raw`: Weighted sum of normalized inputs before conversion to probability.
- `p_up = sigmoid(raw)` and `p_down = 1 - p_up`.
- `confidence = abs(p_up - 0.5) * 2`; values near 1 are more directional.

Change feature weights/scales under `probability` in `config.yaml`. These are
model estimates, not guaranteed true probabilities.

`aries/indicators.py`

- `compute_indicators()`: Calculates moving averages, slopes, velocity, volume
  averages/spike, target persistence, order-book imbalance, and exit stability.

## Additional BUY Triggers

`aries/strategy.py`

- `_select_after_entry()`: Uses this priority: lock the box, repair the weak
  side, high-conviction same-side add, otherwise hold.
- `_select_lock_only()`: Allows an early repair only when it immediately locks.
- `_select_high_conviction_same_side()`: Attempts to add to the dominant side.
- `_high_conviction_reject_reason()`: Lists every same-side-add trigger. The
  returned reason explains exactly which gate blocked the add.

Main settings:

```yaml
timing:
  repair_start_after_first_entry_seconds: 5
  preferred_repair_after_first_entry_seconds: 8

risk:
  min_shape_improvement_usdc: 0.01
  allow_repair_at_small_negative_edge: true
  max_repair_negative_edge: 0.04

high_conviction_same_side:
  enabled: false
  min_probability: 0.80
  min_confidence: 0.60
  min_edge: 0.03
```

## Sizing Additional BUYs

`aries/sizing.py`

- `active_ladder()`: Returns the configured USDC ladder.
- `choose_clip()`: Picks the initial ladder step for entry, repair, or add.
- `size_for_price()`: Applies growth cap, remaining window risk, minimum
  notional, and minimum shares to the ladder amount.

`aries/growth_sizing.py`

- `growth_sizing_metrics()`: Calculates fractional-Kelly and remaining-risk
  limits. Repairs normally size toward the UP/DOWN PnL gap.
- `candidate_growth_score()`: Scores first-entry candidates using expected log
  growth, worst-case change, inventory imbalance, and order cost.
- `growth_cap_below_ladder`: No configured ladder clip fits below the allowed
  amount. Lower the ladder or change the relevant growth/risk settings.

## Position Shape

`aries/position_shape.py`

- `shape_from_fills()`: Reconstructs the position from stored BUY/SELL fills.
- `apply_buy()` / `apply_sell()`: Simulate a candidate before execution.
- `dominant_side()`: Side with more shares.
- `weak_side()`: Side with fewer shares and normally the repair target.

Core computation:

```text
total_cost = gross_buy_cost - gross_sell_proceeds
pnl_if_up = up_shares - total_cost
pnl_if_down = down_shares - total_cost
worst_case_pnl = min(pnl_if_up, pnl_if_down)
locked = worst_case_pnl > 0
```

## SELL Triggers

`aries/exit_strategy.py`

- `build_inventory_sell_candidate()`: Public SELL selector. It respects
  `dominant_inventory_trim_enabled` and `directional_weak_side_exit_enabled`
  independently. If both are enabled, inventory trims keep priority.
- `build_inventory_sell_probe()`: Terminal-only diagnostics for both sell
  roles. It explains the closest side and blocker without approving an order.
- `_build_dominant_inventory_trim()`: Sells oversized inventory only when the
  sale improves `worst_case_pnl`.
- `_build_directional_weak_side_exit()`: Sells a smaller/equal weak side only
  when its PM bid exceeds Aries' model probability and added risk stays capped.

`aries/strategy.py`

- `_confirm_sell_candidate()`: Requires the same side/role SELL signal for
  several cycles before execution.

Important SELL values:

- `bid`: Executable PM price received when selling.
- `model_exit_edge = bid - p_side`: Positive means Aries values immediate sale
  above holding the share to resolution.
- `worst_case_deterioration`: Added downside from a directional weak-side exit.
- `stability_score`: MA-based support for the side being sold.
- `opposite_stability_score`: MA-based support for the retained side.

All SELL tuning is under `intra_window_sells` in `config.yaml`. See
`docs/intra_window_sells.md` for detailed formulas and safety limits.

## Execution And Debugging

- `aries/paper_broker.py::submit_order()`: Final paper-order validation.
- `aries/paper_broker.py::_reject_reason()`: Read this when a paper order is
  rejected; every safety rejection originates here.
- `aries/paper_broker.py::simulate_fill()`: Stores an immediate paper fill.
- `aries/live_execution.py::place_order()`: Guarded live BUY/SELL execution.
  SELL additionally requires `live.allow_sell_orders`, sufficient conditional
  shares, and role-specific risk validation.
- `aries/logger.py::terminal_status()`: Controls terminal panels and values.

For any unexpected action, search `selected_reason`, `order_reason`, and the
candidate `reason` in `logs/decisions.jsonl`.
