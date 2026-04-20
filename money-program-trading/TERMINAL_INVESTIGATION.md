# TERMINAL-on-Open-Position Investigation (Phase 1)

**Incident:** DEMO Day-1 2026-04-20. JNJ SHORT filled at 13:14:24 UTC, monitor
marked the candidate `status=TERMINAL` at 13:32:24 UTC (t+18min) while the
position was still live at IG with the original stop (238.81) and no trail
events. Mark manually closed to lock profit.

This document is Phase 1 output only: grep trail, event-table forensics,
a failing reproduction test, and the hypothesis match. **No `src/` code
changes.** Fix is Phase 2.

---

## 1. Grep trail — every `state.terminal = True` call site

Only 3 assignments in the entire `src/` tree (grep `terminal\s*=\s*True` /
`state\.terminal`):

| # | Location | Condition |
|---|----------|-----------|
| A | [src/engine/monitor.py:536](money-program-trading/src/engine/monitor.py:536) | Inside `_handle_fire` — broker's `place_open_position` returned `success=False` or `fill_price is None`. Terminal reason: `INVALIDATED_PRE_TRIGGER`. Only reached before `state.fill_ts_utc` is populated, so does not apply to a filled position. |
| B | [src/engine/monitor.py:716](money-program-trading/src/engine/monitor.py:716) | Inside `_handle_exit` — called when `trail_manager.evaluate_exit` returns `ExitAction.EXIT`. Sets terminal **unconditionally**, including when `broker.close_position` returned `success=False` (see lines 701-708: "Recording terminal event anyway — operator must reconcile"). |
| C | [src/engine/monitor.py:1192](money-program-trading/src/engine/monitor.py:1192) | Inside `_emit_session_end_events` — loop shutdown path. Guarded by `if state is None or state.fired or state.terminal: continue` (line 1176), so filled positions are skipped. |

Call-site **B** is the only one reachable for a filled, open position.

Broad `except` audit per spec step 4 (`grep -rn "except Exception" src/engine/`
and `grep -rn "except:" src/engine/`): no bare `except:` inside the monitor
tick loop touches `state.terminal`. The one broad handler in the tick loop is
[monitor.py:355](money-program-trading/src/engine/monitor.py:355)
(`except Exception as e: logger.exception("Tick failed: %s", e)`), which logs
and continues — it does not mutate state.

---

## 2. `candidate_events` table — JNJ on 2026-04-20 (fill → last snapshot)

```
$ sqlite3 data/trading.db "SELECT ts_utc, event_type, reason_code,
      terminal_reason, substr(payload_json,1,200) FROM candidate_events ce
      JOIN shortlist_entries se ON ce.candidate_id=se.candidate_id
      WHERE se.symbol='JNJ' ORDER BY ts_utc ASC;"
```

| ts_utc | event_type | reason_code | terminal_reason | payload (excerpt) |
|---|---|---|---|---|
| 13:14:24 | SHORTLIST_ADDED |  |  | grade=C, stake=11.6 £/pt, planned_risk=£50 |
| 13:14:24 | TRIGGER_FIRED |  |  | last_price=234.25, trigger_low=233.5, **trigger_high=234.5** |
| 13:14:24 | ORDER_PLACED |  |  | ref=LNEA2AU68ELTYP5, stake=11.6 £/pt, stop=238.81 |
| 13:14:24 | FILLED |  |  | deal_id=DIAAAAW9ELXVPAB, **fill_price=233.9**, initial_stop=238.81, initial_risk=£56.96 |
| **13:32:24** | **INVALIDATION_EXIT** | **INVALIDATION** | **INVALIDATION_EXIT** | **last_price=234.96, mins_since_fill=18, fill_price=233.9** |

**Smoking gun: the `INVALIDATION_EXIT` event at 13:32:24.**

Snapshot series for JNJ over the 18-minute window (truncated):

```
13:20:24  TRIGGERED_OPEN  last=234.195  peak_pnl=0.0  invalidation_window_active=1  mins=6
...
13:31:54  TRIGGERED_OPEN  last=234.48   peak_pnl=0.0  invalidation_window_active=1  mins=17
13:32:24  TERMINAL         last=234.96   peak_pnl=0.0  invalidation_window_active=1  mins=18
```

Monitor's local view of JNJ price stayed in 234.02–234.96 the whole 18 minutes.
It never observed a favourable move — `peak_unrealised_pnl_gbp` is 0.0 on every
row. (Mark's narrative of +£37.68 peak at ~23233 reflects the live IG deal view,
which disagrees with the prices the monitor was polling — a separate data-feed
divergence, out of scope for this bug.)

### What fired the exit

[src/engine/trail_manager.py:336-342](money-program-trading/src/engine/trail_manager.py:336):

```python
mins_since_fill = (now - position.fill_ts_utc).total_seconds() / 60.0
if (
    mins_since_fill < config.invalidation_window_minutes   # 18 < 30 → True
    and _adverse_trigger_cross(plan, last)                 # SHORT: 234.96 > 234.5 → True
):
    return ExitOutcome(action=ExitAction.EXIT, reason=ExitReason.INVALIDATION, ...)
```

The rule behaved as designed: within 30 min of fill, price re-crossed
`trigger_high` (234.96 > 234.5) on a SHORT, so `evaluate_exit` returned
`EXIT(INVALIDATION)`. `MonitorLoop._handle_exit` then ran.

### Why the position stayed open at IG

`_handle_exit` at [monitor.py:691-717](money-program-trading/src/engine/monitor.py:691)
calls `broker.close_position`, then sets `state.terminal = True`
**unconditionally**. The close-failure branch (L701-708) explicitly logs
`"Close failed … Recording terminal event anyway — operator must reconcile"`
and falls through to L716.

If that close call silently failed (or was retried by IG and the response
mis-parsed), the DB records `INVALIDATION_EXIT`, the monitor stops ticking
(next tick's `if state.terminal: continue` skips the plan), but IG still
has an open short with the original 238.81 stop.

We do not have the live session log (`reports/live_20260420_*.log` not present
in the repo) to confirm whether `close.success` was `False` — but the observed
reality (position live at IG, monitor shows TERMINAL) is consistent with that
branch, and it is the only silent-failure mode present in the code.

---

## 3. Failing reproduction test

`tests/test_monitor_terminal_invariant_repro.py` — one test, reproducing the
JNJ failure mode in isolation:

> A SHORT position filled 18 minutes ago, adverse re-cross into invalidation,
> `broker.close_position` returns `success=False`. Asserts the spec's
> Phase 2 invariant — that without a successful close, the plan **must not**
> be marked terminal. This is expected to **FAIL** against current code.

Actual outcome when run (Phase 1, no fix applied):

```
FAILED tests/test_monitor_terminal_invariant_repro.py::test_failed_close_must_not_mark_terminal
AssertionError: Plan was marked terminal despite broker.close_position returning success=False
```

See the test file for the full scenario.

---

## 4. Hypothesis match

Mapping to the 5 spec hypotheses:

| # | Hypothesis | Match? |
|---|---|---|
| 1 | **Exception-swallowing handler** — an exception in the exit-evaluation path is caught and falls back to `state.terminal = True` | **Strongest match.** Not a literal `except Exception`, but the same failure *pattern*: an ambiguous/failed broker close is swallowed (logger.error + fallthrough) and defensively marks terminal ("we don't know what to do so stop trying"). |
| 2 | Broker reply handler misinterprets a successful fill as terminal | Partial — `_handle_exit` does conflate "we decided to close" with "close actually happened", but it's not about the original fill. |
| 3 | Session clock / cutoff check flips terminal on filled positions | Conceptually similar (time-windowed rule evaluated on fills), but the firing rule is `INVALIDATION` (trail_manager.py:336-342), not session-clock. The rule firing was correct by its own logic; the bug is downstream. |
| 4 | Two code paths updating `state` | No. |
| 5 | State persistence / reload bug | No — single session, no reload. |

**Best match: Hypothesis 1.**

Root cause in one sentence: `MonitorLoop._handle_exit` sets `state.terminal = True`
regardless of whether `broker.close_position` succeeded, so a silently-failed
close (or any `EXIT` decision paired with a broker error) abandons the position
at IG while the monitor stops ticking it.

**Recommended Phase 2 fix shape: C** — narrow the defensive terminal
assignment. When `broker.close_position` returns `success=False`:
1. Emit a loud audit / ERROR event (distinct from the terminal event).
2. Do **not** set `state.terminal = True`; keep the plan tickable so the
   next tick re-evaluates and (if rule still fires) retries the close.
3. Optionally gate retries behind a backoff counter on the runtime state.

The invariant backstop assertion at the top of `run_one_tick` (per Phase 2
step 8 of the spec) is a useful second line of defence for any future
regression in this class.

### Secondary concerns surfaced (out of scope for Phase 2 fix — flag for follow-up)

- **Monitor's market snapshot disagreed with live IG pricing for JNJ.** The
  monitor's `last_traded` stayed 234.02–234.96 over 18 minutes while the live
  deal hit 232.33. This is a data-feed / scaling / caching concern separate
  from the TERMINAL bug — worth its own investigation, because even a correct
  Phase 2 fix will not prevent spurious INVALIDATION_EXITs triggered by a
  stale feed.
- **`broker.close_position` reliability.** If close calls fail silently often
  enough to matter, the broker wrapper itself needs better error surfacing.
