# Exit Management v1

**Status:** Draft for Mark's review. Locks in the stepped £-threshold trailing rule referenced from the CLAUDE instructions and Observability Design.
**Date:** 2026-04-16
**Home:** `entry-rules/money-program-trading/src/engine/trail_manager.py` (new module) + `src/engine/executor.py` (wiring).

## 1. Purpose

Define the deterministic rules for managing an open position from fill to exit. Small-account sizing (single-entry, 100% on trigger) means every exit is one-shot — we don't have a T2 to scale out into. So exits need to be mechanical, well-ordered, and rule-based.

The trail rule is a **stepped £-threshold ratchet**: arm at +£25 peak P&L, lock +£1 above breakeven, then raise the stop £5 for every additional £5 of peak P&L, and fully exit at +£50. All thresholds reference **peak unrealised P&L** (one-way ratchet — the stop never moves backwards).

## 2. The Exit Hierarchy

Six possible exits, checked in this order of precedence each minute during the session:

| # | Event | Trigger condition | Action | Log event |
|---|---|---|---|---|
| 1 | **Invalidation exit** | Within `INVALIDATION_WINDOW_MIN` (default 30) of fill, price re-crosses trigger level adversely (LONG: back below trigger_low; SHORT: back above trigger_high) | Close at market | `INVALIDATION_EXIT` |
| 2 | **Initial stop hit** | Price reaches initial stop_price (peak P&L still < `TRAIL_ACTIVATION_GBP`) | IG working order fills; we reconcile | `STOP_HIT` |
| 3 | **Trail activation** | Peak unrealised P&L ≥ `TRAIL_ACTIVATION_GBP` (default £25) | Move stop to breakeven + `TRAIL_INITIAL_LOCK_GBP` (default £1). Enter trail-mode. | `TRAIL_MODE_ACTIVATED` + `STOP_MOVED(reason=TRAIL_ARM)` |
| 4 | **Trail step** | In trail-mode AND peak unrealised P&L has advanced another `TRAIL_STEP_TRIGGER_GBP` (default £5) since last step/arm | Raise stop by `TRAIL_STEP_SIZE_GBP` (default £5). Increment trail_step_count. | `STOP_MOVED(reason=TRAIL_STEP)` |
| 5 | **Hard target hit** | Unrealised P&L ≥ `TRAIL_HARD_TARGET_GBP` (default £50) | Close at market | `TARGET_HIT(reason=HARD_TARGET_GBP)` |
| 6 | **Trail stop hit** | In trail-mode AND price reaches current trailed stop_price | IG stop fills; we reconcile | `TRAIL_EXIT` |
| 7 | **Timestop** | Position has been open ≥ `MAX_HOLD_SESSIONS` (default 3) trading sessions | Close at market near session end | `TIMESTOP_HIT` |

**Precedence note:** rules are checked in order each minute. Multiple conditions can be implied by a single price tick — precedence is strict (lower # wins). In practice trail-step (#4) is evaluated before trail-stop-hit (#6) so that a fast move through multiple £5 bands advances the stop first, then checks whether that advanced stop is breached.

**Multi-band gaps in one tick:** if a single price tick crosses multiple £5 bands (e.g. peak jumps from £27 to £43), the stop advances to the final band (+£16) in **one** `STOP_MOVED(reason=TRAIL_STEP)` event, not one event per band. Payload includes `old_trail_step_count` / `new_trail_step_count` so "bands crossed in one tick" is recoverable from the delta during log analysis. This is simpler for the executor (one IG `modify_stop` call), idempotent on restart, and preserves the analytical signal.

**Mapping peak P&L → locked profit:**

| Peak unrealised P&L | Stop position (relative to entry) | Locked profit if stop fills |
|---|---|---|
| £0 – £24.99 | initial stop (original risk) | negative (full 1R loss) |
| £25.00 – £29.99 | +£1 above BE | **+£1** |
| £30.00 – £34.99 | +£6 above BE | **+£6** |
| £35.00 – £39.99 | +£11 above BE | **+£11** |
| £40.00 – £44.99 | +£16 above BE | **+£16** |
| £45.00 – £49.99 | +£21 above BE | **+£21** |
| ≥ £50.00 | hard exit fires — close at market | **+£50** |

Trail distance once armed = `TRAIL_HARD_TARGET_GBP − TRAIL_ACTIVATION_GBP − stepped_locked = 50 − peak + locked`. At every band the distance between peak and stop is £24 (e.g. peak £30, stop locks +£6, distance = £24).

## 3. Directional Handling

Every price check must be direction-aware. Trail logic is P&L-based (GBP), not price-based, so direction is encapsulated inside `unrealised_pnl_gbp`.

**LONG:**
- Invalidation adverse cross: `price < trigger_low`
- Stop hit: `price ≤ stop_price`
- Unrealised P&L: `(current_price − entry_price) × stake_gbp_per_pt`
- Stop move (trail arm): `new_stop_price = entry_price + (TRAIL_INITIAL_LOCK_GBP / stake_gbp_per_pt)`
- Stop move (trail step): `new_stop_price = prev_stop_price + (TRAIL_STEP_SIZE_GBP / stake_gbp_per_pt)`

**SHORT:**
- Invalidation adverse cross: `price > trigger_high`
- Stop hit: `price ≥ stop_price`
- Unrealised P&L: `(entry_price − current_price) × stake_gbp_per_pt`
- Stop move (trail arm): `new_stop_price = entry_price − (TRAIL_INITIAL_LOCK_GBP / stake_gbp_per_pt)`
- Stop move (trail step): `new_stop_price = prev_stop_price − (TRAIL_STEP_SIZE_GBP / stake_gbp_per_pt)`

The "next red bar" concept from generic methodologies does NOT apply — we trade both directions. The stepped £-ratchet is inherently symmetric.

**Peak-based one-way ratchet.** The trail references `peak_unrealised_pnl_gbp`, which only increases. A retracement never lowers the stop. This prevents whipsaw "I was up £40 but now up £28" events from giving back the locked £16.

## 4. Config Parameters

In `src/config/settings.py`, new section:

```python
EXIT_MANAGEMENT = {
    "trail_activation_gbp": 25.00,    # peak £ unrealised P&L that arms trail-mode
    "trail_initial_lock_gbp": 1.00,   # £ above BE that stop is placed at on trail arm
    "trail_step_trigger_gbp": 5.00,   # every +£5 of peak P&L beyond activation...
    "trail_step_size_gbp": 5.00,      # ...moves the stop up £5 (one band per step)
    "trail_hard_target_gbp": 50.00,   # peak £ P&L that forces a market close
    "invalidation_window_minutes": 30,
    "max_hold_sessions": 3,           # hard timestop
    "timestop_exit_minutes_before_close": 15,  # buffer to avoid auction spreads
}
```

Rationale for defaults:
- **£25 activation / £50 hard target:** simple to monitor mentally; roughly 0.5% and 1% of a £5k account; starts locking profit early because small-account R-per-trade is tight and we'd rather bank £1 than watch a +£25 unwind to −1R.
- **£1 initial lock:** a nominal "we got something" to distinguish a triggered trail from a theoretical one. Avoids the zero-point ambiguity of a pure BE stop (exchanges and platforms sometimes round, and we want unambiguous attribution in the journal).
- **£5 step / £5 trigger:** symmetrical and simple. Locks 100% of each new £5 band — aggressive by design, because the edge is assumed to fade with time.
- **£50 hard target:** hard exit to avoid giving back a near-max gain on a spike-and-reverse; tunable without code change.
- **30-min invalidation:** matches §4 of the CLAUDE instructions.
- **3-session timestop:** upper end of the 2–3 day hold cap.

All thresholds scale with account size via config — no hard-coded constants in `trail_manager.py`.

## 5. State Machine

```
                 ┌─────────────────┐
                 │ PENDING_TRIGGER │
                 └────────┬────────┘
                          │ price crosses trigger
                          ▼
                 ┌─────────────────┐
                 │ TRIGGERED_OPEN  │
                 │ (first 30 min)  │
                 └────────┬────────┘
            ┌─────────────┼──────────────┬────────────────────┐
 adverse    │             │ 30 min pass  │ peak ≥ £25         │ initial stop hit
 re-cross   │             │              │                    │
   │        │             │              │                    │
   ▼        │             ▼              ▼                    ▼
INVALIDATION│  ┌────────────────────┐ ┌──────────────┐ ┌──────────┐
  EXIT     │  │ TRIGGERED_OPEN     │ │ TRAIL_MODE   │ │ TERMINAL │
           │  │ (normal)           │ │ (stepped)    │ │ (STOP)   │
           │  └──────────┬─────────┘ └──────┬───────┘ └──────────┘
           │             │                  │
           │             ├── initial stop ──┤
           │             │                  ├── every +£5 peak → step stop up £5
           │             │                  │   (stays in TRAIL_MODE)
           │             │                  │
           │             ├── timestop ──────┤
           │             │                  ├── trail stop hit
           │             │                  ├── peak ≥ £50 → hard exit
           │             │                  ├── timestop
           │             ▼                  ▼
           │       TERMINAL             TERMINAL
           ▼
      TERMINAL
```

## 6. Implementation Notes

- New module `src/engine/trail_manager.py` exposes:
  ```python
  def evaluate_exit(position: Position, current_price: float, now_utc: datetime) -> ExitDecision
  ```
  Returns one of: `("HOLD", None)`, `("EXIT", exit_event_type, reason_code)`, `("MOVE_STOP", new_stop_price, reason_code)`.
- Trail-mode state is **derivable from `peak_unrealised_pnl_gbp`** — no persistent flag needed. Given `peak`, `entry_price`, `initial_stop_price`, and stake, the expected stop position is:
  ```python
  def expected_stop(peak_gbp, entry_price, initial_stop, stake_gbp_per_pt, direction, cfg):
      if peak_gbp < cfg.trail_activation_gbp:
          return initial_stop
      # How many £5 bands above activation?
      bands = int((peak_gbp - cfg.trail_activation_gbp) // cfg.trail_step_trigger_gbp)
      locked_gbp = cfg.trail_initial_lock_gbp + bands * cfg.trail_step_size_gbp
      offset = locked_gbp / stake_gbp_per_pt
      return entry_price + offset if direction == "LONG" else entry_price − offset
  ```
  `evaluate_exit` then compares `expected_stop` with the recorded `stop_price` in the Position — if it's higher (LONG) / lower (SHORT), emit `MOVE_STOP` with `reason_code=TRAIL_STEP` (or `TRAIL_ARM` on first move).
- This formulation is **idempotent on `run.py` restart**: recomputing from peak P&L reproduces the correct stop without any stored flag.
- `src/engine/executor.py` per-minute loop:
  1. Update `peak_unrealised_pnl_gbp` on Position (max of existing and current).
  2. Call `evaluate_exit`.
  3. Act on decision: `IGOrders.modify_stop()` or `IGOrders.close_position()`.
  4. Emit the matching event via SessionWriter.
- Unit tests in `tests/test_trail_manager.py` cover: LONG and SHORT at each band boundary (£24.99, £25, £29.99, £30, …, £49.99, £50), precedence when multiple conditions trigger same minute, idempotent restart with only `peak_unrealised_pnl_gbp` in DB, peak-ratchet (stop does not move back when P&L retraces).

## 7. What This Does NOT Do (v1)

- No adaptive trail beyond the fixed £5 bands. ATR / chandelier / swing-low trailing is explicitly out of scope for v1.
- No scale-out at intermediate bands. Single-entry = single-exit; the stepped trail IS the scale-out substitute.
- No reactive adjustment for news / earnings during the hold. Rule 11 (earnings auto-exit) is separate and higher-precedence than this hierarchy — that comes from `risk_manager.py`.
- No weekend gap protection beyond the 2–3 day timestop. If timestop falls mid-weekend, close on the last open session minute before weekend close.
- No partial close when £50 is hit — it is a full market exit.

## 8. Confirmed Defaults

| Parameter | Default | Notes |
|---|---|---|
| `trail_activation_gbp` | **£25** | Peak P&L that arms trail-mode |
| `trail_initial_lock_gbp` | **£1** | Stop moves to BE + £1 on arm |
| `trail_step_trigger_gbp` | **£5** | Every +£5 of peak P&L… |
| `trail_step_size_gbp` | **£5** | …raises the stop £5 |
| `trail_hard_target_gbp` | **£50** | Hard market exit |
| `invalidation_window_minutes` | 30 | From §4 CLAUDE instructions |
| `max_hold_sessions` | 3 | Timestop |

Confirmed by Mark 2026-04-16.
