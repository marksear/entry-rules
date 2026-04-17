# Observability Design v1

**Status:** Draft for Mark's review. Not yet implemented.
**Date:** 2026-04-16 (updated same day with Mark's answers to open questions)
**Home:** `entry-rules/money-program-trading/src/logging_mod/` (single owner).

**Key decisions locked in:**
- One scan per day. Swing-committee writes the executable trades list to the existing `data/trades.json` contract AND a richer scan artifact to `data/scans/scan_YYYYMMDD.json` for the log ingester.
- Max hold: 2–3 days. Most trades should end up as day trades. Trailing-stop trigger on absolute £ profit threshold (sub-spec TBD).
- Stopped out = no re-entry on same symbol the same day. Terminal.
- No Claude narrative in logs. All logged interactions are rule-based decisions (enter / don't enter with reason code, move stop with reason, close with reason). This collapses the InteractionRecord stream into the EventRecord stream.
- DEMO / LIVE flag is mandatory on every session and denormalised to every snapshot/event row for defensive querying.

## 1. Purpose

We have chosen single-entry sizing (100% on trigger, no T1/T2 tranches — see §4 of the revised CLAUDE instructions). That removes the natural "is it working?" checkpoint that tranche protocols provided. The compensating control is deliberately rich logging.

The logs serve three jobs:

1. **Compensating telemetry** — answer "was the trade working?" post-hoc without having paid for a staged entry.
2. **Backtest substrate** — every rule-change proposal becomes a SQL/Polars query over the logs. No separate backtest system.
3. **Proof of edge** — per-grade realised R, per-pillar conditional win rate, spread cost vs estimate, 30-min invalidation accuracy. The evidence we use to tune rules.

## 2. Scope (what we log, what we don't)

Per Mark's decisions:

- **One scan per day.** Morning scan only. Produces the executable trades list (`data/trades.json`, existing contract from `sample_trades.json`) plus a richer scan artifact (`data/scans/scan_YYYYMMDD.json`) for the log ingester.
- **We log the morning shortlist only** — the A+/A/B candidates the scan selected as "potentially enter today". Typical size ≈ 5–20 per session.
- **We do NOT log the full scan universe per minute.** The universe scoring is captured once per scan (ScanRecord) for retrospective "why wasn't X selected?" analysis.
- **Tracking lifecycle:** starts when a candidate enters the shortlist at scan-time and ends at terminal status. Terminal statuses are: `STOPPED_OUT` (done for the day, no re-entry), `TARGET_HIT`, `TIMESTOP_HIT` (2–3 day cap reached), `TRAIL_EXIT` (trailing-stop exit after £-threshold trigger), `INVALIDATION_EXIT` (30-min post-entry re-cross), `INVALIDATED_PRE_TRIGGER` (gate flip or stop level breached before entry), `SESSION_ENDED_NO_TRIGGER` (end of day with no entry).
- **Hold cap:** max 2–3 days. Enforced by a time-stop rule in the executor.
- **Ideal outcome:** most trades end intraday as day trades.

Expected volume: 15 candidates × 500 session minutes ≈ 7.5k snapshot rows/day during entry phase. Post-entry rows continue for the held position's life (max ~1,500 extra rows per swing held across 2–3 days). SQLite is over-provisioned for this.

## 3. Record Types

Five record types, five tables. No separate interaction stream — rule-based decisions are events.

### 3.1 SessionRecord (stream: sessions)
One row per trading session. Opens at scan time, closes at market close.

Fields: `session_id` (pk, uuid), `session_date` (date), `session_label` (e.g. `US_REGULAR` / `UK_REGULAR` / `US_EXTENDED`), `broker_mode` (`DEMO` / `LIVE` — **required**), `account_size_gbp` (snapshot at session open), `opened_at_utc`, `closed_at_utc`, `scan_id` (singular, one scan per session), `rule_set_version` (git SHA of active rule set), `schema_version`.

### 3.2 ScanRecord (stream: scans)
One row per morning scan, emitted by swing-committee as a rich artifact alongside the existing `trades.json` execution contract. Holds the full universe scoring output for retrospective analysis.

Fields: `scan_id` (pk, uuid), `session_id` (fk), `scanned_at_utc`, `universe_size`, `scored_symbols_json` (full scoring payload — all ~175 tickers with their pillar votes, grade if any, rejection reason if any), `regime_snapshot_json` (MCL regime at scan time), `broker_mode`, `rule_set_version`, `schema_version`.

### 3.3 ShortlistEntry (stream: shortlist)
One row per A+/A/B candidate selected from a scan. This is the set we then track per-minute.

Fields: `candidate_id` (pk, uuid), `scan_id` (fk), `session_id` (fk), `symbol`, `market` (US/UK), `direction` (LONG/SHORT), `setup_type` (L-A … S-E), `grade` (`A+` / `A` / `B`), `trigger_low`, `trigger_high`, `stop_price`, `target_price`, `planned_stake_gbp_per_pt`, `planned_risk_gbp`, `planned_risk_pct_account`, `pillar_votes_json` (which of the six passed), `committee_stance`, `notes`, `broker_mode`, `created_at_utc`, `schema_version`, `rule_set_version`.

### 3.4 CandidateSnapshot (stream: snapshots)
One row per shortlisted candidate per minute while its status is non-terminal. The state vector.

Flat columns (~55):

*Identity:* `id`, `session_id`, `candidate_id`, `scan_id`, `symbol`, `market`, `direction`, `setup_type`, `grade`, `ts_utc`, `minute_bucket` (YYYYMMDDHHMM int for easy grouping), `status` (`PENDING_TRIGGER` / `TRIGGERED_OPEN` / `TERMINAL_*`), `broker_mode`, `schema_version`, `rule_set_version`.

*Price state:* `last_price`, `bid`, `ask`, `spread_pts`, `spread_pct`, `dist_to_trigger_pts`, `dist_to_stop_pts`, `dist_to_target_pts`.

*Indicators:* `adx_14`, `rs_percentile`, `ma_alignment_bitmap` (50/150/200 stacked = 0b111), `vol_pace_ratio` (projected-session / 50d avg), `vwap_offset_pts`, `atr_daily`, `atr_intraday_est`.

*Regime:* `mcl_regime` (GREEN/YELLOW/RED), `mcl_score`, `vix_level`.

*Committee (slowly-changing, copied in for row-self-sufficiency):* `pillar_pass_count`, `pillar_bitmap` (which 6 passed — 6 bits), `committee_stance`.

*Risk / sizing (live):* `current_stake_gbp_per_pt`, `current_risk_gbp`, `margin_required_gbp`, `portfolio_heat_pct`, `fits_heat_limit` (bool), `fits_margin` (bool), `would_enter_now` (bool), `rejection_code` (R01–R19 or NULL).

*Position state (only when status = TRIGGERED_OPEN):* `fill_price`, `fill_ts_utc`, `elapsed_mins_in_position`, `elapsed_sessions_in_position` (1/2/3 against the 2–3 day cap), `unrealized_pnl_gbp`, `peak_unrealised_pnl_gbp` (one-way ratchet — max of itself and `unrealized_pnl_gbp`; drives the stepped trail), `unrealized_r` (P&L / initial risk), `current_stop_price`, `current_locked_profit_gbp` (£ above BE locked by the current trail position — 0 pre-arm, £1/£6/£11/£16/£21 per trail band), `stop_moved_count`, `trail_step_count` (0 = not armed, 1 = armed at BE+£1, 2 = +£6, …, 5 = +£21), `invalidation_window_active` (bool — first 30 min post-entry), `trail_mode_active` (bool — true once peak P&L ≥ `TRAIL_ACTIVATION_GBP`), `mins_to_timestop` (count-down to 2–3 day hard close).

*Indexes:* `(session_id, ts_utc)`, `(symbol, ts_utc)`, `(candidate_id, ts_utc)`, `(status, ts_utc)`.

### 3.5 CandidateEvent (stream: events)
Event-driven. Writes on state transitions AND on rule-based decisions (enter / don't enter / move stop / close). This is the decision log — no separate InteractionRecord stream.

Fields: `id`, `session_id`, `candidate_id`, `ts_utc`, `event_type`, `actor` (`GATE_ENGINE` / `RISK_MANAGER` / `EXECUTOR` / `TRAIL_MANAGER` / `TIMESTOP_MONITOR`), `reason_code` (R01–R19 or internal code like `TIMESTOP`, `TRAIL_EXIT`, `INVALIDATION`), `payload_json` (before/after values, e.g. `{"old_stop": 138.80, "new_stop": 142.10}`), `broker_mode`, `schema_version`, `rule_set_version`.

`event_type` enum:
- *Pre-entry:* `SHORTLIST_ADDED`, `GATE_FLIPPED`, `TRIGGER_ARMED`, `ENTRY_EVALUATED_NO_ENTER` (with rejection code — the minute-by-minute "why we didn't enter"), `INVALIDATED_PRE_TRIGGER`, `SESSION_ENDED_NO_TRIGGER`.
- *Entry:* `TRIGGER_FIRED`, `ORDER_PLACED`, `FILLED`.
- *In-position:* `STOP_MOVED` (payload carries `old_stop`, `new_stop`, `reason` ∈ {`TRAIL_ARM`, `TRAIL_STEP`, `MANUAL`}, `old_trail_step_count`, `new_trail_step_count`, `old_locked_gbp`, `new_locked_gbp`, `peak_pnl_gbp_at_move`). When a fast tick crosses multiple £5 bands at once, a **single** `STOP_MOVED(reason=TRAIL_STEP)` event is emitted with `trail_step_count` jumping to the final band — the `new_trail_step_count − old_trail_step_count` delta is the "bands crossed in one tick" signal for log analysis.), `TRAIL_MODE_ACTIVATED` (peak P&L ≥ `TRAIL_ACTIVATION_GBP` / £25), `INVALIDATION_EXIT` (30-min re-cross), `STOP_HIT`, `TARGET_HIT` (incl. `reason=HARD_TARGET_GBP` when peak P&L hits £50 hard exit), `TIMESTOP_HIT`, `TRAIL_EXIT` (trailing stop caught up).
- *Environmental:* `REGIME_CHANGED`, `REJECTED_RISK_BUDGET`.

Every rule-based decision the system makes produces one event row. No Claude narrative, no free text — just the event_type, reason_code, and the before/after payload the rule needed to fire.

## 4. Writer API

Python context manager, one `SessionWriter` per active session. Clean call-sites:

```python
from logging_mod import SessionWriter

with SessionWriter.open(session_date=today, label="US_REGULAR",
                        broker_mode="DEMO", account_size_gbp=5000.0) as sess:
    sess.log_scan(scan_record)
    for candidate in shortlist:
        sess.log_shortlist_entry(candidate)

    # During session, per minute for each active candidate:
    sess.log_snapshot(candidate_id, state_dict)

    # On any rule-based decision or state transition:
    sess.log_event(candidate_id,
                   event_type="STOP_MOVED",
                   actor="TRAIL_MANAGER",
                   reason_code="TRAIL_STEP",
                   payload={"old_stop": 138.80, "new_stop": 142.10})
```

Writes are buffered and flushed every 30s or on explicit `sess.flush()`. Crash-safety: WAL-mode SQLite, buffer to journal file on startup recovery. `broker_mode` is required at session open and stamped on every row.

## 5. Storage

**Hot (< 90 days):** SQLite. One DB per session date: `data/logs/YYYY/YYYY-MM-DD.sqlite`. Daily file bounds the max query set and makes archival trivial.

**Cold (> 90 days):** Parquet. Nightly job converts closed daily SQLite files to partitioned Parquet under `data/archive/snapshots/year=YYYY/month=MM/day=DD.parquet` (and similar for other streams). Raw SQLite deleted after 95 days.

**Query:** a thin `logs_query.py` helper that can read transparently from hot SQLite or cold Parquet via DuckDB (single SQL interface over both).

## 6. swing-committee → entry-rules Contract

Morning scan writes two files atomically (write to `.tmp`, then rename):

1. **`data/trades.json`** — the existing execution contract (shape per `sample_trades.json`: symbol, direction, market, entry_low, entry_high, stop, stake, status, notes). `run.py` already reads this to place orders. Unchanged.

2. **`data/scans/scan_YYYYMMDD.json`** — the rich scan artifact for the logging layer. Contains:
   - ScanRecord (full universe scoring, regime snapshot, rule_set_version, broker_mode)
   - N ShortlistEntry records — one per shortlisted A+/A/B candidate, with pillar votes, committee stance, entry plan, planned sizing.

At session open, `run.py` (or a dedicated `session_init.py`):
1. Detects `data/scans/scan_YYYYMMDD.json` exists.
2. Opens a new `SessionWriter` (with `broker_mode` from config).
3. Calls `sess.log_scan(...)` + `sess.log_shortlist_entry(...)` for each entry.
4. Kicks off the per-minute snapshot loop for the active shortlist.
5. Loads `data/trades.json` for the execution engine as today.

No separate inbox or file-move choreography needed — the scan artifact is read-once at session open.

Schema mismatch on the scan artifact = session aborts with a clear error. Never silently dropped.

## 7. Versioning & Provenance

Every record carries:

- `schema_version` — integer, bumped whenever the Pydantic model changes. Old rows remain readable; queries join through a small migration table if needed.
- `rule_set_version` — short git SHA of entry-rules at record-write time. Lets us ask "how did the A+ win rate change after the rule tweak on 12 May?" by partitioning on rule_set_version.

Migrations live in `src/logging_mod/migrations/NNN_description.py` — standard Alembic-style, idempotent, forward-only.

## 8. Retention

- SQLite: 90 days hot.
- Parquet: indefinite (2-year default, archive tier after that).
- No PII is logged, so GDPR isn't a concern here — but the account balance and stake sizes are sensitive. Logs stay local on Mark's machine; no cloud sync without explicit opt-in.

## 9. Implementation Order

1. Pydantic models in `src/models/` for the five record types (Session, Scan, Shortlist, Snapshot, Event).
2. SQLite DDL + `src/logging_mod/db.py` extended (today's file only handles the existing audit table).
3. `SessionWriter` context manager + per-stream writer modules (`session_log.py`, `scan_log.py`, `shortlist_log.py`, `snapshot_log.py`, `event_log.py`).
4. `session_init.py` — reads `data/scans/scan_YYYYMMDD.json`, opens a session, logs scan + shortlist, returns the live `SessionWriter` to `run.py`.
5. One-minute scheduler hook in `run.py` — for each active shortlist candidate, gather state and call `sess.log_snapshot()`.
6. Decision-point instrumentation — every rule evaluation site in `gates.py`, `entry_classifier.py`, `risk_manager.py`, `executor.py` emits the appropriate event (including `ENTRY_EVALUATED_NO_ENTER` at each per-minute evaluation).
7. swing-committee side: emit the rich scan artifact to `data/scans/scan_YYYYMMDD.json` at end of `/api/scanner` runs. `data/trades.json` already works.
8. Tests: `tests/test_logging.py` covering round-trip write→read for each record type; an integration test opens a session, writes 3 minutes of snapshots for 2 candidates, logs a `TRIGGER_FIRED` + `STOP_MOVED` + `STOP_HIT` sequence, asserts DB state, asserts `broker_mode` on every row.
9. Cold archive job (cron): nightly sqlite→parquet conversion + old-file deletion.
10. Session report script: daily summary (candidates tracked, terminal status breakdown, rejection code histogram, per-grade realised R, 30-min invalidation hit/miss, time-to-exit distribution vs the 2–3 day cap).

Estimated build time: 3–5 focused sessions for steps 1–5 (critical path to start collecting data). Steps 6–10 can follow.

## 10. Resolved Decisions

1. ~~Scan frequency.~~ **One morning scan per day.** No mid-session re-scan.
2. ~~Post-entry snapshot resolution.~~ **1-minute throughout.** Acceptable because holds are capped at 2–3 days and most trades should finish intraday. Extra rows per held swing ≈ 1,500 max — immaterial.
3. ~~Re-entry on stopped-out symbol.~~ **Not allowed same day.** Stopped out = terminal, done. Single candidate_id per shortlist entry; no re-link field needed.
4. ~~Claude narrative retention.~~ **No narrative retention.** Logs capture rule-based decisions only — event_type + reason_code + structured payload. Claude's qualitative output is ephemeral.
5. ~~DEMO/LIVE flag.~~ **Required on every session.** Denormalised to every snapshot and event row for defensive querying.

## 11. Companion Sub-Spec

Exit management rules are defined in **`Exit_Management_v1.md`** (same folder) — stepped £-threshold trail (arm £25 → BE+£1; +£5 stop per +£5 of peak P&L; hard market exit at +£50). The logging layer reflects those rules via the following events: `TRAIL_MODE_ACTIVATED`, `STOP_MOVED(reason=TRAIL_ARM|TRAIL_STEP|MANUAL)`, `TARGET_HIT(reason=HARD_TARGET_GBP)`, `TRAIL_EXIT`, `TIMESTOP_HIT`, `INVALIDATION_EXIT`, `STOP_HIT`. The snapshot row fields `peak_unrealised_pnl_gbp`, `current_locked_profit_gbp`, `trail_step_count`, `trail_mode_active`, `invalidation_window_active`, and `mins_to_timestop` together fully determine the exit state machine per candidate — idempotent on restart.

## 12. Out of Scope (explicit)

- Full-universe per-minute snapshots.
- Real-time dashboarding (we build SQL report scripts, not Grafana).
- Replicating logs off-machine (local only for v1).
- Multi-user / multi-account support.
- Claude narrative text in logs.
- Free-text anywhere in logs (every field is structured or enum).
- ProRealTime-side logging (PRT writes its own execution logs; we reconcile on a separate cycle).
