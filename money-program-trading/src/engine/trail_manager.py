"""
trail_manager — pure exit-management rule math for one open position.

The deterministic exit hierarchy (from Exit_Management_v1.md, mirrored in the
project's exit-management feedback memory):

1. Invalidation exit (within first N minutes of fill, price re-crosses trigger
   adversely) → EXIT(INVALIDATION).
2. Initial stop hit (trail not yet armed, price reaches initial stop) →
   EXIT(INITIAL_STOP).
3. Hard target (peak unrealised P&L ≥ ``trail_hard_target_gbp``, default £50) →
   EXIT(HARD_TARGET).
4. Trail stop hit (trail armed, price reaches current trailed stop) →
   EXIT(TRAIL_EXIT).
5. Trail activation / trail step (peak crosses band) → MOVE_STOP(TRAIL_ARM)
   or MOVE_STOP(TRAIL_STEP).
6. Timestop (position open ≥ ``timestop_sessions``) → EXIT(TIMESTOP).
7. Otherwise → HOLD.

Why this ordering differs slightly from the memory's numbered list:
- Hard target is evaluated before trail step because at peak ≥ £50 we want
  ONE clean market exit, not a stop-advance-then-exit pair of events. Exiting
  at £50 is the endpoint of the ratchet; advancing the stop to some capped
  band right before the exit is pure log noise.
- Multi-band gap handling (memory): when a single tick crosses several £5
  bands, emit ONE STOP_MOVED to the final band. That's what
  ``compute_trail_step_count(peak)`` does naturally — it derives the expected
  step count from peak alone, so the comparison ``expected > current`` gives
  the advance in one step regardless of how many bands we actually crossed.

This module does NOT touch IG, SQLite, or the SessionWriter. It consumes a
snapshot + a PositionState + config and returns an ExitOutcome. The
MonitorLoop owns the I/O around it (snapshot fetch, stop modification,
position close, event emission).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..models.common import Direction
from ..models.log_enums import CandidateGrade
from .monitor import CandidatePlan
from .session_clock import SessionClock

# ---------------------------------------------------------------------------
# Config + state dataclasses
# ---------------------------------------------------------------------------


# Grade → £ hard-cap target. Scales with risk so every trade caps at ~0.5R.
# - A+: 1.25% risk → £125 on £10k → £62.50 target (0.50R)
# - A/B: 1.00% risk → £100 on £10k → £50.00 target (0.50R)
# - C: bypass/mechanics-test only — sized at B's 0.5% ladder, target matches B.
# The mapping lives at module scope (not on ExitConfig) so session_init can
# keep passing the scalar ``trail_hard_target_gbp`` as the default. Callers
# should route grade-aware lookups through :func:`get_hard_target_gbp`.
GRADE_TARGET_GBP: dict[str, float] = {
    "A+": 62.50,
    "A": 50.00,
    "B": 50.00,
    "C": 50.00,
}


def get_hard_target_gbp(
    grade: CandidateGrade | str | None, config: "ExitConfig"
) -> float:
    """Resolve the £ hard-cap target for a plan's grade.

    Falls back to ``config.trail_hard_target_gbp`` (historically a scalar
    default of £50) when the grade is missing, unknown, or ``None``. This
    keeps legacy callers that pass the scalar directly working, while the
    new broker-enforced limit-on-open path consumes the mapping.
    """
    if grade is None:
        return config.trail_hard_target_gbp
    key = grade.value if isinstance(grade, CandidateGrade) else str(grade)
    return GRADE_TARGET_GBP.get(key, config.trail_hard_target_gbp)


@dataclass(frozen=True)
class ExitConfig:
    """Thresholds driving the exit hierarchy. All £ GBP.

    Defaults match ``src/config/settings.py`` — callers should build this from
    :func:`get_settings` so operators can tune behaviour without code changes.

    ``trail_hard_target_gbp`` is the fallback used by
    :func:`get_hard_target_gbp` when a plan's grade is missing or unknown.
    The grade-aware target lives in the module-level
    :data:`GRADE_TARGET_GBP` map; the trail ladder and the broker-enforced
    limit-on-open both route through the helper so the per-grade scaling
    is applied in exactly one place.
    """

    trail_activation_gbp: float = 25.0
    trail_initial_lock_gbp: float = 1.0
    trail_step_trigger_gbp: float = 5.0
    trail_step_size_gbp: float = 5.0
    trail_hard_target_gbp: float = 50.0
    invalidation_window_minutes: int = 30
    timestop_sessions: int = 3


@dataclass
class PositionState:
    """Per-position live state the exit rule needs.

    All fields are re-derivable from the event log on restart — no hidden
    mutable flags. ``peak_pnl_gbp`` is the monotonic high-water mark of
    unrealised P&L since fill; the caller is expected to max() it in each
    tick before calling :func:`evaluate_exit`.
    """

    fill_price: float
    fill_ts_utc: datetime
    stake_gbp_per_pt: float
    initial_stop_price: float
    current_stop_price: float
    peak_pnl_gbp: float
    trail_step_count: int  # 0 = pre-arm; 1..5 = each £5 band after arm
    sessions_held: int  # 1 = same session as fill; +1 each subsequent session


# ---------------------------------------------------------------------------
# Outcome types
# ---------------------------------------------------------------------------


class ExitAction(str, Enum):
    """High-level decision returned per tick."""

    HOLD = "HOLD"
    MOVE_STOP = "MOVE_STOP"
    EXIT = "EXIT"
    NO_PRICE = "NO_PRICE"


class ExitReason(str, Enum):
    """Why a MOVE_STOP / EXIT fired. Maps directly onto ``EventType`` +
    ``TerminalReason`` in the observability layer:

    - INVALIDATION → ``EventType.INVALIDATION_EXIT`` / ``TerminalReason.INVALIDATION_EXIT``
    - INITIAL_STOP → ``EventType.STOP_HIT`` / ``TerminalReason.STOPPED_OUT``
    - TRAIL_ARM    → ``EventType.TRAIL_MODE_ACTIVATED`` + ``EventType.STOP_MOVED``
    - TRAIL_STEP   → ``EventType.STOP_MOVED`` (reason=TRAIL_STEP)
    - HARD_TARGET  → ``EventType.TARGET_HIT`` / ``TerminalReason.HARD_TARGET_HIT``
    - TRAIL_EXIT   → ``EventType.TRAIL_EXIT`` / ``TerminalReason.TRAIL_EXIT``
    - TIMESTOP     → ``EventType.TIMESTOP_HIT`` / ``TerminalReason.TIMESTOP_HIT``
    """

    INVALIDATION = "INVALIDATION"
    INITIAL_STOP = "INITIAL_STOP"
    TRAIL_ARM = "TRAIL_ARM"
    TRAIL_STEP = "TRAIL_STEP"
    HARD_TARGET = "HARD_TARGET"
    TRAIL_EXIT = "TRAIL_EXIT"
    TIMESTOP = "TIMESTOP"
    # Intraday-preferred: SessionClock.must_hard_close() flipped True.
    # Maps to EventType.HARD_CLOSE_EXIT / TerminalReason.HARD_CLOSE.
    HARD_CLOSE = "HARD_CLOSE"


@dataclass
class ExitOutcome:
    """Everything the caller needs to emit events + act on the decision."""

    action: ExitAction
    reason: ExitReason | None = None

    # MOVE_STOP fields
    new_stop_price: float | None = None
    new_trail_step_count: int | None = None
    new_locked_gbp: float | None = None
    old_trail_step_count: int | None = None
    old_locked_gbp: float | None = None

    # Observability
    last_price: float | None = None
    unrealised_pnl_gbp: float | None = None
    peak_pnl_gbp: float | None = None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def unrealised_pnl_gbp(
    direction: Direction, last_price: float, fill_price: float, stake: float
) -> float:
    """Spread-bet unrealised P&L in £. Points × stake, signed by direction."""
    if direction == Direction.LONG:
        return (last_price - fill_price) * stake
    return (fill_price - last_price) * stake


def compute_trail_step_count(peak_pnl_gbp: float, config: ExitConfig) -> int:
    """Step count implied by peak P&L alone (0 = pre-arm, 1..5 after arm).

    Peak P&L | step | locked
    < £25    |  0   | (initial stop)
    £25–29   |  1   | +£1
    £30–34   |  2   | +£6
    £35–39   |  3   | +£11
    £40–44   |  4   | +£16
    £45–49   |  5   | +£21

    Caps at 5 — peak ≥ £50 is handled by the HARD_TARGET exit, which runs
    before a 6th band can matter.
    """
    if peak_pnl_gbp < config.trail_activation_gbp:
        return 0
    bands_past_arm = int(
        (peak_pnl_gbp - config.trail_activation_gbp) // config.trail_step_trigger_gbp
    )
    step = bands_past_arm + 1  # step 1 at arm, +1 per band
    return min(step, 5)


def compute_locked_gbp(trail_step_count: int, config: ExitConfig) -> float:
    """£ locked (above breakeven) at this step count. 0 pre-arm."""
    if trail_step_count <= 0:
        return 0.0
    return (
        config.trail_initial_lock_gbp
        + (trail_step_count - 1) * config.trail_step_size_gbp
    )


def compute_trail_stop_price(
    direction: Direction, fill_price: float, locked_gbp: float, stake: float
) -> float:
    """Stop price corresponding to a given £ lock above breakeven.

    LONG:  stop = fill + locked / stake   (above BE)
    SHORT: stop = fill - locked / stake   (below BE, points flip sign)
    """
    if stake <= 0:
        raise ValueError("stake_gbp_per_pt must be positive")
    offset = locked_gbp / stake
    if direction == Direction.LONG:
        return fill_price + offset
    return fill_price - offset


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------


def _adverse_trigger_cross(plan: CandidatePlan, last_price: float) -> bool:
    """True iff price is back on the wrong side of the trigger post-fill."""
    if plan.direction == Direction.LONG:
        return last_price < plan.trigger_low
    return last_price > plan.trigger_high


def _stop_hit(direction: Direction, last_price: float, stop_price: float) -> bool:
    """True iff ``last_price`` has reached or crossed ``stop_price`` adversely."""
    if direction == Direction.LONG:
        return last_price <= stop_price
    return last_price >= stop_price


def evaluate_exit(
    plan: CandidatePlan,
    position: PositionState,
    snapshot: dict,
    now: datetime,
    config: ExitConfig,
    session_clock: SessionClock | None = None,
) -> ExitOutcome:
    """Per-tick exit decision for one open position.

    Returns exactly one :class:`ExitOutcome`. The caller is responsible for:
    - Calling the broker to modify the stop / close the position.
    - Writing the event(s) corresponding to the reason (see ExitReason docs).
    - Updating the position's runtime state with
      ``new_trail_step_count`` / ``new_stop_price`` on MOVE_STOP.

    This function is pure: given the same inputs, it returns the same outcome.
    It never reads from / writes to IG or the DB.

    When ``session_clock`` is provided and ``now >= hard_close_utc``, the
    function short-circuits with ``EXIT(HARD_CLOSE)`` — the intraday-
    preferred model. We do this *after* confirming the snapshot is usable
    so we still record ``NO_PRICE`` for ticks where the market's closed or
    IG returned junk (otherwise we'd book a hard-close with no last price
    in the payload).
    """
    # --- Snapshot usability ---
    if not snapshot:
        return ExitOutcome(action=ExitAction.NO_PRICE)

    status = (snapshot.get("market_status") or "").upper()
    if status and status != "TRADEABLE":
        return ExitOutcome(action=ExitAction.NO_PRICE)

    last = snapshot.get("last_traded")
    if last is None:
        return ExitOutcome(action=ExitAction.NO_PRICE)

    # --- 0. Session-clock hard close (intraday-preferred) ---
    # Evaluated before everything else so we always market-exit on time.
    # Intentionally NOT gated on any P&L / trail state — if the clock says
    # go, we go.
    if session_clock is not None and session_clock.must_hard_close(now):
        pnl = unrealised_pnl_gbp(
            plan.direction, last, position.fill_price, position.stake_gbp_per_pt
        )
        peak = max(position.peak_pnl_gbp, pnl)
        return ExitOutcome(
            action=ExitAction.EXIT,
            reason=ExitReason.HARD_CLOSE,
            last_price=last,
            unrealised_pnl_gbp=pnl,
            peak_pnl_gbp=peak,
        )

    # --- Core computations reused across branches ---
    pnl = unrealised_pnl_gbp(
        plan.direction, last, position.fill_price, position.stake_gbp_per_pt
    )
    peak = max(position.peak_pnl_gbp, pnl)

    base = dict(
        last_price=last,
        unrealised_pnl_gbp=pnl,
        peak_pnl_gbp=peak,
    )

    # --- 1. Invalidation exit (first N minutes after fill, adverse trigger cross) ---
    mins_since_fill = (now - position.fill_ts_utc).total_seconds() / 60.0
    if (
        mins_since_fill < config.invalidation_window_minutes
        and _adverse_trigger_cross(plan, last)
    ):
        return ExitOutcome(action=ExitAction.EXIT, reason=ExitReason.INVALIDATION, **base)

    # --- 2. Initial stop hit (pre-arm only — armed case is TRAIL_EXIT below) ---
    if position.trail_step_count == 0 and _stop_hit(
        plan.direction, last, position.initial_stop_price
    ):
        return ExitOutcome(action=ExitAction.EXIT, reason=ExitReason.INITIAL_STOP, **base)

    # --- 3. Hard target — grade-scaled (A+ £62.50; A/B/C £50). Fall back to
    #     config.trail_hard_target_gbp when the plan's grade isn't in the
    #     mapping. The broker now also attaches an IG-side limit_level at
    #     this price so the cap fires even if the monitor misses a tick.
    hard_target = get_hard_target_gbp(plan.grade, config)
    if peak >= hard_target:
        return ExitOutcome(action=ExitAction.EXIT, reason=ExitReason.HARD_TARGET, **base)

    # --- 4. Trail stop hit (armed and price crossed the current trailed stop) ---
    if position.trail_step_count >= 1 and _stop_hit(
        plan.direction, last, position.current_stop_price
    ):
        return ExitOutcome(action=ExitAction.EXIT, reason=ExitReason.TRAIL_EXIT, **base)

    # --- 5. Trail activation / step (single advance to final band on multi-band gaps) ---
    expected_step = compute_trail_step_count(peak, config)
    if expected_step > position.trail_step_count:
        old_step = position.trail_step_count
        old_locked = compute_locked_gbp(old_step, config)
        new_locked = compute_locked_gbp(expected_step, config)
        new_stop = compute_trail_stop_price(
            plan.direction,
            position.fill_price,
            new_locked,
            position.stake_gbp_per_pt,
        )
        reason = ExitReason.TRAIL_ARM if old_step == 0 else ExitReason.TRAIL_STEP
        return ExitOutcome(
            action=ExitAction.MOVE_STOP,
            reason=reason,
            new_stop_price=new_stop,
            new_trail_step_count=expected_step,
            new_locked_gbp=new_locked,
            old_trail_step_count=old_step,
            old_locked_gbp=old_locked,
            **base,
        )

    # --- 6. Timestop (held ≥ N sessions) ---
    if position.sessions_held >= config.timestop_sessions:
        return ExitOutcome(action=ExitAction.EXIT, reason=ExitReason.TIMESTOP, **base)

    # --- 7. HOLD ---
    return ExitOutcome(action=ExitAction.HOLD, **base)


__all__ = [
    "ExitAction",
    "ExitConfig",
    "ExitOutcome",
    "ExitReason",
    "PositionState",
    "compute_locked_gbp",
    "compute_trail_step_count",
    "compute_trail_stop_price",
    "evaluate_exit",
    "unrealised_pnl_gbp",
]
