# Attach Grade-Scaled £ Take-Profit Limit at Order-Open — Spec for Claude Code

## Context

DEMO Day-1 shakedown 2026-04-20 exposed a silent failure: JNJ SHORT filled at 14:14 UK, hit +£37.68 peak P&L (comfortably past the +£25 trail-arm threshold), but the monitor marked the plan `status=TERMINAL` while the position was still open at IG. The trail-management ladder (£25 arm → BE+£1, +£5 steps, £50 hard cap) stopped evaluating. Mark manually closed to lock profit. If a £50 take-profit limit had been sitting at IG from order-open, the hard cap would have fired regardless of our monitor state.

This spec fixes the safety-net half of that gap. It does NOT fix the underlying TERMINAL-monitor bug (separate spec: `FIX_MONITOR_TERMINAL_BUG_SPEC.md`). Together they make exit management robust to both broker-side and monitor-side failure modes.

## Rule

Every call to `broker.place_open_position` MUST attach a `limit_level` set to the price where unrealised P&L equals the **grade-scaled £ target**:

| Grade | Risk % | Risk £ (£10k) | £ target | Notional R |
|-------|--------|---------------|----------|-----------|
| A+    | 1.25%  | £125          | **£62.50** | 0.50R   |
| A     | 1.00%  | £100          | **£50.00** | 0.50R   |
| B     | 1.00%  | £100          | **£50.00** | 0.50R   |

Design choice: target scales with risk so that every trade caps at ~0.5R regardless of grade. Cleanly symmetric with the risk ladder we just locked.

## Scope

Surgical. Two files in `src/engine/`:

1. `monitor.py` — compute `limit_price` in `_handle_fire` (or wherever `broker.place_open_position` is called) and pass it as `limit_level`.
2. `broker.py` — verify `place_open_position` already threads `limit_level` through (the IG REST scaling path is already in place from the stake-descaling fix). No logic change expected; only confirm that `None` vs `float` is handled correctly.

**Do not** modify the trail-ladder code — the £25 arm, +£5 steps, and £50 hard cap in `trail_manager.py` (or wherever the ratchet lives) stay as they are. This spec only adds a BELT-AND-BRACES broker-enforced exit; the monitor-side ladder still runs between entry and target when the monitor is healthy.

**Do not** change the £25 trail-arm threshold or the +£5 step size — keep those universal for all grades regardless of target. Only the HARD CAP scales. (If that turns out wrong in practice we'll revisit; today's scope is "make the cap broker-enforced without redesigning the ladder".)

## Math

For both LONG and SHORT, given:
- `entry_price` — the fill price in IG-quoted units (not scan units; what the broker used)
- `ig_stake_per_pt` — the POST-descaled, POST-minDealSize-clamped stake the broker actually sent (not the scan-unit stake)
- `target_gbp` — £62.50 for A+, £50 for A/B

`limit_price = entry_price ± (target_gbp / ig_stake_per_pt)`
- **LONG**: `entry_price + delta` (target ABOVE entry)
- **SHORT**: `entry_price - delta` (target BELOW entry)

Worked examples for today's open ARM and JNJ (both were Grade-C bypass so used the £50 baseline):

| Position | Direction | Entry | ig_stake | Delta | Limit level |
|----------|-----------|-------|----------|-------|-------------|
| ARM      | LONG      | 16710 | 0.24/pt  | 50/0.24 = 208 | **16918** |
| JNJ      | SHORT     | 23390 | 0.24/pt  | 50/0.24 = 208 | **23182** |

And for a hypothetical A+ US-share LONG at 16710 with ig_stake=0.50/pt:

| Grade | Entry | ig_stake | target £ | Delta | Limit level |
|-------|-------|----------|----------|-------|-------------|
| A+    | 16710 | 0.50/pt  | £62.50   | 62.5/0.50 = 125 | **16835** |

Note the target delta uses the POST-clamp stake. If minDealSize clamped the stake upward, the real £ target sits at a TIGHTER price than plan assumed — that's correct; £62.50 is £62.50 regardless of how we got to this stake level.

## Steps

Operate in `/Users/mark.sear/CoWork/entry-rules/money-program-trading`.

1. **Verify the repo is clean and on main.** `git status` should show no uncommitted broker/monitor-related changes. Last commit should be `f07f68b` (stake descaling fix). If work-in-progress exists, stash with a descriptive message.

2. **Read the relevant code paths.**
   - `src/engine/broker.py` `place_open_position` — confirm `limit_level` parameter exists and is scaled via `to_ig_units`. If the parameter is missing or gated, fix that first.
   - `src/engine/monitor.py` around `_handle_fire` (monitor's fire-handler, where `broker.place_open_position` is called, approx lines 447-485 — exact lines may have drifted since fix `f07f68b`).
   - `src/models/` for the plan/candidate dataclasses — identify where the `grade` attribute lives so we can map grade → target_gbp.

3. **Add a grade → target mapping.** Minimal config, one place:
   ```python
   # src/engine/exit_config.py  OR  inline in monitor.py if exit_config doesn't exist yet
   GRADE_TARGET_GBP = {
       "A+": 62.50,
       "A":  50.00,
       "B":  50.00,
       "C":  50.00,  # Grade-C bypass treated as B-equivalent for target sizing
   }
   DEFAULT_TARGET_GBP = 50.00
   ```
   If an `exit_config.py` (or similar) already holds `TRAIL_ACTIVATION_GBP`, `TRAIL_HARD_TARGET_GBP`, etc. — put the mapping there and deprecate the scalar `TRAIL_HARD_TARGET_GBP=50` in favour of the mapping. Preserve backward compatibility by exposing a helper `get_hard_target_gbp(grade: str) -> float`.

4. **Compute `limit_price` in `monitor._handle_fire`.** Between the fire-decision and the `broker.place_open_position` call:

   ```python
   # After stake computation, before broker call:
   target_gbp = GRADE_TARGET_GBP.get(plan.grade, DEFAULT_TARGET_GBP)

   # Use the IG-units stake the broker will actually send (descaled + minDealSize-clamped).
   # If that value is computed inside the broker, move the computation up or expose it
   # so monitor can see the FINAL stake. Do not duplicate the descale logic.
   ig_stake = broker.compute_ig_stake(epic=plan.ig_epic, size=plan.planned_stake_gbp_per_pt)

   if ig_stake > 0:
       delta = target_gbp / ig_stake
       if plan.direction == "LONG":
           limit_price = entry_price + delta
       else:  # SHORT
           limit_price = entry_price - delta
   else:
       limit_price = None
       logger.error(
           "Monitor: ig_stake=0 for %s, cannot compute limit_price; opening without target",
           plan.symbol,
       )

   order_result = broker.place_open_position(
       epic=plan.ig_epic,
       direction=plan.direction,
       size=plan.planned_stake_gbp_per_pt,
       stop_price=plan.stop_price,
       limit_level=limit_price,   # <-- NEW
   )
   ```

   The `entry_price` used for the calc should be the plan's trigger/entry price (not the last tick) — we're computing the target at order-open, not per-tick. If the actual fill comes back at a different price (slippage), the target stays where it is; the variance just shows up in the journal as target_delta_gbp ≠ exactly £62.50/£50. That's acceptable for v1.

5. **Verify the broker threads `limit_level` through correctly.** In `broker.place_open_position`, after scaling:
   ```python
   ig_limit_level = self._to_ig(epic, limit_level) if limit_level is not None else None
   ...
   self.ig.create_open_position(
       ...,
       limit_level=ig_limit_level,
       ...
   )
   ```
   If this is already present (expected from the price-scaling fix), no change. If `limit_level` is being silently dropped or not scaled, fix it here.

6. **Extend the fill-event log.** The `FILLED` / `ORDER_PLACED` structured event must now include:
   - `limit_level_scan` — the unscaled limit price in scan units
   - `limit_level_ig` — the post-scaling limit price sent to IG
   - `target_gbp` — the £ target the limit was computed to lock
   - `grade_used` — so we can audit that the correct grade target was applied

   This is mandatory — the journal must show "opened at X, stop at Y, limit at Z for £target" on every fill. If the event doesn't carry this, the change hasn't shipped.

7. **Add regression tests.** Create `tests/test_broker_limit_on_open.py` (or extend existing broker tests):
   - Mock IG at trading_ig layer; capture `create_open_position` kwargs.
   - Mock MarketData with scalingFactor=100, min_deal_size=0.05.
   - **Test A (Grade-A LONG):** `broker.place_open_position(..., size=0.50, limit_level=167.10 + (50/0.50)/100)` via the monitor path. Assert captured IG kwarg `limit_level == 16835` (cents). Verify target recoverable: `(limit_ig - entry_ig) × ig_stake = 50.00`.
   - **Test B (Grade-A+ LONG):** same setup, grade="A+", stake post-descale=0.50/pt. Assert captured limit reflects £62.50.
   - **Test C (Grade-A SHORT):** direction="SHORT", assert limit is BELOW entry and reflects £50.
   - **Test D (Grade-C LONG with minDealSize clamp):** stake descaled to 0.05 → clamped to 0.24 (minDealSize). Assert limit is computed off the CLAMPED 0.24 stake, not the 0.05 descaled stake, and locks exactly £50.
   - **Test E (edge case — ig_stake=0):** no IG stake possible, limit_price=None, assert `limit_level` kwarg is either absent or None; assert error logged.

8. **Run the test suite.** `pytest tests/test_broker_limit_on_open.py -v` first. Then the broker suite: `pytest tests/ -q -k broker`. Then the full engine suite. All must pass.

9. **Do not launch a DEMO session yourself.** Mark will re-run the preflight + launch.

10. **Commit.**

    ```
    feat(broker): attach grade-scaled £ take-profit limit at order-open

    Every opening order now attaches a `limit_level` at IG set to the
    price where unrealised P&L equals the grade's hard target:
      A+ = £62.50, A/B = £50.00.

    Rationale from DEMO Day-1 2026-04-20: monitor marked JNJ's plan
    TERMINAL while position was open at IG; trail-ladder stopped
    evaluating; manual intervention required to lock +£37.68 profit.
    Broker-enforced limit makes the hard cap robust to monitor
    failure — it fires at IG regardless of our tick-loop state.

    Trail ladder (£25 arm → BE+£1 → +£5 steps) unchanged; only the
    hard cap is now broker-side. See FIX_MONITOR_TERMINAL_BUG_SPEC.md
    for the complementary monitor fix.

    Grade target scales with risk (0.5R cap regardless of grade).
    Target is computed from POST-clamp ig_stake so minDealSize
    clamping is respected; divergence from plan shows up in the
    FILLED event as target_gbp vs plan expected.

    Regression tests in tests/test_broker_limit_on_open.py.
    ```

11. **Push to origin/main.** `git push origin main`.

## Acceptance

- Single commit titled `feat(broker): attach grade-scaled £ take-profit limit at order-open` on `main`.
- New test file `tests/test_broker_limit_on_open.py` covers the five cases above; all pass.
- Full engine test suite green (no regressions from stake-descaling commit).
- `FILLED` event payload now includes `limit_level_scan`, `limit_level_ig`, `target_gbp`, `grade_used`.
- Existing `TRAIL_HARD_TARGET_GBP` scalar either removed or wrapped by the grade-mapping helper; no dangling unused constants.
- Manual IG check (Mark will do this post-launch): an open position shows BOTH a stop-loss AND a take-profit limit on the IG web UI.

## Rollback

`git revert <sha>` removes the limit attachment. Pre-revert state is known-working for orders (post stake-descaling fix) but exposes us to monitor-TERMINAL silent failure on exits. Only rollback if this commit introduces a regression on order placement itself.

## Non-goals

- Do NOT remove the monitor-side trail ladder. £25 arm, +£5 ratchet, £50 (or £62.50) cap still run monitor-side — they provide the £1-to-£49 progressive lock-in that this broker-enforced cap does not.
- Do NOT change the £25 trail-arm threshold or the +£5 step size across grades. Only the HARD CAP scales with grade in this change.
- Do NOT refactor the stake-computation logic introduced by the descaling fix. Just ensure its final value is accessible to the caller for the limit calc.
- Do NOT touch `swing-committee/lib/resolveRiskPct.js` or any signal-emitter sizing. Scan side stays as-is; this is an execution-side change.

## Follow-ups for Mark after this lands

- Verify on the next fill that the FILLED event shows `limit_level_ig` populated and the IG web UI shows the take-profit. If both present, the belt is on.
- Task #19 (monitor TERMINAL bug) still blocks unattended DEMO runs even after this ships — the intermediate trail steps between £1 and £49 locked are still monitor-dependent. Next spec.
- Consider whether we want the £25 arm to also scale (£31.25 for A+ to hold proportion)? Currently it does not. Revisit after 10+ DEMO fills show whether the ratio feels right.
