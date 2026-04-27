"""
Unit tests for Rule X3 — UK spread filter at order-open time.

Source-of-truth: ``docs/specs/CANONICAL_ENTRY_RULES.md`` §X3 plus the
desk reference ``Entry_Rules_Desk_Reference.html``:

  > UK spread > 0.3% → Limit only, reduce 25% | UK spread > 0.5% → SKIP

The check sits in the pre-fire path (``monitor._handle_fire``) so any
genuine FIRE on a UK candidate runs through the spread gate before the
order hits IG. US tickers bypass entirely (rule N/A).

Helper under test: ``monitor._check_uk_spread_for_fire(plan, snapshot)``
returns a (action, info) tuple with ``action`` in {'fire', 'reduce',
'skip'}. The downstream ``_handle_fire`` reads ``action`` to decide
whether to skip, reduce stake, or fire normally.
"""

from __future__ import annotations

import uuid

import pytest

from src.engine.monitor import (
    CandidatePlan,
    _check_uk_spread_for_fire,
)
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import BrokerMode, CandidateGrade

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _uk_plan() -> CandidatePlan:
    return CandidatePlan(
        candidate_id=str(uuid.uuid4()),
        scan_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        symbol="SHEL.L",
        market=Market.UK,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.B,
        trigger_low=2750.0,
        trigger_high=2770.0,
        stop_price=2700.0,
        target_price=2840.0,
        ig_epic="KA.D.SHEL.CASH.IP",
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=1.0,
        planned_risk_gbp=50.0,
    )


def _us_plan() -> CandidatePlan:
    return CandidatePlan(
        candidate_id=str(uuid.uuid4()),
        scan_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        symbol="AMD",
        market=Market.US,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.B,
        trigger_low=347.0,
        trigger_high=348.5,
        stop_price=342.28,
        target_price=354.32,
        ig_epic="UA.D.AMD.DAILY.IP",
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=1.0,
        planned_risk_gbp=50.0,
    )


def _snap(*, bid: float | None, ask: float | None) -> dict:
    return {
        "bid": bid,
        "ask": ask,
        "last_traded": (bid + ask) / 2 if (bid is not None and ask is not None) else None,
        "market_status": "TRADEABLE",
        "high": None,
        "low": None,
        "net_change": None,
        "pct_change": None,
        "update_time_utc": None,
        "scaling_factor": 1.0,
    }


# ---------------------------------------------------------------------------
# US ticker — rule N/A
# ---------------------------------------------------------------------------


def test_us_ticker_always_fires_regardless_of_spread():
    """Rule X3 only applies to UK markets. A wide spread on a US ticker
    is informational, not a gate — fire normally."""
    plan = _us_plan()
    # Pathologically wide spread.
    snap = _snap(bid=300.0, ask=310.0)
    action, info = _check_uk_spread_for_fire(plan, snap)
    assert action == "fire"
    assert info is None


# ---------------------------------------------------------------------------
# UK ticker — fail-open paths
# ---------------------------------------------------------------------------


def test_uk_ticker_missing_bid_fails_open():
    """No bid → can't compute spread → don't block. Better to fire on a
    UK candidate with imperfect data than to miss the entry."""
    plan = _uk_plan()
    snap = _snap(bid=None, ask=2762.0)
    action, info = _check_uk_spread_for_fire(plan, snap)
    assert action == "fire"
    assert info is None


def test_uk_ticker_missing_ask_fails_open():
    plan = _uk_plan()
    snap = _snap(bid=2761.0, ask=None)
    action, info = _check_uk_spread_for_fire(plan, snap)
    assert action == "fire"
    assert info is None


def test_uk_ticker_zero_bid_fails_open():
    """Defensive — bid=0 would cause divide-by-zero."""
    plan = _uk_plan()
    snap = _snap(bid=0.0, ask=2762.0)
    action, info = _check_uk_spread_for_fire(plan, snap)
    assert action == "fire"
    assert info is None


# ---------------------------------------------------------------------------
# UK ticker — fire / reduce / skip thresholds
# ---------------------------------------------------------------------------


def test_uk_ticker_tight_spread_fires_normally():
    """Spread well under 0.3% → fire with full stake."""
    plan = _uk_plan()
    # bid 2761, ask 2763 → spread = 2/2761 = 0.072% — well under 0.3%
    snap = _snap(bid=2761.0, ask=2763.0)
    action, info = _check_uk_spread_for_fire(plan, snap)
    assert action == "fire"
    assert info is None


def test_uk_ticker_spread_in_reduce_band_returns_reduce():
    """Spread in (0.3%, 0.5%] → reduce stake. Default reduction = 0.75
    per Settings.uk_spread_reduction."""
    plan = _uk_plan()
    # bid 2750, ask 2761 → spread = 11/2750 = 0.4% — between 0.3% and 0.5%
    snap = _snap(bid=2750.0, ask=2761.0)
    action, info = _check_uk_spread_for_fire(plan, snap)
    assert action == "reduce"
    assert info is not None
    assert info["spread_pct"] == pytest.approx(11 / 2750, rel=1e-6)
    assert info["threshold"] == pytest.approx(0.003)
    assert info["reduction"] == pytest.approx(0.75)


def test_uk_ticker_spread_above_skip_threshold_returns_skip():
    """Spread > 0.5% → SKIP. No order, no reduced order, nothing."""
    plan = _uk_plan()
    # bid 2700, ask 2720 → spread = 20/2700 = 0.74% — well above 0.5%
    snap = _snap(bid=2700.0, ask=2720.0)
    action, info = _check_uk_spread_for_fire(plan, snap)
    assert action == "skip"
    assert info is not None
    assert info["spread_pct"] == pytest.approx(20 / 2700, rel=1e-6)
    assert info["threshold"] == pytest.approx(0.005)


# ---------------------------------------------------------------------------
# Boundary behaviour — strict ">" semantics
# ---------------------------------------------------------------------------


def test_uk_ticker_spread_exactly_at_reduce_threshold_fires():
    """Strict ``>`` semantics — exactly at the threshold is OK to fire."""
    plan = _uk_plan()
    # bid 1000, ask 1003 → spread = 3/1000 = 0.300%
    snap = _snap(bid=1000.0, ask=1003.0)
    action, info = _check_uk_spread_for_fire(plan, snap)
    assert action == "fire"
    assert info is None


def test_uk_ticker_spread_exactly_at_skip_threshold_reduces_only():
    """Exactly 0.5% triggers reduce, not skip — strict ``>`` on both."""
    plan = _uk_plan()
    # bid 1000, ask 1005 → spread = 5/1000 = 0.500%
    snap = _snap(bid=1000.0, ask=1005.0)
    action, info = _check_uk_spread_for_fire(plan, snap)
    assert action == "reduce"
    assert info is not None
