"""
Rule-math tests for ``classify_tick`` — the pure decision function inside
MonitorLoop.

**What these tests are and aren't:**
- They are NOT using synthetic "fake" price feeds. The function under test is
  a pure predicate — given a snapshot dict of the exact shape IG returns, plus
  a plan, does it return FIRE / ARM / HOLD / NO_PRICE correctly?
- They exercise the rule math directly. If we only tested this against live
  IG, we couldn't cover edge cases like "price exactly at trigger" or "market
  closed" without waiting for those conditions in the wild.
- The snapshot shape (keys: ``last_traded``, ``bid``, ``ask``, ``market_status``)
  matches ``MarketData.get_market_snapshot`` output 1:1. If IG changes the
  shape, ``test_market_data_live`` catches it; this file catches rule bugs.

This is the same split the existing ``test_log_models.py`` uses: unit-test the
contract, integration-test the wire.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.engine.monitor import (
    CandidatePlan,
    CandidateRuntimeState,
    Decision,
    classify_tick,
)
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import BrokerMode, CandidateGrade


def _long_plan(**overrides) -> CandidatePlan:
    defaults = dict(
        candidate_id=str(uuid4()),
        scan_id=str(uuid4()),
        session_id=str(uuid4()),
        symbol="OXY",
        market=Market.US,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.A,
        trigger_low=100.00,
        trigger_high=100.10,
        stop_price=97.50,
        target_price=107.0,
        ig_epic="KA.D.OXY.CASH.IP",
        broker_mode=BrokerMode.DEMO,
    )
    defaults.update(overrides)
    return CandidatePlan(**defaults)


def _short_plan(**overrides) -> CandidatePlan:
    defaults = dict(
        candidate_id=str(uuid4()),
        scan_id=str(uuid4()),
        session_id=str(uuid4()),
        symbol="VOD.L",
        market=Market.UK,
        direction=Direction.SHORT,
        setup_type=EntryType.S_E,
        grade=CandidateGrade.A_PLUS,
        trigger_low=75.00,
        trigger_high=75.10,
        stop_price=77.00,
        target_price=70.0,
        ig_epic="KA.D.VOD.CASH.IP",
        broker_mode=BrokerMode.DEMO,
    )
    defaults.update(overrides)
    return CandidatePlan(**defaults)


def _snap(last: float | None = None, status: str = "TRADEABLE", **extra) -> dict:
    """Snapshot dict shaped exactly like MarketData.get_market_snapshot."""
    return {
        "bid": extra.get("bid", last),
        "ask": extra.get("ask", last),
        "last_traded": last,
        "market_status": status,
        "high": None,
        "low": None,
        "net_change": None,
        "pct_change": None,
        "update_time_utc": None,
    }


# ---------------------------------------------------------------------------
# NO_PRICE conditions
# ---------------------------------------------------------------------------


def test_empty_snapshot_yields_no_price():
    plan = _long_plan()
    out = classify_tick(plan, {}, CandidateRuntimeState())
    assert out.decision == Decision.NO_PRICE


def test_market_closed_yields_no_price():
    plan = _long_plan()
    out = classify_tick(plan, _snap(last=99.90, status="CLOSED"), CandidateRuntimeState())
    assert out.decision == Decision.NO_PRICE


def test_market_suspended_yields_no_price():
    plan = _long_plan()
    out = classify_tick(plan, _snap(last=99.90, status="SUSPENDED"), CandidateRuntimeState())
    assert out.decision == Decision.NO_PRICE


def test_missing_last_traded_yields_no_price():
    plan = _long_plan()
    snap = _snap(last=None, status="TRADEABLE")
    out = classify_tick(plan, snap, CandidateRuntimeState())
    assert out.decision == Decision.NO_PRICE


def test_blank_market_status_treated_as_tradeable():
    """IG sometimes omits marketStatus — don't refuse to trade on missing field."""
    plan = _long_plan()
    out = classify_tick(plan, _snap(last=100.50, status=""), CandidateRuntimeState())
    assert out.decision == Decision.FIRE


# ---------------------------------------------------------------------------
# LONG direction
# ---------------------------------------------------------------------------


def test_long_fires_when_price_at_trigger():
    plan = _long_plan(trigger_low=100.00)
    out = classify_tick(plan, _snap(last=100.00), CandidateRuntimeState())
    assert out.decision == Decision.FIRE
    assert out.distance_pts == pytest.approx(0.0)


def test_long_fires_when_price_above_trigger():
    plan = _long_plan(trigger_low=100.00)
    out = classify_tick(plan, _snap(last=100.55), CandidateRuntimeState())
    assert out.decision == Decision.FIRE
    assert out.distance_pts == pytest.approx(0.55)


def test_long_holds_far_below_trigger():
    plan = _long_plan(trigger_low=100.00)
    # 5% below — well outside arm band (0.5%)
    out = classify_tick(plan, _snap(last=95.00), CandidateRuntimeState())
    assert out.decision == Decision.HOLD
    assert out.distance_pts == pytest.approx(-5.0)


def test_long_arms_within_half_pct_of_trigger():
    plan = _long_plan(trigger_low=100.00)
    # 0.3% below — inside 0.5% arm band
    out = classify_tick(plan, _snap(last=99.70), CandidateRuntimeState())
    assert out.decision == Decision.ARM


def test_long_arms_only_once():
    plan = _long_plan(trigger_low=100.00)
    state = CandidateRuntimeState(armed_emitted=True)
    out = classify_tick(plan, _snap(last=99.70), state)
    # Already armed — should HOLD, not re-ARM
    assert out.decision == Decision.HOLD


# ---------------------------------------------------------------------------
# SHORT direction
# ---------------------------------------------------------------------------


def test_short_fires_when_price_at_trigger():
    plan = _short_plan(trigger_high=75.10)
    out = classify_tick(plan, _snap(last=75.10), CandidateRuntimeState())
    assert out.decision == Decision.FIRE


def test_short_fires_when_price_below_trigger():
    plan = _short_plan(trigger_high=75.10)
    out = classify_tick(plan, _snap(last=74.50), CandidateRuntimeState())
    assert out.decision == Decision.FIRE
    assert out.distance_pts == pytest.approx(0.60)


def test_short_holds_far_above_trigger():
    plan = _short_plan(trigger_high=75.10)
    out = classify_tick(plan, _snap(last=80.00), CandidateRuntimeState())
    assert out.decision == Decision.HOLD


def test_short_arms_within_half_pct_of_trigger():
    plan = _short_plan(trigger_high=75.10)
    # 0.3% above — inside 0.5% arm band
    out = classify_tick(plan, _snap(last=75.33), CandidateRuntimeState())
    assert out.decision == Decision.ARM


def test_short_arm_band_scales_with_trigger_magnitude():
    """A 0.5% arm band on a £500 stock is £2.50; on a £50 stock is £0.25."""
    big = _short_plan(trigger_high=500.0)
    small = _short_plan(trigger_high=50.0)
    # £1 above trigger:
    #   big stock: 1/500 = 0.2% — inside 0.5% band → ARM
    #   small stock: 1/50 = 2% — outside band → HOLD
    out_big = classify_tick(big, _snap(last=501.0), CandidateRuntimeState())
    out_small = classify_tick(small, _snap(last=51.0), CandidateRuntimeState())
    assert out_big.decision == Decision.ARM
    assert out_small.decision == Decision.HOLD


# ---------------------------------------------------------------------------
# Arm-band override
# ---------------------------------------------------------------------------


def test_custom_arm_band_widens_arm_condition():
    plan = _long_plan(trigger_low=100.00)
    # 2% below trigger — outside default 0.5% but inside 3% custom band.
    out_default = classify_tick(plan, _snap(last=98.00), CandidateRuntimeState())
    out_wide = classify_tick(
        plan, _snap(last=98.00), CandidateRuntimeState(), arm_band_pct=0.03
    )
    assert out_default.decision == Decision.HOLD
    assert out_wide.decision == Decision.ARM
