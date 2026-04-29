# Canonical Entry Rules — Money Program

**Status:** v0.1 · 2026-04-27 · **the source of truth** for what the
entry system enforces.

This document is the single authoritative list of every rule the
Trading Program must check before entering a position. It is derived
from `Entry_Rules_Desk_Reference.html` (operator-facing) and
`Entry_Refinement_Masterclass_v1.docx` (full rationale), with rule IDs
that map directly to code (`classify_tick` rejection codes, scanner
gate names, scanEmission validators).

When the rules in this document and the rules in code disagree, this
document wins. Per `feedback_masterclass_is_the_law`: the document IS
the law. Per `feedback_trigger_semantics` 2026-04-27 hard rule: we
NEVER fall back to legacy semantics.

The rules are grouped by **where in the pipeline they run**, because
that determines where they're enforced and how they're surfaced to the
operator.

---

## Layer 1 — Pre-screen rules (scanner, before shortlist)

These run in the **swing-committee scanner** (TS/Next.js) and produce
the candidate's `grade` + `pillar_votes`. By the time a candidate
reaches the entry-rules side, all of these have passed (or the
candidate would not be shortlisted).

### Rule P1 — Trend Template (LONG)

Price > 50/150/200 MA. MAs stacked up. 200 MA rising ≥ 1 month.
Stock ≥ 25% off 52-week low. Stock within 25% of 52-week high. Relative
strength rank ≥ 70.

- **Applies to:** LONG
- **Where:** scanner (`/api/scanner/route.js` Trend Template gate)
- **Surfaced via:** scan JSON `grade` field; the candidate would not
  be shortlisted if any sub-rule failed.
- **Code status:** ✓ ENFORCED upstream.

### Rule P2 — Inverse Trend Template (SHORT)

Mirror of P1 inverted. Price < 50/150/200 MA. MAs stacked down. 200 MA
falling ≥ 1 month. Stock ≥ 25% off 52-week high. Within 25% of 52-week
low. RS ≤ 30.

- **Applies to:** SHORT
- **Where:** scanner
- **Surfaced via:** grade
- **Code status:** ✓ ENFORCED upstream.

### Rule P3 — ADX(14) > 25

Confirms genuine trend, not chop. Same threshold for both directions.

- **Applies to:** BOTH
- **Where:** scanner
- **Surfaced via:** grade
- **Code status:** ✓ ENFORCED upstream.

### Rule P4 — Volume Dry-Up (LONG)

≥ 2 of last 5 sessions close with volume < 60% of 50-day average.

- **Applies to:** LONG
- **Where:** scanner
- **Surfaced via:** grade
- **Code status:** ✓ ENFORCED upstream.

### Rule P5 — Distribution Days (SHORT)

≥ 3 of last 10 sessions close down on volume ≥ 1.25× 50-day average.

- **Applies to:** SHORT
- **Where:** scanner
- **Surfaced via:** grade
- **Code status:** ✓ ENFORCED upstream.

### Rule P6 — Squeeze Check (SHORT)

Short Interest < 20%. Days to Cover < 5. Borrow fee < 5% annual. Any
fail → reject the SHORT setup.

- **Applies to:** SHORT
- **Where:** scanner (uses Ortex / supplementary feeds)
- **Surfaced via:** grade
- **Code status:** ⏳ PARTIAL — flagged in memory `project_backlog_priorities`
  as needing supplementary data. May be approximated by other gates today.

### Rule P7 — Climax Top exception (S-D only)

Replaces Rule P2 for the S-D entry type. Stock up ≥ 100% in 8 weeks +
widest range + highest volume + close in lower 25% + extended ≥ 50%
above 200 MA. **All five must be true.**

- **Applies to:** SHORT (S-D entry type only)
- **Where:** scanner
- **Surfaced via:** grade + setup_type
- **Code status:** ⏳ PARTIAL — climax checks exist in the scanner
  scoring but the exact Masterclass thresholds need verification.

---

## Layer 2 — Pre-flight rules (scanEmission, when shortlist entry built)

These run in **swing-committee `scanEmission.js`** when a TAKE-TRADE
verdict is converted to a shortlist entry. They size the position and
validate the trade structure before the JSON ships to entry-rules.

### Rule R1 — Risk per trade ≤ 1%

Maximum 1% of portfolio risked on any single trade.

- **Applies to:** BOTH
- **Where:** `scanEmission.js` `GRADE_TO_RISK_PCT` table
- **Code status:** ⏳ DRIFT — current code: A+/A=0.75-1%, **B=0.5%**.
  Desk reference says 1% standard for all grades. **Open question
  (Task #52):** is the stepped ladder intentional or a regression?

### Rule R2 — Reward:risk ≥ 3:1

`(target_price − trigger_mid) / (trigger_mid − stop_price) ≥ 3.0` for
LONG; mirror for SHORT.

- **Applies to:** BOTH
- **Where:** `scanEmission.js` (drop entry if ratio < threshold)
- **Code status:** ✗ NOT ENFORCED (Task #53). TXN + AMD shipped
  2026-04-27 with 1.1:1 and 1.2:1 respectively. Either drop sub-3:1
  rows in scanEmission or reject on entry-rules ingest.

### Rule R3 — Stop distance ≤ 8%

`abs(entry − stop) / entry ≤ 0.08`. If risk budget can't fit the stop
within 8%, **SKIP** the trade. Never widen the budget.

- **Applies to:** BOTH
- **Where:** `scanEmission.js` toShortlistEntry (validation step)
- **Code status:** ✗ NOT ENFORCED. Easy to add — pure arithmetic.

### Rule R4 — Single-position cap

LONG: ≤ 10% of portfolio at cost. SHORT: ≤ 8% at cost.

- **Applies to:** BOTH
- **Where:** `scanEmission.js` (size cap) or executor pre-fire check
- **Code status:** ✗ NOT ENFORCED at scanEmission. Executor may
  enforce — needs verification.

### Rule R5 — Total open risk ≤ 6%

Sum of (1R per open position) across the book ≤ 6% of portfolio. New
candidates that would push it over the cap are deferred.

- **Applies to:** BOTH
- **Where:** entry-rules `executor.py` pre-fire check (book-level, not
  candidate-level)
- **Code status:** **DROPPED** for current profile (Mark 2026-04-27).
  Top-3 trades/day × 1% per trade = 3% maximum open risk — never binds
  the 6% cap. Re-enable if N rises above 6.

### Rule R6 — Short exposure cap ≤ 50%

Combined notional of all SHORT positions ≤ 50% of portfolio.

- **Applies to:** SHORT
- **Where:** entry-rules executor pre-fire
- **Code status:** **DEFERRED** until SHORT trading returns
  (Mark 2026-04-27). Currently LONG-biased.

---

## Layer 3 — Entry-time rules (`classify_tick`, every tick)

These run on every poll inside `src/engine/monitor.classify_tick`.
They are what the manual_entry_monitor.py and the live engine evaluate
to decide HOLD / ARM / FIRE / REJECT / NO_PRICE.

### Rule S1 — Market tradeable

`snapshot.market_status == "TRADEABLE"` (or empty/missing — IG
omission tolerated). Anything else → NO_PRICE.

- **Applies to:** BOTH
- **Code status:** ✓ ENFORCED.

### Rule S2 — Last price available

`snapshot.last_traded` is non-null. Otherwise → NO_PRICE.

- **Applies to:** BOTH
- **Code status:** ✓ ENFORCED.

### Rule S3 — Entries-open lower bound (R_SESSION_PREMATURE)

US: `now ≥ 09:45 ET` (14:45 BST in summer). UK: `now ≥ 08:15 UK`. The
opening 15 minutes is amateur-hour whipsaw — every Masterclass author
waits. Defaults to `Settings.entry_window_start = 09:45`.

- **Applies to:** BOTH
- **Code status:** ✓ ENFORCED — commit `3a52dec` 2026-04-24.
- **Memory:** `feedback_entry_window_lower_bound`.

### Rule S4 — Entries-cutoff upper bound (R_SESSION_CUTOFF)

US default: 19:30 UK absolute (= 14:30 ET in summer) OR session_end - 60min,
whichever is earlier. Past cutoff → suppress FIRE.

- **Applies to:** BOTH
- **Code status:** ✓ ENFORCED.

### Rule S5 — Day-trade entry cutoff (R_DAY_TRADE_CUTOFF)

Linda Bradford Raschke / standard intraday discipline: directional
day-trade entries should initiate within the first ~1.5 hours after
the market open. After 11:15 ET (US) / 10:30 UK (UK), the morning's
directional regime has typically played out — afternoon entries chase
mature moves with deteriorated R:R and break the intraday-managed
profile's structural assumptions.

This is a SECOND, EARLIER entries cutoff than Rule S4. Rule S4 is
**risk-management-driven** (don't open a position you can't manage to
close); Rule S5 is **entry-quality-driven** (don't open a position
when the morning's signal is stale). Past Rule S5's threshold,
classify_tick returns REJECT(R_DAY_TRADE_CUTOFF) even if the strict-
break trigger has fired.

The rule is opt-in via `SessionClock.day_trade_cutoff_local`. Default
ON for both US (11:15 ET) and UK (10:30 UK) sessions. Pass
`day_trade_cutoff_local=None` to disable for back-compat.

The motivating data point: 2026-04-29 LMT short setup. The breakdown
played out 10:00-10:30 ET (price 514 → 504), but Rule 22 strict-break
of trigger_low (503.90) didn't fire until 10:39 ET — by which time
most of the move was gone. With Rule S5 at 11:15 ET, late entries
past 11:15 are rejected outright; with the tightened 0.35×ATR zone
(see Rule 4) the strict break would have fired at 506.42 around
10:25 ET — well within the 11:15 cutoff. The two rules work as a
pair: the tighter zone fires earlier, the cutoff guards against
chasing.

- **Applies to:** BOTH
- **Code status:** ✓ ENFORCED — commit pending 2026-04-29.
- **Memory:** captured under Rule S5; no separate feedback file.

### Rule 4 — Pivot Buy (LONG L-A) / Pivot Short-Sell (SHORT S-A)

Strict breakout entry. LONG: buy-stop placed 0.35×ATR14 above pivot.
SHORT: sell-stop 0.35×ATR14 below neckline. Tick that crosses the
trigger **strictly** (not just inside zone) is the entry signal.

The **0.35×ATR zone width** (LBR-aligned for intraday-managed profile)
replaces the legacy fixed 3% buffer (changed 2026-04-28 to 0.5×ATR;
tightened to 0.35 on 2026-04-29). Two reasons for the second tightening:

1. **0.5×ATR is the canonical SWING buffer**, not the day-trade buffer.
   Multi-day swing trades have 2-3 days for confirmation and tolerate a
   half-ATR pre-confirmation buffer. Same-day intraday-managed entries
   (09:45-15:55 ET window) cannot — the buffer eats ~1/3 of the move
   before the strict-break trigger fires. LMT 2026-04-29 case study:
   pivot 512.29, real breakdown 10:00 ET, strict break of 503.90
   (0.5×ATR low) didn't fire until 10:39 ET. With 0.35×ATR (trigger_low
   506.42) the strict break would have fired at 10:25 ET — meaningfully
   earlier without weakening Rule 22.
2. **Pairs with Rule S5** (day-trade entries cutoff at 11:15 ET). The
   tighter zone fires earlier; the cutoff guards against chasing if the
   trigger never confirms. Together they preserve Rule 22's anti-
   whipsaw discipline while restoring the R:R-at-fill the swing-buffer
   was destroying.

The arithmetic is computed deterministically server-side in
`swing-committee/lib/triggerDerivation.js` (see `docs/lean_scan_spec.md`
§4.6) and must remain in lockstep with the Python backtest harness in
`money-program-trading/src/backtest/replay_scanner.py`.

- **Applies to:** L-A (LONG) / S-A (SHORT) primarily; informs **Rule 22**
- **Zone formula (LONG):** `trigger_low = lastClose; trigger_high = lastClose + ATR14 × 0.35`
- **Zone formula (SHORT):** `trigger_high = lastClose; trigger_low = lastClose − ATR14 × 0.35`
- **Code status:** ✓ ENFORCED via Rule 22 (commit `008d200` 2026-04-24).
  Zone formula updated 2026-04-29 — pending commit on swing-committee main.
- **Memory:** `feedback_trigger_semantics`.

### Rule 4-Chase — Open > pivot + 3% → SKIP

If the day opens more than 3% above the pivot WITHOUT a Buyable Gap Up
classification, **DO NOT chase**. Wait for a pullback. Mirror for SHORT.

- **Applies to:** BOTH
- **Code status:** ✗ NOT ENFORCED in classify_tick. Easy to add as a
  pre-trigger check on session_open_price.

### Rule 5 — Volume Confirmation

Breakout/breakdown candle volume must be ≥ 1.4× 50-day average. Mid-
session check: if projected volume < threshold → reduce to 50% pilot.
End of day: if volume still < threshold → close pilot.

- **Applies to:** BOTH
- **Code status:** **DROPPED** for the small-account intraday-managed
  profile (Mark 2026-04-27). Per-tick volume confirmation requires an
  intraday volume feed we don't fetch, and the value-at-risk reduction
  is small for an intraday-only hold (15:55 ET hard-close caps overnight
  exposure to zero anyway). Re-enable if intraday volume becomes
  available or if hold-overnight returns.

### Rule 6 — Holy Grail / EMA Pullback (L-B / S-B)

L-B entry: buy-stop above the high of the candle that touched the
rising 10/20 EMA on declining volume. S-B mirror.

- **Applies to:** L-B / S-B entry types
- **Code status:** **DROPPED** for current profile (Mark 2026-04-27).
  Universe today is L-A pivot-breakout setups exclusively. If scans
  start producing L-B / S-B entries, re-enable.

### Rule 9A — Buyable Gap Up, 15-min OR (LONG)

If first observed tick ≥ trigger_low → gap-up day. Track 15-min
opening range. R20 during the window. R21 after, until break above
OR_high. FIRE on strict break.

- **Applies to:** LONG
- **Code status:** ✓ ENFORCED — commit `6e175aa` 2026-04-24.
- **Memory:** `project_rule9_bgu_shipped`.

### Rule 9B — BGU VWAP Pullback fallback (LONG)

Alternative L-C entry: limit at session VWAP. Engages after the OR
window if price pulls back to VWAP without breaking OR_high — Raschke
"limit-at-VWAP" classic.

- **Applies to:** LONG (L-C entry type)
- **Code status:** ✗ NOT ENFORCED — `vwap()` helper exists in
  `src/engine/rules.py` but classify_tick doesn't call it.

### Rule 9-Late — Late-stage gap = exhaustion → DO NOT BUY

Late-stage Buyable Gap Up is exhaustion, not a setup. Reject the trade
even if all other gates pass.

- **Applies to:** LONG
- **Code status:** ✗ NOT ENFORCED. "Stage" classification needs to be
  available on the candidate (it is — scanner computes it for the
  trend template).

### Rule 10 — Shortable Gap Down (SHORT mirror of Rule 9)

S-C entry. Late-stage gap down → 15-min OR → sell-stop below OR low,
or limit at VWAP. Early-stage gap down = possible shakeout = DO NOT
SHORT.

- **Applies to:** SHORT
- **Code status:** **DEFERRED** until SHORT trading returns
  (Mark 2026-04-27). Track alongside R6 / Rule 9-Late.

### Rule 22 — Strict trigger breakout (non-gap days)

LONG: `last > trigger_high`. SHORT: `last < trigger_low`. Inside-zone
ticks return REJECT(R22). The legacy "any tick in zone fires" semantic
is **garbage and must never be reintroduced** (per
`feedback_trigger_semantics` 2026-04-27).

- **Applies to:** BOTH
- **Code status:** ✓ ENFORCED — commit `008d200` 2026-04-24.

---

## Layer 4 — Pre-fire rules (executor, after classify_tick FIRE)

When classify_tick returns FIRE, the executor runs a final round of
checks before placing the order on IG. These are protective gates.

### Rule X1 — Risk budget capacity

Total open risk + this candidate's 1R ≤ Rule R5 cap. If over, defer.

- **Code status:** ⏳ NEEDS VERIFICATION. Not visible to manual tool.

### Rule X2 — Position-size cap

Single-position notional ≤ Rule R4 cap.

- **Code status:** ⏳ NEEDS VERIFICATION.

### Rule X3 — UK spread filter (Rule 12)

Spread > 0.3% → reduce stake by 25%. Spread > 0.5% → SKIP entirely.

- **Applies to:** UK tickers
- **Code status:** ⏳ PARTIAL — Settings has `uk_spread_*_threshold`
  values, may not be wired. Needs verification.

### Rule X4 — Earnings blackout (part of Rule 11)

Auto-exit / cover EOD before any earnings report. No exceptions.

- **Where:** scanEmission emit-time check (drops candidates with
  earnings within ±5 days from the shortlist).
- **Code status:** ✓ ENFORCED in code, **gated by env var**. Filter
  in `swing-committee/lib/eventFilter.js` runs whenever
  `EVENT_FILTER_ENABLED=1` AND a calendar payload is supplied. Tests
  in `lib/scanEmission.test.mjs` confirm: tickers with imminent
  earnings drop from the shortlist with `event_suppressions[]`
  populated on the scan record.
- **To enable in production:** set `EVENT_FILTER_ENABLED=1` in the
  Vercel dashboard for the swing-committee project (Settings →
  Environment Variables → Production), then redeploy. Verify by
  running a scan that includes a ticker with earnings ≤ 5 days out
  and confirming `scan_record.event_suppressions` is populated in
  the downloaded JSON.
- **Memory:** `feedback_event_filter_design`.

### Rule X5 — Sector correlation cap

≥ 3 open positions in the same sector → reduce each new entry to 75%
of normal size.

- **Code status:** **DROPPED** for current profile (Mark 2026-04-27).
  Top-3 trades/day × even distribution makes it unlikely to hit 3 same-
  sector. Re-enable if N rises and concentration becomes a real risk.

---

## Layer 5 — Post-fill rules (NOT entry, listed for completeness)

These don't gate entry but are part of the rule stack the operator
must respect.

### Rule 7 — Scaling (T1 / T2)

T1 = 60% at primary entry. T2 = 40% on follow-through ONLY if T1 is
in profit. NEVER average into a loser.

- **Code status:** OFF by design per `feedback_small_account_sizing` —
  small-account profile uses single-entry 100% on trigger, 1% risk per
  trade, top 3 trades/day.

### Rule 8 — Order Types

Breakout/breakdown → stop-limit. Pullback/rally → limit. Gap → limit
on pullback or stop above/below OR.

- **Code status:** ✓ ENFORCED in executor. `feedback_broker_enforced_target`
  also requires limit_level attached at order-open for the £50 hard cap.

### Rule 11 — Overnight & Catalyst Protection

Earnings auto-exit. Binary catalyst (FDA / legal etc.) → 50% or exit.
Overnight gap sizing: 10% gap = max 1% portfolio loss.

- **Code status:** ⏳ PARTIAL.

### Rule 12 — UK-Specific

LSE auction (07:50-08:00 GMT): limit orders only. Stamp duty +0.5%
on long purchases. CFD vs spread bet rules.

- **Code status:** ✓ ENFORCED at execution-mode level (spread bet only
  per `Settings.account_mode = SPREADBET`).

### Rule E1 — Emergency Cover (15% adverse)

Auto-cover any SHORT with 15% adverse move. No waiting. No hoping.

- **Code status:** ⏳ NEEDS VERIFICATION in exit logic.

### Rule E2 — Failed gap reclaim window

Failed gap → 3-day window to reclaim, then quarantine for 10 days.

- **Code status:** ⏳ Settings has `gap_reclaim_window=3` and
  `quarantine_days=10`. Wiring needs verification.

---

## Coverage matrix

| Rule | Applies to | Where | Status |
|------|-----------|-------|--------|
| P1 Trend Template | LONG | scanner | ✓ |
| P2 Inverse TT | SHORT | scanner | ✓ |
| P3 ADX > 25 | BOTH | scanner | ✓ |
| P4 Volume Dry-Up | LONG | scanner | ✓ |
| P5 Distribution | SHORT | scanner | ✓ |
| P6 Squeeze | SHORT | scanner | ⏳ |
| P7 Climax Top | S-D | scanner | ⏳ |
| R1 Risk 1% | BOTH | scanEmission | ⏳ drift |
| R2 R:R 3:1 | BOTH | scanEmission | ✗ |
| R3 Stop ≤ 8% | BOTH | scanEmission | ✗ |
| R4 Position cap | BOTH | scanEmission/exec | ✗ |
| R5 Total risk 6% | BOTH | executor | **DROPPED** (top-3 × 1% never binds 6%) |
| R6 Short cap 50% | SHORT | executor | **DEFERRED** (SHORT off) |
| S1 Market tradeable | BOTH | classify_tick | ✓ |
| S2 Price available | BOTH | classify_tick | ✓ |
| S3 Entries open | BOTH | classify_tick | ✓ |
| S4 Entries cutoff | BOTH | classify_tick | ✓ |
| S5 Day-trade cutoff | BOTH | classify_tick | ✓ (default ON 11:15 ET / 10:30 UK, opt-out via `day_trade_cutoff_local=None`) |
| 4 Pivot Buy | L-A/S-A | classify_tick (Rule 22) | ✓ |
| 4-Chase | BOTH | classify_tick | ✓ |
| 5 Volume Confirm | BOTH | classify_tick | **DROPPED** (intraday-only profile) |
| 6 EMA Pullback | L-B/S-B | classify_tick | **DROPPED** (L-A only universe) |
| 9A BGU 15-min OR | LONG | classify_tick | ✓ |
| 9B BGU VWAP | LONG | classify_tick | ✗ |
| 9-Late | LONG | classify_tick | ✗ |
| 10 SGD | SHORT | classify_tick | **DEFERRED** (SHORT off) |
| 22 Strict breakout | BOTH | classify_tick | ✓ |
| X1 Budget capacity | BOTH | executor | ⏳ |
| X2 Position cap | BOTH | executor | ⏳ |
| X3 UK spread | UK | executor | ⏳ ACTIVE (Mark 2026-04-28 trading UK) — display ✓, executor wiring needed |
| X4 Earnings blackout | BOTH | scanEmission/exec | ✓ in code, ⏳ env-flag off in Vercel — flip `EVENT_FILTER_ENABLED=1` to enable |
| X5 Sector correlation | BOTH | executor | **DROPPED** (rare bind at N=3) |
| 7 Scaling | BOTH | executor | OFF (design) |
| 8 Order Types | BOTH | executor | ✓ |
| 11 Overnight | BOTH | exit | ⏳ |
| 12 UK Specific | UK | exec/account | ✓ |
| E1 15% emergency cover | SHORT | exit | ⏳ |
| E2 Gap reclaim window | BOTH | exit | ⏳ |

---

## Status legend

- **✓ ENFORCED** — fully wired, has tests, passes the live engine.
- **⏳ PARTIAL / DRIFT** — implemented but doesn't match the spec, or
  gated by a flag that's off, or uses a heuristic not the canonical
  threshold.
- **✗ NOT ENFORCED** — not in code today.
- **OFF (design)** — deliberately disabled (e.g. scaling — single
  entry per `feedback_small_account_sizing`).

## Maintenance

When a rule's status changes, update the status column in the matrix
AND the rule's section. Add a "shipped" note with the commit SHA where
relevant.

When a new rule is discovered or the Masterclass updates, add it here
first, then implement. Code that gates entries without a corresponding
rule in this document is suspect.
