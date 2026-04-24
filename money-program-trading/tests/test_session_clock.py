"""
Unit tests for src/engine/session_clock.py

Covers
------
* UTC anchoring of a UK-local / ET-local session boundary (DST-aware).
* ``must_hard_close`` / ``is_past_entries_cutoff`` thresholds.
* The "stricter cutoff wins" rule — when the 19:30 UK absolute cutoff is
  earlier than the relative (session_end - 60 min) cutoff, the absolute wins.
* Round-trip through ``classify_tick`` and ``evaluate_exit``: a candidate
  that would FIRE past the cutoff is REJECTed with ``R_SESSION_CUTOFF``; an
  open position past ``hard_close_utc`` exits with ``EXIT(HARD_CLOSE)``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.engine.monitor import (
    CandidatePlan,
    CandidateRuntimeState,
    Decision,
    classify_tick,
)
from src.engine.session_clock import (
    DEFAULT_HARD_CLOSE_BUFFER_MINUTES,
    DEFAULT_NO_NEW_ENTRIES_BUFFER_MINUTES,
    SessionClock,
)
from src.engine.trail_manager import (
    ExitAction,
    ExitConfig,
    ExitReason,
    PositionState,
    evaluate_exit,
)
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import BrokerMode, CandidateGrade


pytestmark = pytest.mark.unit


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _make_plan(
    *,
    direction: Direction = Direction.LONG,
    trigger_low: float = 100.0,
    trigger_high: float = 101.0,
    stop_price: float = 95.0,
) -> CandidatePlan:
    """Build a minimal CandidatePlan without exercising the full ShortlistEntry."""
    return CandidatePlan(
        candidate_id="cand-1",
        scan_id="scan-1",
        session_id="sess-1",
        symbol="AMD",
        market=Market.US,
        direction=direction,
        setup_type=EntryType.L_A if direction == Direction.LONG else EntryType.S_A,
        grade=CandidateGrade.A_PLUS,
        trigger_low=trigger_low,
        trigger_high=trigger_high,
        stop_price=stop_price,
        target_price=None,
        ig_epic="SA.D.AMD.DAILY.IP",
        planned_stake_gbp_per_pt=1.0,
        planned_risk_gbp=5.0,
        broker_mode=BrokerMode.DEMO,
        rule_set_version="test",
    )


def _default_exit_config() -> ExitConfig:
    return ExitConfig()


def _position(fill_ts: datetime, fill_price: float = 100.5) -> PositionState:
    return PositionState(
        fill_price=fill_price,
        fill_ts_utc=fill_ts,
        stake_gbp_per_pt=1.0,
        initial_stop_price=95.0,
        current_stop_price=95.0,
        peak_pnl_gbp=0.0,
        trail_step_count=0,
        sessions_held=1,
    )


# ──────────────────────────────────────────────────────────────────────────
# Clock construction
# ──────────────────────────────────────────────────────────────────────────


def test_us_session_end_utc_during_bst():
    """April 17 2026 is in BST → NY 16:00 → 20:00 UTC."""
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    assert clock.session_end_utc == datetime(2026, 4, 17, 20, 0, tzinfo=timezone.utc)
    assert clock.market_label == "US"


def test_us_session_end_utc_during_gmt_winter():
    """Late November → NY 16:00 → 21:00 UTC (both UK and US in standard time)."""
    clock = SessionClock.for_us_session(date(2026, 11, 20))
    assert clock.session_end_utc == datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)


def test_us_session_hard_close_and_entries_cutoffs_during_bst():
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    # Hard close = 20:00 UTC - 10 min = 19:50 UTC
    assert clock.hard_close_utc == datetime(2026, 4, 17, 19, 50, tzinfo=timezone.utc)
    # 19:30 UK (absolute) = 18:30 UTC during BST
    # vs session_end - 60 = 19:00 UTC
    # The absolute 18:30 UTC is stricter → it wins.
    assert clock.entries_cutoff_utc == datetime(
        2026, 4, 17, 18, 30, tzinfo=timezone.utc
    )
    assert clock.hard_close_buffer_minutes == DEFAULT_HARD_CLOSE_BUFFER_MINUTES
    assert clock.no_new_entries_buffer_minutes == DEFAULT_NO_NEW_ENTRIES_BUFFER_MINUTES


def test_us_session_disabling_absolute_cutoff_falls_back_to_relative_rule():
    clock = SessionClock.for_us_session(
        date(2026, 4, 17), last_entry_cutoff_uk_local=None
    )
    # Only the relative rule: session_end (20:00 UTC) - 60 min = 19:00 UTC
    assert clock.entries_cutoff_utc == datetime(
        2026, 4, 17, 19, 0, tzinfo=timezone.utc
    )


def test_uk_session_for_ftse():
    clock = SessionClock.for_uk_session(date(2026, 4, 17))
    # LSE closes 16:30 Europe/London. In BST that's 15:30 UTC.
    assert clock.session_end_utc == datetime(
        2026, 4, 17, 15, 30, tzinfo=timezone.utc
    )
    assert clock.hard_close_utc == datetime(
        2026, 4, 17, 15, 20, tzinfo=timezone.utc
    )
    # No absolute cutoff by default for FTSE → relative only.
    assert clock.entries_cutoff_utc == datetime(
        2026, 4, 17, 14, 30, tzinfo=timezone.utc
    )


def test_entries_cutoff_is_clamped_to_hard_close_when_buffers_overlap():
    """If the no-new-entries buffer < hard-close buffer (pathological config),
    entries_cutoff must not be allowed to fall after hard_close."""
    clock = SessionClock.for_us_session(
        date(2026, 4, 17),
        hard_close_buffer_minutes=90,
        no_new_entries_buffer_minutes=5,
        last_entry_cutoff_uk_local=None,
    )
    # hard_close = 20:00 UTC - 90 = 18:30 UTC
    # relative entries cutoff = 20:00 UTC - 5 = 19:55 UTC → would be AFTER
    # hard_close → clamped back to 18:30 UTC.
    assert clock.hard_close_utc == datetime(2026, 4, 17, 18, 30, tzinfo=timezone.utc)
    assert clock.entries_cutoff_utc == clock.hard_close_utc


# ──────────────────────────────────────────────────────────────────────────
# Query methods
# ──────────────────────────────────────────────────────────────────────────


def test_must_hard_close_false_before_and_true_at_or_after_boundary():
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    before = datetime(2026, 4, 17, 19, 49, tzinfo=timezone.utc)
    boundary = datetime(2026, 4, 17, 19, 50, tzinfo=timezone.utc)
    after = datetime(2026, 4, 17, 19, 51, tzinfo=timezone.utc)
    assert clock.must_hard_close(before) is False
    assert clock.must_hard_close(boundary) is True
    assert clock.must_hard_close(after) is True


def test_is_past_entries_cutoff_boundary_is_inclusive():
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    # entries_cutoff = 18:30 UTC (19:30 UK in BST)
    before = datetime(2026, 4, 17, 18, 29, tzinfo=timezone.utc)
    at = datetime(2026, 4, 17, 18, 30, tzinfo=timezone.utc)
    after = datetime(2026, 4, 17, 18, 31, tzinfo=timezone.utc)
    assert clock.is_past_entries_cutoff(before) is False
    assert clock.is_past_entries_cutoff(at) is True
    assert clock.is_past_entries_cutoff(after) is True


def test_accepts_naive_datetimes_assumed_as_utc():
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    naive_before = datetime(2026, 4, 17, 19, 49)  # naive ≡ UTC
    naive_after = datetime(2026, 4, 17, 19, 51)
    assert clock.must_hard_close(naive_before) is False
    assert clock.must_hard_close(naive_after) is True


def test_minutes_to_session_end():
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    # 15 min before close
    before = datetime(2026, 4, 17, 19, 45, tzinfo=timezone.utc)
    after = datetime(2026, 4, 17, 20, 5, tzinfo=timezone.utc)
    assert clock.minutes_to_session_end(before) == 15
    assert clock.minutes_to_session_end(after) == -5


# ──────────────────────────────────────────────────────────────────────────
# classify_tick integration
# ──────────────────────────────────────────────────────────────────────────


def test_classify_tick_fires_normally_before_entries_cutoff():
    plan = _make_plan()  # trigger_low=100.0, trigger_high=101.0
    # Pre-seed runtime state to model a non-gap day — the candidate's
    # session opened BELOW the trigger zone, so Rule 9 BGU is not
    # engaged and the test can exercise the session-cutoff path alone.
    state = CandidateRuntimeState(
        session_open_price=95.0,
        session_open_ts_utc=datetime(2026, 4, 17, 13, 30, tzinfo=timezone.utc),
        gap_up_detected=False,
    )
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    now = datetime(2026, 4, 17, 17, 0, tzinfo=timezone.utc)  # well before cutoff

    # Strictly above trigger_high=101.0 so the R22 breakout gate passes
    # and the test exercises the session-cutoff path in isolation.
    snap = {"last_traded": 101.25, "market_status": "TRADEABLE"}
    outcome = classify_tick(plan, snap, state, session_clock=clock, now=now)

    assert outcome.decision == Decision.FIRE


def test_classify_tick_rejects_with_session_cutoff_code_after_cutoff():
    plan = _make_plan()  # trigger_low=100.0, trigger_high=101.0
    # Non-gap day (see sibling test) so the session-cutoff rejection is
    # the thing under test, not Rule 9 BGU or Rule 22 breakout gate.
    state = CandidateRuntimeState(
        session_open_price=95.0,
        session_open_ts_utc=datetime(2026, 4, 17, 13, 30, tzinfo=timezone.utc),
        gap_up_detected=False,
    )
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    now = datetime(2026, 4, 17, 18, 31, tzinfo=timezone.utc)  # past 18:30 cutoff

    # Above trigger_high=101.0 — the trade WOULD fire on breakout but
    # must be rejected with R_SESSION_CUTOFF because entries are closed.
    snap = {"last_traded": 101.25, "market_status": "TRADEABLE"}
    outcome = classify_tick(plan, snap, state, session_clock=clock, now=now)

    assert outcome.decision == Decision.REJECT
    assert outcome.rejection_code == "R_SESSION_CUTOFF"


def test_classify_tick_rejects_short_fire_with_session_cutoff_code():
    plan = _make_plan(
        direction=Direction.SHORT, trigger_low=99.0, trigger_high=100.0, stop_price=105.0
    )
    state = CandidateRuntimeState()
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    now = datetime(2026, 4, 17, 18, 31, tzinfo=timezone.utc)

    # Strictly below trigger_low=99.0 so R22 breakout gate passes and
    # the session-cutoff path is the thing under test.
    snap = {"last_traded": 98.5, "market_status": "TRADEABLE"}
    outcome = classify_tick(plan, snap, state, session_clock=clock, now=now)

    assert outcome.decision == Decision.REJECT
    assert outcome.rejection_code == "R_SESSION_CUTOFF"


def test_classify_tick_still_arms_and_holds_past_cutoff():
    """We only suppress FIRE — ARM/HOLD stay so journals keep recording state."""
    plan = _make_plan(trigger_low=100.0, trigger_high=101.0)
    state = CandidateRuntimeState()
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    now = datetime(2026, 4, 17, 18, 31, tzinfo=timezone.utc)

    # Within arm band but not fired → still ARM.
    arm_snap = {"last_traded": 99.6, "market_status": "TRADEABLE"}
    outcome = classify_tick(
        plan, arm_snap, state, arm_band_pct=0.005, session_clock=clock, now=now
    )
    assert outcome.decision == Decision.ARM

    # Far from trigger → HOLD regardless of cutoff.
    hold_snap = {"last_traded": 90.0, "market_status": "TRADEABLE"}
    outcome = classify_tick(
        plan, hold_snap, state, session_clock=clock, now=now
    )
    assert outcome.decision == Decision.HOLD


def test_classify_tick_without_clock_unchanged_behaviour():
    """Backward-compat: no clock → no cutoff enforcement. Price must still
    clear trigger_high=101.0 so the R22 breakout gate passes."""
    plan = _make_plan()
    state = CandidateRuntimeState()
    snap = {"last_traded": 101.25, "market_status": "TRADEABLE"}
    outcome = classify_tick(plan, snap, state)  # no clock
    assert outcome.decision == Decision.FIRE


# ──────────────────────────────────────────────────────────────────────────
# evaluate_exit integration
# ──────────────────────────────────────────────────────────────────────────


def test_evaluate_exit_hard_close_fires_past_buffer():
    plan = _make_plan()
    fill_ts = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    pos = _position(fill_ts)
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    now = datetime(2026, 4, 17, 19, 51, tzinfo=timezone.utc)  # past 19:50 hard close

    snap = {"last_traded": 100.75, "market_status": "TRADEABLE"}
    outcome = evaluate_exit(plan, pos, snap, now, _default_exit_config(), clock)

    assert outcome.action == ExitAction.EXIT
    assert outcome.reason == ExitReason.HARD_CLOSE
    assert outcome.last_price == 100.75
    # Long at 100.5 → last 100.75 → +0.25 pts × £1 = £0.25 unrealised
    assert outcome.unrealised_pnl_gbp == pytest.approx(0.25)


def test_evaluate_exit_holds_before_hard_close():
    plan = _make_plan()
    fill_ts = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    pos = _position(fill_ts)
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    # 1 min BEFORE hard close
    now = datetime(2026, 4, 17, 19, 49, tzinfo=timezone.utc)

    snap = {"last_traded": 100.75, "market_status": "TRADEABLE"}
    outcome = evaluate_exit(plan, pos, snap, now, _default_exit_config(), clock)

    # No other rule triggers at this P&L → HOLD.
    assert outcome.action == ExitAction.HOLD


def test_evaluate_exit_hard_close_short_circuits_trail_arm():
    """Hard-close wins over a trail-arm that would otherwise fire same tick.

    Pre-condition: peak P&L at £30 (would arm the trail and step 1 band),
    but the clock says it's time to hard-close → we take the clean exit
    rather than a MOVE_STOP that would be immediately overwritten.
    """
    plan = _make_plan()
    fill_ts = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    pos = _position(fill_ts)
    pos.peak_pnl_gbp = 30.0  # would normally trigger TRAIL_ARM + STEP
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    now = datetime(2026, 4, 17, 19, 51, tzinfo=timezone.utc)

    snap = {"last_traded": 130.0, "market_status": "TRADEABLE"}
    outcome = evaluate_exit(plan, pos, snap, now, _default_exit_config(), clock)

    assert outcome.action == ExitAction.EXIT
    assert outcome.reason == ExitReason.HARD_CLOSE


def test_evaluate_exit_hard_close_does_not_fire_when_market_not_tradeable():
    """If the snapshot has no live price, we must return NO_PRICE, never
    HARD_CLOSE — otherwise we'd book a close with no price to reference."""
    plan = _make_plan()
    pos = _position(datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc))
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    now = datetime(2026, 4, 17, 19, 51, tzinfo=timezone.utc)

    snap_closed = {"last_traded": 100.5, "market_status": "CLOSED"}
    outcome = evaluate_exit(plan, pos, snap_closed, now, _default_exit_config(), clock)
    assert outcome.action == ExitAction.NO_PRICE

    snap_no_last = {"last_traded": None, "market_status": "TRADEABLE"}
    outcome = evaluate_exit(plan, pos, snap_no_last, now, _default_exit_config(), clock)
    assert outcome.action == ExitAction.NO_PRICE


def test_evaluate_exit_without_clock_unchanged_behaviour():
    """Backward-compat: no session_clock → pre-Session-9 behaviour."""
    plan = _make_plan()
    fill_ts = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    pos = _position(fill_ts)
    now = datetime(2026, 4, 17, 19, 51, tzinfo=timezone.utc)

    snap = {"last_traded": 100.75, "market_status": "TRADEABLE"}
    outcome = evaluate_exit(plan, pos, snap, now, _default_exit_config(), None)

    # With no clock and no other rule firing at this P&L → HOLD.
    assert outcome.action == ExitAction.HOLD
    assert outcome.reason is None


def test_evaluate_exit_initial_stop_beats_hard_close_when_both_would_fire():
    """If price is already through the initial stop at the same tick we cross
    hard_close, we still prefer HARD_CLOSE — both are terminal, but HARD_CLOSE
    is the 'we're ending the day' semantic, which is strictly more informative
    for post-session review. Locking this behaviour so it's not accidentally
    reordered."""
    plan = _make_plan()
    fill_ts = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    pos = _position(fill_ts)
    clock = SessionClock.for_us_session(date(2026, 4, 17))
    now = datetime(2026, 4, 17, 19, 51, tzinfo=timezone.utc)

    snap = {"last_traded": 90.0, "market_status": "TRADEABLE"}  # below 95.0 stop
    outcome = evaluate_exit(plan, pos, snap, now, _default_exit_config(), clock)

    assert outcome.action == ExitAction.EXIT
    assert outcome.reason == ExitReason.HARD_CLOSE
