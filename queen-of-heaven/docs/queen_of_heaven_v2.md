# Queen of Heaven v2: late-window TWAP-lock profile

## The edge

Polymarket BTC 5m markets resolve on a **60-second Chainlink TWAP** (since
2026-08-14): the price to beat is the TWAP at window open and the final price is
the average over the last 60 seconds (240s-300s). Up wins ties.

From 240s on, part of the final average is already locked in, so a lead is more
certain than "current price vs target" suggests. v2 buys the favoured side late
in the window when a calibrated probability beats the ask after the taker fee.
With realistic timing (see below) the edge is small: about 5-6c per $1 staked.

## Probability model (`aries/twap_probability.py`)

Brownian model of the resolution TWAP on Binance 1s closes:

- open = mean of the 60 closes ending at window open
- volatility = std of 1s price changes over the last 900s
- before 240s: final ~ N(spot, sigma^2 * ((240 - t) + 20))
- from 240s: the locked part of the average is known; the rest has variance
  sigma^2 * remaining^3 / (3 * 60^2)
- p_up = flip + (1 - 2 * flip) * Phi(z / k) with k = 1.25 (fat tails; the 5m
  bot uses k = 1.075 (V5) from Oct 24, the 15m bot keeps 1.25) and
  flip = 0.01 (Binance-vs-Chainlink resolution mismatch)
- momentum (since Oct 21): every remaining second is expected at spot +
  0.246 * (change over the last 2s) + 0.088 * (change 2-5s ago) + 0.036 *
  (change 5-10s ago) (`queen_of_heaven.twap_momentum`)

Calibration was fit on official outcomes Oct 5-14 and checked on Oct 15-18.

Binance 1s closes trend rather than bounce: the lag-1 autocorrelation of 1s
changes is +0.12, and 60s variance is 1.6-1.7x what 1s changes imply (k
absorbs this). About a quarter of the last 2s move continues over the next
15-60s in every period. The momentum coefficients are an OLS fit on Oct 5-18
prices alone, so no outcome data was used to choose them
(`research/momentum.py`). C1 at L=3 with a 3c limit, PnL per $1:

| Period | Without momentum | With momentum |
|---|---|---|
| Oct 5-18 | +22.1 (381) | +23.4 (399) |
| Sep 21-Oct 4 | +18.6 (393) | +18.4 (381) |
| Aug 14-Sep 20 | +67.1 (1185) | +73.9 (1195) |
| Oct 19-22 (forward) | +2.9 (100) | +2.5 (99) |

Log-loss improved in every period. Refitting k and flip on 230-280s alone
improved log-loss too, but not trading, so it was not adopted. The model is
under-confident between 0.7 and 0.9 in every period (`research/calib_check.py`)
and well calibrated above 0.95, so a single scale cannot fix both.

## Timing correction (important)

Polymarket data-api trade timestamps lag the real match time by about 2.75s.
This was measured against the websocket order book recorded with
`research/record_feeds.py` (`research/print_lag.py`): the book ask matches the
print price in 69% of informative cases at a 2.75s shift, versus 30% unshifted.

The first v2 backtest used print timestamps as-is, so it bought at prices from
1-2s **before** the Binance move that triggered the trade. That look-ahead bias
inflated the results (Oct 5-18 +75 per $1, t=7.1). All results below shift
prints 3s earlier (L=3); L=2 is shown as an optimistic fast-execution bound.

## Gates (config `queen_of_heaven`)

| Setting | Value |
|---|---|
| entry time | 230-290s (5m, 280 until Oct 23); 830-880s (15m) |
| ask band | 0.60-0.90 |
| model probability | >= 0.90 |
| edge after fee | >= 0.02 |
| entries per window | 1 |

Fees: `polymarket.taker_fee_rate: 0.07`, fee = shares * 0.07 * p * (1 - p)
(market `feeSchedule` `crypto_fees_v2`). Paper fills include this fee.

Paper fills: a BUY fills against the book `paper.fill_delay_seconds` (0.5s)
after the decision, at the ask at that moment, and only if that ask is at most
the seen ask + `paper.limit_tolerance` (0.03). Otherwise the order is canceled
and the bot may try again on the next second. This mirrors the live marketable
limit order at the seen ask + `live.limit_tolerance` (0.03), which fills at the
book price and is canceled if it does not match. Before this change, paper
filled at the seen ask with no delay, which flattered results.

Positions are held to resolution. All backtests assume that, but until Oct 23
the configs still had the older heuristic `intra_window_sells` switched on
(stop trims, profit trims, weak-side exits). It fired once: on Oct 22 the 15m
bot bought DOWN at 0.795 at 840s, and 4.5s later a hard stop trim sold all of
it at 0.476, below the model's own 0.60. DOWN won, so the trim cost $3.06 on a
$4.85 stake against holding (and the session tally missed it because the
window ended flat). `intra_window_sells.enabled` is now false in all three
configs, for paper and live. Model-based exits are covered under the backtest
below and are not used.

Limit price: the backtest allows a fill up to 1c above the seen ask. A higher
limit fills more trades, and the trades it adds are mostly ones where the price
ran away because the market agreed with the model, so they win more often. C1
over all four periods at L=3 (`research/tolerance.py`; the fill is the first
print at or below the limit within 2s):

| Limit above seen ask | Trades | PnL per $1 | t |
|---|---|---|---|
| 0c (current live code) | 1451 | +84.4 | 3.79 |
| 1c | 1705 | +94.4 | 4.36 |
| 2c | 1865 | +103.1 | 4.80 |
| 3c | 1977 | +106.4 | 4.96 |
| 5c | 2221 | +107.7 | 5.49 |

It helped in every period. In the first live paper session with delayed fills,
6 of 7 orders were cancelled at a 0c limit, and every cancelled side won. The
recorded book shows why: the bot's book matched the recorder's, and the bot
tended to fire on a brief dip in the ask that was gone 0.5s later.

Paper and live use 3c since Oct 21. Capping the allowance where the edge after
fee falls below 0 or 1c (`research/tolerance2.py`) changed little and not
consistently across periods.

Model limit (5m, from Oct 23 07:00): the BUY limit rises to the highest cent at
which the model still beats the price by 1c after the fee, and never sits below
the seen ask + 3c (`queen_of_heaven.limit_edge_floor` 0.01; paper and live).
At p = 0.986 and a seen ask of 0.90 the limit is 0.97. Paper orders kept being
cancelled when the ask jumped from 0.90 to 0.98 within 0.5s and the side then
won. Chosen on Aug 14-Oct 18 prints (`research/tolerance3.py`, momentum
models, PnL per $1):

| Model, timing | Flat 3c | Flat 10c | Model limit, 1c floor |
|---|---|---|---|
| V1, L=3 | +115.7 (1975), t 5.41 | +141.4 (2682) | +143.3 (2462), t 7.31 |
| V4, L=3 | +141.7 (2865) | +190.1 (3698) | +198.0 (3513), t 8.31 |
| V5, L=3 | +127.6 (2816) | +174.5 (3624) | +182.0 (3461), t 7.62 |
| V1, L=2 | +173.4 | +227.3 | +233.4 |
| V1, L=4 | +124.3 | +139.1 | +139.6 |

It beat the flat 3c in every period for all three models (V1: +23.4 -> +29.2,
+18.4 -> +26.6, +73.9 -> +87.6), and floors of 0 and 2c gave about the same.
It pays 2-3c above the seen ask on average. On the recorded books (Oct 20-22,
clock-corrected) the gain is small but the same sign for all three: V1 +1.17 ->
+1.30, V4 +0.98 -> +1.11, V5 +3.27 -> +3.50 (1.2s decisions; at VPS speed
+2.08 -> +2.16, +3.77 -> +3.98, +5.97 -> +6.21). On 15m it first looked mixed
(+56.1 -> +59.7, two of three periods slightly worse), but with fills capped at
the seen ask (next section) it is clearly better there too (+5.9 -> +16.4), so
the 15m bot uses it from Oct 23 09:08. `forward2.py` scores V1L/V4L/V5L with
this limit from Oct 24; the frozen V0-V5 lines and the V1/V4 switch rule stay
at the flat 3c.

Fill realism. The print simulation fills at the first print at or below the
limit within 2s, and each second's ask is the lowest buy print in it, so it
often fills below the ask the bot saw: 12% of its 5m fills are more than 5c
below the seen ask. On the recorded books (Oct 20-22, same windows, V1, flat
3c, `research/rec_vs_print.py`) the book ask 0.5s after a decision was never
more than 5c below the decision ask (0 of 31), and the books made 4.7c per
trade (6.9c at VPS speed) against 8.1c on prints and 5.2c on prints with
each fill capped at the seen ask. So the capped number is the realistic one,
and the uncapped prints overstate the edge. Aug 14-Oct 18, PnL per $1
(`research/tolerance3.py`, "capped" = fill no better than the seen ask):

| | L=3 prints | L=3 capped | L=4 capped |
|---|---|---|---|
| 5m V1, flat 3c | +115.7 | +52.4 (t 2.9) | +34.7 (t 1.8) |
| 5m V1, model limit | +143.3 | +100.0 (t 5.4) | +58.1 (t 2.9) |
| 5m V5, model limit | +182.0 | +120.2 (t 5.4) | +70.3 (t 3.0) |
| 5m V4, model limit | +198.0 | +134.6 (t 6.0) | +86.9 (t 3.7) |
| 15m V1, flat 3c | +56.1 | +5.9 (t 0.5) | -0.5 |
| 15m V1, model limit | +59.7 | +16.4 (t 1.3) | +5.9 (t 0.5) |

The 5m edge survives (about 4c per trade at L=3 and 2.4c at L=4 for V1 with
the model limit; the sharper models keep more), and the model limit matters
more than it seemed. The 15m edge does not: it was almost all cheap fills, and
even with the model limit it is not significant. The 15m bot stays on paper
only; the recorder saves 15m books from Oct 23 09:00 (`pm15` rows) so it can
be checked on books. Forward Oct 19-22 at a flat 3c, capped: V1 -0.79 (99),
V4 +2.87 (151), V5 +6.61 (153); `forward2.py` reports capped lines from now on.

Week by week, the deployed 5m rule (C1, 230-290s, model limit) with capped
fills (`research/weekly_capped.py`, 10 weeks from Aug 13): V1 was positive in
10 of 10 weeks at L=3 (+107.9 over 2561 trades, 4.2c per trade, t=5.7) and
9 of 10 at L=4 (+64.2, 2.6c, t=3.2; the losing week was Sep 3, -9.2). V5:
10/10 and 8/10 (3.6c and 2.3c); V4: 9/10 and 8/10 (4.0c and 2.8c). At L=4
the last two weeks (Oct 8-21) are thin for all three (V1 +2.4 and +1.6, V5
+1.9 and -0.2, V4 +12.0 and +1.4), so speed is what keeps the recent edge.

Gates under the model limit. Each C1 gate was re-checked one at a time on
Aug 14-Oct 18 (`research/gate_grid.py`, V1/V5/V4, L=3 and L=4). No change to
the ask band, probability or edge gate beat C1 in every period for every model.
An ask floor of 0.50 looked best (V1 +143 -> +173) but the windows it changes
mostly enter one second earlier on a single low print and then fill at the
normal ask (`research/amin_look.py`), which the bot's book never shows.
Entry time (`research/time_band.py`, model arrays extended to 200-290s):
starting before 230s adds trades that make little or lose (V1 200-280s:
+163.9 at L=3 but +128.9 at L=4), while ending at 290s instead of 280s was
better in every period for all three models at L=3 (V1 +143.3 -> +154.3, V5
+182.0 -> +194.5, V4 +198.0 -> +208.2) and equal or better at L=4, capped as
well (V1 +100.0 -> +107.9 at L=3, +58.1 -> +64.2 at L=4). On the recorded books
the extra entries after 280s made money for all three (3-4 each). The 5m bots
enter through 290s from Oct 23 08:57, and the V1L/V4L/V5L forward lines use
230-290s (amended before any of their forward days). On 15m ending at 885s
was slightly better (+56.1 -> +59.4) and 890s not, so 15m keeps 880s.
Scored with capped fills (`gate_grid.py --capped`) the gate check gives the
same answer: only the 0.50 ask floor beats C1 in every period, for V1 and V4
at L=3 only, and that is the low-print effect above. The capped edge depends
on speed: at L=4, Oct 5-18 made V1 +4.8, V5 -1.5 and V4 +11.3, against
+18.8, +20.4 and +24.8 at L=3, so execution speed (the VPS) matters more than
any gate.

Setups standing before the entry window. With capped fills, entries in the
first seconds of the window made little (V1 at 230-238s under 1c per trade,
from 239s on 5-7c; `research/edge_buckets.py`). The weak ones are setups that
already stood before 230s: the entry condition (ask 0.60-0.90, p >= 0.90,
edge after fee >= 2c, same side) held at 229s and before, so the bot buys at
230s a price the market had been refusing for a while, most likely because it
knows something Binance does not show. Setups that appear inside the window
are the market lagging Binance. Aug 14-Oct 18, capped, deployed rule
(`research/opp_age2.py`): "pre" entries made -0.2c per trade for V1 at L=3
(664 trades) and +0.1c at L=4, V5 -0.3c/-0.9c, V4 +1.2c/+0.7c, and lost in
Oct 5-18 for all six; all other entries made 5.5-6.1c at L=3 and 3.2-3.9c at
L=4. Retries after a missed fill are fine (V1 5.8c/8.7c), so the bot keeps
retrying. Skipping pre setups, with a run continuing through up to 10 seconds
without the condition and stopping at a second with it on the other side
(`research/opp_age3.py`), gives V1 +114.9 vs +107.9 at L=3 (7/10 weeks
better, 5.4c vs 4.2c per trade, t 6.87 vs 5.71) and +67.4 vs +64.2 at L=4
(6/10), V5 +152.9 vs +129.1 and +91.8 vs +75.0, V4 +144.5 vs +143.2 and +92.9
vs +95.8: about the same profit from 17% fewer trades, with t higher for all
three models at both speeds. Much of the pre loss is one week (Oct 8-14), so
the total gain is modest; the case is that these trades carry risk for no
return. The recorded books (Oct 20-22, checked after the choice;
`research/rec_pre.py`) agree: pre entries V1 -0.58 (4), V4 +0.02 (8), V5
+0.19 (9) per $1, the rest about 4-10c per trade. The 5m bots skip them from
Oct 23 12:00 (`queen_of_heaven.pre_window_setup_gap_seconds: 10`, decision
reason `queen_setup_before_window`); `forward2.py` scores V1N/V4N/V5N from
Oct 24. On 15m (`research/opp_age15.py`, 830-880s, setup tracked from 800s)
pre entries lost money for all three models at both speeds (-0.6 to -3.3c per
trade) and skipping them raised every total (V1 +16.4 -> +19.2 at L=3, +5.9 ->
+10.5 at L=4; V4 -2.8 -> +6.7 at L=4), though it was worse in the latest
period, where the 15m edge is gone anyway. The 15m paper bot uses the rule
from Oct 23 13:20; 15m still has no proven edge.
What made the setup does not matter much: split by the favoured model
probability change and the seen ask change over the 3s or 6s before entry
(`research/move_source.py`), every group made money in every period for all
three models; setups after the ask rose 3c or more did best (5-7c per trade,
the bot buying while the market catches up with Binance), setups where only
the ask fell with a flat model did worst (0.2-4c). No filter. Time of day and
weekday do not matter either (`research/hour_blocks.py`): every 4-hour UTC
block and both weekdays and weekends made money for all three models at L=3
(4.1-6.7c per trade) and L=4 (1.9-5.3c), and the few losing period cells are
scattered, so the bots trade around the clock.

Decision cadence. The bot acts on a Binance move only at its next cycle. With
one cycle per second (a cycle takes about 0.06-0.13s) decisions fall evenly
across each second, so a move waits about 0.65s on average. Since 1s of delay
costs about 40% of the capped edge (V1 model limit +100 at L=3, +58 at L=4),
the bots recheck every 0.25s inside the entry band from Oct 23 12:00
(`bot.fast_decision_interval_seconds`, `bot.fast_decision_from_seconds` /
`_to_seconds`: 222-292s on 5m, 822-882s on 15m), which cuts the wait to about
0.28s at roughly 5% of a core per bot. The print backtest cannot score
sub-second timing, so this rests on the L sensitivity, not a test.

## Backtest (timing-corrected)

Settings were chosen on Sep 21-Oct 18 (in-sample on both halves), then tested
once on the untouched Aug 14-Sep 20 holdout. Per $1 stake, L=3:

| Period | Role | Trades | PnL per $1 | Win | t |
|---|---|---|---|---|---|
| Oct 5-18 | tuning | 332 | +17.7 | 86.1% | 2.09 |
| Sep 21-Oct 4 | tuning | 343 | +18.4 | 85.7% | 2.15 |
| Aug 14-Sep 20 | **holdout** | 1012 | +59.7 | 85.6% | 3.34 |
| all | | 1687 | +95.8 | | 4.45 |

All 10 weeks were positive. With fills 1c worse: +73.4; 2c worse: +51.8. Max
drawdown -9.9 per $1 stake. The previous settings (240-290s, ask 0.50-0.90,
p >= 0.85, edge >= 0.03) made +146.6 over 1501 trades but with t=2.74 and a
losing week.

Things that did not help: blending in the market price, Binance taker-buy
imbalance, volatility regime (rechecked Oct 27 after the Oct 26 loss for
V1/V5/V4 under the deployed rule with capped fills, Aug 14-Oct 18: the
fifth of trades with the highest previous-hour BTC volatility was the best
for every model, V5 +7.9c per trade over 560, t=5.9, positive in all three
periods; `research/vol_regime.py`), scaling in, skipping entries after sharp market
drops, skipping entries after a sharp Binance move (Oct 21 13:25 paper loss:
a $45 drop in 2-3s just before entry that fully reversed; but over all 5m
periods, entries whose largest 2s move in the last 15s was 8+ sigma toward the
favoured side made +8.0c per trade over 170 trades, and calibration holds in
every move bucket; `research/jump_test.py`), time-of-day filters (no 4-hour block was consistently better across
periods), staking in proportion to the computed edge, an additive
Chainlink-Binance basis variance of $1-3 in the model (5m: +115 at $0, +92 at
$2, +81 at $3; the removed trades won as often as the rest;
`research/basis_test.py`), and resting maker bids instead of taking (bids only fill when someone
trades through them, which is usually just before the price moves against you;
every maker setting lost money, `research/maker_bt.py`).

Selling early when the market pays more than the model's value
(`research/exit_test.py`, `research/exit_robust.py`): after a C1 fill, sell at
the bid once the bid after fee beats the model probability by 10 points. At
L=3 this added +24.7 per $1 on 5m (-1.9, +7.1 and +19.6 by period; t=2.2)
and +22.6 on 15m (+8.2, +6.8, +7.6; t=2.5), but lost 3.8 on ETH. At those
exits the model said 0.58, the bid was 0.81 and the position went on to win
75% of the time, so the market was too high but the model too low. The gain
depends on speed. Acting one second later (exit prints shifted 4s) cuts it to
+0.9 on 5m and +5.7 on 15m, and at L=2 it is +87 on 5m. It also disappears on
5m without the bids implied by the other side's asks. The bids it sells into
are stale for a second or two after Binance moves, so this is a race against
faster traders. On the recorded books (Oct 20-22, V1 entries, clock-corrected) it made
+0.43 over 4 exits when filled 0.5s after the decision and -0.49 over 3 at
1.5s. Not used;
worth re-testing on books recorded from the VPS.

## Forward test

Each day after Oct 18 is scored as it becomes available, with C1 left
unchanged (`research/forward.py`):

| Day | Trades | PnL per $1 | Win | t |
|---|---|---|---|---|
| Oct 19 | 18 | -1.48 | 77.8% | -0.69 |
| Oct 20 | 25 | +0.66 | 84.0% | +0.28 |
| Oct 21 | 29 | +3.36 | 93.1% | +1.94 |
| Oct 22 | 21 | +0.05 | 81.0% | +0.02 |
| Oct 23 | 19 | -0.87 | 78.9% | -0.38 |
| Oct 24 | 33 | -0.47 | 78.8% | -0.15 |
| Oct 25 | 23 | -0.53 | 78.3% | -0.20 |
| Oct 26 | 29 | -1.73 | 75.9% | -0.57 |
| Oct 27 | 16 | +0.77 | 87.5% | +0.46 |
| total | 213 | -0.24 | 81.7% | |

The table above is at a 1c limit with the original model. From Oct 21,
`research/forward2.py` also scores four variants fixed that day, at a 3c limit
(the maps are in `research/variants.json`): V0 the original model, V1 with
momentum (deployed), V2 V0 with a binned recalibration fit on Oct 5-18, V3 V1
with its own map. Over all BTC periods plus ETH (below), V1 had the best t
(+5.39, 5.6c per trade). V2/V3 made more in total but less per trade (4.1-4.3c),
so they stay in shadow until forward days decide.

V4 and V5 (frozen Oct 22 04:40, before any forward day was scored for
them) test a sharper model. V4 takes sigma from overlapping 30s Binance
changes over 900s instead of 1s changes (`queen_of_heaven.twap_vol_horizon_seconds`
30, `twap_vol_multiplier` 0.85). V5 keeps 1s changes with k 1.075. Both k
values were fit by log loss on Sep 21-Oct 18 at 230-280s
(`research/volfit.py`, `research/volfit2.py`; C1 unchanged, momentum, 3c):

| Model | Oct 5-18 | Sep 21-Oct 4 | Aug 14-Sep 20 | all | t | windows V1 skips |
|---|---|---|---|---|---|---|
| V1 (deployed) | +24.0 (396) | +18.4 (381) | +73.9 (1195) | +116.3 | 5.44 | |
| V5 k 1.075 | +25.3 (542) | +22.6 (573) | +79.7 (1701) | +127.6 | 5.39 | +48.3 (844), t 3.99 |
| V4 30s vol | +27.5 (511) | +29.7 (551) | +84.5 (1803) | +141.7 | 5.98 | +59.2 (936), t 4.74 |

The windows the deployed model skips are profitable on their own in every
period, but in the windows both models trade, the sharper ones enter earlier
and give some of it back. They trade about 45% more often at 4.5-5c per
trade instead of 5.9c. Longer volatility windows (1800s, 3600s) improved log
loss but nearly lost Oct 5-18, so they were dropped. The comparison used all
three periods, so only forward days can confirm it. A second 5m paper bot runs
V4 (`config_5m_v4.yaml`, `aries_v4.db`, `logs/*_v4.jsonl`) beside the deployed
one. Rule, fixed before any forward day was scored for V4: once V1 and V4 each
have at least 300 forward trades from Oct 19 (prints, 3c, `forward2.py`), the
live model becomes whichever made more in total. Until then V1 stays live.

Forward days at a 3c limit (`forward2.py`, PnL per $1, trades in brackets):

| Day | V0 | V1 (live) | V2 | V3 | V4 | V5 |
|---|---|---|---|---|---|---|
| Oct 19 | -1.46 (18) | -1.21 (17) | -0.46 (32) | +0.25 (30) | +0.87 (27) | +0.36 (31) |
| Oct 20 | +0.85 (28) | +0.84 (28) | +2.14 (41) | +1.88 (39) | -0.18 (36) | +1.88 (39) |
| Oct 21 | +3.44 (33) | +3.06 (32) | +6.12 (47) | +6.82 (48) | +6.30 (47) | +6.82 (48) |
| Oct 22 | +0.10 (21) | -0.18 (22) | +1.34 (35) | +1.42 (35) | +1.18 (41) | +1.45 (35) |
| Oct 23 | -0.26 (25) | -0.27 (24) | +1.62 (41) | +2.16 (40) | -0.88 (33) | +2.16 (40) |
| Oct 24 | -0.07 (36) | +0.01 (37) | +3.87 (53) | +3.99 (54) | +4.88 (54) | +3.99 (54) |
| Oct 25 | -0.05 (26) | +0.42 (27) | +2.34 (35) | +2.74 (37) | +2.80 (39) | +2.74 (37) |
| Oct 26 | -1.74 (33) | -1.53 (34) | -3.53 (55) | -6.45 (53) | -5.87 (47) | -6.43 (53) |
| Oct 27 | +0.84 (18) | +0.86 (18) | +0.50 (29) | -0.32 (27) | -0.98 (24) | -0.32 (27) |
| total | +1.64 (238) | +2.00 (239) | +13.95 (368) | +12.48 (363) | +8.13 (348) | +12.64 (364) |

After five days every variant is still ahead at the print fill, and the
sharper ones trade about 50% more and make more, as in the backtest (V5
t=2.27, V3 2.25, V1 0.45). At the capped fill (max(fill, seen), the realistic
print metric) V1 is -1.82 (-1.5c per trade), V4 +1.38 (+0.8c) and V5 +7.77
(+4.0c, t=1.48). Five days is still noise. Up to Oct 22, on the
recorded books (Oct 20-22, 439 windows, decide 1.2s after each second, fill
0.5s later within 3c; `research/rec_variants.py`, recorder times corrected for
the local clock as described under Known live risks): V1 25 trades +1.17 per
$1, V4 44 trades +0.98, V5 42 trades +3.27 (7.8c per trade, se 4.9c). Speed
matters on the same books (`--speed DECIDE FILL`): deciding 1.05s after each
second and filling 0.15s later, as a VPS should manage, gave V1 +2.08 (30), V4
+3.77 (51) and V5 +5.97 (47; 12.7c per trade, se 3.8c); deciding 2.0s after
cost V1 its edge (-1.23) and cut V4 and V5 to +1.18 and +1.69. The samples are
small, but stale Binance data costs money. The bot decides once
per ~1.03s (`bot.decision_interval_seconds` 1; a cycle takes 15-60ms, so 0.25s
would cost little). On the recorded feeds with the live Binance trade price
(`research/rec_speed.py`, 415 windows), a 0.25s loop traded 10-20% more often
than a 1s loop and made a little more: V1 +2.78 (31) vs +1.73 (26) averaged
over the four 1s phases, V4 +1.49 (49) vs +1.26 (44), V5 +4.54 (49) vs +3.53
(44). One trade moves these by about 1, so the interval stays at 1s for now.

Paper vs backtest on Oct 21 (`research/reconcile.py`): while the 5m paper bot
ran (03:00-18:09 local), the backtest traded 20 windows and the bot filled 14,
13 of them the same windows and sides. One order was cancelled when the ask
rose 4c within 0.5s. The other five backtest trades, all winners (+0.82 of the
day's +3.06), came in sudden moves. The recorded book shows the ask inside
0.60-0.90 for only one to three seconds while the model crossed 0.90 (08:25:
UP went from 0.14 to 0.82 in one second at 254s and to 0.94 by 257s). In one
of them (10:50) the prints the backtest used match the recorded book with no
lag at all, so at L=3 it bought at a price that appeared only 3s after its
decision. Fast moves are where the print backtest is most optimistic; the bot
on a faster connection should catch some of them.

Oct 22: the home connection dropped at 12:18 and stayed down until the
processes were stopped at 15:20, so all bots and the recorder covered
04:10-12:18 only. The bots kept skipping with `binance_stale` and placed
nothing on stale data. In those hours V1's backtest traded twice and the V1
bot filled one of them (07:25, at 0.67 against the backtest's 0.89). The V4
bot (`reconcile.py ... --v4`, scored with the V4 model) filled 6 times and won
5; its backtest traded 10, 4 in the same windows. The six it missed all won,
and each was a fast move: at the bot's last sampled decision the ask was
in-band but the model under 0.90, and by the next one the ask was gone
(0.99+). The 15m bot filled 4 times; the backtest skipped one of them (05:45
DOWN at 0.71, model 0.91, where the print ask was 0.89 and the edge only
1.5c), and UP won: Binance had already reversed by the real decision time,
which the bot's 7s-slow clock hid. All Oct 22 paper decisions used Binance
prices about 7s old (the clock item under Known live risks), so they are not a
fair test of the model; paper from Oct 23 05:41 on is. From Oct 23
a third 5m paper bot runs V5 (`config_5m_v5.yaml`, `aries_v5.db`), because V5
leads on both the forward prints and the recorded books; it is for evidence
only and does not change the V1/V4 switch rule.

Oct 23 (bots up 00:00-13:38 and 18:20-22:15; both stops were Claude Code
ending background jobs for low memory on the PC, not bot faults): a weak day
for V1 and V4 at the print fill (-0.27 over 24, -0.88 over 33) and a fair one
for V5 (+2.16 over 40). The V1 bot filled 9 times and its backtest traded 24
windows, 7 in common; both lost 11:45 and 20:15, two genuine late reversals
(20:15: BTC $9 below the open with 23s left and the model at 0.93, then up
about $50 in the last 20s). The model limit, 230-290s and pre-window rules are
scored only from Oct 24, but an informal run on Oct 19-23
(`research/forward2_informal.py`; not pre-registered, though the rules were
chosen on Aug 14-Oct 18 prints only) gives, capped: V1N +3.36 (145 trades,
+2.3c), V4N +7.46 (197, +3.8c), V5N +14.08 (199, +7.1c, t=2.98), and V5L
without the pre rule +12.44 (241). Recorded books Oct 20-23 (603 windows,
`research/rec_pre.py`, deployed rules): V1 +2.10 (36), V4 +1.61 (55), V5
+4.83 (54) at home speed; V1 +3.25 (44), V4 +5.22 (65), V5 +8.43 (62) at VPS
speed. On the books the pre-window rule no longer helps: with Oct 23 added,
the 9-17 entries it skips per model made 0-7c per trade. It stays, because it
was chosen on far more print data. In the same windows, capped prints (+3.1c
per trade) again sit close to the books (+3.4c) while uncapped prints (+5.2c)
overstate (`research/rec_vs_print.py`). On the books a 0.25s decision loop
beat the 1s loop (`research/rec_speed.py`): V1 +3.42 against +2.40 averaged
over the four 1s phases, V5 +7.07 against +5.84, V4 +3.14 against +2.85; the
bots use it from Oct 23 11:57. The 15m forward set (Oct 19-23, V1, flat 3c)
made +3.40 over 37 trades (t=1.57, the same capped). V5 leads on every
measure. The switch rule covers V1 and V4 only, and neither has 300 forward
trades yet (123 and 184).

Selection rule, fixed Oct 24 05:45 EAT before Oct 24 was scored (`SELECT` in
`research/variants.json`): once V1N, V4N and V5N each have 300 forward trades
from Oct 24, the one with the highest capped total is the recommended 5m
model; if that is not V1, switching `config.yaml` is the owner's call. The
V1/V4 rule above is unchanged and still reported.

Oct 24 16:02 EAT: the owner made V5 the main 5m model. `config.yaml` now has
`twap_vol_multiplier` 1.075 (storage stays `aries.db`, so its V1 history
ends at window 1792775700 and V5 starts at 1792846937); V1 runs as a paper
shadow (`config_5m_v1.yaml`, `aries_v1.db`) next to V4, and
`config_5m_v5.yaml` is retired (same model as `config.yaml`). Nothing else
changed: still paper, same gates, limit and pre-window rules. The forward
scoring and the SELECT rule carry on as written, so they now say whether
the switch was right.

Stale Binance prices (found and fixed Oct 24 21:50 EAT). At 20:44 the V1 and
V4 paper bots bought UP on Binance prices 37s and 20s old (the recorder, on the
same PC and line, had the live price within 0.3s); both lost, while V5's later
DOWN entry won. Cause: the bots read `btcusdt@trade` plus the much busier
`btcusdt@bookTicker` on one socket and fell 15-40s behind during home
internet lag (the 1011 keepalive timeouts), and every book update set the
freshness time to its arrival time, so `binance_stale` never fired. The TWAP
model only uses trade prices, so the bots now read `btcusdt@aggTrade` alone
(the recorder's stream), freshness comes from trade exchange times, and a
1s ping keeps a caught-up stream fresh between trades (an answered ping means
Binance has delivered everything it queued; quiet markets go 5-8s without a
trade). A trade received more than 2s late means a backlog, and then only the
trade time counts (`aries/binance_ws.py`, `binance.heartbeat_seconds`,
`binance.max_trade_lag_seconds`, tests in `tests/test_binance_flow.py`).
Backtests never had this problem (they use exchange-stamped 1s bars), so
earlier paper results may include a few stale-price entries; the paper
sessions before 21:53 Oct 24 have the old feed.

Oct 24, the first pre-registered day for the model limit and pre-window rules
and for SELECT (`forward2.py`, prints, 3c, capped = max(fill, seen)):
V1N +2.36 (46 trades; capped +0.04), V4N +2.67 (54; capped -0.91), V5N
+4.62 (53; capped +1.17); V1L/V4L/V5L +0.82/+4.54/+4.04 (capped
-1.52/+0.67/+0.55). SELECT has 46/54/53 of the 300 trades it needs. Over
the six forward days at the capped fill, V1 is -3.86 (160 trades, -2.4c per
trade), V4 +2.71 (238, +1.1c) and V5 +8.19 (247, +3.3c, t=1.33); the V1/V4
rule is not due yet (160 and 238 of 300). The informal Oct 19-24 run (same
caveat as above) gives, capped: V1N +3.41 (191, +1.8c), V4N +6.55 (251,
+2.6c), V5N +15.25 (252, +6.1c, t=2.71). On the recorded books with the
deployed rules (`research/rec_pre.py`, entries the pre-window rule skips
left out), Oct 24 alone (97 windows): V1 +0.69 (12), V4 +0.12 (17), V5
+1.06 (15) at home speed, V1 +1.37 (14), V4 +1.25 (19), V5 +2.14 (17) at
VPS speed; Oct 20-24 (700 windows): V1 +2.79 (48), V4 +1.73 (72), V5 +5.42
(68, +8.0c) at home speed and V1 +4.62 (58), V4 +6.48 (84), V5 +10.08 (78,
+12.9c) at VPS speed. The entries the pre-window rule skips made +8-9c per
trade on the books for V4 and V5 (18 and 21), but on the capped forward
prints V5N (with the rule) still beats V5L (without it), so the rule stays.
On the same windows at a flat 3c, V1 on the books made +5.1c per trade
against +5.2c for uncapped prints and +1.9c capped (`research/rec_vs_print.py`).

Paper vs backtest on Oct 24 (`research/reconcile.py`, `--v5` for the main
bot; bots up 16:02-00:05 EAT): in those windows the V5 backtest traded 20
times and the V5 bot filled 18, 14 the same windows and sides. The bot's
four extra trades all won. The six backtest trades it missed also all won
(+0.79): 21:50 was the 21:53 restart, 22:55 the pre-window rule, 21:15 and
22:50 had the ask above 0.90 or the model below 0.90 at the bot's decisions,
and the logs for 16:10 and 19:30 have rotated out. Both lost 22:30, a
genuine late reversal (Chainlink rose about $23 in the last 50s while the
book priced DOWN at 0.89-0.91). The V1 bot filled 13 (7 shared with its
backtest), the V4 bot 18 (14 shared); their 20:40 UP losses were the stale
feed. Paper totals for the day: V5 17 wins in 18 fills, +$7.35 on $50.98;
V1 10 in 13, -$4.19; V4 16 in 18, +$5.12; 15m 3 in 3, +$5.86. The 15m
backtest (V1, flat 3c) made -0.55 over 13 trades on Oct 24 (+2.73 over 49
for Oct 19-24, t=0.92) and the 15m bot's three fills were all backtest
trades; on Oct 19-24 15m windows V4 made +4.7 (63) and V5 +3.0 (64)
against V1's +2.7 (`research/volfit15.py`), the reverse of Aug 14-Oct 20
below, so the 15m bot stays on V1.

Oct 25 (a Sunday; no paper bots or recorder ran, so there is no paper or
book check): V1N +2.38 (26 trades; capped +1.81), V4N +4.02 (36; capped
+3.04), V5N +3.78 (32; capped +3.12); V1L/V4L/V5L +1.03/+3.48/+3.39
(capped +0.47/+2.44/+2.68). Pre-registered totals for Oct 24-25: V1N +4.75
(72; capped +1.86), V4N +6.69 (90; capped +2.14), V5N +8.39 (85; capped
+4.30, +5.1c per trade, t=1.15). SELECT has 72/90/85 of the 300 trades it
needs. Over the seven forward days at the capped fill, V1 is -4.08 (187
trades, -2.2c per trade), V4 +4.39 (277, +1.6c) and V5 +10.12 (284, +3.6c,
t=1.51); the V1/V4 rule has 187 and 277 of 300. The original C1 at 1c
(first table) is +0.72 over 168, clear of the stop rule. Informal Oct
19-25, capped: V1N +5.22 (217, +2.4c), V4N +9.59 (287, +3.3c), V5N +18.38
(284, +6.5c, t=3.05). On 15m windows Oct 19-25 (`research/volfit15.py`) V1
made +3.4 (58), V5 +4.0 (75) and V4 +5.6 (73), about +0.7 (9), +1.0 (11)
and +0.9 (10) of it on Oct 25. One forward week does not outweigh Aug
14-Oct 20, so the 15m bot stays on V1.

Oct 26 (no paper bots ran) lost for every model: V1N -0.86 (38 trades;
capped -1.68), V4N -3.89 (52; capped -4.48), V5N -6.45 (54; capped -6.91);
V1L/V4L/V5L -1.10/-5.19/-6.61 (capped -1.95/-5.87/-7.51). It was the most
volatile of the eight forward days (BTC 1s volatility 0.51bp/s against
0.29-0.48) and the market's own late favourites (prints at 0.80-0.90 in
240-280s, mean 0.858) won 89.8% against 92-94% on most days. V5N won 41 of
54; its 13 losses are spread from 01:10 to 21:40 UTC and no window was
truncated (`research/day_diag.py`). On such a day the sharper models pay most
for their confidence. Pre-registered totals for Oct 24-26: V1N +3.89 (110;
capped +0.17), V4N +2.80 (142; capped -2.34), V5N +1.94 (139; capped -2.61),
so V1N now leads at the capped fill; SELECT has 110/142/139 of 300. Over
the eight forward days at the capped fill, V1 is -6.43 (221 trades, -2.9c
per trade), V4 -2.07 (324, -0.6c) and V5 +2.70 (337, +0.8c, t=0.35); the
V1/V4 rule has 221 and 324 of 300. The original C1 at 1c (first table) is
-1.01 over 197 (-0.5c per trade), clear of the -3c stop rule. Informal Oct
19-26, capped: V1N +3.54 (255, +1.4c), V4N +5.12 (339, +1.5c), V5N +11.46
(338, +3.4c, t=1.60). On 15m windows Oct 19-26 V1 made +4.2 (64), V5 +3.7
(81) and V4 +6.1 (82), of which +0.8 (6), -0.3 (6) and +0.5 (9) on Oct 26;
the 15m bot stays on V1. Nothing is re-tuned on forward days.

Oct 27 was calm (BTC 1s volatility 0.38bp/s; the market's 0.80-0.90
favourites won 94.0%) and roughly flat: V1N +1.64 (22 trades; capped +1.57),
V4N -0.18 (31; capped -0.39), V5N +0.86 (36; capped +0.34, 32 wins);
V1L/V4L/V5L +2.05/+0.94/+1.50 (capped +1.98/+0.65/+0.95). The paper bots ran
12:54-16:10 EAT (40 windows): V5 4/4 +$0.94 on $7.89, V1 3/3 +$1.34, V4 3/3
+$1.54. Every paper fill was on the same side, and had the same result, as
the backtest's entry in that window. In those windows the V5N backtest
filled 6 (+0.60 per $1) and the paper bot took 4 of them; it missed 13:55
and 15:05, both wins (`research/session_vs_bt.py`). In both the price
flipped within a second and the prints lagged the book (13:55: the DOWN
ask went 0.93 to 0.99 in 245-251s while the backtest paid 0.95 at 249s),
so the bot was right to skip them. Scored on the recorder's books (41
windows, `research/rec_pre.py`, home speed) V5 made 4 trades for +0.41,
V1 4 for +0.75 and V4 4 for +0.73. Pre-registered totals
for Oct 24-27: V1N +5.53 (132; capped +1.75), V4N +2.62 (173; capped
-2.73), V5N +2.81 (175; capped -2.27); SELECT has 132/173/175 of 300. Over
the nine forward days at the capped fill, V1 is -5.72 (239 trades, -2.4c per
trade), V4 -3.43 (348, -1.0c) and V5 +1.74 (364, +0.5c, t=0.22); the V1/V4
rule has 239 and 348 of 300. The original C1 at 1c (first table) is -0.24
over 213 (-0.1c per trade). Informal Oct 19-27, capped: V1N +5.11 (277,
+1.8c), V4N +4.73 (370, +1.3c), V5N +11.81 (374, +3.2c, t=1.57). On 15m
windows Oct 19-27 V1 made +3.9 (69), V5 +3.6 (87) and V4 +6.0 (88); Oct 27
alone was about -0.3 (5), -0.1 (6) and -0.1 (6). Nothing is re-tuned on forward
days.

On 15m windows the sharper models are worse: all four 15m sets, Aug 14-Oct 20,
gave V1 +58.5 (704 trades), V5 +46.9 (896) and V4 +42.1 (880)
(`research/volfit15.py`). So the 15m bot stays on V1, which fits its
slight over-confidence there.

D1, the reverse trade, is also in shadow: buy the side priced below 0.5 when
the model gives it at least 10 points more than its ask after fee (ask
0.02-0.40, 230-280s, 3c limit, momentum model; `research/underdog.py`). On
Aug 14-Oct 18 it made +563 per $1 over 4693 trades (t=2.37), but nearly all of
that comes from prints a cent or two below the seen price, which move the
payoff of a 5-10c share by 10-20%. Paying the seen price instead leaves +19
(t=0.09). The prints also say the market prices underdogs about right: when
the model gives the underdog 10+ points more than the market, it wins 13.2%
against a market price of 12.4% and a model value of 32%. On the recorded books
(Oct 20-21, `research/rec_underdog.py`, clock-corrected) it made +0.40 per $1
per trade over 94 trades (se 0.34), and on Oct 19-20 prints +0.9 over 138. It was frozen on the
evening of Oct 21 and is scored from Oct 22 by `research/forward2.py`. Each
trade is a 15-20% shot, so it needs well over 1000 forward trades to show
anything. Oct 22, its first scored day: -9.9 per $1 over 66 trades at the fill
(-13.9 at the seen price), 18.2% won.

Expected per day is about +1.0 +/- 1.8, so one day says little. Stop rule
(fixed before the forward data): after at least 100 forward trades, a mean
below -3c per trade means the backtested edge is not there. Settings are not
re-tuned on forward days.

## Other assets

Polymarket also lists 5m TWAP markets for ETH, SOL, XRP, DOGE and BNB, with
much less volume (ETH about $2.8k per window vs $55k for BTC). Tested on ETH
Oct 5-18 with the BTC calibration and C1 unchanged (fixed in advance): the
model is calibrated the same way as on BTC, but trading is weak, +5.5 per $1
over 203 trades at a 3c limit (2.7c per trade, t=0.77) vs 5.8c per trade for
BTC on the same days. ETH momentum is about half as strong (0.12 on the last
2s). Not worth running on ETH yet. ETH 15m, Oct 5-18: only 34 trades
(prints are sparse), +4.4 per $1 (t=1.99). The model is strongly
under-confident on ETH (the favoured side wins 4-13 points more often than
predicted between 0.5 and 0.95), so ETH would need its own calibration, which
14 days of thin data cannot support yet.

BTC 4h markets (`btc-updown-4h-{ts}`, 6 a day) use the same 60s Chainlink
TWAP rule and the same fees, but they are too thin to trade this way. Over all
420 of them (Aug 14-Oct 22, `research/btc4h`, built with `build_cache_iv.py
14400`), only 20 had a buy print at 0.60-0.90 in the last 70-20s, and C1 made
3-6 trades in 70 days (V1 3 for +0.5 per $1, V4 6 for +0.4), because the
outcome is usually settled long before the close. There
are no 1h or daily markets under that naming. The hourly "Bitcoin Up or Down -
October 21, 10AM ET" markets do exist, with the same fees and a 0.001 tick,
but they settle on the Binance BTC/USDT 1h candle close against its open: a
single price, with no averaging and so no locked portion to exploit. They are
also thin late (Oct 21: 0-131 trades in the last 5 minutes, most at 0.99+,
because the hour is usually decided by then).

BTC 15m markets resolve the same way (60s Chainlink TWAP at open and over the
last 60s; volume about $20k per window). Tested on Oct 5-18 with C1 and the
calibration unchanged, entering 830-880s (the same 70-20s before close):

| Period (3c limit) | Trades | PnL per $1 | t | With momentum |
|---|---|---|---|---|
| Oct 5-18 | 137 | +19.0 | 2.52 | +19.6 |
| Sep 21-Oct 4 | 128 | +7.3 | 1.32 | +9.6 |
| Aug 14-Sep 20 | 420 | +31.6 | 1.83 | +26.9 |
| all | 685 | +57.9 | 2.94 | +56.1 |
| Oct 19-22 (forward) | 29 | +3.3 | 1.83 | +3.8 |

10 of 11 weeks were positive (`research/weekly.py` on the 15m dirs). A few
fills far below the seen ask inflate this (one at about 0.15); capping each
winner at +1.5 per $1 leaves +38.5 (5.6c per trade, t=2.95). At L=2, Oct 5-18
is +12.1 (t=2.18, no outlier). Unlike 5m, the 15m model is slightly
over-confident at 0.9-0.98 in Aug-Sep. `config_15m.yaml` runs a
paper instance (interval 900, slug `btc-updown-15m-...`, crypto price variant
`fifteen`, separate database and logs) alongside the 5m bot; the cache builder
is `research/build_cache_iv.py`. Momentum helps on 15m too (+19.0 -> +19.6,
t=2.60; `research/momentum.py ... --interval 900`), so both bots use it.
The 830-880s window was checked on all three 15m periods
(`research/window15.py`, momentum model, 3c). Starting earlier loses money:
770-880s made -29.6 over 1167 trades and 800-880s +12.1. Starting later
makes less: 840-880s +42.5 and 850-880s +39.2, against +56.1 for 830-880s.
Ending at 890s adds +1.6, which is noise. As on 5m, the edge starts only
about 10s before the final-minute lock, so the window stays. With fills capped
at the seen ask most of this 15m edge goes away (see Fill realism above).

Sizing: the model's computed edge barely predicts realized PnL (every edge
quintile realized 3.5-7c per trade), so research results are per $1 at a flat
stake. With capped fills the top edge quintile does make more (V1 at L=3:
9.9c vs 1.7-4.8c), but stakes proportional to edge only scale PnL up with the
risk (t 5.66 vs 5.71 flat, drawdown 12.8 vs 6.3; `research/edge_buckets.py`). The paper bots still size with 0.10 fractional Kelly on the model
probability, clipped to $1-5 (`growth_sizing`, `queen_of_heaven.stake_ladder_usdc`;
Oct 22 fills ranged $0.77-3.22), so judge paper results per $1 staked. For
real money a flat stake is the supported choice.

## Known live risks

- Polymarket's book sometimes reprices before Binance moves (seen live: a 0.96
  -> 0.85 move 1.5s ahead of Binance). The bot then buys on stale information.
- The Chainlink-Binance basis drifts: the basis change between the open and
  close TWAPs is about $2 at the median and $4.7 at p90, enough to flip close
  windows (paper, Oct 20 13:40: Binance closed $1.28 below its open TWAP,
  Chainlink $1.38 above, and the bot's DOWN lost). This is rare: 2 winner
  mismatches in 279 recorded windows (Oct 20-21). A Chainlink-anchored model
  (Chainlink open TWAP, Chainlink values already received for the locked part,
  Binance plus the live basis for the rest; `research/cl_model.py`) did not
  help on 327 recorded windows (Oct 20-21, clock-corrected): log loss 0.1192
  vs 0.1218, but 14 book-simulated trades for +0.52 per $1 vs 20 for +0.99,
  and it took both losses (Oct 20 13:40, Oct 21 13:25) too.
  Chainlink reaches the bot about 1.7s after its observation second and trails
  Binance by 1-2s, so Binance stays the model's price source.
- The bot rejects books with a spread above `edge.max_spread` (0.06), which the
  prints-only backtest cannot apply. On the recorded windows it blocked none of
  24 book-simulated C1 trades, so it costs nothing measurable.
- The bot's CLOB stream can fall behind in busy windows. In paper on Oct 20
  at 19:00, the bot's book showed asks 13-15s old while the recorder's was
  current, and it booked a fill at 0.76 when UP was trading at 0.95+. Book
  updates carry an exchange timestamp, so the bot now skips trading when the
  latest update arrived more than `polymarket.max_book_lag_seconds` (1.5s)
  later than the fastest one in the window.
- Late-window prints are small (median ~$4), but the book is much deeper than
  the paper stakes ($1-3). At the 13 paper orders on Oct 21 the best ask
  held $15-1080 (median about $600), and the logged ask depth was $740-6200
  (the bot's `polymarket_flow.*_best_ask_size` and `*_ask_depth`, in shares,
  times the ask). Most entries could take $100+ at the seen ask. Larger orders
  still need testing: the dips the bot buys are brief, and others compete for
  them.
- The edge depends on speed (L=2 results are clearly better than L=3) and may
  shrink as other bots compete.
- The local clock. Binance bars are keyed by exchange trade time, but the bot
  took "now" from the local clock, and the model ignores bars stamped after
  "now". The home PC (Windows Time stopped) drifted from -0.1s on Oct 20 to
  +0.75s on Oct 21, then sat about 7s slow from Oct 22, so until Oct 23 05:41
  the model ran on Binance prices 6.5-7s old and every entry came 7s late.
  The Oct 22 15m loss at 05:45 was this: at the real decision time Binance had
  already reversed (the backtest model was at 0.51), while the stale model still
  said 0.91. `aries/clock.py` now measures the offset to Binance server time
  (`/api/v3/time`, lowest-latency of 5 requests) at start and every 600s
  (`bot.clock_sync_*`), and every timestamp in the bot goes through it, so
  ages between local timestamps are unchanged. The recorder still stamps local
  time; `research/rec_clock.py` estimates each file's offset from its Binance
  rows (5th percentile of receive minus trade time, less 0.18s one-way
  latency), and the book simulations subtract it. Keep NTP on the VPS anyway.

## Reproducing

Scripts are in `research/` (they need `numpy`, which the bot does not):

```bash
python research/fetch_events.py data/events.jsonl 2026-10-05 2026-10-18
python research/fetch_trades.py data/events.jsonl data/trades.jsonl
# (data-api stops at offset 10000; windows with more than ~10500 prints keep
#  the newest ones, tagged truncated_before, still from ~110-165s on)
# Binance 1s zips from data.binance.vision -> data/binance/
python research/build_binance.py data/binance data/binance_1s.npz
python research/fit_calibration.py data/binance_1s.npz data/events.jsonl research data/calibration.json
python research/build_cache.py data/binance_1s.npz data/events.jsonl data/trades.jsonl research \
    research/calibration.json data/bt2_cache.pkl
# cross-validated search with prints shifted L seconds (data dirs hold bt2_cache.pkl + trades)
python research/cv_search.py 3 dataA dataB
# record live feeds, run a book-based simulation (also caches trades), then measure the print lag
python research/record_feeds.py rec 24
python research/rec_analysis.py rec research rec_cache
python research/print_lag.py rec rec_cache/trades.json
```

`research/holdout.py` evaluates fixed finalists on all three periods; its data
directory names are set at the top of the file.
