# Fix Monitor TERMINAL State Dropping Trail Management on Open Positions — Spec for Claude Code

## Context

DEMO Day-1 shakedown 2026-04-20, catastrophic silent failure:

- JNJ SHORT filled at 14:14:25 UK, entry 23390, stop 23881, stake 0.24/pt.
- By 14:30 UK, JNJ had moved in our favour to ~23233, giving +£37.68 unrealised (well past the +£25 trail-arm threshold).
- Per the exit-management ladder (`feedback_exit_management.md`): trail should have armed at +£25 (stop to BE+£1 ≈ 23386), then stepped up at +£30 (stop to BE+£6 ≈ 23366) and +£35 (stop to BE+£11 ≈ 23346).
- Actual IG stop: 23881 — unchanged from initial fill. No trail events fired.
- `SELECT symbol, status, ... FROM candidate_snapshots WHERE symbol='JNJ' ORDER BY ts_utc DESC LIMIT 1` returned `status=TERMINAL, last_seen=13:32:24 UTC` — monitor stopped writing snapshots 18+ minutes before we manually intervened.
- Mark manually closed to lock +£37.68. If price had reversed without intervention, the initial stop at 23881 would have fired and we'd have turned a winner into a -£117.84 loss.

The monitor marked a plan `TERMINAL` while the position was still open at IG. `TERMINAL` in `MonitorLoop.run_one_tick` means "skip this plan on subsequent ticks" — so the trail-ladder logic (which runs inside the tick loop for fired plans) never got called. Position is live at broker; monitor has given up on it.

This is the worst class of bug in the system: open risk, no active management, no loud error.

## Scope

Two phases. Phase 1 is investigation (must land before Phase 2 code changes). Phase 2 is the fix.

### Phase 1: Root-cause investigation (no code changes yet)

Find **exactly why** `state.terminal = True` is being set on a filled, still-open position. Do NOT guess. Do NOT ship a "maybe this fixes it" change.

Candidate hypotheses to investigate, in order of likelihood:

1. **Exception-swallowing handler** — an exception in the post-fill exit-evaluation path (modify_stop call, trail_manager evaluation, IG API error) is caught broadly and sets `state.terminal = True` as a defensive "we don't know what to do so stop trying" fallback. This is the most common pattern for this symptom.
2. **Broker reply handler misinterpreting a successful fill as terminal** — `TRIGGERED_OPEN` status is being routed through a code path that flips terminal because the open-order lifecycle ends there, but the position-lifecycle (which uses the same plan object) does not start a new ticking loop.
3. **Session clock or cutoff check** — a session-end or last-entry cutoff check is running against filled positions and flipping them terminal even though they should continue until exit.
4. **Two code paths updating `state`** — e.g. an exit-manager writes terminal while the monitor is still mid-tick, and the next tick skips the plan.
5. **State persistence/reload bug** — if the monitor reloads state from DB and the DB already has terminal=true from a prior run or a stale row, it never re-opens the ticking.

### Phase 2: Fix (based on Phase 1 findings)

The fix must restore this invariant:

> **A plan with `fill_ts_utc` set and no terminating event (STOP_HIT, TARGET_HIT, TRAIL_EXIT, TIMESTOP_HIT, INVALIDATION_EXIT, manual_close) must be ticked on every monitor iteration until one of those terminating events fires.**

Three possible fix shapes, pick based on Phase 1 root cause:

- **Fix A (most likely):** Remove the premature `state.terminal = True` assignment in the offending code path. Filled positions stay `state.fired=True, state.terminal=False` until a proper close event fires.
- **Fix B:** If exit-management is supposed to live in a separate `exit_manager` loop distinct from the pre-trigger monitor, wire that loop up properly in `run.py` so filled plans get handed off to it (and verify the hand-off actually happens on live fills, not just in tests).
- **Fix C:** If the exception-swallowing hypothesis is right, replace the bare `except: state.terminal=True` with a specific catch + retry-with-backoff, and let unexpected errors propagate loudly so they surface in logs instead of silently killing the tick.

## Steps

Operate in `/Users/mark.sear/CoWork/entry-rules/money-program-trading`.

### Phase 1: Investigation

1. **Verify the repo is clean and on main.** Last commit should be the `feat(broker): attach grade-scaled £ take-profit limit` from the sibling spec (or `f07f68b` stake descaling if that ships first).

2. **Grep for every place `terminal` is set.**
   ```
   grep -rn "terminal\s*=\s*True" src/
   grep -rn "state\.terminal" src/
   grep -rn "TERMINAL" src/
   ```
   Build an exhaustive list of call sites. For each: identify the conditions under which it fires. Document findings as code comments or in a file `TERMINAL_INVESTIGATION.md` at repo root (delete after fix ships).

3. **Reproduce the failure against JNJ-like scenario in a test.** Using mocked IG responses:
   - Place an open position successfully.
   - Return a successful fill from the broker.
   - Drive the monitor loop forward a few ticks with price moves that should trigger trail arming.
   - Inspect plan state after each tick. The moment `state.terminal` flips is the bug site.

   If the test can't reproduce it (i.e. the monitor correctly ticks open positions in unit tests), then the bug is in the interaction with real IG responses — add integration-style tests using the recorded IG session from today if you can find one in `reports/live_20260420_*.log`.

4. **Check for exception-swallowing.** Specifically grep:
   ```
   grep -rn "except Exception" src/engine/
   grep -rn "except:" src/engine/
   ```
   Any broad catch that touches `state.terminal` or that runs inside a tick loop for open positions is a suspect. Narrow the catch or add explicit logging before settling terminal.

5. **Check the `candidate_events` and `audit_log` tables for JNJ from today.**
   ```
   sqlite3 data/trading.db "SELECT ts_utc, event_type, reason_code, terminal_reason, substr(payload_json,1,200) FROM candidate_events ce JOIN shortlist_entries se ON ce.candidate_id=se.candidate_id WHERE se.symbol='JNJ' ORDER BY ts_utc ASC;"
   ```
   Events between 13:14:25 (fill) and 13:32:24 (last snapshot) will show what the monitor was doing right before it stopped ticking. The LAST event before the gap is the smoking gun.

6. **Write findings to `TERMINAL_INVESTIGATION.md`** with:
   - Which of the 5 hypotheses matched.
   - The exact file:line where terminal is erroneously being set.
   - The reproduction test (or failed attempt).
   - Recommended fix shape (A, B, or C).

   Commit this as a separate investigation commit before Phase 2 starts, so the diagnostic record is preserved even if the fix itself is simple.

### Phase 2: Fix

7. **Apply the fix.** Exact code depends on Phase 1, but the test suite changes (below) are mandatory regardless.

8. **Add the invariant assertion.** In `MonitorLoop.run_one_tick` (or wherever the per-tick dispatch lives), at the top of the loop:
   ```python
   if state.terminal and state.fired and not state.has_close_event():
       logger.error(
           "INVARIANT VIOLATION: plan %s is terminal+fired with no close event. "
           "This is the TERMINAL-monitor bug. Forcing state.terminal=False to "
           "resume ticking. Investigate logs between fill_ts_utc and now.",
           plan.symbol,
       )
       # Defensive: re-enable ticking rather than leave position unmanaged.
       state.terminal = False
   ```
   This is a backstop. If the root-cause fix misses a case, this prevents the failure mode from being silent — we get a loud ERROR log AND the monitor keeps ticking. If the defensive re-enable causes a different bug, that's fine, it will be loud and diagnosable.

9. **Add regression tests.** `tests/test_monitor_terminal_invariant.py` (new file):

   - **Test 1 — filled position stays tickable through trail arming.** Mock fill, drive peak P&L past +£25, assert `state.terminal == False` after each tick AND assert `STOP_MOVED` event fires with `reason=TRAIL_ARM`.
   - **Test 2 — filled position stays tickable through trail-step.** Mock fill, drive peak P&L past +£30, +£35. Assert stop moves in +£5 increments. Assert plan never flips terminal.
   - **Test 3 — terminal is only set on valid close events.** For each of {STOP_HIT, TARGET_HIT, TRAIL_EXIT, TIMESTOP_HIT, INVALIDATION_EXIT}: mock the condition → assert terminal becomes True AND the corresponding event fires AND no further ticks process the plan.
   - **Test 4 — exception in exit evaluation does NOT terminate the plan.** Raise a simulated IG API error from `broker.modify_stop`. Assert: (a) the error is logged, (b) state.terminal stays False, (c) the monitor tries again on next tick (with backoff if implemented).
   - **Test 5 — the invariant assertion fires loudly.** Force state.terminal=True on a filled plan via direct manipulation → run a tick → assert ERROR logged AND state.terminal flipped back to False.

10. **Run the full engine suite.** `pytest tests/ -q`. All pass. Special attention to any test that ASSUMED the buggy TERMINAL-on-fill behaviour — update those tests; their expectations were wrong.

11. **Do not launch DEMO yourself.**

12. **Commit (or two commits: investigation + fix, if you split them).**

    Investigation commit (if separate):
    ```
    docs(monitor): investigate TERMINAL-on-open-position bug

    Documents root cause for the DEMO Day-1 2026-04-20 silent-failure
    where JNJ SHORT stopped being ticked by the monitor while still
    open at IG, causing trail-ladder (£25 arm, +£5 steps) to not fire
    and leaving a +£37.68 peak unlocked.

    Root cause: <fill in from Phase 1>.

    See TERMINAL_INVESTIGATION.md for the grep trail, the failing
    reproduction test, and the recommended fix shape.
    ```

    Fix commit:
    ```
    fix(monitor): keep ticking filled positions until a close event fires

    Monitor was prematurely setting state.terminal on filled positions,
    short-circuiting the trail-ladder (£25 arm, +£5 steps, £50 cap).
    DEMO Day-1 2026-04-20: JNJ SHORT at +£37.68 peak never had its
    stop moved from initial — trail logic never evaluated because the
    plan was terminal. Manual close required.

    Root cause: <one sentence summary from Phase 1>.

    Fix: <one sentence on the code change>.

    Invariant backstop added in MonitorLoop.run_one_tick: a
    fired+terminal plan with no close event now triggers an ERROR log
    and forces terminal back to False, so any regression in this
    class of bug is loud rather than silent.

    Regression tests in tests/test_monitor_terminal_invariant.py
    cover the trail-arming flow, all five valid close events, the
    exception-in-exit-eval path, and the invariant assertion itself.
    ```

13. **Push to origin/main.** `git push origin main`.

## Acceptance

- Phase 1 findings documented (in commit, or TERMINAL_INVESTIGATION.md, or both).
- Phase 2 fix targets the actual root cause (not a guess).
- Invariant backstop assertion in place in `MonitorLoop.run_one_tick`.
- New regression test file `tests/test_monitor_terminal_invariant.py` covers 5 scenarios; all pass.
- Full engine test suite green.
- Manual re-run of a mock DEMO scenario (fill, drive P&L past £25) shows: `STOP_MOVED(reason=TRAIL_ARM)` event fires, IG stop moves to BE+£1 level, plan remains active and tickable.

## Rollback

`git revert` removes the fix and restores the broken state. Pre-fix behaviour is known-broken; rollback only if the fix introduces a NEW regression (e.g. failing to set terminal on legitimate close events, causing positions to re-tick after exit).

## Non-goals

- Do NOT redesign the `MonitorLoop` or split it into pre-trigger + post-fill loops unless Phase 1 reveals that's the root cause. Minimal fix preferred.
- Do NOT touch `trail_manager.py` or the ladder thresholds. The ladder is correct; it just isn't being called.
- Do NOT change IG REST order-modify logic. The broker already supports `modify_stop` per the exit-management memory.
- Do NOT remove the `TRAIL_HARD_TARGET_GBP` monitor-side cap. Even with the sibling spec adding broker-side limits, the monitor-side cap is a useful secondary check (especially for partial fills or where the limit price didn't attach).

## Follow-ups for Mark after this lands

- Re-run DEMO Day-2 with both this fix AND the sibling `ADD_LIMIT_ON_OPEN` change in place. Verify on fill: IG shows both stop AND limit attached, and when P&L crosses +£25 the monitor writes a `STOP_MOVED(TRAIL_ARM)` event and IG's stop updates.
- If this fix turns out to be large (>1 day), consider running DEMO with only the sibling spec (broker-side £ limit) for a few days to at least get hard-cap protection, while this bug is investigated more thoroughly.
- Add monitor-loop health-check metric (e.g. count of filled-but-tickless plans) to the session summary so any future regression is visible in the end-of-day report.
