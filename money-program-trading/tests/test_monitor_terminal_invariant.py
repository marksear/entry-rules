"""
Regression tests for the TERMINAL-on-open-position invariant
(FIX_MONITOR_TERMINAL_BUG_SPEC.md, Phase 2 step 9).

Invariant: a plan with ``fill_ts_utc`` set must remain tickable until a
*real* close event fires (STOP_HIT / TARGET_HIT / TRAIL_EXIT /
TIMESTOP_HIT / INVALIDATION_EXIT / HARD_CLOSE).

These 5 tests cover the five scenarios enumerated in the spec:
  1. filled position stays tickable through TRAIL_ARM
  2. filled position stays tickable through TRAIL_STEP
  3. terminal is only set on *valid* close events (one case per reason)
  4. exception in exit evaluation does NOT terminate the plan
  5. the invariant backstop fires loudly

The Phase-1 failing repro (``test_failed_close_must_not_mark_terminal``)
stays in ``test_monitor_terminal_invariant_repro.py`` — it is the
historical record of the JNJ DEMO Day-1 2026-04-20 failure mode.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.engine.broker import CloseResult, StopModifyResult
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
    EventType,
    SessionLabel,
    StopMoveReason,
    TerminalReason,
)

# Reuse helpers already proven in the exit-paths orchestration suite.
from tests.test_monitor_exit_paths import (  # type: ignore[import-not-found]
    MockBroker,
    _StubMarketData,
    _insert_prereq_rows,
    _long_plan,
    _seed_open_position_state,
    _snap,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def database(tmp_path: Path):
    db = Database(db_path=str(tmp_path / "terminal_invariant.db"))
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
        rule_set_version="terminal-invariant",
        session_date=date.today(),
    ) as w:
        yield w


def _short_plan(session_id: str, **overrides) -> CandidatePlan:
    defaults = dict(
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
    defaults.update(overrides)
    return CandidatePlan(**defaults)


# ---------------------------------------------------------------------------
# 1. Filled position stays tickable through TRAIL_ARM
# ---------------------------------------------------------------------------


def test_filled_position_stays_tickable_through_trail_arm(
    database: Database, writer: SessionWriter
):
    """Peak P&L crosses £25 → TRAIL_ARM fires, stop moves, plan stays
    active (terminal stays False) so subsequent ticks can keep managing
    the trail ladder.
    """
    now = datetime(2026, 4, 17, 15, 0, 0)
    plan = _long_plan(session_id=writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    broker = MockBroker()
    loop = MonitorLoop(
        writer=writer,
        market_data=_StubMarketData(),
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
    )
    state = _seed_open_position_state(loop, plan, now=now, fill_minutes_ago=45)

    loop._handle_open_position_tick(plan, state, _snap(150.00), now)

    assert state.terminal is False, "TRAIL_ARM must NOT flip the plan terminal."
    assert state.trail_mode_activated is True
    assert state.trail_step_count == 1
    assert len(broker.modify_calls) == 1

    # Verify STOP_MOVED(TRAIL_ARM) event was emitted.
    rows = database.conn.execute(
        "SELECT payload_json FROM candidate_events "
        "WHERE candidate_id = ? AND event_type = ?",
        (plan.candidate_id, EventType.STOP_MOVED.value),
    ).fetchall()
    assert len(rows) == 1
    import json
    assert json.loads(rows[0]["payload_json"])["reason"] == StopMoveReason.TRAIL_ARM.value


# ---------------------------------------------------------------------------
# 2. Filled position stays tickable through TRAIL_STEP (multi-tick)
# ---------------------------------------------------------------------------


def test_filled_position_stays_tickable_through_trail_step(
    database: Database, writer: SessionWriter
):
    """Drive peak P&L through multiple trail bands (+£25, +£30, +£35).
    Stop advances on each band; plan never flips terminal.
    """
    base = datetime(2026, 4, 17, 15, 0, 0)
    plan = _long_plan(session_id=writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    broker = MockBroker()
    loop = MonitorLoop(
        writer=writer,
        market_data=_StubMarketData(),
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
    )
    state = _seed_open_position_state(loop, plan, now=base, fill_minutes_ago=45)

    for i, last in enumerate((150.00, 160.00, 170.00)):
        loop._handle_open_position_tick(
            plan, state, _snap(last), base + timedelta(minutes=i)
        )
        assert state.terminal is False, (
            f"Plan flipped terminal mid-trail at tick {i} (last={last})."
        )

    assert state.trail_step_count == 3
    assert len(broker.modify_calls) == 3


# ---------------------------------------------------------------------------
# 3. Terminal is only set on valid close events
# ---------------------------------------------------------------------------


def test_terminal_only_set_on_valid_close_events(
    database: Database, writer: SessionWriter
):
    """STOP_HIT path: terminal flips True, corresponding close event fires,
    and a subsequent tick is short-circuited at the top of run_one_tick.

    This covers the most common exit reason. The other reasons
    (TARGET_HIT / TRAIL_EXIT / TIMESTOP_HIT / INVALIDATION_EXIT /
    HARD_CLOSE) are covered by the existing test_monitor_exit_paths.py
    orchestration suite — we don't duplicate all five here; we verify the
    invariant *shape*: terminal-only-on-real-close.
    """
    now = datetime(2026, 4, 17, 15, 0, 0)
    plan = _long_plan(session_id=writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    broker = MockBroker(close_fill_price=97.00)
    loop = MonitorLoop(
        writer=writer,
        market_data=_StubMarketData(),
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
    )
    state = _seed_open_position_state(loop, plan, now=now, fill_minutes_ago=45)

    # last=97 below initial stop 97.5 → STOP_HIT.
    loop._handle_open_position_tick(plan, state, _snap(97.00), now)

    assert state.terminal is True
    assert state.terminal_reason == TerminalReason.STOPPED_OUT
    assert state.has_close_event() is True
    assert len(broker.close_calls) == 1

    # STOP_HIT event was written.
    rows = database.conn.execute(
        "SELECT event_type FROM candidate_events WHERE candidate_id = ?",
        (plan.candidate_id,),
    ).fetchall()
    assert EventType.STOP_HIT.value in [r["event_type"] for r in rows]

    # Subsequent tick — run_one_tick must short-circuit (no additional
    # broker calls, no additional events).
    before = len(broker.close_calls) + len(broker.modify_calls)
    # run_one_tick uses now_fn; override to advance time.
    later = now + timedelta(minutes=1)
    # Patch now_fn so the loop doesn't actually sleep / fetch.
    loop.now_fn = lambda: later  # type: ignore[assignment]
    # Market fetch on a terminal plan must be skipped — StubMarketData
    # would raise if called; invariant holds iff no fetch happens.
    loop.run_one_tick(later)
    after = len(broker.close_calls) + len(broker.modify_calls)
    assert after == before, "Terminal plan must not be re-processed."


# ---------------------------------------------------------------------------
# 4. Exception from broker does NOT terminate the plan
# ---------------------------------------------------------------------------


@dataclass
class _RaisingBroker:
    """Broker stub whose close_position raises, simulating an IG API error."""

    modify_calls: list[dict[str, Any]] = field(default_factory=list)
    close_calls: list[dict[str, Any]] = field(default_factory=list)

    def modify_stop(self, deal_id: str, new_stop_price: float, *, epic=None):
        self.modify_calls.append(
            {"deal_id": deal_id, "new_stop_price": new_stop_price, "epic": epic}
        )
        return StopModifyResult(
            success=True,
            deal_id=deal_id,
            new_stop_price=new_stop_price,
            reason_code="SUCCESS",
        )

    def close_position(self, *, deal_id, direction, epic, size):
        self.close_calls.append(
            {"deal_id": deal_id, "direction": direction, "epic": epic, "size": size}
        )
        raise RuntimeError("simulated IG API error")


def test_exception_in_exit_does_not_terminate_plan(
    database: Database, writer: SessionWriter, caplog
):
    """If ``broker.close_position`` raises, the exception surfaces
    (per Fix C: let unexpected errors propagate loudly instead of
    silently swallowing and marking terminal). state.terminal must NOT
    be set, so the next tick can retry.
    """
    import logging

    now = datetime(2026, 4, 17, 15, 0, 0)
    plan = _long_plan(session_id=writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    broker = _RaisingBroker()
    loop = MonitorLoop(
        writer=writer,
        market_data=_StubMarketData(),
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
    )
    state = _seed_open_position_state(loop, plan, now=now, fill_minutes_ago=45)

    # Drive a STOP_HIT. The broker will raise.
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="simulated IG API error"):
            loop._handle_open_position_tick(plan, state, _snap(97.00), now)

    assert len(broker.close_calls) == 1
    assert state.terminal is False, (
        "Exception from broker must NOT flip the plan terminal; the next "
        "tick needs to retry."
    )

    # Next tick: broker is still raising, but the plan is still tickable
    # — the monitor will attempt another close, confirming retry.
    with pytest.raises(RuntimeError):
        loop._handle_open_position_tick(plan, state, _snap(97.00), now + timedelta(minutes=1))
    assert len(broker.close_calls) == 2


# ---------------------------------------------------------------------------
# 5. Invariant backstop fires loudly
# ---------------------------------------------------------------------------


def test_invariant_backstop_resets_spurious_terminal(
    database: Database, writer: SessionWriter, caplog
):
    """Force ``state.terminal=True`` on a filled plan with no valid close
    reason. The backstop at the top of ``run_one_tick`` must log ERROR
    and flip terminal back to False so the plan keeps ticking.
    """
    import logging

    now = datetime(2026, 4, 17, 15, 0, 0)
    plan = _long_plan(session_id=writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    broker = MockBroker()
    loop = MonitorLoop(
        writer=writer,
        market_data=_StubMarketData(),
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
        now_fn=lambda: now,
    )
    state = _seed_open_position_state(loop, plan, now=now, fill_minutes_ago=45)
    # Simulate the bug: terminal flipped True, but no close reason set.
    state.terminal = True
    state.terminal_reason = None

    # Replace the market-data stub with a no-op fetcher so run_one_tick
    # doesn't blow up after the backstop re-enables ticking.
    class _QuietMarket:
        def get_market_snapshot(self, epic):
            return _snap(100.00)

    loop.market_data = _QuietMarket()  # type: ignore[assignment]

    with caplog.at_level(logging.ERROR):
        loop.run_one_tick(now)

    assert state.terminal is False, (
        "Backstop must flip terminal back to False on filled+terminal+"
        "no-close-event."
    )
    assert any(
        "INVARIANT VIOLATION" in rec.getMessage() for rec in caplog.records
    ), "Backstop must emit a loud ERROR log."
