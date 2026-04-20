"""
Phase 1 failing reproduction for the DEMO Day-1 2026-04-20 TERMINAL-on-
open-position bug (JNJ SHORT, filled 13:14:24 UTC, marked TERMINAL at
13:32:24 UTC while still live at IG).

Scenario:
  - SHORT position, filled 18 minutes ago (inside the 30-minute
    invalidation window).
  - Adverse price re-cross of trigger_high → trail_manager.evaluate_exit
    returns EXIT(INVALIDATION).
  - broker.close_position returns success=False (simulating the silent
    IG close failure we suspect happened on JNJ).

Invariant under test (from FIX_MONITOR_TERMINAL_BUG_SPEC.md):
    A plan with fill_ts_utc set and no *successful* terminating broker
    action must remain tickable until a real close event happens.

Current behaviour: MonitorLoop._handle_exit sets state.terminal=True
regardless of broker.close_position success → this test will FAIL,
which is the point of a Phase 1 repro.

The test will pass once Phase 2 fix shape C lands (narrow the defensive
terminal assignment so a failed close keeps the plan tickable).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.engine.monitor import (
    CandidatePlan,
    CandidateRuntimeState,
    MonitorLoop,
)
from src.engine.trail_manager import ExitConfig
from src.logging_mod.db import Database
from src.logging_mod.session_writer import SessionWriter
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import (
    BrokerMode,
    CandidateGrade,
    SessionLabel,
)

# Reuse the mock broker + helpers from the existing orchestration suite so
# we match conventions exactly.
from tests.test_monitor_exit_paths import (  # type: ignore[import-not-found]
    MockBroker,
    _StubMarketData,
    _insert_prereq_rows,
    _snap,
)

pytestmark = pytest.mark.unit


def _short_plan(session_id: str) -> CandidatePlan:
    return CandidatePlan(
        candidate_id=str(uuid.uuid4()),
        scan_id=str(uuid.uuid4()),
        session_id=session_id,
        symbol="JNJ",
        market=Market.US,
        direction=Direction.SHORT,
        setup_type=EntryType.S_A,
        grade=CandidateGrade.B,
        trigger_low=233.50,
        trigger_high=234.50,
        stop_price=238.81,
        target_price=None,
        ig_epic="KA.D.JNJ.CASH.IP",
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=11.6,
        planned_risk_gbp=56.96,
    )


@pytest.fixture
def database(tmp_path: Path):
    db = Database(db_path=str(tmp_path / "terminal_repro.db"))
    db.initialize()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def writer(database: Database):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="terminal-repro",
        session_date=date.today(),
    ) as w:
        yield w


def test_failed_close_must_not_mark_terminal(
    database: Database, writer: SessionWriter
):
    """JNJ SHORT — inside invalidation window, broker.close fails → plan
    must stay tickable (state.terminal must be False).

    Expected to FAIL against current code (Phase 1 repro). Passes after
    Phase 2 fix C lands.
    """
    now = datetime(2026, 4, 20, 13, 32, 24)
    plan = _short_plan(writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    # Broker reports a *failed* close — simulating the silent IG failure
    # suspected on JNJ DEMO Day-1.
    broker = MockBroker(close_should_succeed=False, close_fill_price=None)

    loop = MonitorLoop(
        writer=writer,
        market_data=_StubMarketData(),
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
    )

    # Seed runtime state exactly as it stood at 13:32:24 UTC on 2026-04-20:
    # filled 18 min ago, stop unchanged from initial, no trail activity.
    state = CandidateRuntimeState(
        fired=True,
        deal_id="DIAAAAW9ELXVPAB",
        deal_reference="LNEA2AU68ELTYP5",
        fill_price=233.90,
        fill_ts_utc=now - timedelta(minutes=18),
        stake_gbp_per_pt=11.6,
        initial_stop_price=238.81,
        current_stop_price=238.81,
        peak_pnl_gbp=0.0,
        trail_step_count=0,
        trail_mode_activated=False,
        sessions_held=1,
    )
    loop._runtime[plan.candidate_id] = state

    # Adverse re-cross: last=234.96 > trigger_high=234.50 → INVALIDATION
    # (mins_since_fill=18 is inside the 30-minute window).
    loop._handle_open_position_tick(plan, state, _snap(234.96), now)

    # We expect the broker to have been *asked* to close — that part is
    # correct behaviour.
    assert len(broker.close_calls) == 1, (
        "Expected exactly one close_position call for the INVALIDATION exit."
    )

    # **Invariant under test.** Because the broker close returned
    # success=False, the position may still be live at IG. The monitor
    # must NOT give up on the plan — it must keep ticking so the next
    # tick can retry.
    assert state.terminal is False, (
        "Plan was marked terminal despite broker.close_position returning "
        "success=False — this is the JNJ-on-DEMO-Day-1 silent-failure "
        "mode. Monitor has stopped ticking but position may still be "
        "open at IG."
    )
