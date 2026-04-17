# Trail & Exit Events — Taxonomy

Reference for the event types emitted by `MonitorLoop` during the open-position phase of a candidate's lifecycle. Use this when auditing a `candidate_events` stream, interpreting a journal, or wiring a new consumer (e.g. a dashboard) against the event log.

All event payloads are pydantic `BaseModel`s with a `kind` discriminator literal. Full definitions live in `src/models/candidate_event.py`; event-type strings in `src/models/log_enums.py::EventType`. This doc is the "when is each emitted, what's in the payload" reference — not a replacement for the models.

---

## Event ordering, per trade

For a trade that runs to a clean trailed exit, the event stream looks like:

```
SHORTLIST_ADDED        (scan ingest)
TRIGGER_ARMED          (pre-trigger path hitting the watch zone)
TRIGGER_FIRED          (entry condition met)
ORDER_PLACED           (broker.place_open_position called)
FILLED                 (IG confirms fill_price + deal_id)
TRAIL_MODE_ACTIVATED   (peak P&L ≥ trail_activation_gbp — once per trade)
STOP_MOVED (×N)        (reason=TRAIL_ARM first, then TRAIL_STEP per band)
TRAIL_EXIT             (terminal — price retraces to trailed stop)
```

Stopped-out-before-arm skips TRAIL_* entirely: `… FILLED → STOP_HIT`. Hard-target exits skip TRAIL_EXIT: `… STOP_MOVED* → TARGET_HIT`. Timestop bypasses price-triggered exits: `… TIMESTOP_HIT`.

---

## TRAIL_MODE_ACTIVATED

Emitted exactly once per trade, the first tick where `peak_pnl_gbp ≥ config.trail_activation_gbp` (default £25). Paired with a `STOP_MOVED` event at the same tick whose `reason=TRAIL_ARM`. After this, the initial stop no longer matters — the trail owns the stop.

**Payload** (`TrailModeActivatedPayload`):
- `peak_pnl_gbp` — peak unrealised P&L at arm (≥ `trail_activation_gbp`)
- `trail_activation_gbp` — the threshold that armed the trail (snapshot of config at arm time)
- `initial_locked_gbp` — £ locked above breakeven on the arm step (default £1)

**Idempotence guard**: `state.trail_mode_activated` prevents re-emission. If you see two of these for one trade in the DB, that's a bug.

## STOP_MOVED

Emitted whenever the trail ratchets the stop. Fires once per tick even if multiple bands are crossed (fast gap) — inspect `new_trail_step_count − old_trail_step_count` to count bands. Paired with an IG `broker.modify_stop` call; if IG rejects the modify, the event is NOT emitted and the recorded stop stays at the previous level.

**Payload** (`StopMovedPayload`):
- `reason` — `TRAIL_ARM` (first move, coincides with TRAIL_MODE_ACTIVATED), `TRAIL_STEP` (band ratchet), or `MANUAL` (reserved, not yet emitted automatically)
- `old_stop`, `new_stop` — stop prices
- `old_trail_step_count`, `new_trail_step_count` — 0–5 (bounded by `config.max_trail_steps`)
- `old_locked_gbp`, `new_locked_gbp` — £ locked above breakeven, computed from step count
- `peak_pnl_gbp_at_move` — peak P&L observed on this tick

## STOP_HIT

Terminal. Emitted when price hits the **initial** stop before trail arm. Implies `trail_mode_activated=False`. Realised P&L is negative (trade went against you before the trail could engage). `broker.close_position` is called first.

**Payload** (`StopHitPayload`):
- `stop_price` — the stop that was hit (always the initial stop for this event)
- `fill_price` — original entry price
- `realised_pnl_gbp` — computed from broker close fill price when available; falls back to outcome last_price, then to unrealised P&L

**Terminal reason**: `STOPPED_OUT`.

## TRAIL_EXIT

Terminal. Emitted when price retraces to the trailed stop **after** the trail has armed. `locked_gbp > 0` — that's the profit the trail preserved. Realised P&L may exceed `locked_gbp` depending on how close to the peak the retrace stop sits.

**Payload** (`TrailExitPayload`):
- `trail_stop_price` — the trailed stop that was hit
- `fill_price` — close fill price from broker (or last_price fallback)
- `locked_gbp` — £ locked at exit, derived from `compute_locked_gbp(trail_step_count, config)`. **Always > 0** post-arm; if you see 0 here, the monitor has regressed (see the fix comment at monitor.py's `_emit_terminal` TRAIL_EXIT branch)
- `trail_step_count` — final step count (1–5)
- `realised_pnl_gbp` — from broker fill; usually ≥ `locked_gbp`

**Terminal reason**: `TRAIL_EXIT`.

## TARGET_HIT

Terminal. Emitted when `peak_pnl_gbp` crosses the hard-target threshold (default £50 for HARD_TARGET_GBP). This is a £-based target, not a price-based one — `target_price` in the payload is `None` for this variant.

**Payload** (`TargetHitPayload`):
- `reason` — `HARD_TARGET_GBP` (£ threshold) or `R_MULTIPLE` (reserved, not yet emitted)
- `target_price` — `None` for `HARD_TARGET_GBP`; set to the trigger price for R-multiple variants when implemented
- `peak_pnl_gbp` — peak at the moment of exit (≥ `config.hard_target_gbp`)
- `realised_pnl_gbp` — from broker close fill

**Terminal reason**: `HARD_TARGET_HIT`.

## TIMESTOP_HIT

Terminal. Emitted when a position has been held across `config.timestop_sessions` complete sessions without hitting stop/target/trail-exit. Session boundaries are counted by the session_init loop — `sessions_held` is authoritative.

**Payload** (`TimestopHitPayload`):
- `last_price` — closing tick price at timestop
- `sessions_held` — integer session count that triggered the timestop
- `realised_pnl_gbp` — from broker close fill

**Terminal reason**: `TIMESTOP_HIT`.

## INVALIDATION_EXIT

Terminal. Emitted when, within `config.invalidation_window_minutes` of fill, price crosses back through the trigger in the wrong direction. Protects against fake breakouts — a LONG that fires then collapses back through trigger within the window is cut immediately rather than waiting for the full stop.

**Payload** (`InvalidationExitPayload`):
- `last_price` — tick price that triggered invalidation
- `mins_since_fill` — whole minutes between fill_ts and this tick (should be ≤ `invalidation_window_minutes`)
- `fill_price` — original entry

**Terminal reason**: `INVALIDATION_EXIT`.

---

## Observability contracts

1. **Every open-position tick writes a snapshot.** Silent evaluation is a bug. If you see a gap in snapshot timestamps for a live candidate, the monitor is misbehaving.
2. **Terminal events always have `terminal_reason` populated.** Queries filtering on `terminal_reason IS NOT NULL` should return every closed trade.
3. **Every terminal event corresponds to a `broker.close_position` call** (when broker is wired). Exception: if IG rejects the close, the terminal event is emitted anyway and `logger.error` logs "operator must reconcile" — check IG UI manually.
4. **`STOP_MOVED` emission is gated on IG modify success.** If IG rejects the stop-modify, the event is suppressed. Means: absence of `STOP_MOVED` after price enters the next band is a signal to check IG connectivity, not code.
5. **`TRAIL_MODE_ACTIVATED` is at-most-once per trade.** Enforced by `state.trail_mode_activated` flag. Test: `test_trail_arm_does_not_reemit_on_repeated_ticks`.

## Common auditing queries

Final event per terminated candidate:

```sql
SELECT candidate_id, event_type, terminal_reason, ts_utc,
       json_extract(payload, '$.realised_pnl_gbp') AS pnl
FROM candidate_events
WHERE terminal_reason IS NOT NULL
ORDER BY ts_utc DESC;
```

Trail ratchet history for a trade:

```sql
SELECT ts_utc,
       json_extract(payload, '$.reason') AS reason,
       json_extract(payload, '$.old_trail_step_count') AS old_step,
       json_extract(payload, '$.new_trail_step_count') AS new_step,
       json_extract(payload, '$.new_locked_gbp') AS locked
FROM candidate_events
WHERE candidate_id = ? AND event_type = 'STOP_MOVED'
ORDER BY ts_utc;
```

Multi-band fast-gap detection (any tick where the trail crossed >1 band):

```sql
SELECT candidate_id, ts_utc,
       json_extract(payload, '$.new_trail_step_count') -
       json_extract(payload, '$.old_trail_step_count') AS bands
FROM candidate_events
WHERE event_type = 'STOP_MOVED'
  AND bands > 1;
```
