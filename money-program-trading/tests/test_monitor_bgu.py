"""
Unit tests for Masterclass Rule 9 — Buyable Gap Up (BGU) Protocol — as
wired into ``monitor.classify_tick``.

Source-of-truth spec: ``docs/specs/RULE_9_BGU_SPEC.md``.
Masterclass citation: Entry_Refinement_Masterclass_v1.docx §3.3 Rule 9.

Scope (this PR): LONG-side Rule 9A — the 15-minute opening-range wait
plus break-above-OR-high trigger. Rule 9B (VWAP pullback) and late-stage
gap rejection are deferred.

Behaviour under test:

1. **Non-gap day.** A LONG candidate whose first tick is below
   ``trigger_low`` takes the existing pre-BGU path. No BGU state set;
   the zone-crossing eventually fires normally.
2. **Gap-up inside the 15-min window.** First tick at/above
   ``trigger_low`` flips ``gap_up_detected=True`` and tracks the opening
   range. Any subsequent tick that would have fired under the old rule
   returns ``REJECT(R20)`` — 'opening range not yet formed'.
3. **Gap-up after the window, no break.** When 15 minutes elapse and
   price is still at or below the tracked ``opening_range_high``,
   classify_tick returns ``REJECT(R21)`` — 'price has not broken OR
   high'.
4. **Gap-up after the window, break confirmed.** A tick strictly above
   the frozen ``opening_range_high`` returns ``FIRE`` (subject to the
   existing session-cutoff check).
5. **Session-cutoff precedence.** If the OR break happens AFTER the
   entries-cutoff time, the R_SESSION_CUTOFF rejection wins — we never
   open a position destined for immediate HARD_CLOSE.
6. **TMUS 2026-04-24 regression fixture.** Scan said LONG, trigger
   zone 193.00–196.00. Tape opened inside the zone, spiked briefly to
   $193.90, then declined steadily. Rule 9A must return REJECT across
   that whole sequence — no FIRE.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from src.engine.monitor import (
    CandidatePlan,
    CandidateRuntimeState,
    Decision,
    classify_tick,
)
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import BrokerMode, CandidateGrade
from src.utils.time_utils import utc_now

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Plan + snapshot fixtures
# ---------------------------------------------------------------------------


def _long_plan(trigger_low: float = 193.0, trigger_high: float = 196.0) -> CandidatePlan:
    """A LONG plan with the same shape as today's TMUS scan signal."""
    return CandidatePlan(
        candidate_id=str(uuid.uuid4()),
        scan_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        symbol="TMUS",
        market=Market.US,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.B,
        trigger_low=trigger_low,
        trigger_high=trigger_high,
        stop_price=trigger_low * 0.95,
        target_price=trigger_high * 1.05,
        ig_epic="IX.D.TMUS.DAILY.IP",
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=1.0,
        planned_risk_gbp=50.0,
    )


def _snap(last: float, status: str = "TRADEABLE") -> dict[str, Any]:
    """Mirror the shape of ``MarketData.get_market_snapshot`` output."""
    return {
        "bid": last,
        "ask": last,
        "last_traded": last,
        "market_status": status,
        "high": None,
        "low": None,
        "net_change": None,
        "pct_change": None,
        "update_time_utc": None,
        "scaling_factor": 1.0,
    }


# ---------------------------------------------------------------------------
# 1. Non-gap day: existing behaviour unchanged
# ---------------------------------------------------------------------------


def test_non_gap_day_gates_inside_zone_with_r22_then_fires_on_breakout():
    """A LONG candidate whose FIRST observed tick is below trigger_low is
    not a gap-up — ``gap_up_detected`` stays False.

    Under the Rule-22 breakout gate (feedback_trigger_semantics), a tick
    INSIDE the zone is still not an entry — we require a strict break
    above trigger_high. So a mid-zone tick must REJECT(R22), and only
    a tick strictly above trigger_high may FIRE."""
    plan = _long_plan()
    state = CandidateRuntimeState()
    t0 = utc_now()

    # First tick well below trigger zone — not a gap-up.
    out = classify_tick(plan, _snap(last=190.0), state, now=t0)
    assert out.decision in (Decision.HOLD, Decision.ARM)
    assert state.gap_up_detected is False

    # Later, price rallies INTO the trigger zone [193, 196]. Before this
    # commit this would have fired. Now: R22 — awaiting breakout above
    # trigger_high.
    out = classify_tick(plan, _snap(last=193.5), state, now=t0 + timedelta(minutes=5))
    assert out.decision == Decision.REJECT
    assert out.rejection_code == "R22"

    # Price strictly breaks above trigger_high=196.0 → FIRE.
    out = classify_tick(plan, _snap(last=196.25), state, now=t0 + timedelta(minutes=8))
    assert out.decision == Decision.FIRE


# ---------------------------------------------------------------------------
# 2. Gap-up inside the 15-min window: R20
# ---------------------------------------------------------------------------


def test_gap_up_first_tick_flags_detection_and_rejects_r20():
    """First-tick-in-zone flips gap_up_detected and seeds the OR. The
    same tick returns REJECT(R20) rather than the old FIRE."""
    plan = _long_plan()
    state = CandidateRuntimeState()
    t0 = utc_now()

    out = classify_tick(plan, _snap(last=193.3), state, now=t0)

    assert state.gap_up_detected is True
    assert state.session_open_price == pytest.approx(193.3)
    assert state.opening_range_high == pytest.approx(193.3)
    assert state.opening_range_low == pytest.approx(193.3)
    assert out.decision == Decision.REJECT
    assert out.rejection_code == "R20"


def test_gap_up_ticks_within_window_continue_to_reject_r20():
    """Subsequent ticks inside the 15-min window keep returning R20 and
    update the OR high/low as the range develops."""
    plan = _long_plan()
    state = CandidateRuntimeState()
    t0 = utc_now()

    classify_tick(plan, _snap(last=193.3), state, now=t0)
    out = classify_tick(plan, _snap(last=193.9), state, now=t0 + timedelta(minutes=3))

    assert out.decision == Decision.REJECT
    assert out.rejection_code == "R20"
    assert state.opening_range_high == pytest.approx(193.9)
    assert state.opening_range_low == pytest.approx(193.3)


# ---------------------------------------------------------------------------
# 3. Gap-up after the window, no break: R21
# ---------------------------------------------------------------------------


def test_gap_up_after_window_no_break_rejects_r21():
    """15 min elapsed; price still at or below the frozen OR high.
    Return REJECT(R21) — waiting for a continuation break."""
    plan = _long_plan()
    state = CandidateRuntimeState()
    t0 = utc_now()

    # Seed the OR at $193.9 over the first 15 minutes.
    classify_tick(plan, _snap(last=193.3), state, now=t0)
    classify_tick(plan, _snap(last=193.9), state, now=t0 + timedelta(minutes=5))
    classify_tick(plan, _snap(last=193.5), state, now=t0 + timedelta(minutes=14))

    # Window closed. Price still inside the zone but below OR high.
    out = classify_tick(
        plan, _snap(last=193.7), state, now=t0 + timedelta(minutes=16),
    )

    assert out.decision == Decision.REJECT
    assert out.rejection_code == "R21"


def test_opening_range_frozen_after_window():
    """After 15 min, new highs DO NOT extend the OR. The OR is the
    first-15-min range only (Masterclass §3.3)."""
    plan = _long_plan()
    state = CandidateRuntimeState()
    t0 = utc_now()

    classify_tick(plan, _snap(last=193.3), state, now=t0)
    classify_tick(plan, _snap(last=193.9), state, now=t0 + timedelta(minutes=10))
    or_high_at_window_close = state.opening_range_high
    assert or_high_at_window_close == pytest.approx(193.9)

    # After the window a tick at 194.5 would break OR — assert the
    # stored OR high did NOT move up to 194.5.
    classify_tick(plan, _snap(last=194.5), state, now=t0 + timedelta(minutes=16))
    assert state.opening_range_high == pytest.approx(193.9)


# ---------------------------------------------------------------------------
# 4. Gap-up after the window, break confirmed: FIRE
# ---------------------------------------------------------------------------


def test_gap_up_after_window_break_fires():
    """Price breaks above the frozen OR high after the 15-min window.
    classify_tick returns FIRE — Rule 9A satisfied."""
    plan = _long_plan()
    state = CandidateRuntimeState()
    t0 = utc_now()

    classify_tick(plan, _snap(last=193.3), state, now=t0)
    classify_tick(plan, _snap(last=193.9), state, now=t0 + timedelta(minutes=5))
    classify_tick(plan, _snap(last=193.5), state, now=t0 + timedelta(minutes=14))

    # 16 min after open, price breaks above the 193.9 OR high.
    out = classify_tick(
        plan, _snap(last=194.2), state, now=t0 + timedelta(minutes=16),
    )
    assert out.decision == Decision.FIRE


# ---------------------------------------------------------------------------
# 5. Session-cutoff precedence
# ---------------------------------------------------------------------------


class _StubClockPastCutoff:
    """Minimal SessionClock stand-in that says 'past entries cutoff'."""

    def is_past_entries_cutoff(self, now) -> bool:  # noqa: D401
        return True

    def must_hard_close(self, now) -> bool:  # unused here
        return False


def test_session_cutoff_wins_over_or_break():
    """If the OR break happens AFTER the entries cutoff, R_SESSION_CUTOFF
    still wins — no late entries regardless of Rule 9A satisfaction."""
    plan = _long_plan()
    state = CandidateRuntimeState()
    t0 = utc_now()

    classify_tick(plan, _snap(last=193.3), state, now=t0)
    classify_tick(plan, _snap(last=193.9), state, now=t0 + timedelta(minutes=10))

    out = classify_tick(
        plan,
        _snap(last=194.2),
        state,
        session_clock=_StubClockPastCutoff(),
        now=t0 + timedelta(minutes=20),
    )
    assert out.decision == Decision.REJECT
    assert out.rejection_code == "R_SESSION_CUTOFF"


# ---------------------------------------------------------------------------
# 6. TMUS 2026-04-24 regression fixture
# ---------------------------------------------------------------------------


def test_tmus_2026_04_24_regression_never_fires():
    """The TMUS 5-minute tape on 2026-04-24:

    - Scan pre-market: LONG, trigger 193.00–196.00, stop 185, target 205.
    - 09:30 ET: open ~$193.0 (inside zone).
    - 09:35 ET: spiked to ~$193.9 high of day.
    - 09:40–10:00 ET: declined below trigger zone.
    - 10:00 onwards: steady fall to $189 and below.

    Under the old classify_tick this fires LONG at 09:30 at the top.
    Under Rule 9A (this PR) it must never fire: inside-window ticks
    return R20, post-window ticks return R21 because price never breaks
    above the $193.9 OR high. This is the single most important
    regression test in the BGU PR.
    """
    plan = _long_plan(trigger_low=193.00, trigger_high=196.00)
    state = CandidateRuntimeState()
    t0 = utc_now()  # proxy for 09:30 ET open

    # Tick series mirroring the chart (time, last_traded)
    ticks = [
        (0, 193.0),     # open inside zone → gap-up detected
        (2, 193.5),     # OR building
        (5, 193.9),     # high-of-day spike
        (7, 193.7),
        (10, 193.2),    # drifting back
        (14, 192.8),    # dropped below zone
        (16, 192.9),    # just after window — still below OR high
        (20, 192.5),
        (30, 191.0),
        (45, 190.2),
        (60, 189.5),
        (90, 189.0),
    ]

    fire_count = 0
    r20_count = 0
    r21_count = 0
    other_count = 0
    for elapsed_min, last in ticks:
        out = classify_tick(
            plan, _snap(last=last), state, now=t0 + timedelta(minutes=elapsed_min),
        )
        if out.decision == Decision.FIRE:
            fire_count += 1
        elif out.decision == Decision.REJECT and out.rejection_code == "R20":
            r20_count += 1
        elif out.decision == Decision.REJECT and out.rejection_code == "R21":
            r21_count += 1
        else:
            other_count += 1

    # The one rule that absolutely must hold for this to have been a useful PR:
    assert fire_count == 0, (
        "BGU Rule 9A MUST prevent all FIRE decisions across the TMUS tape. "
        f"Got {fire_count} FIRE events. Check classify_tick BGU gate."
    )
    # Additionally: we should see at least one R20 (inside window) and
    # either R21s or neutral HOLD/ARM outcomes later.
    assert r20_count >= 1
