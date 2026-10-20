# Gap Fade V1 — 2026-10-19 session audit

## A. Final verdict

**PARTIALLY VALID — OPERATIONAL ISSUE DID NOT AFFECT DECISION**

The original live decision artifact is **not trustworthy as an exact research-parity calculation**, because its 09:15 opening prices were not the exchange-authoritative opens. The discrepancy was real and material to an alpha input. However, an independent post-market recomputation using Kite's official one-minute candles produced the same categorical result: only PNB cleared the 1.5% gap gate, PNB failed retention, and there were no valid signals or trades. Thus the observed zero-trade outcome was legitimate, while the input-generation defect prevents calling the original calculation fully valid.

Session ID: `2b5a0f98816e45b1a93c1b65c9121d89`. Frozen cell: `BASE_0930_OPEN_EOD`. Frozen canonical SHA-256: `a54e28b50089567f78fd1b33c12048ee21eedfefb6282c88e5abd131adfc8bc8`. Operational config SHA-256: `f900c20c6633831ba973b0c5f95a577878b913f38d08958632e64eefd7e0b7c3`. The edge remains unvalidated.

## B. Session timeline

| IST | Event |
|---|---|
| 08:41:33.356 | Kite read-only profile authenticated. |
| 08:42:16.014 | Session started; live-feed connection initiated. |
| 08:42:16.116 | WebSocket connected and all 115 tokens subscribed in full mode. |
| 09:00:00.221 | First crossed-depth warning. |
| 09:09:22.920 | Last crossed-depth warning. |
| shortly after 09:16 | The first 09:15 candle necessarily completed; its exact wall-clock completion was not logged. |
| 09:30:07 | All 1,725 required bars and the single 115-symbol decision were persisted. |
| 09:30:07–11:33:27 | Ungraceful laptop interruption occurred; the exact instant is not observable. |
| 11:33:27.085 | Authentication logged for an incomplete/aborted restart attempt; no database resume followed. |
| 11:39:06.691 | Authentication for the successful restart. |
| 11:39:50.168 | Session resumed with 0 fills and 0 active orders; 1,725 bars restored. |
| 11:39:50.488 | WebSocket reconnected and all tokens resubscribed. |
| 15:00:00 | EOD flat assertion passed with 0 positions and 0 managed orders. |
| 15:00:00.349 | Feed stopped cleanly. |
| 15:00:00.364 | Session finalized as completed. |

No valid market tick is individually logged, so the exact first-valid-tick receive/exchange timestamp cannot be recovered. This is an observability limitation, not evidence of a missing required bar.

## C. Data integrity

The universe contained 115 unique symbols, with no duplicates or omissions. Every symbol had a prior close dated 2026-10-16, a recorded session open, a 09:29 price, and exactly 15 completed one-minute bars from 09:15 through 09:29. There were exactly 1,725 unique symbol/minute records, with valid OHLC and nonnegative volume.

The material defect was the definition of the session open. `ZerodhaTickNormalizer` ignored the WebSocket `ohlc.open` value. `OneMinuteCandleBuilder._new_state` then assigned `tick.last_price` from the first received tick in a minute to `state.open`. With 115 subscriptions, the first received 09:15 LTP was commonly later than the exchange's first trade. This is why the live-built open differed from the official candle even though all minute buckets existed.

The cross-check found 101 symbols with at least one compared value discrepancy: **100 had a 09:15-open discrepancy**, and one additional symbol had a 09:29-close discrepancy. The earlier shorthand “101/115 open discrepancies” therefore overstates the open-only count by one. Median absolute open error was 17.6605 bps; the maximum was 106.14996 bps for HDFCAMC (live 2399.2 versus official 2374.0). These are economically material because gap and retention directly use the open.

The evidence rules out other proposed causes:

- Pre-open ticks remained in their own pre-09:15 minute states; the builder rolls on the exchange-timestamp minute boundary.
- Every required timestamp is correctly aligned to Asia/Kolkata from 09:15 through 09:29.
- The mismatch existed in files persisted before the laptop shutdown, so it is not a restart artifact.
- No malformed crossed-depth warning occurred at or after 09:15.
- No stale/cached price source is used for `Candle.open`; the direct source was demonstrably the first received LTP.

## D. 115-symbol decision funnel

| Stage | Entering | Passing | Rejected at stage |
|---|---:|---:|---:|
| Universe | 115 | 115 | 0 |
| Valid official prior close and minute data | 115 | 115 | 0 |
| Official positive gap >= 1.5% | 115 | 1 | 114 |
| Official retention < 50% | 1 | 0 | 1 |
| Stop geometry valid | 0 | 0 | 0 |
| Portfolio/RMS eligible | 0 | 0 | 0 |
| Selected | 0 | 0 | 0 |
| Order submitted | 0 | 0 | 0 |
| Filled | 0 | 0 | 0 |
| Exited | 0 | 0 | 0 |

The original live-built funnel has the identical counts. Its sole gap event was also PNB. Official recomputation for PNB: prior close 116.30, official open 120.87, gap 3.929493%, official 09:29 close 123.90, retention 166.301969%. It therefore fails the strict `<50%` rule. For diagnostic completeness, the official 09:30 minute open was 123.86 and the frozen stop would have been 120.894174, invalid short-stop geometry; geometry was never reached because retention had already failed.

No symbol changed categorical eligibility between the original and corrected calculations. Consequently, there is no per-symbol changed-result table and no missed valid signal. Numerical input differences remain material even though today's categorical results did not change.

## E. Rejection reason distribution

| Exact reason | Count |
|---|---:|
| `POSITIVE_GAP_BELOW_1P5PCT` | 114 |
| `RETENTION_NOT_BELOW_50PCT` | 1 |

RMS was not invoked because no candidate reached that stage.

## F. Top 10 nearest misses

This table describes the recorded decision inputs. Retention, score, stop, and 09:30-open stop distance for below-gap names are counterfactual diagnostics only; the engine correctly stopped at the gap gate. The 09:30 minute open is a bar-open proxy, not proof of an exact tick fill.

| Symbol | Prior close | Live open | Gap % | 09:29 | Diagnostic retention % | Diagnostic score | Stop | Stop distance % | Exact rejection | Threshold distance |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| PNB | 116.30 | 120.05 | 3.2244 | 123.90 | 202.6667 | -0.033104 | 120.0740 | -3.0567 | `RETENTION_NOT_BELOW_50PCT` | 152.6667 pp above retention limit |
| UNIONBANK | 171.68 | 173.86 | 1.2698 | 175.40 | 170.6422 | -0.008970 | 173.8948 | -0.7903 | `POSITIVE_GAP_BELOW_1P5PCT` | 0.2302 pp below gap limit |
| CANBK | 118.29 | 119.66 | 1.1582 | 121.52 | 235.7664 | -0.015724 | 119.6839 | -1.5352 | `POSITIVE_GAP_BELOW_1P5PCT` | 0.3418 pp below |
| DLF | 675.00 | 682.25 | 1.0741 | 676.90 | 26.2069 | 0.007926 | 682.3865 | 0.8105 | `POSITIVE_GAP_BELOW_1P5PCT` | 0.4259 pp below |
| ACC | 1187.20 | 1197.00 | 0.8255 | 1187.60 | 4.0816 | 0.007918 | 1197.2394 | 0.7777 | `POSITIVE_GAP_BELOW_1P5PCT` | 0.6745 pp below |
| VEDL | 246.10 | 248.10 | 0.8127 | 244.90 | -60.0000 | 0.013003 | 248.1496 | 1.3062 | `POSITIVE_GAP_BELOW_1P5PCT` | 0.6873 pp below |
| DIVISLAB | 9144.50 | 9209.50 | 0.7108 | 9271.00 | 194.6154 | -0.006725 | 9211.3419 | -0.6863 | `POSITIVE_GAP_BELOW_1P5PCT` | 0.7892 pp below |
| ADANIPORTS | 1750.80 | 1762.90 | 0.6911 | 1754.30 | 28.9256 | 0.004912 | 1763.2526 | 0.4931 | `POSITIVE_GAP_BELOW_1P5PCT` | 0.8089 pp below |
| ICICIPRULI | 445.00 | 448.00 | 0.6742 | 437.95 | -235.0000 | 0.022584 | 448.0896 | 2.2569 | `POSITIVE_GAP_BELOW_1P5PCT` | 0.8258 pp below |
| LT | 3627.10 | 3648.80 | 0.5983 | 3634.40 | 33.6406 | 0.003970 | 3649.5298 | 0.3914 | `POSITIVE_GAP_BELOW_1P5PCT` | 0.9017 pp below |

No threshold change is recommended or implied.

## G. Orders/fills/trades

**ZERO TRADES.** Signal log rows: 0. Accepted signals: 0. Orders: 0. Fills: 0. Stops: 0. Exits: 0. There was no open position when the laptop powered off.

## H. P&L and full cost reconciliation

Gross P&L ₹0.00. Brokerage ₹0.00, STT ₹0.00, exchange charge ₹0.00, SEBI charge ₹0.00, stamp duty ₹0.00, GST ₹0.00, IPFT ₹0.00, modeled slippage ₹0.00; total costs ₹0.00 and net P&L ₹0.00. The empty trade ledger, daily report, final portfolio snapshot, and session database all reconcile. Starting cash, ending cash, and ending equity are ₹4,00,000.00. Drawdown is 0.

## I. Malformed tick audit

There were exactly 46,225 `best_bid cannot exceed best_ask` warnings, all from 09:00:00.221 through 09:09:22.920. Count before 09:15: 46,225. Count at/after 09:15: 0.

The normalizer parsed LTP, exchange timestamp, volume, and depth, then constructed `Tick`; the crossed-depth invariant raised and `_on_ticks` discarded the whole tick. Thus otherwise usable LTP/timestamp/volume was indeed discarded. It did **not** cause the 09:15-open defect today: no such drop occurred during the required window, and all 1,725 required candles were present. The open defect instead came from ignoring `ohlc.open` and treating the first received LTP as authoritative.

## J. Shutdown/resume audit

The decision was executed exactly once. `decision_state.json`, `completed_bars.csv`, `signal_log.csv`, and `rejection_log.csv` all retained 09:30:07 modification times and were not rewritten after resume. The database contains one session, seven journal events, zero orders, zero fills, zero trades, zero positions, one final snapshot, and no risk events. The auxiliary `paper_trading.sqlite` contains no records for this session.

Resume restored 1,725 bars and reconstructed 0 fills and 0 active orders. It did not recompute the decision, duplicate an order/fill, reset P&L, lose a position, recreate a stop, or corrupt risk state. The incomplete 11:33 attempt did not record a resume. Resume behavior itself was safe.

One reporting-only defect was found: `GAP_FADE_V1_START` used the `now` captured before the slow prior-close fetch, so its journal timestamp could precede `SESSION_CREATED`/`SESSION_RESUMED`. This did not affect trading or recovery; it has been changed to timestamp the event when written.

## K. EOD risk and position state

Open positions 0; active paper orders 0; active stops 0; pending entries 0; cash/equity ₹4,00,000; realized/unrealized/daily net P&L ₹0; negative cash none; maximum gross exposure ₹0; maximum simultaneous positions 0. The ₹1,500 hard-loss rule was not approached. `EOD_flat_pass` is true.

## L. Research/paper parity assessment

Frozen thresholds, ranking, stop, entry convention, EOD exit, configuration, and canonical strategy identity are unchanged. The decision logic matches Phase-7B, but the pre-fix live market-data adapter did not supply the official session-open input required for parity. Post-market official recomputation confirms that this defect did not alter today's no-signal result.

## M. Bugs discovered

1. **B — market-data issue:** WebSocket `ohlc.open` was ignored; the first observed 09:15 LTP was mislabeled as the session open. This can change future decisions and required correction.
2. **F — reporting-only issue:** runner start-audit timestamps were captured before prior-close retrieval, creating nonchronological journal timestamps.
3. **Observability limitation, not changed:** exact accepted tick times are not persisted. This did not obstruct today's categorical reconstruction.

## N. Fixes made

`Tick` now carries the authoritative session open; the Zerodha normalizer reads it from `ohlc.open`; the candle builder applies it only to the Asia/Kolkata 09:15 minute while retaining first-regular-tick behavior when the field is unavailable. Pre-open ticks cannot set the regular-session open. The runner writes its start-audit timestamp at record time.

Changed files: `src/paper_trading/models.py`, `src/paper_trading/market_data/zerodha.py`, `src/paper_trading/market_data/candle_builder.py`, `scripts/run_gap_fade_v1_live_paper.py`, `tests/test_live_tick_normalization.py`, and `tests/test_live_candle_builder.py`. No Gap Fade configuration, adapter alpha rule, or frozen research file changed.

## O. Test results

Focused market-data, feed, and Gap Fade tests: **43 passed**. Full suite: **155 passed**. Python compile checks passed. `git diff --check` passed. Added regressions cover official-open normalization, pre-open isolation, first-regular-tick fallback, and Asia/Kolkata boundaries. Existing restart test confirms a committed decision is restored without recomputation.

## P. Recommendation for the next paper session

Use the corrected implementation for another **forward paper observation only** after refreshing the expired Kite token. Do not run the pre-fix code unchanged. The current corrected code is operationally suitable for paper observation, subject to normal preflight; it is not validated alpha and must not be used for live orders. No alpha threshold should be changed.
