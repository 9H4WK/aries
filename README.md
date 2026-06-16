# Aries

Aries is a BTC 5-minute Polymarket strategy bot focused on BTC `UP` / `DOWN` window markets. It reads Binance BTC price pressure, evaluates both Polymarket sides, and can route the same approved decisions to paper trading or the guarded Polymarket CLOB V2 executor.

## Phase 1 vs Phase 2

Phase 1 was a dry-run engine:

- stream Binance BTCUSDT data
- resolve the active BTC 5-minute Polymarket market
- subscribe to UP and DOWN quotes
- build MA pressure and probabilities
- simulate shape changes in memory
- log every decision

Phase 2 adds paper trading and validation:

- approved `BUY_UP` / `BUY_DOWN` decisions create paper orders
- accepted paper orders fill immediately at the selected ask
- paper fills rebuild the position shape from SQLite
- resolved windows calculate realized PnL
- reports are written to `reports/`
- validation checks the session for stale quotes, missing reasons, bad fills, and PnL mismatches

Paper mode remains the default. Authenticated account monitoring and real execution
are separate, explicit modes.

## How To Run

```bash
python main.py
```

No command-line arguments are used. All settings come from `config.yaml`.

For a concise map of the important Python functions, variables, calculations,
and settings, read [`docs/strategy_code_guide.md`](docs/strategy_code_guide.md).

## Strategy Scope

Aries stays limited to:

- BTC 5-minute Polymarket `UP` / `DOWN` markets
- Binance BTC tick and moving-average pressure
- buy-both-sides shape logic
- paper trading, authenticated live monitoring, and guarded live BUY/SELL execution

“Buy both sides” means Aries always evaluates both candidates after the first entry. It does not force a balance trade at any price.

The default paper profile begins looking for a strong entry after 8 seconds,
uses the normal entry gates after 18 seconds, and at 42 seconds selects the
best valid side even when the normal confidence gate has not passed. This
targets a fill before 45 seconds while still rejecting stale quotes, invalid
prices, orders below the market minimum, and orders that cross risk limits.
After the first fill, Aries quickly evaluates opposite-side repairs that
improve worst-case PnL; it never balances at any price.

`risk.min_account_equity_fraction: 0.50` rejects a new order when the open
window's worst-case result would take the account below half of its configured
starting balance. Set it to `0` to disable this floor.

## Optional Intra-Window Sells

Aries includes disabled-by-default inventory-trim and weak-side-exit models. They use the
executable PM bid, MA144 exit stability, position worst-case PnL, and time left
to partially or fully sell selected inventory.

Enable it first for paper testing:

```yaml
intra_window_sells:
  enabled: true
  modes: ["paper", "live"]
```

Live sells additionally require `mode: "live"` and
`live.allow_sell_orders: true`. See
[`docs/intra_window_sells.md`](docs/intra_window_sells.md) for formulas,
settings, research sources, and execution limitations.

## Paper Mode

`config.yaml` now defaults to:

```yaml
mode: "paper"
```

In paper mode:

1. Strategy approves `BUY_UP` or `BUY_DOWN`.
2. `PaperBroker` validates price, quote freshness, min shares, lock state, window timing, and risk caps.
3. Accepted paper orders fill immediately at the chosen Polymarket ask.
4. Paper fills are written to SQLite.
5. Position shape is rebuilt from stored paper fills:

```text
pnl_if_up = up_shares - total_cost
pnl_if_down = down_shares - total_cost
worst_case_pnl = min(pnl_if_up, pnl_if_down)
locked = worst_case_pnl > 0
```

When the shape is locked, trading stops for that window by default.

## Changing MA Settings

Edit `config.yaml`:

```yaml
binance:
  ma_fast: 7
  ma_mid: 25
  ma_slow: 69
```

## Changing Ladder Sizing

Edit `config.yaml`:

```yaml
sizing:
  ladder_usdc: [1.0, 2.0, 3.0]
  research_ladder_usdc: [5.0, 10.0, 20.0, 50.0, 70.0, 100.0, 150.0, 200.0]
  use_research_ladder: false
```

Useful related settings:

```yaml
sizing:
  first_entry_step: 0
  repair_start_step: 0
  same_side_start_step: 1
  min_order_shares: 1.0

risk:
  max_window_notional_usdc: 3.0
  max_window_loss_usdc: 1.0
  max_same_side_adds: 0
```

## Account Growth Sizing

Aries can conservatively scale the existing ladder with settled account balance
and cap directional entries using fractional Kelly sizing:

```yaml
growth_sizing:
  enabled: true
  fractional_kelly: 0.10
  apply_kelly_to_repairs: false
  use_growth_score_for_first_entry: true
  scale_with_account_balance: true
  min_account_scale: 0.25
  max_account_scale: 3.0
  safety_buffer_pp: 0.002
```

The growth layer never bypasses edge, timing, stale-quote, minimum-share, or hard
risk checks. First and same-side entries select the largest configured ladder
clip below the fractional-Kelly and remaining-risk caps. Repair sizing uses the
exact UP/DOWN outcome-PnL gap as a cap, then snaps down to the ladder.

## High-Conviction Same-Side Adds

The optional same-side model can add to the current dominant side only when
probability, confidence, target distance, MA direction/slopes, velocity,
persistence, volume, order-book imbalance, timing, edge, and ask-price gates
all pass. Lock and repair opportunities still have priority.

It is disabled by default. To paper-test it, edit:

```yaml
high_conviction_same_side:
  enabled: true
  min_probability: 0.80
  min_confidence: 0.60
  min_edge: 0.03
  min_net_edge: 0.02
  max_ask: 0.88
  max_adds_per_window: 2
  ladder_usdc: [2.0, 3.0, 5.0]
  fractional_kelly: 0.20
  max_window_notional_usdc: 7.0
  max_window_loss_usdc: 7.0
  scale_risk_caps_with_account_balance: false
```

`min_probability: 0.80` means the selected side must have at least 80%
probability. Since Aries confidence is scaled from distance to 50%,
an 80% probability corresponds to 60% confidence.

The remaining settings in `config.yaml` let you require the price to be on the
target side and tune distance, trend, volume, order-book, and timing gates.
The `$7` notional and loss limits are hard caps for these directional adds
while `scale_risk_caps_with_account_balance` is `false`.
A fully one-sided `$7` position can lose close to `$7`, so validate this in
paper mode before considering live mode.

## Intra-Window Sells

Paper mode supports two independently guarded sell objectives:

- `INVENTORY_TRIM` sells oversized inventory only when worst-case PnL improves.
- `DIRECTIONAL_WEAK_SIDE_EXIT` may sell a smaller model-weak leg when its PM
  bid exceeds Aries' estimated probability and the retained side has strong
  probability/stability confirmation.

Directional weak-side exits intentionally increase directional exposure, so
they have separate fraction, worst-case-deterioration, post-sell-loss, and lock
settings under `intra_window_sells`. Use
`dominant_inventory_trim_enabled` and `directional_weak_side_exit_enabled`
independently to test dominant-only, weak-side-only, or both.
Live SELL submission remains independently disabled until
`live.allow_sell_orders` is explicitly enabled. See
[`docs/intra_window_sells.md`](docs/intra_window_sells.md) for formulas and the
paper-validation protocol.

`growth_cap_below_ladder` means the Kelly/remaining-risk allowance was smaller
than the smallest eligible ladder clip. For example, a `$1.98` allowance cannot
select a `$2.00` clip. These settings control that behavior:

```yaml
high_conviction_same_side:
  scale_ladder_with_account_balance: false
  allow_allowed_notional_below_ladder: true
  min_allowed_notional_fallback_usdc: 1.0
```

With the fallback enabled, Aries may use the exact safe allowance, such as
`$1.98`, while still enforcing minimum shares and all broker/risk checks.
Set it to `false` to require a full configured ladder clip.

Terminal sizing diagnostics show:

- `S_t/target`: current BTC price divided by the Polymarket target
- `P(S_T>target)`: Aries' current UP probability
- `T_ratio`: fraction of the 5-minute window remaining
- Kelly percentage/cap, allowed notional, ladder clip/account ratio, repair target, and growth score

Research basis:

- [J. L. Kelly Jr., *A New Interpretation of Information Rate*](https://www.princeton.edu/~wbialek/rome/refs/kelly_56.pdf)
- [Rockafellar and Uryasev, *Optimization of Conditional Value-at-Risk*](https://sites.math.washington.edu/~rtr/papers/rtr179-CVaR1.pdf)
- [Avellaneda and Stoikov, *High-frequency trading in a limit order book*](https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf)

These papers support the sizing and risk concepts. They do not prove Aries'
probability model or strategy is profitable; paper validation and calibration
remain required.

## Logs

JSONL logs are written to:

- `logs/decisions.jsonl`
- `logs/fills.jsonl`

Key events include:

- `DECISION`
- `CANDIDATE_EVALUATION`
- `PAPER_ORDER_ATTEMPT`
- `PAPER_ORDER_ACCEPTED`
- `PAPER_ORDER_REJECTED`
- `PAPER_FILL`
- `POSITION_SHAPE`
- `WINDOW_RESOLVED`
- `WINDOW_PNL`
- `VALIDATION_PASSED`
- `VALIDATION_FAILED`

## Reports

Reports are written to `reports/`:

- `reports/session_summary.json`
- `reports/window_pnl.csv`
- `reports/paper_fills.csv`
- `reports/optimization_summary.json`
- `reports/live_session_summary.json`
- `reports/live_window_pnl.csv`
- `reports/live_matched_orders.csv`

## Advisory Optimization

Aries analyzes settled history by first-entry time, fill time, entry price,
candidate role, decision reason, and fills per window. A bucket is recommended
only when it has enough samples and remains profitable in both an earlier
training segment and a later chronological validation segment.

The terminal `OPTIMIZATION` panel shows the validated buckets, profit factor,
payoff ratio, breakeven win rate, maximum drawdown, and probability-calibration
status. Recommendations are advisory only and never rewrite `config.yaml`.
In live mode, `source: auto` uses settled matched live orders instead of paper
fills, keeping real and simulated performance separate.

```yaml
optimization:
  enabled: true
  source: "auto"
  min_settled_windows: 100
  min_samples_per_bucket: 20
  min_calibration_samples: 100
  validation_fraction: 0.30
```

Probability calibration is reported only when decisions can be joined to enough
settled outcomes. This prevents sizing from treating an unvalidated probability
estimate as a measured edge.

Research basis:

- [Niculescu-Mizil and Caruana, probability calibration](https://www.cs.cornell.edu/~alexn/papers/calibration.icml05.crc.rev3.pdf)
- [Bailey et al., probability of backtest overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)
- [Busseti, Ryu, and Boyd, risk-constrained Kelly](https://web.stanford.edu/~boyd/papers/pdf/kelly.pdf)

## Database

SQLite data is stored in `aries.db`.

Phase 2 adds:

- `paper_orders`
- `paper_fills`
- `window_results`
- `window_pnl`

Paper positions are reconstructed from `paper_fills` instead of cached balance tables.

## Storage Retention

The terminal still refreshes every decision cycle, but repetitive monitor snapshots are
only persisted at the configured sample interval. Trade decisions, orders, fills,
settled window results, session balance, and realized PnL remain stored.

Edit `config.yaml` to control retention:

```yaml
storage:
  max_decisions_log_mb: 50
  max_decisions_log_rows: 10000
  max_fills_log_mb: 50
  max_database_mb: 50
  decision_sample_seconds: 15
  decision_rows_to_keep: 5000
  maintenance_interval_seconds: 60
```

JSONL files retain their newest complete records when capped. SQLite maintenance
prunes only old rows from the high-frequency `decisions` table; it never automatically
deletes paper orders, fills, window results, or window PnL. A fixed database limit
cannot be guaranteed forever if retained trading history itself eventually exceeds it.

## Polymarket CLOB V2 Modes

All modes use the same strategy, timing, sizing, and risk settings:

```yaml
mode: "paper" # dry_run, paper, live_monitor, or live
```

- `paper`: uses live public market quotes, but only creates SQLite paper fills.
- `live_monitor`: shows authenticated open orders, recent trades, and open/closed
  positions. It can never place an order.
- `live`: sends approved BUY and explicitly armed SELL decisions to the official
  Polymarket CLOB V2 SDK.

The terminal's `LIVE ACCOUNT` row shows current-market account activity and CLOB
collateral cash balance. Live orders
are stored in the `live_order_audit` SQLite table, and user order/trade stream events
are stored in `live_account_events`.

Aries sends price-protected, marketable limit orders at the same selected ask
for BUY or bid for SELL used by paper mode. A `MATCHED` response is immediately written to the immutable live audit
and used to reconstruct live position/risk state, even if Polymarket's account
position endpoint is temporarily empty. If the selected-ask order rests as `LIVE`
instead of matching, Aries cancels it immediately by default so an old decision
cannot fill later.

Independent live hard limits prevent strategy settings or stale account data from
causing repeated exposure:

```yaml
live:
  min_order_shares: 5.0
  max_orders_per_window: 3
  max_sells_per_window: 3
  max_total_actions_per_window: 8
  max_window_notional_usdc: 7.0
  minimum_collateral_balance_usdc: 10.0
  max_session_drawdown_fraction: 0.15
  rejection_backoff_seconds: 30
  halt_on_balance_error: true
```

Before a SELL, Aries checks the current conditional-token balance for the
selected UP/DOWN token. The official SDK performs conditional-token allowance
recovery when supported. A balance/allowance rejection halts new orders until
restart and review.

Live sizing uses current CLOB collateral and never adds paper PnL to the live
bankroll.

## Configure Polymarket Keys

Install the current official beta SDK through the project requirements:

```bash
python -m pip install -r requirements.txt
```

Set secrets as environment variables. Do not put private values in `config.yaml`:

```bash
export POLYMARKET_PRIVATE_KEY='0x...'
export POLYMARKET_DEPOSIT_WALLET='0x...'
export POLYMARKET_API_KEY='...'
export POLYMARKET_API_SECRET='...'
export POLYMARKET_API_PASSPHRASE='...'
```

The three API credential variables are optional together; when omitted, the official
SDK derives credentials from the private key. The deposit wallet should be the wallet
shown by Polymarket for the account being monitored/traded.

For a persistent Linux setup:

```bash
install -m 600 /dev/null ~/.aries.env
nano ~/.aries.env
set -a
source ~/.aries.env
set +a
```

To monitor the real account while continuing to paper trade:

```yaml
mode: "paper"
live:
  monitor_enabled: true
  enabled: false
  allow_real_orders: false
  confirm_real_orders: ""
```

To arm real execution, all four settings must match:

```yaml
mode: "live"
live:
  enabled: true
  allow_real_orders: true
  confirm_real_orders: "I_UNDERSTAND_REAL_ORDERS"
```

To additionally arm both live SELL roles:

```yaml
intra_window_sells:
  enabled: true
  modes: ["paper", "live"]
  dominant_inventory_trim_enabled: true
  directional_weak_side_exit_enabled: true # Required only for weak-side exits.

live:
  allow_sell_orders: true
```

`allow_sell_orders: false` permits live BUYs but rejects all live SELLs.

Keep `post_only: false`; Aries submits at the selected ask or bid. Real execution also
requires a funded Polymarket account with the required approvals. Start with
`live_monitor`, then paper mode with monitoring enabled, before arming `live`.

Current implementation references:

- [Polymarket CLOB V2 migration](https://docs.polymarket.com/v2-migration)
- [Official Python SDK](https://docs.polymarket.com/dev-tooling/python)
- [Authentication](https://docs.polymarket.com/api-reference/authentication)
- [User WebSocket channel](https://docs.polymarket.com/market-data/websocket/user-channel)
- [Create orders](https://docs.polymarket.com/trading/orders/create)

## Common Debugging Tips

- Read `logs/decisions.jsonl` first when a trade was skipped.
- Read `logs/fills.jsonl` when an order was rejected or filled.
- Check `quote_age_seconds` in paper-order metadata when investigating stale quote behavior.
- Check `shape_before` and `shape_after` in decision and fill logs when validating lock behavior.
- Use `reports/window_pnl.csv` to review realized PnL by window.
- Run validation after test changes to catch missing reasons and PnL mismatches.

## Verification

```bash
python -m compileall .
python -m pytest -q
```
