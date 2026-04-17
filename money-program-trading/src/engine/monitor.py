"""
MonitorLoop — the per-minute polling loop for shortlisted candidates.

Runs for the lifetime of a trading session. Each tick, for every candidate
that isn't already terminal, we:

1. Fetch a live IG market snapshot.
2. Classify the tick (pre-trigger) or evaluate the exit rule (post-fill).
3. Write a CandidateSnapshot row with the full live state.
4. Emit the right event(s) on state transitions (ARM, FIRE → ORDER_PLACED +
   FILLED, STOP_MOVED, STOP_HIT / TARGET_HIT / TRAIL_EXIT / INVALIDATION_EXIT
   / TIMESTOP_HIT).
5. On loop exit, emit SESSION_ENDED_NO_TRIGGER for any candidate that never
   fired.

Design rules
------------
- NO synthetic price data anywhere. Every decision is made from a real IG
  snapshot. Two pure decision functions (``classify_tick`` and
  :func:`~src.engine.trail_manager.evaluate_exit`) are exercised in unit
  tests with literal dicts shaped like IG returns; the live wire is covered
  by tests/test_monitor_live.py.
- Pre-trigger and post-fill paths each dispatch through a single pure
  function. The MonitorLoop only owns I/O orchestration.
- State transitions are idempotent at the event level: ARM once, FILLED
  once, TRAIL_MODE_ACTIVATED once. The runtime state flags these so a
  restarted tick never re-emits.
- Positions left open at session end stay live in IG. The next day's
  session_init is expected to load them from the DB and resume management.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import uuid4

from ..data.market_data import MarketData
from ..logging_mod.session_writer import SessionWriter
from ..models.candidate_event import (
    CandidateEvent,
    EntryEvaluatedNoEnterPayload,
    FilledPayload,
    InvalidationExitPayload,
    OrderPlacedPayload,
    SessionEndedNoTriggerPayload,
    StopHitPayload,
    StopMovedPayload,
    TargetHitPayload,
    TimestopHitPayload,
    TrailExitPayload,
    TrailModeActivatedPayload,
    TriggerArmedPayload,
    TriggerFiredPayload,
)
from ..models.candidate_snapshot import CandidateSnapshot
from ..models.common import Direction, EntryType, Market
from ..models.log_enums import (
    ActorKind,
    BrokerMode,
    CandidateGrade,
    CandidateStatus,
    EventType,
    StopMoveReason,
    TargetHitReason,
    TerminalReason,
)

logger = logging.getLogger(__name__)

# Arm band: within this fraction of trigger distance, emit TRIGGER_ARMED.
# 0.005 = within 0.5% of trigger on the favourable side.
DEFAULT_ARM_BAND_PCT = 0.005


class Decision(str, Enum):
    """What the per-tick rule says to do with a candidate right now."""

    HOLD = "HOLD"  # Far from trigger — write snapshot, no event.
    ARM = "ARM"  # Price is in the arm band — emit TRIGGER_ARMED (once).
    FIRE = "FIRE"  # Trigger crossed — emit TRIGGER_FIRED.
    REJECT = "REJECT"  # Gate/budget says no-enter this tick.
    NO_PRICE = "NO_PRICE"  # Market closed / snapshot empty — write snapshot only.


@dataclass
class CandidatePlan:
    """Immutable plan for one shortlisted candidate at session open.

    This is what the monitor tracks across ticks. Keeps all the fields we need
    without re-reading the DB every minute.
    """

    candidate_id: str
    scan_id: str
    session_id: str
    symbol: str
    market: Market
    direction: Direction
    setup_type: EntryType
    grade: CandidateGrade
    trigger_low: float
    trigger_high: float
    stop_price: float
    target_price: float | None
    ig_epic: str
    broker_mode: BrokerMode
    rule_set_version: str = ""
    # Pre-computed at scan time (swing-committee) and carried through ingest.
    # Used as the spread-bet size when we place the opening order. £/pt.
    planned_stake_gbp_per_pt: float = 0.0
    planned_risk_gbp: float = 0.0
    # Carried from the scan. When True, a future runtime gate engine must
    # compute gate masks for observability but must NOT block trigger firing
    # on any pre-trade entry gate. Exits + sizing are unaffected.
    gate_bypass: bool = False


@dataclass
class CandidateRuntimeState:
    """Mutable per-candidate state tracked across ticks.

    Contains both pre-trigger and post-fill state — separating them into two
    dataclasses wasn't worth the ceremony; the flags make clear which phase
    each field applies to.
    """

    # Pre-trigger -------------------------------------------------------
    armed_emitted: bool = False
    fired: bool = False
    last_rejection_code: str | None = None
    last_snapshot_ts: datetime | None = None

    # Post-fill (populated on successful FILLED event) ------------------
    deal_id: str | None = None
    deal_reference: str | None = None
    fill_price: float | None = None
    fill_ts_utc: datetime | None = None
    stake_gbp_per_pt: float | None = None
    initial_stop_price: float | None = None
    current_stop_price: float | None = None
    peak_pnl_gbp: float = 0.0
    trail_step_count: int = 0
    trail_mode_activated: bool = False
    sessions_held: int = 1  # 1 = same session as fill; session_init bumps on resume

    # Terminal ----------------------------------------------------------
    terminal: bool = False
    terminal_reason: TerminalReason | None = None


@dataclass
class TickOutcome:
    """Structured return from classify_tick — used by tests and logging."""

    decision: Decision
    rejection_code: str | None = None
    distance_pts: float | None = None  # signed; negative = past trigger


# ---------------------------------------------------------------------------
# Pure decision function — unit-testable without IG
# ---------------------------------------------------------------------------


def classify_tick(
    plan: CandidatePlan,
    snapshot: dict,
    runtime: CandidateRuntimeState,
    arm_band_pct: float = DEFAULT_ARM_BAND_PCT,
) -> TickOutcome:
    """Decide what to do for one candidate given a live snapshot.

    Args:
        plan: The candidate's immutable plan.
        snapshot: The dict returned by ``MarketData.get_market_snapshot``.
            Keys used: ``last_traded``, ``bid``, ``ask``, ``market_status``.
        runtime: Mutable per-candidate runtime state (for once-only events).
        arm_band_pct: Fraction of trigger distance at which we consider the
            candidate "armed". Default 0.5%.

    Returns:
        TickOutcome with a Decision + optional rejection_code + distance.

    The rule in words
    -----------------
    - If no usable price / market not tradeable: ``NO_PRICE``.
    - Long & last_price >= trigger_low: ``FIRE``.
    - Short & last_price <= trigger_high: ``FIRE``.
    - Price within ``arm_band_pct`` of trigger on the favourable side: ``ARM``
      (if not already emitted).
    - Otherwise: ``HOLD`` (we're outside the arm band — write snapshot only).

    REJECT is a slot reserved for a future gate engine — this module never
    returns it today. It exists here so callers can be stable when that lands.

    Gate bypass
    -----------
    When the landed gate engine runs, it must honour ``plan.gate_bypass``: if
    True, it should still compute gate masks for observability (so the journal
    shows what would have been rejected), but it must NOT return REJECT — the
    user curated this shortlist themselves in a mechanics-test scan. Exits and
    sizing are NOT affected by bypass.
    """
    if not snapshot:
        return TickOutcome(decision=Decision.NO_PRICE)

    status = (snapshot.get("market_status") or "").upper()
    # IG uses "TRADEABLE" for live markets. Anything else (CLOSED,
    # OFFLINE, EDITS_ONLY, AUCTION, SUSPENDED, etc.) means we should not
    # evaluate the trigger.
    if status and status != "TRADEABLE":
        return TickOutcome(decision=Decision.NO_PRICE)

    last = snapshot.get("last_traded")
    if last is None:
        return TickOutcome(decision=Decision.NO_PRICE)

    if plan.direction == Direction.LONG:
        distance = last - plan.trigger_low  # ≥0 means fired
        if distance >= 0:
            return TickOutcome(decision=Decision.FIRE, distance_pts=distance)
        # arm band: how far (in pts) below trigger_low
        band_pts = plan.trigger_low * arm_band_pct
        if -distance <= band_pts and not runtime.armed_emitted:
            return TickOutcome(decision=Decision.ARM, distance_pts=distance)
        return TickOutcome(decision=Decision.HOLD, distance_pts=distance)
    else:  # SHORT
        distance = plan.trigger_high - last  # ≥0 means fired
        if distance >= 0:
            return TickOutcome(decision=Decision.FIRE, distance_pts=distance)
        band_pts = plan.trigger_high * arm_band_pct
        if -distance <= band_pts and not runtime.armed_emitted:
            return TickOutcome(decision=Decision.ARM, distance_pts=distance)
        return TickOutcome(decision=Decision.HOLD, distance_pts=distance)


# ---------------------------------------------------------------------------
# Monitor loop orchestration
# ---------------------------------------------------------------------------


@dataclass
class MonitorLoop:
    """Per-minute loop over the shortlist for one session.

    Does not own the SessionWriter, MarketData, or Broker — all are injected.
    Each tick is idempotent; the loop can be restarted mid-session without
    corrupting the log.

    ``broker`` is optional so tests that only exercise the pre-trigger path
    (or the snapshot write) can construct a loop without IG order placement.
    When a FIRE decision needs to place a real order and ``broker`` is None,
    we log a warning, emit an ENTRY_EVALUATED_NO_ENTER with a clear rejection
    code, and mark the candidate fired=True so we don't re-evaluate.
    """

    writer: SessionWriter
    market_data: MarketData
    plans: list[CandidatePlan]
    broker: object | None = None
    exit_config: object | None = None  # ExitConfig; lazily imported to avoid cycles
    tick_interval_seconds: int = 60
    arm_band_pct: float = DEFAULT_ARM_BAND_PCT
    now_fn: Callable[[], datetime] = field(default=datetime.utcnow)

    _runtime: dict[str, CandidateRuntimeState] = field(default_factory=dict, init=False)
    _stop_requested: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for plan in self.plans:
            self._runtime.setdefault(plan.candidate_id, CandidateRuntimeState())
        if self.exit_config is None:
            # Lazy import avoids a cycle (trail_manager imports CandidatePlan).
            from .trail_manager import ExitConfig

            self.exit_config = ExitConfig()

    def seed_resumed_position(
        self, plan: CandidatePlan, state: CandidateRuntimeState
    ) -> None:
        """Register a position carried over from a prior session.

        Appends the plan (if not already present) and overwrites the runtime
        slot with the rehydrated state so the next tick routes straight into
        the open-position path. Safe to call any number of times before
        ``run_until``.
        """
        if not any(p.candidate_id == plan.candidate_id for p in self.plans):
            self.plans.append(plan)
        self._runtime[plan.candidate_id] = state

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_until(self, end_time_utc: datetime) -> None:
        """Run the loop until ``end_time_utc`` (UTC) or SIGINT.

        Writes a snapshot per active candidate per tick and emits events at
        decision points. On clean shutdown, emits SESSION_ENDED_NO_TRIGGER
        for any candidate still pending (never fired).
        """
        self._install_sigint_handler()

        while not self._stop_requested:
            now = self.now_fn()
            if now >= end_time_utc:
                logger.info("End time reached — stopping monitor loop.")
                break

            try:
                self.run_one_tick(now)
            except Exception as e:
                logger.exception("Tick failed: %s", e)

            # Sleep to next tick boundary (best-effort; drift-tolerant).
            elapsed = (self.now_fn() - now).total_seconds()
            remaining = max(0.0, self.tick_interval_seconds - elapsed)
            self._sleep_interruptible(remaining)

        self._emit_session_end_events()

    def request_stop(self) -> None:
        """Request a clean shutdown at the next tick boundary."""
        self._stop_requested = True

    def _install_sigint_handler(self) -> None:
        def _handler(_signum, _frame):
            logger.info("SIGINT received — requesting clean shutdown.")
            self.request_stop()

        try:
            signal.signal(signal.SIGINT, _handler)
        except (ValueError, AttributeError):
            # Can happen in non-main threads / some test environments. Not fatal.
            logger.debug("Could not install SIGINT handler in this thread.")

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in 1-second chunks so stop_requested takes effect promptly."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stop_requested:
            time.sleep(min(1.0, end - time.monotonic()))

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def run_one_tick(self, now: datetime | None = None) -> None:
        """Process one tick across all still-active candidates."""
        now = now or self.now_fn()
        for plan in self.plans:
            state = self._runtime[plan.candidate_id]
            if state.terminal:
                # Already closed — don't poll or write further snapshots.
                continue

            snapshot = self._safe_fetch(plan.ig_epic)

            if not state.fired:
                self._handle_pre_trigger_tick(plan, state, snapshot, now)
            else:
                self._handle_open_position_tick(plan, state, snapshot, now)

            state.last_snapshot_ts = now

    def _safe_fetch(self, epic: str) -> dict:
        try:
            return self.market_data.get_market_snapshot(epic)
        except Exception as e:
            logger.warning("Snapshot fetch failed for %s: %s", epic, e)
            return {}

    # ------------------------------------------------------------------
    # Pre-trigger path
    # ------------------------------------------------------------------

    def _handle_pre_trigger_tick(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        snapshot: dict,
        now: datetime,
    ) -> None:
        outcome = classify_tick(plan, snapshot, state, self.arm_band_pct)
        self._write_snapshot_pre_trigger(plan, state, snapshot, now, outcome)

        if outcome.decision == Decision.FIRE:
            self._handle_fire(plan, state, snapshot, now)
        elif outcome.decision == Decision.ARM:
            self._emit_trigger_armed(plan, now)
            state.armed_emitted = True
        elif outcome.decision == Decision.REJECT:
            code = outcome.rejection_code or "R_UNSPECIFIED"
            if code != state.last_rejection_code:
                self._emit_entry_evaluated_no_enter(plan, snapshot, now, code)
                state.last_rejection_code = code

    def _handle_fire(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        snapshot: dict,
        now: datetime,
    ) -> None:
        """On FIRE: emit TRIGGER_FIRED, place the order, emit ORDER_PLACED +
        FILLED, and transition the candidate to TRIGGERED_OPEN. On broker
        failure, mark the candidate terminal with an observability event so
        we don't silently skip it."""
        self._emit_trigger_fired(plan, snapshot, now)

        if self.broker is None:
            logger.warning(
                "FIRE for %s but no broker configured — marking fired (no fill).",
                plan.symbol,
            )
            state.fired = True
            self._emit_entry_evaluated_no_enter(plan, snapshot, now, "R_NO_BROKER")
            return

        if plan.planned_stake_gbp_per_pt <= 0:
            logger.error(
                "FIRE for %s but planned_stake_gbp_per_pt is 0 — refusing to fill.",
                plan.symbol,
            )
            state.fired = True
            self._emit_entry_evaluated_no_enter(plan, snapshot, now, "R_NO_STAKE")
            return

        result = self.broker.place_open_position(
            epic=plan.ig_epic,
            direction=plan.direction,
            size=plan.planned_stake_gbp_per_pt,
            stop_price=plan.stop_price,
        )

        # Emit ORDER_PLACED regardless of success — "we tried to open" is a fact.
        self._emit_order_placed(plan, state, result, now)
        state.fired = True
        state.deal_reference = result.deal_reference or None

        if not result.success or result.fill_price is None:
            logger.warning(
                "Order rejected for %s: status=%s reason=%s",
                plan.symbol,
                result.deal_status,
                result.reason_code,
            )
            self._emit_entry_evaluated_no_enter(
                plan, snapshot, now, result.reason_code or "R_BROKER_REJECT"
            )
            # Broker didn't open a position — we never had one to manage.
            state.terminal = True
            state.terminal_reason = TerminalReason.INVALIDATED_PRE_TRIGGER
            return

        # Populate runtime position state and emit FILLED.
        state.deal_id = result.deal_id or ""
        state.fill_price = result.fill_price
        state.fill_ts_utc = now
        state.stake_gbp_per_pt = plan.planned_stake_gbp_per_pt
        state.initial_stop_price = plan.stop_price
        state.current_stop_price = plan.stop_price
        state.peak_pnl_gbp = 0.0
        state.trail_step_count = 0
        state.trail_mode_activated = False
        state.sessions_held = 1

        initial_risk_gbp = abs(result.fill_price - plan.stop_price) * plan.planned_stake_gbp_per_pt
        self._emit_filled(plan, state, initial_risk_gbp, now)
        logger.info(
            "FILLED %s %s @ %.4f (stake %.2f £/pt, stop %.4f, risk %.2f GBP)",
            plan.symbol,
            plan.direction.value,
            result.fill_price,
            plan.planned_stake_gbp_per_pt,
            plan.stop_price,
            initial_risk_gbp,
        )

    # ------------------------------------------------------------------
    # Post-fill (open position) path
    # ------------------------------------------------------------------

    def _handle_open_position_tick(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        snapshot: dict,
        now: datetime,
    ) -> None:
        """Run the exit engine for a TRIGGERED_OPEN candidate."""
        from .trail_manager import (
            ExitAction,
            PositionState,
            evaluate_exit,
            unrealised_pnl_gbp,
        )

        # Track peak P&L regardless of exit outcome — must update BEFORE we
        # snapshot so the written row reflects the latest high-water mark.
        if snapshot and snapshot.get("last_traded") is not None and state.fill_price is not None:
            live_pnl = unrealised_pnl_gbp(
                plan.direction,
                snapshot["last_traded"],
                state.fill_price,
                state.stake_gbp_per_pt or 0.0,
            )
            state.peak_pnl_gbp = max(state.peak_pnl_gbp, live_pnl)

        if any(
            v is None
            for v in (
                state.fill_price,
                state.fill_ts_utc,
                state.stake_gbp_per_pt,
                state.initial_stop_price,
                state.current_stop_price,
            )
        ):
            logger.error(
                "Open-position tick for %s but runtime state incomplete — skipping.",
                plan.symbol,
            )
            return

        pos = PositionState(
            fill_price=state.fill_price,
            fill_ts_utc=state.fill_ts_utc,
            stake_gbp_per_pt=state.stake_gbp_per_pt,
            initial_stop_price=state.initial_stop_price,
            current_stop_price=state.current_stop_price,
            peak_pnl_gbp=state.peak_pnl_gbp,
            trail_step_count=state.trail_step_count,
            sessions_held=state.sessions_held,
        )
        outcome = evaluate_exit(plan, pos, snapshot, now, self.exit_config)

        self._write_snapshot_open_position(plan, state, snapshot, now, outcome)

        if outcome.action == ExitAction.MOVE_STOP:
            self._handle_move_stop(plan, state, outcome, now)
        elif outcome.action == ExitAction.EXIT:
            self._handle_exit(plan, state, outcome, now)
        # HOLD / NO_PRICE → just the snapshot, nothing else.

    def _handle_move_stop(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        outcome,
        now: datetime,
    ) -> None:
        """MOVE_STOP: modify IG, emit STOP_MOVED (+ TRAIL_MODE_ACTIVATED on ARM)."""
        from .trail_manager import ExitReason

        if self.broker is None or not state.deal_id:
            logger.warning(
                "MOVE_STOP for %s but no broker/deal_id — updating in-memory only.",
                plan.symbol,
            )
            ok = True
        else:
            modify = self.broker.modify_stop(state.deal_id, outcome.new_stop_price)
            ok = modify.success
            if not ok:
                logger.warning(
                    "IG stop-modify rejected for %s (deal_id=%s): %s",
                    plan.symbol,
                    state.deal_id,
                    modify.reason_code,
                )
                # Don't update state — the recorded stop stays at the previous level.
                return

        # On TRAIL_ARM, emit TRAIL_MODE_ACTIVATED first so log consumers see
        # "armed at 15:03" before "stop moved at 15:03".
        if outcome.reason == ExitReason.TRAIL_ARM and not state.trail_mode_activated:
            self._emit_trail_mode_activated(plan, state, outcome, now)
            state.trail_mode_activated = True

        self._emit_stop_moved(plan, state, outcome, now)

        state.current_stop_price = outcome.new_stop_price
        state.trail_step_count = outcome.new_trail_step_count

    def _handle_exit(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        outcome,
        now: datetime,
    ) -> None:
        """EXIT: close the position at IG, emit the terminal event."""

        close_fill_price: float | None = None
        if self.broker is not None and state.deal_id:
            close = self.broker.close_position(
                deal_id=state.deal_id,
                direction=plan.direction,
                epic=plan.ig_epic,
                size=state.stake_gbp_per_pt or 0.0,
            )
            if not close.success:
                logger.error(
                    "Close failed for %s (deal_id=%s): %s. "
                    "Recording terminal event anyway — operator must reconcile.",
                    plan.symbol,
                    state.deal_id,
                    close.reason_code,
                )
            close_fill_price = close.fill_price
        realised_pnl = self._realised_pnl_gbp(plan, state, close_fill_price, outcome)

        event_type, terminal_reason = _exit_reason_to_types(outcome.reason)
        self._emit_terminal(
            plan, state, outcome, event_type, terminal_reason, realised_pnl, now
        )
        state.terminal = True
        state.terminal_reason = terminal_reason
        logger.info(
            "Closed %s %s (%s): realised_pnl=%.2f GBP",
            plan.symbol,
            plan.direction.value,
            outcome.reason.value if outcome.reason else "UNKNOWN",
            realised_pnl,
        )

    def _realised_pnl_gbp(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        close_fill_price: float | None,
        outcome,
    ) -> float:
        """Realised P&L from the close fill (or best estimate if not available)."""
        from .trail_manager import unrealised_pnl_gbp

        if state.fill_price is None or state.stake_gbp_per_pt is None:
            return 0.0
        exit_price = close_fill_price
        if exit_price is None:
            exit_price = outcome.last_price
        if exit_price is None:
            return outcome.unrealised_pnl_gbp or 0.0
        return unrealised_pnl_gbp(
            plan.direction, exit_price, state.fill_price, state.stake_gbp_per_pt
        )

    # ------------------------------------------------------------------
    # Snapshot writers
    # ------------------------------------------------------------------

    def _write_snapshot_pre_trigger(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        snapshot: dict,
        now: datetime,
        outcome: TickOutcome,
    ) -> None:
        status = (
            CandidateStatus.TRIGGERED_OPEN
            if state.fired and not state.terminal
            else CandidateStatus.PENDING_TRIGGER
        )
        snap = CandidateSnapshot(
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            scan_id=plan.scan_id,
            symbol=plan.symbol,
            market=plan.market,
            direction=plan.direction,
            setup_type=plan.setup_type,
            grade=plan.grade,
            ts_utc=now,
            minute_bucket=_minute_bucket(now),
            status=status,
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
            last_price=snapshot.get("last_traded") if snapshot else None,
            bid=snapshot.get("bid") if snapshot else None,
            ask=snapshot.get("ask") if snapshot else None,
            dist_to_trigger_pts=outcome.distance_pts,
        )
        self.writer.write_snapshot(snap)

    def _write_snapshot_open_position(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        snapshot: dict,
        now: datetime,
        outcome,
    ) -> None:
        from .trail_manager import ExitAction, compute_locked_gbp

        status = (
            CandidateStatus.TERMINAL
            if outcome.action == ExitAction.EXIT
            else CandidateStatus.TRIGGERED_OPEN
        )
        locked = compute_locked_gbp(state.trail_step_count, self.exit_config)
        mins_since_fill = (
            int((now - state.fill_ts_utc).total_seconds() / 60.0)
            if state.fill_ts_utc
            else None
        )
        snap = CandidateSnapshot(
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            scan_id=plan.scan_id,
            symbol=plan.symbol,
            market=plan.market,
            direction=plan.direction,
            setup_type=plan.setup_type,
            grade=plan.grade,
            ts_utc=now,
            minute_bucket=_minute_bucket(now),
            status=status,
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
            last_price=snapshot.get("last_traded") if snapshot else None,
            bid=snapshot.get("bid") if snapshot else None,
            ask=snapshot.get("ask") if snapshot else None,
            fill_price=state.fill_price,
            fill_ts_utc=state.fill_ts_utc,
            current_stake_gbp_per_pt=state.stake_gbp_per_pt,
            current_stop_price=state.current_stop_price,
            unrealised_pnl_gbp=outcome.unrealised_pnl_gbp,
            peak_unrealised_pnl_gbp=outcome.peak_pnl_gbp,
            current_locked_profit_gbp=locked,
            trail_step_count=state.trail_step_count,
            trail_mode_active=state.trail_mode_activated,
            invalidation_window_active=_invalidation_window_active(
                state, now, self.exit_config.invalidation_window_minutes
            ),
            elapsed_mins_in_position=mins_since_fill,
        )
        self.writer.write_snapshot(snap)

    # ------------------------------------------------------------------
    # Event emitters (pre-trigger)
    # ------------------------------------------------------------------

    def _emit_trigger_armed(self, plan: CandidatePlan, now: datetime) -> None:
        event = CandidateEvent(
            id=str(uuid4()),
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            ts_utc=now,
            event_type=EventType.TRIGGER_ARMED,
            actor=ActorKind.GATE_ENGINE,
            payload=TriggerArmedPayload(
                trigger_low=plan.trigger_low,
                trigger_high=plan.trigger_high,
                stop_price=plan.stop_price,
                direction=plan.direction,
            ),
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
        )
        self.writer.write_event(event)
        logger.info(
            "TRIGGER_ARMED: %s %s (trigger=%.4f/%.4f)",
            plan.symbol,
            plan.direction.value,
            plan.trigger_low,
            plan.trigger_high,
        )

    def _emit_trigger_fired(
        self, plan: CandidatePlan, snapshot: dict, now: datetime
    ) -> None:
        last = (snapshot or {}).get("last_traded") or 0.0
        event = CandidateEvent(
            id=str(uuid4()),
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            ts_utc=now,
            event_type=EventType.TRIGGER_FIRED,
            actor=ActorKind.GATE_ENGINE,
            payload=TriggerFiredPayload(
                last_price=float(last),
                trigger_low=plan.trigger_low,
                trigger_high=plan.trigger_high,
            ),
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
        )
        self.writer.write_event(event)
        logger.info(
            "TRIGGER_FIRED: %s %s @ %.4f",
            plan.symbol,
            plan.direction.value,
            last,
        )

    def _emit_entry_evaluated_no_enter(
        self, plan: CandidatePlan, snapshot: dict, now: datetime, code: str
    ) -> None:
        last = (snapshot or {}).get("last_traded")
        event = CandidateEvent(
            id=str(uuid4()),
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            ts_utc=now,
            event_type=EventType.ENTRY_EVALUATED_NO_ENTER,
            actor=ActorKind.GATE_ENGINE,
            reason_code=code,
            payload=EntryEvaluatedNoEnterPayload(
                rejection_code=code,
                last_price=last,
            ),
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
        )
        self.writer.write_event(event)

    # ------------------------------------------------------------------
    # Event emitters (fill path)
    # ------------------------------------------------------------------

    def _emit_order_placed(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        result,
        now: datetime,
    ) -> None:
        event = CandidateEvent(
            id=str(uuid4()),
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            ts_utc=now,
            event_type=EventType.ORDER_PLACED,
            actor=ActorKind.EXECUTOR,
            payload=OrderPlacedPayload(
                ig_deal_reference=result.deal_reference or "",
                order_type="MARKET",
                stake_gbp_per_pt=plan.planned_stake_gbp_per_pt,
                stop_price=plan.stop_price,
            ),
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
        )
        self.writer.write_event(event)

    def _emit_filled(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        initial_risk_gbp: float,
        now: datetime,
    ) -> None:
        event = CandidateEvent(
            id=str(uuid4()),
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            ts_utc=now,
            event_type=EventType.FILLED,
            actor=ActorKind.EXECUTOR,
            payload=FilledPayload(
                ig_deal_id=state.deal_id or "",
                fill_price=state.fill_price or 0.0,
                fill_ts_utc=now,
                stake_gbp_per_pt=state.stake_gbp_per_pt or 0.0,
                initial_stop_price=state.initial_stop_price or 0.0,
                initial_risk_gbp=initial_risk_gbp,
            ),
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
        )
        self.writer.write_event(event)

    # ------------------------------------------------------------------
    # Event emitters (trail/exit)
    # ------------------------------------------------------------------

    def _emit_trail_mode_activated(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        outcome,
        now: datetime,
    ) -> None:
        event = CandidateEvent(
            id=str(uuid4()),
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            ts_utc=now,
            event_type=EventType.TRAIL_MODE_ACTIVATED,
            actor=ActorKind.TRAIL_MANAGER,
            payload=TrailModeActivatedPayload(
                peak_pnl_gbp=outcome.peak_pnl_gbp or 0.0,
                trail_activation_gbp=self.exit_config.trail_activation_gbp,
                initial_locked_gbp=outcome.new_locked_gbp or 0.0,
            ),
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
        )
        self.writer.write_event(event)

    def _emit_stop_moved(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        outcome,
        now: datetime,
    ) -> None:
        from .trail_manager import ExitReason

        reason = (
            StopMoveReason.TRAIL_ARM
            if outcome.reason == ExitReason.TRAIL_ARM
            else StopMoveReason.TRAIL_STEP
        )
        event = CandidateEvent(
            id=str(uuid4()),
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            ts_utc=now,
            event_type=EventType.STOP_MOVED,
            actor=ActorKind.TRAIL_MANAGER,
            reason_code=reason.value,
            payload=StopMovedPayload(
                reason=reason,
                old_stop=state.current_stop_price or 0.0,
                new_stop=outcome.new_stop_price or 0.0,
                old_trail_step_count=outcome.old_trail_step_count or 0,
                new_trail_step_count=outcome.new_trail_step_count or 0,
                old_locked_gbp=outcome.old_locked_gbp or 0.0,
                new_locked_gbp=outcome.new_locked_gbp or 0.0,
                peak_pnl_gbp_at_move=outcome.peak_pnl_gbp or 0.0,
            ),
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
        )
        self.writer.write_event(event)

    def _emit_terminal(
        self,
        plan: CandidatePlan,
        state: CandidateRuntimeState,
        outcome,
        event_type: EventType,
        terminal_reason: TerminalReason,
        realised_pnl_gbp: float,
        now: datetime,
    ) -> None:
        """Dispatch to the correct terminal-event payload shape."""
        from .trail_manager import ExitReason, compute_locked_gbp

        last_price = outcome.last_price or 0.0
        peak = outcome.peak_pnl_gbp or 0.0

        if outcome.reason == ExitReason.INVALIDATION:
            mins_since_fill = (
                int((now - state.fill_ts_utc).total_seconds() / 60.0)
                if state.fill_ts_utc
                else 0
            )
            payload = InvalidationExitPayload(
                last_price=last_price,
                mins_since_fill=mins_since_fill,
                fill_price=state.fill_price or 0.0,
            )
        elif outcome.reason == ExitReason.INITIAL_STOP:
            payload = StopHitPayload(
                stop_price=state.current_stop_price or 0.0,
                fill_price=state.fill_price or 0.0,
                realised_pnl_gbp=realised_pnl_gbp,
            )
        elif outcome.reason == ExitReason.HARD_TARGET:
            payload = TargetHitPayload(
                reason=TargetHitReason.HARD_TARGET_GBP,
                target_price=None,
                peak_pnl_gbp=peak,
                realised_pnl_gbp=realised_pnl_gbp,
            )
        elif outcome.reason == ExitReason.TRAIL_EXIT:
            # evaluate_exit only populates old/new_locked_gbp on MOVE_STOP
            # branches; on the TRAIL_EXIT branch they're None. Derive locked
            # £ from the authoritative step count so journals see a real
            # value instead of silently zeroing out the trail's gains.
            payload = TrailExitPayload(
                trail_stop_price=state.current_stop_price or 0.0,
                fill_price=last_price,
                locked_gbp=compute_locked_gbp(
                    state.trail_step_count or 0, self.exit_config
                ),
                trail_step_count=state.trail_step_count or 0,
                realised_pnl_gbp=realised_pnl_gbp,
            )
        elif outcome.reason == ExitReason.TIMESTOP:
            payload = TimestopHitPayload(
                last_price=last_price,
                sessions_held=state.sessions_held,
                realised_pnl_gbp=realised_pnl_gbp,
            )
        else:
            raise ValueError(f"Unhandled exit reason: {outcome.reason}")

        event = CandidateEvent(
            id=str(uuid4()),
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            ts_utc=now,
            event_type=event_type,
            actor=ActorKind.TRAIL_MANAGER,
            reason_code=(outcome.reason.value if outcome.reason else None),
            payload=payload,
            terminal_reason=terminal_reason,
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
        )
        self.writer.write_event(event)

    # ------------------------------------------------------------------
    # Session end
    # ------------------------------------------------------------------

    def _emit_session_end_events(self) -> None:
        """On clean shutdown, emit SESSION_ENDED_NO_TRIGGER for any candidate
        that never fired. Open positions are intentionally left live in IG —
        the next session_init run resumes management of them.
        """
        now = self.now_fn()
        events: list[CandidateEvent] = []
        for plan in self.plans:
            state = self._runtime.get(plan.candidate_id)
            if state is None or state.fired or state.terminal:
                continue
            events.append(
                CandidateEvent(
                    id=str(uuid4()),
                    session_id=plan.session_id,
                    candidate_id=plan.candidate_id,
                    ts_utc=now,
                    event_type=EventType.SESSION_ENDED_NO_TRIGGER,
                    actor=ActorKind.GATE_ENGINE,
                    payload=SessionEndedNoTriggerPayload(),
                    terminal_reason=TerminalReason.SESSION_ENDED_NO_TRIGGER,
                    broker_mode=plan.broker_mode,
                    rule_set_version=plan.rule_set_version,
                )
            )
            state.terminal = True
            state.terminal_reason = TerminalReason.SESSION_ENDED_NO_TRIGGER
        if events:
            self.writer.write_events(events)
            logger.info(
                "Emitted SESSION_ENDED_NO_TRIGGER x %d on loop shutdown.", len(events)
            )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _minute_bucket(dt: datetime) -> int:
    """YYYYMMDDHHMM integer for GROUP BY queries."""
    return int(dt.strftime("%Y%m%d%H%M"))


def _invalidation_window_active(
    state: CandidateRuntimeState, now: datetime, window_minutes: int
) -> bool:
    if state.fill_ts_utc is None:
        return False
    return (now - state.fill_ts_utc).total_seconds() / 60.0 < window_minutes


def _exit_reason_to_types(reason) -> tuple[EventType, TerminalReason]:
    """Map trail_manager.ExitReason to (EventType, TerminalReason).

    Separated from the event emitter so the mapping is discoverable in one
    place — adding a new exit reason is a single-file change here, not a
    scavenger hunt across the loop.
    """
    from .trail_manager import ExitReason

    return {
        ExitReason.INVALIDATION: (
            EventType.INVALIDATION_EXIT,
            TerminalReason.INVALIDATION_EXIT,
        ),
        ExitReason.INITIAL_STOP: (EventType.STOP_HIT, TerminalReason.STOPPED_OUT),
        ExitReason.HARD_TARGET: (
            EventType.TARGET_HIT,
            TerminalReason.HARD_TARGET_HIT,
        ),
        ExitReason.TRAIL_EXIT: (EventType.TRAIL_EXIT, TerminalReason.TRAIL_EXIT),
        ExitReason.TIMESTOP: (EventType.TIMESTOP_HIT, TerminalReason.TIMESTOP_HIT),
    }[reason]
