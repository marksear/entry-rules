"""
Unit tests for src.engine.resume.rehydrate_open_positions.

These tests feed a pre-populated SQLite DB + a hand-crafted live positions
list into the function and assert the rehydrated (plan, state) tuples are
shaped correctly. No IG calls — ``fetch_live_positions`` is NOT exercised
here (that's an integration concern).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from src.engine.monitor import CandidateRuntimeState
from src.engine.resume import rehydrate_open_positions
from src.logging_mod.db import Database
from src.logging_mod.session_writer import SessionWriter
from src.models.log_enums import BrokerMode, SessionLabel


class _FakeMarketData:
    """Stand-in for MarketData.resolve_epic — returns a deterministic epic."""

    def __init__(self, epic: str = "IX.D.SPTRD.DAILY.IP"):
        self._epic = epic
        self.calls: list[tuple[str, str]] = []

    def resolve_epic(self, symbol: str, market: str = "US") -> str:
        self.calls.append((symbol, market))
        return self._epic


@pytest.fixture
def database(tmp_path: Path):
    db = Database(db_path=str(tmp_path / "resume_test.db"))
    db.initialize()
    try:
        yield db
    finally:
        db.close()


def _insert_session(db: Database, session_id: str) -> None:
    db.conn.execute(
        """
        INSERT INTO sessions (
            session_id, session_date, session_label, broker_mode,
            account_size_gbp, opened_at_utc, rule_set_version, schema_version
        ) VALUES (?, ?, 'US_REGULAR', 'DEMO', 1000.0, ?, 'test', 1)
        """,
        (
            session_id,
            datetime.utcnow().date().isoformat(),
            datetime.utcnow().isoformat(),
        ),
    )


def _insert_scan(db: Database, scan_id: str, session_id: str) -> None:
    db.conn.execute(
        """
        INSERT INTO scans (
            scan_id, session_id, scanned_at_utc, universe_size, broker_mode,
            regime, schema_version
        ) VALUES (?, ?, ?, 0, 'DEMO', 'GREEN', 1)
        """,
        (scan_id, session_id, datetime.utcnow().isoformat()),
    )


def _insert_shortlist(
    db: Database,
    candidate_id: str,
    scan_id: str,
    session_id: str,
    *,
    symbol: str = "AAPL",
    market: str = "US",
    direction: str = "LONG",
    stake: float = 0.5,
    risk: float = 5.0,
) -> None:
    db.conn.execute(
        """
        INSERT INTO shortlist_entries (
            candidate_id, scan_id, session_id, symbol, market, direction,
            setup_type, grade, trigger_low, trigger_high, stop_price,
            planned_stake_gbp_per_pt, planned_risk_gbp, planned_risk_pct_account,
            broker_mode, created_at_utc, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, 'L-A', 'B', 100.0, 100.5, 95.0, ?, ?, 0.005,
                  'DEMO', ?, 1)
        """,
        (
            candidate_id,
            scan_id,
            session_id,
            symbol,
            market,
            direction,
            stake,
            risk,
            datetime.utcnow().isoformat(),
        ),
    )


def _insert_event(
    db: Database,
    *,
    candidate_id: str,
    session_id: str,
    event_type: str,
    payload: dict,
    terminal_reason: str | None = None,
    ts_utc: datetime | None = None,
) -> None:
    db.conn.execute(
        """
        INSERT INTO candidate_events (
            event_id, session_id, candidate_id, ts_utc, event_type, actor,
            payload_json, terminal_reason, broker_mode, schema_version
        ) VALUES (?, ?, ?, ?, ?, 'MONITOR', ?, ?, 'DEMO', 1)
        """,
        (
            str(uuid.uuid4()),
            session_id,
            candidate_id,
            (ts_utc or datetime.utcnow()).isoformat(),
            event_type,
            json.dumps(payload),
            terminal_reason,
        ),
    )


def _insert_snapshot(db: Database, candidate_id: str, session_id: str) -> None:
    """Minimal snapshot so sessions_held count sees this session as 'prior'."""
    db.conn.execute(
        """
        INSERT INTO candidate_snapshots (
            session_id, candidate_id, ts_utc, symbol, status,
            broker_mode, schema_version
        ) VALUES (?, ?, ?, 'AAPL', 'TRIGGERED_OPEN', 'DEMO', 1)
        """,
        (session_id, candidate_id, datetime.utcnow().isoformat()),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_rehydrate_returns_empty_when_no_live_positions(database):
    result = rehydrate_open_positions(
        database=database,
        live_positions=[],
        new_session_id="sess-new",
        market_data=_FakeMarketData(),
    )
    assert result == []


def test_rehydrate_matches_live_position_to_filled_event(database):
    prior_session = "sess-prior"
    new_session = "sess-new"
    candidate_id = "cand-aapl-1"
    scan_id = "scan-1"

    _insert_session(database, prior_session)
    _insert_session(database, new_session)
    _insert_scan(database, scan_id, prior_session)
    _insert_shortlist(database, candidate_id, scan_id, prior_session)

    fill_payload = {
        "kind": "FILLED",
        "ig_deal_id": "DIAAAA",
        "fill_price": 150.25,
        "fill_ts_utc": "2026-04-16T14:45:00",
        "stake_gbp_per_pt": 0.5,
        "initial_stop_price": 145.0,
        "initial_risk_gbp": 2.625,
    }
    _insert_event(
        database,
        candidate_id=candidate_id,
        session_id=prior_session,
        event_type="FILLED",
        payload=fill_payload,
    )
    # Prior session snapshot so sessions_held = 2 after resume.
    _insert_snapshot(database, candidate_id, prior_session)
    database.conn.commit()

    live_positions = [
        {
            "deal_id": "DIAAAA",
            "deal_reference": "REFAAA",
            "size": 0.5,
            "direction": "BUY",
            "fill_price": 150.25,
            "stop_level": 146.5,  # IG now reports stop at 146.5 — may have been nudged
            "created_date_utc": "2026-04-16T14:45:00",
            "epic": "IX.D.AAPL.CASH.IP",
            "currency": "GBP",
        }
    ]

    result = rehydrate_open_positions(
        database=database,
        live_positions=live_positions,
        new_session_id=new_session,
        market_data=_FakeMarketData("IX.D.AAPL.CASH.IP"),
    )

    assert len(result) == 1
    plan, state = result[0]

    # Plan: session_id flipped to the NEW session so events route correctly.
    assert plan.session_id == new_session
    assert plan.candidate_id == candidate_id
    assert plan.symbol == "AAPL"
    assert plan.ig_epic == "IX.D.AAPL.CASH.IP"
    assert plan.planned_stake_gbp_per_pt == pytest.approx(0.5)

    # State: fully rehydrated.
    assert isinstance(state, CandidateRuntimeState)
    assert state.fired is True
    assert state.deal_id == "DIAAAA"
    assert state.fill_price == pytest.approx(150.25)
    assert state.stake_gbp_per_pt == pytest.approx(0.5)
    assert state.initial_stop_price == pytest.approx(145.0)
    # IG's stopLevel wins over DB's initial.
    assert state.current_stop_price == pytest.approx(146.5)
    assert state.peak_pnl_gbp == 0.0  # no STOP_MOVED yet
    assert state.trail_step_count == 0
    assert state.trail_mode_activated is False
    # sessions_held = 1 prior session + 1 current = 2
    assert state.sessions_held == 2
    assert state.terminal is False


def test_rehydrate_picks_up_latest_stop_moved(database):
    prior = "sess-prior"
    new = "sess-new"
    cid = "cand-nvda"
    scan = "scan-2"

    _insert_session(database, prior)
    _insert_session(database, new)
    _insert_scan(database, scan, prior)
    _insert_shortlist(database, cid, scan, prior, symbol="NVDA", stake=0.5)

    fill_payload = {
        "kind": "FILLED",
        "ig_deal_id": "DINVDA",
        "fill_price": 500.0,
        "fill_ts_utc": "2026-04-16T14:45:00",
        "stake_gbp_per_pt": 0.5,
        "initial_stop_price": 480.0,
        "initial_risk_gbp": 10.0,
    }
    _insert_event(
        database,
        candidate_id=cid,
        session_id=prior,
        event_type="FILLED",
        payload=fill_payload,
        ts_utc=datetime(2026, 4, 16, 14, 45),
    )
    # Two STOP_MOVED events — the later one should win.
    _insert_event(
        database,
        candidate_id=cid,
        session_id=prior,
        event_type="STOP_MOVED",
        payload={
            "kind": "STOP_MOVED",
            "reason": "TRAIL_ARM",
            "old_stop": 480.0,
            "new_stop": 501.0,
            "old_trail_step_count": 0,
            "new_trail_step_count": 1,
            "old_locked_gbp": 0.0,
            "new_locked_gbp": 1.0,
            "peak_pnl_gbp_at_move": 25.0,
        },
        ts_utc=datetime(2026, 4, 16, 15, 0),
    )
    _insert_event(
        database,
        candidate_id=cid,
        session_id=prior,
        event_type="STOP_MOVED",
        payload={
            "kind": "STOP_MOVED",
            "reason": "TRAIL_STEP",
            "old_stop": 501.0,
            "new_stop": 512.0,
            "old_trail_step_count": 1,
            "new_trail_step_count": 3,
            "old_locked_gbp": 1.0,
            "new_locked_gbp": 11.0,
            "peak_pnl_gbp_at_move": 37.5,
        },
        ts_utc=datetime(2026, 4, 16, 15, 20),
    )
    database.conn.commit()

    live_positions = [
        {
            "deal_id": "DINVDA",
            "deal_reference": "REFNVDA",
            "size": 0.5,
            "direction": "BUY",
            "fill_price": 500.0,
            "stop_level": 512.0,
            "created_date_utc": "2026-04-16T14:45:00",
            "epic": "IX.D.NVDA.CASH.IP",
            "currency": "GBP",
        }
    ]

    result = rehydrate_open_positions(
        database=database,
        live_positions=live_positions,
        new_session_id=new,
        market_data=_FakeMarketData("IX.D.NVDA.CASH.IP"),
    )
    assert len(result) == 1
    _, state = result[0]

    # Latest STOP_MOVED wins.
    assert state.trail_step_count == 3
    assert state.peak_pnl_gbp == pytest.approx(37.5)
    assert state.trail_mode_activated is True
    assert state.current_stop_price == pytest.approx(512.0)


# ---------------------------------------------------------------------------
# Exclusion paths
# ---------------------------------------------------------------------------


def test_rehydrate_skips_candidates_with_terminal_events(database):
    prior = "sess-prior"
    new = "sess-new"
    cid = "cand-closed"
    scan = "scan-3"

    _insert_session(database, prior)
    _insert_session(database, new)
    _insert_scan(database, scan, prior)
    _insert_shortlist(database, cid, scan, prior)

    _insert_event(
        database,
        candidate_id=cid,
        session_id=prior,
        event_type="FILLED",
        payload={
            "kind": "FILLED",
            "ig_deal_id": "DICLOSED",
            "fill_price": 100.0,
            "fill_ts_utc": "2026-04-16T14:45:00",
            "stake_gbp_per_pt": 0.5,
            "initial_stop_price": 95.0,
            "initial_risk_gbp": 2.5,
        },
    )
    # Already exited — terminal_reason populated. Resume must skip.
    _insert_event(
        database,
        candidate_id=cid,
        session_id=prior,
        event_type="STOP_HIT",
        payload={
            "kind": "STOP_HIT",
            "stop_price": 95.0,
            "fill_price": 100.0,
            "realised_pnl_gbp": -2.5,
        },
        terminal_reason="STOP_HIT",
    )
    database.conn.commit()

    live_positions = [
        {
            "deal_id": "DICLOSED",
            "deal_reference": "REFCLOSED",
            "size": 0.5,
            "direction": "BUY",
            "fill_price": 100.0,
            "stop_level": 95.0,
            "created_date_utc": "2026-04-16T14:45:00",
            "epic": "IX.D.X.CASH.IP",
            "currency": "GBP",
        }
    ]

    result = rehydrate_open_positions(
        database=database,
        live_positions=live_positions,
        new_session_id=new,
        market_data=_FakeMarketData(),
    )
    assert result == [], "Candidate with terminal event must be skipped"


def test_rehydrate_warns_on_live_position_with_no_filled_event(database, caplog):
    """A live IG position with no matching FILLED in the DB should be logged
    and skipped — never silently resumed."""
    new = "sess-new"
    _insert_session(database, new)
    database.conn.commit()

    live_positions = [
        {
            "deal_id": "DIORPHAN",
            "deal_reference": "REFORPHAN",
            "size": 1.0,
            "direction": "BUY",
            "fill_price": 50.0,
            "stop_level": 45.0,
            "created_date_utc": "2026-04-16T14:45:00",
            "epic": "IX.D.X.CASH.IP",
            "currency": "GBP",
        }
    ]

    with caplog.at_level("WARNING"):
        result = rehydrate_open_positions(
            database=database,
            live_positions=live_positions,
            new_session_id=new,
            market_data=_FakeMarketData(),
        )
    assert result == []
    assert any("DIORPHAN" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# POSITION_RESUMED event emission (session 8 item #2)
# ---------------------------------------------------------------------------


def test_rehydrate_writes_position_resumed_event(database):
    """When a SessionWriter is supplied, each resumed candidate should land
    a POSITION_RESUMED row in candidate_events with the right payload.

    Verifies: (a) event_type = POSITION_RESUMED, (b) payload carries the
    deal_id + trail step + session handoff info, (c) source_of_stop correctly
    reports IG_STOP_LEVEL when IG has a stopLevel."""
    prior = "sess-prior"
    cid = "cand-resumed"
    scan = "scan-resumed"

    _insert_session(database, prior)
    _insert_scan(database, scan, prior)
    _insert_shortlist(database, cid, scan, prior, symbol="AAPL", stake=0.5)

    fill_payload = {
        "kind": "FILLED",
        "ig_deal_id": "DIRESUMED",
        "fill_price": 150.0,
        "fill_ts_utc": "2026-04-16T14:45:00",
        "stake_gbp_per_pt": 0.5,
        "initial_stop_price": 145.0,
        "initial_risk_gbp": 2.5,
    }
    _insert_event(
        database,
        candidate_id=cid,
        session_id=prior,
        event_type="FILLED",
        payload=fill_payload,
    )
    _insert_event(
        database,
        candidate_id=cid,
        session_id=prior,
        event_type="STOP_MOVED",
        payload={
            "kind": "STOP_MOVED",
            "reason": "TRAIL_ARM",
            "old_stop": 145.0,
            "new_stop": 151.0,
            "old_trail_step_count": 0,
            "new_trail_step_count": 1,
            "old_locked_gbp": 0.0,
            "new_locked_gbp": 1.0,
            "peak_pnl_gbp_at_move": 25.0,
        },
    )
    _insert_snapshot(database, cid, prior)
    database.conn.commit()

    live_positions = [
        {
            "deal_id": "DIRESUMED",
            "deal_reference": "REFRESUMED",
            "size": 0.5,
            "direction": "BUY",
            "fill_price": 150.0,
            "stop_level": 151.5,
            "created_date_utc": "2026-04-16T14:45:00",
            "epic": "IX.D.AAPL.CASH.IP",
            "currency": "GBP",
        }
    ]

    # Real SessionWriter — inserts a new session row and gets a real session_id.
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="test-sha",
    ) as writer:
        new_session_id = writer.session_id
        result = rehydrate_open_positions(
            database=database,
            live_positions=live_positions,
            new_session_id=new_session_id,
            market_data=_FakeMarketData("IX.D.AAPL.CASH.IP"),
            writer=writer,
            broker_mode=BrokerMode.DEMO,
            rule_set_version="test-sha",
        )

    assert len(result) == 1

    # Exactly one POSITION_RESUMED row, scoped to the new session and the
    # resumed candidate.
    rows = database.conn.execute(
        """
        SELECT event_type, candidate_id, session_id, actor,
               broker_mode, rule_set_version, payload_json
        FROM candidate_events
        WHERE event_type = 'POSITION_RESUMED'
        """
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["candidate_id"] == cid
    assert row["session_id"] == new_session_id
    assert row["actor"] == "EXECUTOR"
    assert row["broker_mode"] == "DEMO"
    assert row["rule_set_version"] == "test-sha"

    payload = json.loads(row["payload_json"])
    assert payload["kind"] == "POSITION_RESUMED"
    assert payload["ig_deal_id"] == "DIRESUMED"
    assert payload["fill_price"] == pytest.approx(150.0)
    assert payload["stake_gbp_per_pt"] == pytest.approx(0.5)
    assert payload["initial_stop_price"] == pytest.approx(145.0)
    # IG stopLevel wins → current_stop = 151.5, source = IG_STOP_LEVEL.
    assert payload["current_stop_price"] == pytest.approx(151.5)
    assert payload["source_of_stop"] == "IG_STOP_LEVEL"
    # Trail ratchet carried over from STOP_MOVED.
    assert payload["peak_pnl_gbp_at_resume"] == pytest.approx(25.0)
    assert payload["trail_step_count"] == 1
    assert payload["trail_mode_activated"] is True
    # sessions_held = 1 prior + 1 current = 2.
    assert payload["sessions_held"] == 2
    assert payload["prior_fill_session_id"] == prior


def test_rehydrate_without_writer_does_not_emit_events(database):
    """When writer=None (default), no POSITION_RESUMED rows are written.

    Important for unit tests and for callers that have a reason to skip the
    event emission (e.g. dry-run resume)."""
    prior = "sess-prior"
    new = "sess-new"
    cid = "cand-no-emit"
    scan = "scan-no-emit"

    _insert_session(database, prior)
    _insert_session(database, new)
    _insert_scan(database, scan, prior)
    _insert_shortlist(database, cid, scan, prior)
    _insert_event(
        database,
        candidate_id=cid,
        session_id=prior,
        event_type="FILLED",
        payload={
            "kind": "FILLED",
            "ig_deal_id": "DISILENT",
            "fill_price": 200.0,
            "fill_ts_utc": "2026-04-16T14:45:00",
            "stake_gbp_per_pt": 0.5,
            "initial_stop_price": 195.0,
            "initial_risk_gbp": 2.5,
        },
    )
    database.conn.commit()

    live_positions = [
        {
            "deal_id": "DISILENT",
            "deal_reference": "REFSILENT",
            "size": 0.5,
            "direction": "BUY",
            "fill_price": 200.0,
            "stop_level": 195.0,
            "created_date_utc": "2026-04-16T14:45:00",
            "epic": "IX.D.X.CASH.IP",
            "currency": "GBP",
        }
    ]

    result = rehydrate_open_positions(
        database=database,
        live_positions=live_positions,
        new_session_id=new,
        market_data=_FakeMarketData(),
    )
    assert len(result) == 1

    rows = database.conn.execute(
        "SELECT COUNT(*) AS n FROM candidate_events WHERE event_type = 'POSITION_RESUMED'"
    ).fetchone()
    assert rows["n"] == 0


def test_rehydrate_event_reports_fill_initial_when_no_stop_available(database):
    """If IG reports no stopLevel and the DB has no STOP_MOVED, the event
    should mark source_of_stop = FILL_INITIAL."""
    prior = "sess-prior"
    cid = "cand-no-stop"
    scan = "scan-no-stop"

    _insert_session(database, prior)
    _insert_scan(database, scan, prior)
    _insert_shortlist(database, cid, scan, prior)
    _insert_event(
        database,
        candidate_id=cid,
        session_id=prior,
        event_type="FILLED",
        payload={
            "kind": "FILLED",
            "ig_deal_id": "DIBARE",
            "fill_price": 100.0,
            "fill_ts_utc": "2026-04-16T14:45:00",
            "stake_gbp_per_pt": 0.5,
            "initial_stop_price": 95.0,
            "initial_risk_gbp": 2.5,
        },
    )
    database.conn.commit()

    live_positions = [
        {
            "deal_id": "DIBARE",
            "deal_reference": "REFBARE",
            "size": 0.5,
            "direction": "BUY",
            "fill_price": 100.0,
            "stop_level": None,  # IG has no guaranteed-stop record
            "created_date_utc": "2026-04-16T14:45:00",
            "epic": "IX.D.X.CASH.IP",
            "currency": "GBP",
        }
    ]

    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="",
    ) as writer:
        rehydrate_open_positions(
            database=database,
            live_positions=live_positions,
            new_session_id=writer.session_id,
            market_data=_FakeMarketData(),
            writer=writer,
            broker_mode=BrokerMode.DEMO,
        )

    row = database.conn.execute(
        "SELECT payload_json FROM candidate_events WHERE event_type = 'POSITION_RESUMED'"
    ).fetchone()
    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["source_of_stop"] == "FILL_INITIAL"
    assert payload["current_stop_price"] == pytest.approx(95.0)
    assert payload["trail_step_count"] == 0
    assert payload["trail_mode_activated"] is False
