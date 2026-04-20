"""
Rule-math tests for the trail_manager exit engine.

These are pure tests — no IG, no DB, no fixtures beyond plan/state/snapshot
constructors. They exercise the six-level exit hierarchy with known inputs
and shapes matching what the MonitorLoop will feed in at runtime.

Mirrors the split used by ``test_monitor_classify_tick.py``: integration
tests (tests/test_trail_manager_live.py, TBD) hit IG DEMO to verify the
wiring, and these tests catch rule bugs regardless of market state.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from src.engine.monitor import CandidatePlan
from src.engine.trail_manager import (
    ExitAction,
    ExitConfig,
    ExitReason,
    PositionState,
    compute_locked_gbp,
    compute_trail_step_count,
    compute_trail_stop_price,
    evaluate_exit,
    unrealised_pnl_gbp,
)
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import BrokerMode, CandidateGrade

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


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


def _long_position(**overrides) -> PositionState:
    defaults = dict(
        fill_price=100.00,
        fill_ts_utc=datetime(2026, 4, 17, 14, 0, 0),
        stake_gbp_per_pt=1.0,  # £1/pt keeps the P&L arithmetic easy
        initial_stop_price=97.50,
        current_stop_price=97.50,
        peak_pnl_gbp=0.0,
        trail_step_count=0,
        sessions_held=1,
    )
    defaults.update(overrides)
    return PositionState(**defaults)


def _short_position(**overrides) -> PositionState:
    defaults = dict(
        fill_price=75.10,
        fill_ts_utc=datetime(2026, 4, 17, 14, 0, 0),
        stake_gbp_per_pt=1.0,
        initial_stop_price=77.00,
        current_stop_price=77.00,
        peak_pnl_gbp=0.0,
        trail_step_count=0,
        sessions_held=1,
    )
    defaults.update(overrides)
    return PositionState(**defaults)


def _snap(last: float | None, status: str = "TRADEABLE") -> dict:
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
    }


CFG = ExitConfig()  # defaults: £25 arm, £1 initial lock, £5 step, £50 hard target


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_unrealised_pnl_long():
    # 1 pt favourable × £1/pt = £1
    assert unrealised_pnl_gbp(Direction.LONG, 101.0, 100.0, 1.0) == pytest.approx(1.0)
    # 1 pt adverse = -£1
    assert unrealised_pnl_gbp(Direction.LONG, 99.0, 100.0, 1.0) == pytest.approx(-1.0)


def test_unrealised_pnl_short_symmetric():
    assert unrealised_pnl_gbp(Direction.SHORT, 74.0, 75.0, 1.0) == pytest.approx(1.0)
    assert unrealised_pnl_gbp(Direction.SHORT, 76.0, 75.0, 1.0) == pytest.approx(-1.0)


@pytest.mark.parametrize(
    "peak,expected_step",
    [
        (0.0, 0),
        (24.99, 0),
        (25.0, 1),
        (29.99, 1),
        (30.0, 2),
        (34.99, 2),
        (35.0, 3),
        (40.0, 4),
        (45.0, 5),
        (49.99, 5),
        (100.0, 5),  # capped — hard target handles this
    ],
)
def test_compute_trail_step_count_bands(peak, expected_step):
    assert compute_trail_step_count(peak, CFG) == expected_step


@pytest.mark.parametrize(
    "step,expected_locked",
    [(0, 0.0), (1, 1.0), (2, 6.0), (3, 11.0), (4, 16.0), (5, 21.0)],
)
def test_compute_locked_gbp_ladder(step, expected_locked):
    assert compute_locked_gbp(step, CFG) == pytest.approx(expected_locked)


def test_trail_stop_price_long_is_above_fill():
    # £1 locked, £1/pt stake → 1 point above fill
    stop = compute_trail_stop_price(Direction.LONG, 100.0, 1.0, 1.0)
    assert stop == pytest.approx(101.0)


def test_trail_stop_price_short_is_below_fill():
    stop = compute_trail_stop_price(Direction.SHORT, 75.0, 1.0, 1.0)
    assert stop == pytest.approx(74.0)


def test_trail_stop_scales_with_stake():
    # £5 locked, £0.5/pt stake → 10 pts above fill
    stop = compute_trail_stop_price(Direction.LONG, 100.0, 5.0, 0.5)
    assert stop == pytest.approx(110.0)


# ---------------------------------------------------------------------------
# NO_PRICE guards (mirror classify_tick)
# ---------------------------------------------------------------------------


def test_empty_snapshot_is_no_price():
    out = evaluate_exit(_long_plan(), _long_position(), {}, datetime.utcnow(), CFG)
    assert out.action == ExitAction.NO_PRICE


def test_market_closed_is_no_price():
    out = evaluate_exit(
        _long_plan(),
        _long_position(),
        _snap(100.0, status="CLOSED"),
        datetime.utcnow(),
        CFG,
    )
    assert out.action == ExitAction.NO_PRICE


def test_missing_last_traded_is_no_price():
    out = evaluate_exit(
        _long_plan(),
        _long_position(),
        _snap(None),
        datetime.utcnow(),
        CFG,
    )
    assert out.action == ExitAction.NO_PRICE


# ---------------------------------------------------------------------------
# Invalidation exit
# ---------------------------------------------------------------------------


def test_invalidation_window_adverse_cross_long():
    # Filled at 100, trigger_low=100. 10 min after fill, price back at 99.5 → adverse.
    now = datetime(2026, 4, 17, 14, 10)
    out = evaluate_exit(
        _long_plan(trigger_low=100.0),
        _long_position(fill_ts_utc=datetime(2026, 4, 17, 14, 0)),
        _snap(99.50),
        now,
        CFG,
    )
    assert out.action == ExitAction.EXIT
    assert out.reason == ExitReason.INVALIDATION


def test_invalidation_window_adverse_cross_short():
    # SHORT symmetric: adverse = back ABOVE trigger_high.
    now = datetime(2026, 4, 17, 14, 15)
    out = evaluate_exit(
        _short_plan(trigger_high=75.10),
        _short_position(fill_ts_utc=datetime(2026, 4, 17, 14, 0)),
        _snap(75.80),
        now,
        CFG,
    )
    assert out.action == ExitAction.EXIT
    assert out.reason == ExitReason.INVALIDATION


def test_invalidation_window_expired():
    # Past the 30-min window: an adverse cross is just an initial-stop scenario.
    now = datetime(2026, 4, 17, 14, 45)
    out = evaluate_exit(
        _long_plan(trigger_low=100.0),
        _long_position(fill_ts_utc=datetime(2026, 4, 17, 14, 0)),
        _snap(99.50),
        now,
        CFG,
    )
    # No invalidation; 99.50 is above initial_stop (97.50) → HOLD.
    assert out.action == ExitAction.HOLD


# ---------------------------------------------------------------------------
# Initial stop hit (pre-arm)
# ---------------------------------------------------------------------------


def test_initial_stop_hit_long():
    now = datetime(2026, 4, 17, 14, 45)  # past invalidation window
    out = evaluate_exit(
        _long_plan(),
        _long_position(initial_stop_price=97.50),
        _snap(97.50),
        now,
        CFG,
    )
    assert out.action == ExitAction.EXIT
    assert out.reason == ExitReason.INITIAL_STOP


def test_initial_stop_hit_short_symmetric():
    now = datetime(2026, 4, 17, 14, 45)
    out = evaluate_exit(
        _short_plan(),
        _short_position(initial_stop_price=77.00),
        _snap(77.00),
        now,
        CFG,
    )
    assert out.action == ExitAction.EXIT
    assert out.reason == ExitReason.INITIAL_STOP


# ---------------------------------------------------------------------------
# Trail activation (TRAIL_ARM)
# ---------------------------------------------------------------------------


def test_trail_arm_fires_on_first_25_gbp_peak_long():
    # £1/pt stake, 25 pts favourable = £25 peak → ARM at step 1.
    now = datetime(2026, 4, 17, 15, 0)
    out = evaluate_exit(
        _long_plan(),
        _long_position(peak_pnl_gbp=25.0, trail_step_count=0),
        _snap(125.0),  # sitting at +25 pts
        now,
        CFG,
    )
    assert out.action == ExitAction.MOVE_STOP
    assert out.reason == ExitReason.TRAIL_ARM
    assert out.new_trail_step_count == 1
    assert out.new_locked_gbp == pytest.approx(1.0)
    # Stop moves to fill + £1/£1 = 101.0
    assert out.new_stop_price == pytest.approx(101.0)


def test_trail_arm_short_stop_is_below_fill():
    now = datetime(2026, 4, 17, 15, 0)
    out = evaluate_exit(
        _short_plan(),
        _short_position(peak_pnl_gbp=25.0, trail_step_count=0),
        _snap(50.10),  # 25 pts favourable for a short from 75.10
        now,
        CFG,
    )
    assert out.action == ExitAction.MOVE_STOP
    assert out.reason == ExitReason.TRAIL_ARM
    # SHORT stop = fill - locked/stake = 75.10 - 1 = 74.10
    assert out.new_stop_price == pytest.approx(74.10)


# ---------------------------------------------------------------------------
# Trail step advance (post-arm)
# ---------------------------------------------------------------------------


def test_trail_step_from_1_to_2_at_peak_30():
    now = datetime(2026, 4, 17, 15, 0)
    out = evaluate_exit(
        _long_plan(),
        _long_position(
            peak_pnl_gbp=30.0,
            trail_step_count=1,
            current_stop_price=101.0,
        ),
        _snap(130.0),
        now,
        CFG,
    )
    assert out.action == ExitAction.MOVE_STOP
    assert out.reason == ExitReason.TRAIL_STEP
    assert out.new_trail_step_count == 2
    assert out.new_locked_gbp == pytest.approx(6.0)
    assert out.old_trail_step_count == 1
    assert out.old_locked_gbp == pytest.approx(1.0)


def test_multi_band_gap_advances_to_final_band_in_one_step():
    """Peak jumps from £20 to £42 in one tick → step 4, locked £16, single MOVE_STOP."""
    now = datetime(2026, 4, 17, 15, 0)
    out = evaluate_exit(
        _long_plan(),
        _long_position(
            peak_pnl_gbp=42.0,
            trail_step_count=0,  # not yet armed
            current_stop_price=97.50,
        ),
        _snap(142.0),
        now,
        CFG,
    )
    assert out.action == ExitAction.MOVE_STOP
    # First advance — ARM, but from step 0 to step 4 in one go.
    assert out.reason == ExitReason.TRAIL_ARM
    assert out.new_trail_step_count == 4
    assert out.new_locked_gbp == pytest.approx(16.0)
    assert out.old_trail_step_count == 0
    assert out.old_locked_gbp == pytest.approx(0.0)


def test_trail_step_held_when_peak_not_high_enough():
    # Step 1, peak £27 — still in £25–29.99 band. No advance.
    now = datetime(2026, 4, 17, 15, 0)
    out = evaluate_exit(
        _long_plan(),
        _long_position(
            peak_pnl_gbp=27.0, trail_step_count=1, current_stop_price=101.0
        ),
        _snap(127.0),
        now,
        CFG,
    )
    assert out.action == ExitAction.HOLD


# ---------------------------------------------------------------------------
# Hard target (peak ≥ £50 → exit, skip stop advance)
# ---------------------------------------------------------------------------


def test_hard_target_exits_even_when_step_advance_would_otherwise_fire():
    """Peak £55: step-count would be 5 (capped) but HARD_TARGET takes precedence."""
    now = datetime(2026, 4, 17, 15, 30)
    out = evaluate_exit(
        _long_plan(),
        _long_position(peak_pnl_gbp=55.0, trail_step_count=3, current_stop_price=111.0),
        _snap(155.0),
        now,
        CFG,
    )
    assert out.action == ExitAction.EXIT
    assert out.reason == ExitReason.HARD_TARGET
    # Should NOT have advanced the stop in the same outcome.
    assert out.new_stop_price is None


def test_hard_target_symmetric_short():
    # ``_short_plan()`` is grade A+ → £62.50 hard-target under the 2026-04-21
    # grade-scaled ladder (see trail_manager.GRADE_TARGET_GBP). Peak must
    # reach £62.50 to trip HARD_TARGET on an A+ plan; £50 alone is below
    # the A+ threshold and keeps the position open.
    now = datetime(2026, 4, 17, 15, 30)
    out = evaluate_exit(
        _short_plan(),
        _short_position(peak_pnl_gbp=62.5, trail_step_count=5, current_stop_price=70.10),
        _snap(25.10),  # favourable past the A+ target
        now,
        CFG,
    )
    assert out.action == ExitAction.EXIT
    assert out.reason == ExitReason.HARD_TARGET


# ---------------------------------------------------------------------------
# Trail stop hit (armed, price crossed current trailed stop)
# ---------------------------------------------------------------------------


def test_trail_exit_when_armed_and_price_retraces_to_current_stop_long():
    """Armed at step 2, current_stop=106. Price drops to 106 → TRAIL_EXIT."""
    now = datetime(2026, 4, 17, 15, 30)
    out = evaluate_exit(
        _long_plan(),
        _long_position(
            peak_pnl_gbp=32.0,  # reached band 2
            trail_step_count=2,
            current_stop_price=106.0,
            initial_stop_price=97.5,
        ),
        _snap(106.0),
        now,
        CFG,
    )
    assert out.action == ExitAction.EXIT
    assert out.reason == ExitReason.TRAIL_EXIT


def test_trail_exit_symmetric_short():
    now = datetime(2026, 4, 17, 15, 30)
    out = evaluate_exit(
        _short_plan(),
        _short_position(
            peak_pnl_gbp=32.0,
            trail_step_count=2,
            current_stop_price=69.10,
            initial_stop_price=77.00,
        ),
        _snap(69.10),
        now,
        CFG,
    )
    assert out.action == ExitAction.EXIT
    assert out.reason == ExitReason.TRAIL_EXIT


def test_price_below_initial_stop_pre_arm_is_initial_stop_not_trail_exit():
    """Pre-arm (step_count=0): a stop hit is INITIAL_STOP, not TRAIL_EXIT."""
    now = datetime(2026, 4, 17, 15, 30)
    out = evaluate_exit(
        _long_plan(),
        _long_position(
            peak_pnl_gbp=5.0,
            trail_step_count=0,
            current_stop_price=97.5,
            initial_stop_price=97.5,
        ),
        _snap(97.5),
        now,
        CFG,
    )
    assert out.reason == ExitReason.INITIAL_STOP


# ---------------------------------------------------------------------------
# Timestop
# ---------------------------------------------------------------------------


def test_timestop_fires_after_three_sessions():
    now = datetime(2026, 4, 17, 20, 45)
    out = evaluate_exit(
        _long_plan(),
        _long_position(
            fill_ts_utc=datetime(2026, 4, 15, 14, 30),  # 2+ days earlier
            peak_pnl_gbp=10.0,
            trail_step_count=0,
            sessions_held=3,
        ),
        _snap(101.0),
        now,
        CFG,
    )
    assert out.action == ExitAction.EXIT
    assert out.reason == ExitReason.TIMESTOP


def test_timestop_does_not_fire_on_session_2():
    now = datetime(2026, 4, 17, 20, 45)
    out = evaluate_exit(
        _long_plan(),
        _long_position(sessions_held=2),
        _snap(101.0),
        now,
        CFG,
    )
    assert out.action == ExitAction.HOLD


# ---------------------------------------------------------------------------
# Precedence — INVALIDATION wins over stop hits
# ---------------------------------------------------------------------------


def test_invalidation_wins_over_initial_stop_when_both_apply():
    """Inside invalidation window, adverse cross AND price at stop: INVALIDATION."""
    now = datetime(2026, 4, 17, 14, 5)  # 5 min after fill — inside window
    out = evaluate_exit(
        _long_plan(trigger_low=100.0),
        _long_position(
            fill_ts_utc=datetime(2026, 4, 17, 14, 0),
            initial_stop_price=97.5,
        ),
        _snap(97.5),  # both below trigger AND at initial stop
        now,
        CFG,
    )
    assert out.reason == ExitReason.INVALIDATION


def test_hard_target_wins_over_trail_step_on_same_tick():
    """Peak £52 in a single tick — hard target, not step advance."""
    now = datetime(2026, 4, 17, 15, 30)
    out = evaluate_exit(
        _long_plan(),
        _long_position(peak_pnl_gbp=52.0, trail_step_count=1, current_stop_price=101.0),
        _snap(152.0),
        now,
        CFG,
    )
    assert out.action == ExitAction.EXIT
    assert out.reason == ExitReason.HARD_TARGET


# ---------------------------------------------------------------------------
# HOLD path — price in the middle of the range
# ---------------------------------------------------------------------------


def test_price_between_stop_and_target_holds():
    now = datetime(2026, 4, 17, 15, 0)
    out = evaluate_exit(
        _long_plan(),
        _long_position(peak_pnl_gbp=10.0, trail_step_count=0),
        _snap(104.0),  # +4 pts, +£4 — below arm threshold
        now,
        CFG,
    )
    assert out.action == ExitAction.HOLD
    assert out.unrealised_pnl_gbp == pytest.approx(4.0)
    assert out.peak_pnl_gbp == pytest.approx(10.0)  # peak preserved


def test_peak_tracks_highest_even_after_retracement():
    """Live PNL < recorded peak — outcome still carries peak value."""
    now = datetime(2026, 4, 17, 15, 0)
    out = evaluate_exit(
        _long_plan(),
        _long_position(peak_pnl_gbp=20.0, trail_step_count=0),
        _snap(102.0),  # live +£2
        now,
        CFG,
    )
    assert out.unrealised_pnl_gbp == pytest.approx(2.0)
    assert out.peak_pnl_gbp == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Custom config still honoured
# ---------------------------------------------------------------------------


def test_custom_config_raises_activation_threshold():
    cfg = ExitConfig(trail_activation_gbp=40.0)
    now = datetime(2026, 4, 17, 15, 0)
    # £30 peak would arm on defaults; under cfg it should still HOLD.
    out = evaluate_exit(
        _long_plan(),
        _long_position(peak_pnl_gbp=30.0, trail_step_count=0),
        _snap(130.0),
        now,
        cfg,
    )
    assert out.action == ExitAction.HOLD


def test_invalidation_window_respects_config_override():
    cfg = ExitConfig(invalidation_window_minutes=5)
    now = datetime(2026, 4, 17, 14, 10)  # 10 min — outside 5-min window
    out = evaluate_exit(
        _long_plan(trigger_low=100.0),
        _long_position(fill_ts_utc=datetime(2026, 4, 17, 14, 0)),
        _snap(99.5),
        now,
        cfg,
    )
    # Outside shorter window — price 99.5 is still above initial_stop (97.5), so HOLD.
    assert out.action == ExitAction.HOLD


# ---------------------------------------------------------------------------
# Smoke: the returned outcome always carries last_price / pnl / peak
# ---------------------------------------------------------------------------


def test_exit_outcome_always_carries_observability_fields():
    now = datetime(2026, 4, 17, 15, 0)
    out = evaluate_exit(
        _long_plan(),
        _long_position(),
        _snap(103.5),
        now,
        CFG,
    )
    assert out.last_price == pytest.approx(103.5)
    assert out.unrealised_pnl_gbp is not None
    assert out.peak_pnl_gbp is not None


# A small sanity check: the module re-exports the expected helpers.
def test_public_api_is_stable():
    from src.engine import trail_manager as tm

    expected = {
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
    }
    assert expected.issubset(set(dir(tm)))


# Guard against me writing a test that accidentally bypasses the 30-min window.
def test_fixture_times_are_consistent():
    pos = _long_position()
    # Default fill ts is 14:00 UTC.
    delta = datetime(2026, 4, 17, 14, 45) - pos.fill_ts_utc
    assert delta == timedelta(minutes=45)
