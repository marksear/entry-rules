"""
Orchestration tests for MonitorLoop's terminal-event emission chain.

Scope
-----
These tests validate that ``MonitorLoop._handle_open_position_tick`` — given
a pre-populated ``CandidateRuntimeState`` and a synthetic snapshot — calls
the broker correctly and writes the right event(s) to SQLite.

The pure exit-rule math lives in ``src/engine/trail_manager.py`` and is
covered by ``tests/test_trail_manager.py``. What's missing from that suite
is an orchestration test: does the monitor wire the rule decisions to the
broker + writer properly?

Specifically, we verify that on each exit outcome the loop:
  1. Calls ``broker.modify_stop`` / ``broker.close_position`` exactly as
     expected.
  2. Emits terminal events (STOP_HIT / TARGET_HIT / TRAIL_EXIT) with the
     correct payload shape and fields.
  3. Emits TRAIL_MODE_ACTIVATED exactly once (regression against a
     re-emission bug on subsequent ticks).

No IG DEMO. A ``MockBroker`` records calls and returns canned success
results. The SessionWriter still writes to a tmp SQLite DB, so the
event-shape assertions go through the real serialiser.
"""

from __future__ import annotations

import json
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
    TargetHitReason,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Mock broker — records calls, returns canned successes
# ---------------------------------------------------------------------------


@dataclass
class MockBroker:
    """Stand-in for ``src.engine.broker.Broker`` with no IG dependency.

    Every call is appended to ``modify_calls`` / ``close_calls`` so tests
    can assert on call count and arguments. ``close_fill_price`` is the
    fill price returned by ``close_position`` — tests can override per-case
    to exercise the realised-P&L calculation on the monitor side.
    """

    close_fill_price: float | None = None
    close_should_succeed: bool = True
    modify_should_succeed: bool = True

    modify_calls: list[dict[str, Any]] = field(default_factory=list)
    close_calls: list[dict[str, Any]] = field(default_factory=list)

    def modify_stop(
        self,
        deal_id: str,
        new_stop_price: float,
        *,
        epic: str | None = None,
    ) -> StopModifyResult:
        self.modify_calls.append(
            {"deal_id": deal_id, "new_stop_price": new_stop_price, "epic": epic}
        )
        return StopModifyResult(
            success=self.modify_should_succeed,
            deal_id=deal_id,
            new_stop_price=new_stop_price,
            reason_code="SUCCESS" if self.modify_should_succeed else "REJECTED",
        )

    def close_position(
        self, *, deal_id: str, direction: Direction, epic: str, size: float
    ) -> CloseResult:
        self.close_calls.append(
            {
                "deal_id": deal_id,
                "direction": direction,
                "epic": epic,
                "size": size,
            }
        )
        return CloseResult(
            success=self.close_should_succeed,
            deal_id=deal_id,
            fill_price=self.close_fill_price,
            closed_at_utc=datetime.utcnow(),
            reason_code="SUCCESS" if self.close_should_succeed else "REJECTED",
        )


class _StubMarketData:
    """Placeholder so MonitorLoop's required market_data field is satisfied.

    These tests never call run_one_tick (which would fetch snapshots); they
    invoke _handle_open_position_tick directly with a synthetic snapshot.
    """

    def get_market_snapshot(self, epic: str) -> dict:  # pragma: no cover
        raise AssertionError("MarketData.get_market_snapshot should not be called")

    def resolve_epic(self, symbol: str, market: str = "US") -> str:  # pragma: no cover
        return "IX.D.SPTRD.DAILY.IP"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _long_plan(**overrides) -> CandidatePlan:
    defaults = dict(
        candidate_id=str(uuid.uuid4()),
        scan_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        symbol="OXY",
        market=Market.US,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.A,
        trigger_low=100.00,
        trigger_high=100.10,
        stop_price=97.50,
        target_price=200.0,
        ig_epic="KA.D.OXY.CASH.IP",
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=0.5,
        planned_risk_gbp=1.25,
    )
    defaults.update(overrides)
    return CandidatePlan(**defaults)


def _snap(last: float | None, status: str = "TRADEABLE") -> dict:
    """Mirror the MarketData.get_market_snapshot shape used elsewhere."""
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


def _insert_prereq_rows(
    database: Database, writer: SessionWriter, plan: CandidatePlan
) -> None:
    """Insert scan + shortlist rows so snapshot/event FKs are satisfied.

    Mirrors test_monitor_fill_live.py:_insert_prereq_rows — we can't reuse
    it directly because that test is @integration-marked (and wouldn't run
    here anyway).
    """
    database.conn.execute(
        """
        INSERT INTO scans (
            scan_id, session_id, scanned_at_utc, universe_size, broker_mode,
            regime, schema_version
        ) VALUES (?, ?, ?, 0, 'DEMO', 'GREEN', 1)
        """,
        (plan.scan_id, writer.session_id, datetime.utcnow().isoformat()),
    )
    database.conn.execute(
        """
        INSERT INTO shortlist_entries (
            candidate_id, scan_id, session_id, symbol, market, direction,
            setup_type, grade, trigger_low, trigger_high, stop_price,
            planned_stake_gbp_per_pt, planned_risk_gbp, planned_risk_pct_account,
            broker_mode, created_at_utc, schema_version
        ) VALUES (?, ?, ?, ?, 'US', 'LONG', 'L-A', 'A', ?, ?, ?, ?, ?, 0.005,
                  'DEMO', ?, 1)
        """,
        (
            plan.candidate_id,
            plan.scan_id,
            writer.session_id,
            plan.symbol,
            plan.trigger_low,
            plan.trigger_high,
            plan.stop_price,
            plan.planned_stake_gbp_per_pt,
            plan.planned_risk_gbp,
            datetime.utcnow().isoformat(),
        ),
    )
    database.conn.commit()


def _seed_open_position_state(
    loop: MonitorLoop,
    plan: CandidatePlan,
    *,
    now: datetime,
    fill_minutes_ago: int = 45,
    fill_price: float = 100.0,
    stake: float = 0.5,
    initial_stop: float = 97.5,
    deal_id: str = "mock-deal-1",
) -> CandidateRuntimeState:
    """Pre-populate the runtime slot for a filled position.

    Bypasses the pre-trigger + fill path so we can exercise
    ``_handle_open_position_tick`` in isolation. Default ``fill_minutes_ago``
    (45) sits outside the 30-minute invalidation window so adverse-cross
    exits don't shadow the rule branch under test.
    """
    state = CandidateRuntimeState(
        fired=True,
        deal_id=deal_id,
        deal_reference=deal_id + "-ref",
        fill_price=fill_price,
        fill_ts_utc=now - timedelta(minutes=fill_minutes_ago),
        stake_gbp_per_pt=stake,
        initial_stop_price=initial_stop,
        current_stop_price=initial_stop,
        peak_pnl_gbp=0.0,
        trail_step_count=0,
        trail_mode_activated=False,
        sessions_held=1,
    )
    loop._runtime[plan.candidate_id] = state
    return state


@pytest.fixture
def database(tmp_path: Path):
    db = Database(db_path=str(tmp_path / "monitor_exit_paths.db"))
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
        rule_set_version="exit-paths-test",
        session_date=date.today(),
    ) as w:
        yield w


# ---------------------------------------------------------------------------
# Event-row helpers
# ---------------------------------------------------------------------------


def _load_events(database: Database, candidate_id: str) -> list[dict]:
    """Return all candidate_events rows for ``candidate_id`` in insert order.

    ``payload_json`` is deserialised into ``payload`` for ergonomic
    assertions.
    """
    rows = database.conn.execute(
        """
        SELECT event_id, event_type, reason_code, payload_json, terminal_reason
          FROM candidate_events
         WHERE candidate_id = ?
         ORDER BY rowid
        """,
        (candidate_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "reason_code": r["reason_code"],
                "payload": json.loads(r["payload_json"]) if r["payload_json"] else {},
                "terminal_reason": r["terminal_reason"],
            }
        )
    return out


def _first(events: list[dict], event_type: EventType) -> dict:
    for e in events:
        if e["event_type"] == event_type.value:
            return e
    raise AssertionError(
        f"No {event_type.value} event found. Events: "
        f"{[e['event_type'] for e in events]}"
    )


# ---------------------------------------------------------------------------
# 1. STOP_HIT — LONG, initial stop breached outside the invalidation window
# ---------------------------------------------------------------------------


def test_stop_hit_emits_terminal_event_and_closes(
    database: Database, writer: SessionWriter
):
    """Initial stop reached → close_position called, STOP_HIT emitted."""
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
    # last=97 is below initial stop 97.5 → INITIAL_STOP (outside 30m window).
    loop._handle_open_position_tick(plan, state, _snap(97.00), now)

    # Broker interactions
    assert len(broker.modify_calls) == 0, (
        "Stop-out path should not move the stop before closing."
    )
    assert len(broker.close_calls) == 1
    call = broker.close_calls[0]
    assert call["deal_id"] == "mock-deal-1"
    assert call["direction"] == Direction.LONG
    assert call["epic"] == plan.ig_epic
    assert call["size"] == pytest.approx(0.5)

    # Event + payload assertions
    events = _load_events(database, plan.candidate_id)
    stop_hit = _first(events, EventType.STOP_HIT)
    assert stop_hit["terminal_reason"] == "STOPPED_OUT"
    payload = stop_hit["payload"]
    assert payload["kind"] == "STOP_HIT"
    assert payload["stop_price"] == pytest.approx(97.5)
    assert payload["fill_price"] == pytest.approx(100.0)
    # Realised P&L: (close_fill_price - fill_price) * stake = (97 - 100) * 0.5 = -£1.50
    assert payload["realised_pnl_gbp"] == pytest.approx(-1.5)

    # State transitions to terminal
    assert state.terminal is True
    assert state.terminal_reason is not None
    assert state.terminal_reason.value == "STOPPED_OUT"


# ---------------------------------------------------------------------------
# 2. TARGET_HIT — peak P&L crosses £50 → HARD_TARGET_GBP
# ---------------------------------------------------------------------------


def test_hard_target_emits_target_hit_and_closes(
    database: Database, writer: SessionWriter
):
    """Peak P&L ≥ £50 → HARD_TARGET, close_position called, TARGET_HIT emitted."""
    now = datetime(2026, 4, 17, 15, 0, 0)
    plan = _long_plan(session_id=writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    # close_fill_price matches the last snapshot → realised ≈ peak
    broker = MockBroker(close_fill_price=200.00)
    loop = MonitorLoop(
        writer=writer,
        market_data=_StubMarketData(),
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
    )
    state = _seed_open_position_state(loop, plan, now=now, fill_minutes_ago=45)
    # last=200 with fill=100 and £0.50/pt stake → live P&L = 100 × 0.5 = £50 (peak).
    loop._handle_open_position_tick(plan, state, _snap(200.00), now)

    # Broker: one close, no stop move (hard target skips any pending step advance).
    assert len(broker.modify_calls) == 0
    assert len(broker.close_calls) == 1

    # Event assertions
    events = _load_events(database, plan.candidate_id)
    target = _first(events, EventType.TARGET_HIT)
    assert target["terminal_reason"] == "HARD_TARGET_HIT"
    payload = target["payload"]
    assert payload["kind"] == "TARGET_HIT"
    assert payload["reason"] == TargetHitReason.HARD_TARGET_GBP.value
    assert payload["peak_pnl_gbp"] >= 50.0
    assert payload["realised_pnl_gbp"] >= 50.0
    # target_price is absent for HARD_TARGET_GBP — use peak.
    assert payload.get("target_price") is None

    assert state.terminal is True
    assert state.terminal_reason.value == "HARD_TARGET_HIT"


# ---------------------------------------------------------------------------
# 3. Trail arm → trail step → trail exit (full lifecycle)
# ---------------------------------------------------------------------------


def test_trail_arm_then_trail_exit_full_cycle(
    database: Database, writer: SessionWriter
):
    """Three ticks: arm the trail, advance one band, then retrace to stop.

    Ordering asserted:
        tick-a (last=150) → TRAIL_MODE_ACTIVATED, STOP_MOVED (reason=TRAIL_ARM)
        tick-b (last=160) → STOP_MOVED (reason=TRAIL_STEP)   — no TRAIL_MODE
        tick-c (last=120) → TRAIL_EXIT (terminal)
    """
    base_now = datetime(2026, 4, 17, 15, 0, 0)
    plan = _long_plan(session_id=writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    broker = MockBroker(close_fill_price=120.00)
    loop = MonitorLoop(
        writer=writer,
        market_data=_StubMarketData(),
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
    )
    # Use fill_minutes_ago=45 so the invalidation window is closed. Trail
    # ticks are all above trigger_low anyway, but this keeps the scenario
    # clean.
    state = _seed_open_position_state(loop, plan, now=base_now, fill_minutes_ago=45)

    # --- tick (a): last=150 → peak P&L = 50 × 0.5 = £25 → TRAIL_ARM at step 1.
    loop._handle_open_position_tick(plan, state, _snap(150.00), base_now)

    assert len(broker.modify_calls) == 1, (
        f"TRAIL_ARM should modify stop once; got {len(broker.modify_calls)}"
    )
    assert state.trail_mode_activated is True
    assert state.trail_step_count == 1
    # LONG stop = fill + locked/stake = 100 + 1/0.5 = 102
    assert state.current_stop_price == pytest.approx(102.0)

    # --- tick (b): last=160 → peak P&L = 60 × 0.5 = £30 → TRAIL_STEP to step 2.
    loop._handle_open_position_tick(
        plan, state, _snap(160.00), base_now + timedelta(minutes=1)
    )

    assert len(broker.modify_calls) == 2, (
        f"TRAIL_STEP should modify stop a second time; got {len(broker.modify_calls)}"
    )
    assert state.trail_step_count == 2
    # locked=£6, stop = 100 + 6/0.5 = 112
    assert state.current_stop_price == pytest.approx(112.0)

    # --- tick (c): last=120 → under trailed stop of 112? No, 120 > 112.
    # But peak is still £30, and we need to retrace so last <= current_stop_price.
    # At last=112: pnl = 12 × 0.5 = £6, stop check: 112 <= 112 → TRAIL_EXIT.
    # Push it to exactly 112 to trip the trail stop.
    loop._handle_open_position_tick(
        plan, state, _snap(112.00), base_now + timedelta(minutes=2)
    )

    assert len(broker.close_calls) == 1, (
        f"TRAIL_EXIT should close once; got {len(broker.close_calls)}"
    )
    assert state.terminal is True
    assert state.terminal_reason.value == "TRAIL_EXIT"

    # Walk the event log and verify ordering + payload shape.
    events = _load_events(database, plan.candidate_id)
    types = [e["event_type"] for e in events]
    # Expected ordering (at minimum):
    #   TRAIL_MODE_ACTIVATED, STOP_MOVED, STOP_MOVED, TRAIL_EXIT
    assert EventType.TRAIL_MODE_ACTIVATED.value in types
    assert types.count(EventType.STOP_MOVED.value) == 2
    assert EventType.TRAIL_EXIT.value in types

    # TRAIL_MODE_ACTIVATED must precede the first STOP_MOVED.
    trail_idx = types.index(EventType.TRAIL_MODE_ACTIVATED.value)
    first_stop_idx = types.index(EventType.STOP_MOVED.value)
    assert trail_idx < first_stop_idx, (
        "TRAIL_MODE_ACTIVATED must be emitted before STOP_MOVED on arm"
    )
    # TRAIL_EXIT must be the last event.
    assert types[-1] == EventType.TRAIL_EXIT.value

    # Inspect the STOP_MOVED reasons.
    stop_moves = [e for e in events if e["event_type"] == EventType.STOP_MOVED.value]
    assert stop_moves[0]["payload"]["reason"] == StopMoveReason.TRAIL_ARM.value
    assert stop_moves[1]["payload"]["reason"] == StopMoveReason.TRAIL_STEP.value

    # Inspect the TRAIL_EXIT payload.
    exit_event = _first(events, EventType.TRAIL_EXIT)
    payload = exit_event["payload"]
    assert payload["kind"] == "TRAIL_EXIT"
    assert payload["trail_step_count"] >= 1
    assert exit_event["terminal_reason"] == "TRAIL_EXIT"

    # Regression guard for a bug fixed in session 5: ``_emit_terminal``
    # previously read ``outcome.old_locked_gbp or outcome.new_locked_gbp``
    # for the TRAIL_EXIT payload, but ``evaluate_exit`` only populates
    # those on MOVE_STOP outcomes — on EXIT they're None and the payload
    # silently zeroed out. The fix derives ``locked_gbp`` from
    # ``compute_locked_gbp(state.trail_step_count, exit_config)``, which
    # is authoritative. Asserts below keep the fix honest.
    assert payload["locked_gbp"] > 0, (
        "TRAIL_EXIT payload locked_gbp must reflect profit locked by the "
        "trail — compute_locked_gbp() from step count is the source of truth"
    )


# ---------------------------------------------------------------------------
# 4. TRAIL_MODE_ACTIVATED is emitted exactly once
# ---------------------------------------------------------------------------


def test_trail_arm_does_not_reemit_on_repeated_ticks(
    database: Database, writer: SessionWriter
):
    """Regression: subsequent trail-step ticks must not re-emit TRAIL_MODE_ACTIVATED.

    Tick 1 arms (TRAIL_MODE_ACTIVATED + STOP_MOVED).
    Ticks 2 and 3 advance the trail (STOP_MOVED only, no TRAIL_MODE_ACTIVATED).
    Total TRAIL_MODE_ACTIVATED count must be exactly 1.
    """
    base_now = datetime(2026, 4, 17, 15, 0, 0)
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
    state = _seed_open_position_state(loop, plan, now=base_now, fill_minutes_ago=45)

    # Tick 1: arm (peak £25, step 1).
    loop._handle_open_position_tick(plan, state, _snap(150.00), base_now)
    # Tick 2: advance to step 2 (peak £30).
    loop._handle_open_position_tick(
        plan, state, _snap(160.00), base_now + timedelta(minutes=1)
    )
    # Tick 3: advance to step 3 (peak £35).
    loop._handle_open_position_tick(
        plan, state, _snap(170.00), base_now + timedelta(minutes=2)
    )

    events = _load_events(database, plan.candidate_id)
    activation_count = sum(
        1 for e in events if e["event_type"] == EventType.TRAIL_MODE_ACTIVATED.value
    )
    stop_move_count = sum(
        1 for e in events if e["event_type"] == EventType.STOP_MOVED.value
    )

    assert activation_count == 1, (
        f"TRAIL_MODE_ACTIVATED should be emitted exactly once, got {activation_count}"
    )
    assert stop_move_count == 3, (
        f"Expected 3 STOP_MOVED events (one per arm/step), got {stop_move_count}"
    )

    # Verify state ratcheted through the bands.
    assert state.trail_mode_activated is True
    assert state.trail_step_count == 3
    assert not state.terminal
