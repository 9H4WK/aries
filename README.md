# Aries

Aries is a BTC 5-minute Polymarket strategy bot focused on BTC `UP` / `DOWN` window markets. It reads Binance BTC price pressure, evaluates both Polymarket sides, and in Phase 2 turns approved strategy decisions into paper fills stored in SQLite.

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

Live trading is still disabled.

## How To Run

```bash
python main.py
```

No command-line arguments are used. All settings come from `config.yaml`.

## Strategy Scope

Aries stays limited to:

- BTC 5-minute Polymarket `UP` / `DOWN` markets
- Binance BTC tick and moving-average pressure
- buy-both-sides shape logic
- paper trading only

“Buy both sides” means Aries always evaluates both candidates after the first entry. It does not force a balance trade at any price.

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

## Live Execution Stub

`aries/live_execution.py` exists only as a future safety stub. Every method raises:

```text
RuntimeError("Live execution is disabled. Paper trading only.")
```

Even if `live.enabled` or `live.allow_real_orders` are changed, there is still no live API implementation.

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
