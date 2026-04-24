"""
Orchestration tests for MonitorLoop's S-3 Phase 4a staleness escalation.

Validates the ladder defined in docs/specs/S3_LIGHTSTREAMER_SPEC.md §7.2:

* A ``StalePriceError`` thrown by the feed causes monitor to emit
  ``PRICE_STALE`` and skip trigger/exit evaluation for that tick.
* When stale duration crosses ``price_feed_degraded_seconds``,
  ``PRICE_FEED_DEGRADED`` fires exactly once for the episode.
* If a position is open on the epic at the degradation moment, the
  defensive-close path fires via broker REST +
  ``POSITION_CLOSED_DEGRADED_FEED`` terminal event.
* When a fresh tick arrives after a ``PRICE_FEED_DEGRADED``, a
  ``PRICE_FEED_RECOVERED`` event pairs the episode's close-out.
* Short stale glitches (<60s) that never escalated do NOT emit
  RECOVERED — we keep the event stream quiet on routine flicker.
* A failed broker close does NOT mark the plan terminal; the next
  tick can retry (same failure-tolerance shape as ``_handle_exit``).
* REST-mode feeds (which don't raise StalePriceError) keep the
  pre-Phase-4a behaviour unchanged.

Reuses the MockBroker + database/writer fixture patterns from
``test_monitor_exit_paths.py``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.data.price_feed import StalePriceError
from src.engine.broker import CloseResult
from src.engine.monitor import (
    CandidatePlan,
    CandidateRuntimeState,
    MonitorLoop,
)
from src.utils.time_utils import utc_now
from src.engine.trail_manager import ExitConfig
from src.logging_mod.db import Database
from src.logging_mod.session_writer import SessionWriter
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import (
    BrokerMode,
    CandidateGrade,
    EventType,
    SessionLabel,
    TerminalReason,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Mock broker + MarketData stub
# ---------------------------------------------------------------------------


@dataclass
class MockBroker:
    close_fill_price: float | None = 100.5
    close_should_succeed: bool = True
    modify_should_succeed: bool = True
    modify_calls: list[dict[str, Any]] = field(default_factory=list)
    close_calls: list[dict[str, Any]] = field(default_factory=list)

    def modify_stop(self, *args, **kwargs) -> Any:  # unused in these tests
        self.modify_calls.append(kwargs)
        raise AssertionError("modify_stop not expected in staleness tests")

    def close_position(
        self, *, deal_id: str, direction: Direction, epic: str, size: float
    ) -> CloseResult:
        self.close_calls.append(
            {"deal_id": deal_id, "direction": direction, "epic": epic, "size": size}
        )
        return CloseResult(
            success=self.close_should_succeed,
            deal_id=deal_id,
            fill_price=self.close_fill_price,
            closed_at_utc=utc_now(),
            reason_code="SUCCESS" if self.close_should_succeed else "REJECTED",
        )


class _StaleFeed:
    """A MarketData stand-in that raises StalePriceError on
    ``get_market_snapshot``. Models a LightstreamerPriceFeed with no
    recent ticks. Toggle ``should_raise`` to make the next call return a
    normal snapshot instead — simulating recovery."""

    def __init__(self, epic: str = "IX.D.SPTRD.DAILY.IP"):
        self.epic = epic
        self.should_raise = True
        self.age_seconds = 15.0  # what the exception carries
        # Prices deliberately well below the _long_plan() trigger zone
        # (trigger 100.00–100.10) so classify_tick returns HOLD — none
        # of the staleness tests want the recovery path to trip FIRE
        # and pull in the full order-placement machinery.
        self.snapshot_to_return: dict = {
            "bid": 95.0, "ask": 95.1, "last_traded": 95.05,
            "market_status": "TRADEABLE", "high": None, "low": None,
            "net_change": None, "pct_change": None, "update_time_utc": None,
        }

    def get_market_snapshot(self, epic: str) -> dict:
        if self.should_raise:
            raise StalePriceError(epic=epic, age_seconds=self.age_seconds)
        return dict(self.snapshot_to_return)

    def resolve_epic(self, symbol: str, market: str = "US") -> str:
        return self.epic


# ---------------------------------------------------------------------------
# Plan + state fixtures
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
        ig_epic="IX.D.SPTRD.DAILY.IP",
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=0.5,
        planned_risk_gbp=1.25,
    )
    defaults.update(overrides)
    return CandidatePlan(**defaults)


def _insert_prereq_rows(db: Database, writer: SessionWriter, plan: CandidatePlan) -> None:
    db.conn.execute(
        """
        INSERT INTO scans (
            scan_id, session_id, scanned_at_utc, universe_size, broker_mode,
            regime, schema_version
        ) VALUES (?, ?, ?, 0, 'DEMO', 'GREEN', 1)
        """,
        (plan.scan_id, writer.session_id, utc_now().isoformat()),
    )
    db.conn.execute(
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
            plan.candidate_id, plan.scan_id, writer.session_id, plan.symbol,
            plan.trigger_low, plan.trigger_high, plan.stop_price,
            plan.planned_stake_gbp_per_pt, plan.planned_risk_gbp,
            utc_now().isoformat(),
        ),
    )
    db.conn.commit()


def _seed_open_position(
    loop: MonitorLoop, plan: CandidatePlan, *, now: datetime,
    fill_price: float = 100.0, stake: float = 0.5,
    deal_id: str = "mock-deal-stale",
) -> CandidateRuntimeState:
    state = CandidateRuntimeState(
        fired=True,
        deal_id=deal_id,
        fill_price=fill_price,
        fill_ts_utc=now - timedelta(minutes=5),
        stake_gbp_per_pt=stake,
        initial_stop_price=97.5,
        current_stop_price=97.5,
        peak_pnl_gbp=0.0,
    )
    loop._runtime[plan.candidate_id] = state
    return state


@pytest.fixture
def database(tmp_path: Path):
    db = Database(db_path=str(tmp_path / "monitor_staleness.db"))
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
        account_size_gbp=10000.0,
        rule_set_version="staleness-test",
        session_date=date.today(),
    ) as w:
        yield w


def _load_events(db: Database, candidate_id: str) -> list[dict]:
    rows = db.conn.execute(
        """SELECT event_type, payload_json, terminal_reason
           FROM candidate_events WHERE candidate_id = ? ORDER BY rowid""",
        (candidate_id,),
    ).fetchall()
    return [
        {
            "event_type": r["event_type"],
            "payload": json.loads(r["payload_json"]) if r["payload_json"] else {},
            "terminal_reason": r["terminal_reason"],
        }
        for r in rows
    ]


def _event_types(db: Database, candidate_id: str) -> list[str]:
    return [e["event_type"] for e in _load_events(db, candidate_id)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stale_tick_under_threshold_emits_price_stale_only(database, writer):
    """StalePriceError arrives; stale duration is just a few seconds
    (well under the 60s degraded threshold). Monitor emits PRICE_STALE
    but must NOT emit DEGRADED or touch the broker."""
    plan = _long_plan()
    _insert_prereq_rows(database, writer, plan)
    feed = _StaleFeed(plan.ig_epic)
    broker = MockBroker()
    now = utc_now()
    loop = MonitorLoop(
        writer=writer, market_data=feed, plans=[plan],
        broker=broker, exit_config=ExitConfig(),
        tick_interval_seconds=30,
        price_feed_degraded_seconds=60.0,
        now_fn=lambda: now,
    )

    loop.run_one_tick(now)

    events = _event_types(database, plan.candidate_id)
    assert events == ["PRICE_STALE"]
    assert broker.close_calls == []
    # First stale tick stamped the runtime state
    state = loop._runtime[plan.candidate_id]
    assert state.stale_since_utc == now
    assert state.degraded_emitted is False


def test_stale_escalates_to_degraded_at_threshold_no_position(database, writer):
    """Stale for 65s with NO open position: emit DEGRADED (once), no
    broker.close_position call, keep monitor running."""
    plan = _long_plan()
    _insert_prereq_rows(database, writer, plan)
    feed = _StaleFeed(plan.ig_epic)
    broker = MockBroker()
    now = utc_now()

    times = [now - timedelta(seconds=65), now]
    call_idx = {"i": 0}
    def _clock():
        t = times[call_idx["i"]]
        return t

    loop = MonitorLoop(
        writer=writer, market_data=feed, plans=[plan],
        broker=broker, exit_config=ExitConfig(),
        tick_interval_seconds=30,
        price_feed_degraded_seconds=60.0,
        now_fn=_clock,
    )

    # Tick 1 at t=0: first stale. PRICE_STALE emitted; stale_since set.
    call_idx["i"] = 0
    loop.run_one_tick(times[0])
    # Tick 2 at t=+65s: stale duration crosses 60s → DEGRADED.
    call_idx["i"] = 1
    loop.run_one_tick(times[1])

    events = _event_types(database, plan.candidate_id)
    assert events.count("PRICE_STALE") == 2
    assert events.count("PRICE_FEED_DEGRADED") == 1
    # No position → no defensive close
    assert broker.close_calls == []


def test_degraded_with_open_position_force_closes(database, writer):
    """Stale for 65s with an open position: emit DEGRADED,
    broker.close_position called, POSITION_CLOSED_DEGRADED_FEED emitted,
    state.terminal = True with reason DEGRADED_FEED."""
    plan = _long_plan()
    _insert_prereq_rows(database, writer, plan)
    feed = _StaleFeed(plan.ig_epic)
    broker = MockBroker(close_fill_price=101.2)
    t0 = utc_now()
    t65 = t0 + timedelta(seconds=65)

    times = [t0, t65]
    call_idx = {"i": 0}
    loop = MonitorLoop(
        writer=writer, market_data=feed, plans=[plan],
        broker=broker, exit_config=ExitConfig(),
        tick_interval_seconds=30,
        price_feed_degraded_seconds=60.0,
        now_fn=lambda: times[call_idx["i"]],
    )
    # Seed an open position on this plan.
    state = _seed_open_position(loop, plan, now=t0)

    # First stale tick
    call_idx["i"] = 0
    loop.run_one_tick(t0)
    # Second tick 65s later → DEGRADED + defensive close
    call_idx["i"] = 1
    loop.run_one_tick(t65)

    events = _event_types(database, plan.candidate_id)
    assert "PRICE_FEED_DEGRADED" in events
    assert "POSITION_CLOSED_DEGRADED_FEED" in events

    # Broker was called to close
    assert len(broker.close_calls) == 1
    assert broker.close_calls[0]["deal_id"] == state.deal_id
    assert broker.close_calls[0]["epic"] == plan.ig_epic

    # Runtime state terminalised correctly
    assert state.terminal is True
    assert state.terminal_reason == TerminalReason.DEGRADED_FEED
    # And has_close_event recognises the new close type — invariant
    # backstop must not re-open this plan next tick.
    assert state.has_close_event() is True


def test_degraded_close_failure_keeps_plan_tickable(database, writer):
    """broker.close_position returns success=False → state stays
    non-terminal, consecutive_close_failures increments. Mirrors the
    _handle_exit failure-tolerance shape (Day-1 silent-failure guard)."""
    plan = _long_plan()
    _insert_prereq_rows(database, writer, plan)
    feed = _StaleFeed(plan.ig_epic)
    broker = MockBroker(close_should_succeed=False)
    t0 = utc_now()
    t65 = t0 + timedelta(seconds=65)
    times = [t0, t65]
    call_idx = {"i": 0}
    loop = MonitorLoop(
        writer=writer, market_data=feed, plans=[plan],
        broker=broker, exit_config=ExitConfig(),
        tick_interval_seconds=30,
        price_feed_degraded_seconds=60.0,
        now_fn=lambda: times[call_idx["i"]],
    )
    state = _seed_open_position(loop, plan, now=t0)

    call_idx["i"] = 0
    loop.run_one_tick(t0)
    call_idx["i"] = 1
    loop.run_one_tick(t65)

    # Broker was called but close failed
    assert len(broker.close_calls) == 1
    # POSITION_CLOSED_DEGRADED_FEED was NOT emitted (no successful close)
    events = _event_types(database, plan.candidate_id)
    assert "POSITION_CLOSED_DEGRADED_FEED" not in events
    assert "PRICE_FEED_DEGRADED" in events
    # State remains tickable
    assert state.terminal is False
    assert state.consecutive_close_failures == 1


def test_recovery_after_degraded_emits_recovered(database, writer):
    """Stale → degraded → fresh. Must emit PRICE_FEED_RECOVERED and reset
    stale tracking."""
    # Fresh-tick path writes a CandidateSnapshot; its session_id must
    # match the writer's (enforced by _assert_session).
    plan = _long_plan(session_id=writer.session_id)
    _insert_prereq_rows(database, writer, plan)
    feed = _StaleFeed(plan.ig_epic)
    broker = MockBroker()
    t0 = utc_now()
    t65 = t0 + timedelta(seconds=65)
    t95 = t0 + timedelta(seconds=95)
    times = [t0, t65, t95]
    call_idx = {"i": 0}
    loop = MonitorLoop(
        writer=writer, market_data=feed, plans=[plan],
        broker=broker, exit_config=ExitConfig(),
        tick_interval_seconds=30,
        price_feed_degraded_seconds=60.0,
        now_fn=lambda: times[call_idx["i"]],
    )
    # No position open — just pre-trigger state.

    # Tick 1 stale → PRICE_STALE
    call_idx["i"] = 0
    loop.run_one_tick(t0)
    # Tick 2 stale, crosses threshold → PRICE_STALE + PRICE_FEED_DEGRADED
    call_idx["i"] = 1
    loop.run_one_tick(t65)
    # Tick 3 fresh → recovery
    feed.should_raise = False
    call_idx["i"] = 2
    loop.run_one_tick(t95)

    events = _event_types(database, plan.candidate_id)
    assert "PRICE_FEED_DEGRADED" in events
    assert "PRICE_FEED_RECOVERED" in events
    # Recovery must come after degraded
    assert events.index("PRICE_FEED_RECOVERED") > events.index("PRICE_FEED_DEGRADED")
    # Runtime reset
    state = loop._runtime[plan.candidate_id]
    assert state.stale_since_utc is None
    assert state.degraded_emitted is False


def test_short_stale_burst_then_recovery_emits_no_recovered(database, writer):
    """If staleness never crossed the DEGRADED threshold, transitioning
    to fresh must NOT emit PRICE_FEED_RECOVERED (keeps event stream
    quiet on routine <60s glitches)."""
    plan = _long_plan(session_id=writer.session_id)
    _insert_prereq_rows(database, writer, plan)
    feed = _StaleFeed(plan.ig_epic)
    broker = MockBroker()
    t0 = utc_now()
    t20 = t0 + timedelta(seconds=20)
    times = [t0, t20]
    call_idx = {"i": 0}
    loop = MonitorLoop(
        writer=writer, market_data=feed, plans=[plan],
        broker=broker, exit_config=ExitConfig(),
        tick_interval_seconds=30,
        price_feed_degraded_seconds=60.0,
        now_fn=lambda: times[call_idx["i"]],
    )

    call_idx["i"] = 0
    loop.run_one_tick(t0)
    # Only 20s later — under 60s threshold → no DEGRADED yet
    feed.should_raise = False
    call_idx["i"] = 1
    loop.run_one_tick(t20)

    events = _event_types(database, plan.candidate_id)
    assert "PRICE_STALE" in events
    assert "PRICE_FEED_DEGRADED" not in events
    # CRITICAL: no RECOVERED because we never escalated to DEGRADED
    assert "PRICE_FEED_RECOVERED" not in events


def test_non_stale_snapshot_unchanged_flow(database, writer):
    """REST-mode (or plain MarketData) returning snapshots normally —
    Phase 4a must be a no-op. No PRICE_* events; pre-Phase-4a flow
    preserved."""
    plan = _long_plan(session_id=writer.session_id)
    _insert_prereq_rows(database, writer, plan)
    feed = _StaleFeed(plan.ig_epic)
    feed.should_raise = False  # never raise — behave like RestPriceFeed
    broker = MockBroker()
    now = utc_now()
    loop = MonitorLoop(
        writer=writer, market_data=feed, plans=[plan],
        broker=broker, exit_config=ExitConfig(),
        tick_interval_seconds=30,
        price_feed_degraded_seconds=60.0,
        now_fn=lambda: now,
    )

    loop.run_one_tick(now)

    events = _event_types(database, plan.candidate_id)
    assert "PRICE_STALE" not in events
    assert "PRICE_FEED_DEGRADED" not in events
    assert "PRICE_FEED_RECOVERED" not in events
    # The normal pre-trigger path either emits nothing or emits an
    # ENTRY_EVALUATED_NO_ENTER / TRIGGER_ARMED; either is fine. The
    # regression guard is that none of the Phase 4a events fire.


def test_degraded_event_fires_once_per_episode(database, writer):
    """Multiple ticks past the 60s threshold during the same stale
    episode must emit DEGRADED exactly once (not per tick)."""
    plan = _long_plan()
    _insert_prereq_rows(database, writer, plan)
    feed = _StaleFeed(plan.ig_epic)
    broker = MockBroker()
    t0 = utc_now()
    t65 = t0 + timedelta(seconds=65)
    t95 = t0 + timedelta(seconds=95)
    t125 = t0 + timedelta(seconds=125)
    times = [t0, t65, t95, t125]
    call_idx = {"i": 0}
    loop = MonitorLoop(
        writer=writer, market_data=feed, plans=[plan],
        broker=broker, exit_config=ExitConfig(),
        tick_interval_seconds=30,
        price_feed_degraded_seconds=60.0,
        now_fn=lambda: times[call_idx["i"]],
    )

    for i in range(4):
        call_idx["i"] = i
        loop.run_one_tick(times[i])

    events = _event_types(database, plan.candidate_id)
    # Four stale ticks → four PRICE_STALE
    assert events.count("PRICE_STALE") == 4
    # DEGRADED must fire once, not every tick past threshold
    assert events.count("PRICE_FEED_DEGRADED") == 1


def test_has_close_event_recognises_degraded_feed_terminal():
    """CandidateRuntimeState.has_close_event() must include DEGRADED_FEED —
    otherwise the invariant backstop would mis-flag a defensively-closed
    position as the TERMINAL-on-open-position bug and reopen it."""
    state = CandidateRuntimeState()
    state.terminal = True
    state.terminal_reason = TerminalReason.DEGRADED_FEED
    assert state.has_close_event() is True
