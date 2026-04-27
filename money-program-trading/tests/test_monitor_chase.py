"""
Unit tests for Rule 4-Chase — the "never chase" gate in classify_tick.

Source-of-truth: ``docs/specs/CANONICAL_ENTRY_RULES.md`` §Rule 4-Chase
plus the desk reference ``Entry_Rules_Desk_Reference.html``:

  > If open > pivot + 3% without gap classification → SKIP, wait for
  > pullback. Never chase.

Implementation:

* On the first observed tick (the ``session_open_price``-setting branch
  in ``classify_tick``), the runtime flag ``chase_violation`` is set to
  True if the LONG open is more than 3% above ``trigger_low`` (the
  pivot) — or, for SHORT, more than 3% below ``trigger_high``.
* On any subsequent tick where price would otherwise be eligible to
  fire (distance ≥ 0), the LONG / SHORT branches return
  REJECT(R23) before any other gate evaluates. Chase rule is
  terminal for the day — no recovery, no late entry.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

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
# Fixtures
# ---------------------------------------------------------------------------


def _long_plan(trigger_low: float = 100.0, trigger_high: float = 101.0) -> CandidatePlan:
    return CandidatePlan(
        candidate_id=str(uuid.uuid4()),
        scan_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        symbol="TST",
        market=Market.US,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.B,
        trigger_low=trigger_low,
        trigger_high=trigger_high,
        stop_price=trigger_low * 0.97,
        target_price=trigger_high * 1.05,
        ig_epic="IX.D.TST.DAILY.IP",
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=1.0,
        planned_risk_gbp=50.0,
    )


def _short_plan(trigger_low: float = 100.0, trigger_high: float = 101.0) -> CandidatePlan:
    return CandidatePlan(
        candidate_id=str(uuid.uuid4()),
        scan_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        symbol="TST",
        market=Market.US,
        direction=Direction.SHORT,
        setup_type=EntryType.S_A,
        grade=CandidateGrade.B,
        trigger_low=trigger_low,
        trigger_high=trigger_high,
        stop_price=trigger_high * 1.03,
        target_price=trigger_low * 0.95,
        ig_epic="IX.D.TST.DAILY.IP",
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=1.0,
        planned_risk_gbp=50.0,
    )


def _snap(last: float, status: str = "TRADEABLE") -> dict:
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
# LONG — chase rule fires when first tick opens > trigger_low * 1.03
# ---------------------------------------------------------------------------


def test_long_open_within_zone_no_chase_violation():
    """Open between trigger_low and trigger_low+3% is NOT a chase —
    Rule 9A handles it as a legitimate gap-up. chase_violation stays False."""
    plan = _long_plan(trigger_low=100.0, trigger_high=101.0)
    state = CandidateRuntimeState()
    t0 = utc_now()
    out = classify_tick(plan, _snap(last=100.50), state, now=t0)
    assert state.chase_violation is False
    # On a gap-up, R20 fires (BGU OR window). Not R23.
    assert out.rejection_code != "R23"


def test_long_open_above_pivot_plus_3pct_sets_chase_violation():
    """LONG, trigger_low=100, open=104 (4% above pivot) → chase_violation=True
    on the first tick, and the same tick returns REJECT(R23)."""
    plan = _long_plan(trigger_low=100.0, trigger_high=101.0)
    state = CandidateRuntimeState()
    t0 = utc_now()
    out = classify_tick(plan, _snap(last=104.0), state, now=t0)
    assert state.chase_violation is True
    assert out.decision == Decision.REJECT
    assert out.rejection_code == "R23"


def test_long_chase_violation_persists_across_ticks():
    """Once set, chase_violation is sticky for the day. Even if price
    pulls back into the zone later, classify_tick keeps rejecting R23."""
    plan = _long_plan(trigger_low=100.0, trigger_high=101.0)
    state = CandidateRuntimeState()
    t0 = utc_now()

    # First tick: open at 105 (5% above pivot) — chase fires.
    classify_tick(plan, _snap(last=105.0), state, now=t0)
    assert state.chase_violation is True

    # Later tick: price pulls back to 100.50 (inside zone). Still R23.
    out = classify_tick(plan, _snap(last=100.50), state, now=t0 + timedelta(minutes=30))
    assert out.decision == Decision.REJECT
    assert out.rejection_code == "R23"

    # Even later: price breaks above trigger_high. Still R23 — chase wins.
    out = classify_tick(plan, _snap(last=101.50), state, now=t0 + timedelta(minutes=60))
    assert out.decision == Decision.REJECT
    assert out.rejection_code == "R23"


def test_long_chase_does_not_fire_when_open_below_zone():
    """LONG opens below trigger_low — no chase, no gap-up. Normal Rule 22
    breakout path applies."""
    plan = _long_plan(trigger_low=100.0, trigger_high=101.0)
    state = CandidateRuntimeState()
    t0 = utc_now()
    out = classify_tick(plan, _snap(last=98.0), state, now=t0)
    assert state.chase_violation is False
    assert out.decision == Decision.HOLD


def test_long_chase_threshold_is_strict_greater_than():
    """Open exactly at pivot+3% (100 × 1.03 = 103.0) is NOT chase.
    Strict ``>`` semantics — at-threshold tick is allowed to enter."""
    plan = _long_plan(trigger_low=100.0, trigger_high=101.0)
    state = CandidateRuntimeState()
    t0 = utc_now()
    classify_tick(plan, _snap(last=103.0), state, now=t0)
    assert state.chase_violation is False


# ---------------------------------------------------------------------------
# SHORT — symmetric mirror
# ---------------------------------------------------------------------------


def test_short_open_below_pivot_minus_3pct_sets_chase_violation():
    """SHORT, trigger_high=101, open=97 (~4% below pivot) → chase_violation=True."""
    plan = _short_plan(trigger_low=100.0, trigger_high=101.0)
    state = CandidateRuntimeState()
    t0 = utc_now()
    out = classify_tick(plan, _snap(last=97.0), state, now=t0)
    assert state.chase_violation is True
    assert out.decision == Decision.REJECT
    assert out.rejection_code == "R23"


def test_short_chase_does_not_fire_when_open_above_zone():
    """SHORT opens above trigger_high — no chase. Setup unfolds normally."""
    plan = _short_plan(trigger_low=100.0, trigger_high=101.0)
    state = CandidateRuntimeState()
    t0 = utc_now()
    out = classify_tick(plan, _snap(last=102.5), state, now=t0)
    assert state.chase_violation is False
    assert out.decision == Decision.HOLD


def test_short_chase_threshold_is_strict_less_than():
    """SHORT open at exactly pivot-3% (101 × 0.97 = 97.97) is NOT chase."""
    plan = _short_plan(trigger_low=100.0, trigger_high=101.0)
    state = CandidateRuntimeState()
    t0 = utc_now()
    classify_tick(plan, _snap(last=97.97), state, now=t0)
    assert state.chase_violation is False
