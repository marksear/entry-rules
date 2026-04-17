"""
Unit tests for src.reporting.journal.write_session_journal.

Builds a small in-memory SQLite with a hand-crafted session + fills + exits
and asserts the generated markdown has the right structure and counts.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path

import pytest

from src.logging_mod.db import Database
from src.reporting.journal import write_session_journal


@pytest.fixture
def database(tmp_path: Path):
    db = Database(db_path=str(tmp_path / "journal_test.db"))
    db.initialize()
    try:
        yield db
    finally:
        db.close()


def _insert_session(
    db: Database,
    session_id: str,
    *,
    opened: datetime,
    closed: datetime | None = None,
    label: str = "US_REGULAR",
) -> None:
    db.conn.execute(
        """
        INSERT INTO sessions (
            session_id, session_date, session_label, broker_mode,
            account_size_gbp, opened_at_utc, closed_at_utc,
            rule_set_version, schema_version
        ) VALUES (?, ?, ?, 'DEMO', 1000.0, ?, ?, 'test-v1', 1)
        """,
        (
            session_id,
            date.today().isoformat(),
            label,
            opened.isoformat(),
            closed.isoformat() if closed else None,
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
    symbol: str = "AAPL",
    direction: str = "LONG",
    grade: str = "B",
) -> None:
    db.conn.execute(
        """
        INSERT INTO shortlist_entries (
            candidate_id, scan_id, session_id, symbol, market, direction,
            setup_type, grade, trigger_low, trigger_high, stop_price,
            planned_stake_gbp_per_pt, planned_risk_gbp, planned_risk_pct_account,
            broker_mode, created_at_utc, schema_version
        ) VALUES (?, ?, ?, ?, 'US', ?, 'L-A', ?, 100.0, 100.5, 95.0, 0.5, 5.0, 0.005,
                  'DEMO', ?, 1)
        """,
        (
            candidate_id,
            scan_id,
            session_id,
            symbol,
            direction,
            grade,
            datetime.utcnow().isoformat(),
        ),
    )


def _insert_event(
    db: Database,
    *,
    session_id: str,
    candidate_id: str | None,
    event_type: str,
    payload: dict,
    terminal_reason: str | None = None,
    reason_code: str | None = None,
    ts_utc: datetime | None = None,
) -> None:
    db.conn.execute(
        """
        INSERT INTO candidate_events (
            event_id, session_id, candidate_id, ts_utc, event_type, actor,
            payload_json, terminal_reason, reason_code, broker_mode, schema_version
        ) VALUES (?, ?, ?, ?, ?, 'MONITOR', ?, ?, ?, 'DEMO', 1)
        """,
        (
            str(uuid.uuid4()),
            session_id,
            candidate_id,
            (ts_utc or datetime.utcnow()).isoformat(),
            event_type,
            json.dumps(payload),
            terminal_reason,
            reason_code,
        ),
    )


def test_journal_header_and_summary_for_empty_session(database, tmp_path):
    sid = "sess-empty"
    _insert_session(
        database,
        sid,
        opened=datetime(2026, 4, 17, 14, 30),
        closed=datetime(2026, 4, 17, 21, 0),
    )
    database.conn.commit()

    path = write_session_journal(database, sid, tmp_path / "reports")
    content = path.read_text()

    assert path.name.startswith("journal_")
    assert "US_REGULAR" in path.name
    # Header
    assert f"session_id: `{sid}`" in content
    assert "broker_mode: `DEMO`" in content
    assert "account_size_gbp: £1000.00" in content
    assert "duration: 390.0 min" in content
    # Summary zeros
    assert "shortlisted: 0" in content
    assert "fired:       0" in content
    assert "filled:      0" in content
    assert "realised_pnl_gbp: £+0.00" in content
    # Empty-section placeholders
    assert "_No fills this session._" in content
    assert "_None — all fills exited during this session._" in content
    assert "_No exits this session._" in content
    assert "_No ENTRY_EVALUATED_NO_ENTER events._" in content
    assert "_None._" in content


def test_journal_renders_fill_exit_and_reject_rows(database, tmp_path):
    sid = "sess-rich"
    scan = "scan-1"
    cand_win = "cand-win"
    cand_loss = "cand-loss"
    cand_noentry = "cand-noentry"

    _insert_session(
        database,
        sid,
        opened=datetime(2026, 4, 17, 14, 30),
        closed=datetime(2026, 4, 17, 21, 0),
    )
    _insert_scan(database, scan, sid)
    _insert_shortlist(database, cand_win, scan, sid, symbol="AAPL", direction="LONG")
    _insert_shortlist(database, cand_loss, scan, sid, symbol="NVDA", direction="LONG")
    _insert_shortlist(
        database, cand_noentry, scan, sid, symbol="TSLA", direction="LONG"
    )

    # Fill + win on AAPL (closed at trail exit).
    _insert_event(
        database,
        session_id=sid,
        candidate_id=cand_win,
        event_type="FILLED",
        payload={
            "kind": "FILLED",
            "ig_deal_id": "DIWIN",
            "fill_price": 150.0,
            "fill_ts_utc": "2026-04-17T14:50:00",
            "stake_gbp_per_pt": 0.5,
            "initial_stop_price": 145.0,
            "initial_risk_gbp": 2.5,
        },
        ts_utc=datetime(2026, 4, 17, 14, 50),
    )
    _insert_event(
        database,
        session_id=sid,
        candidate_id=cand_win,
        event_type="TRAIL_EXIT",
        payload={
            "kind": "TRAIL_EXIT",
            "trail_stop_price": 165.0,
            "fill_price": 150.0,
            "peak_pnl_gbp": 40.0,
            "realised_pnl_gbp": 7.5,
        },
        terminal_reason="TRAIL_EXIT",
        ts_utc=datetime(2026, 4, 17, 18, 30),
    )

    # Fill + loss on NVDA (stop hit).
    _insert_event(
        database,
        session_id=sid,
        candidate_id=cand_loss,
        event_type="FILLED",
        payload={
            "kind": "FILLED",
            "ig_deal_id": "DILOSS",
            "fill_price": 500.0,
            "fill_ts_utc": "2026-04-17T15:00:00",
            "stake_gbp_per_pt": 0.5,
            "initial_stop_price": 490.0,
            "initial_risk_gbp": 5.0,
        },
        ts_utc=datetime(2026, 4, 17, 15, 0),
    )
    _insert_event(
        database,
        session_id=sid,
        candidate_id=cand_loss,
        event_type="STOP_HIT",
        payload={
            "kind": "STOP_HIT",
            "stop_price": 490.0,
            "fill_price": 500.0,
            "realised_pnl_gbp": -5.0,
        },
        terminal_reason="STOP_HIT",
        ts_utc=datetime(2026, 4, 17, 15, 30),
    )

    # TSLA: no entry (3 rejects, mix of reason codes)
    for code in ("R_CHASE_LIMIT", "R_CHASE_LIMIT", "R_GATE_FLIPPED"):
        _insert_event(
            database,
            session_id=sid,
            candidate_id=cand_noentry,
            event_type="ENTRY_EVALUATED_NO_ENTER",
            payload={"kind": "ENTRY_EVALUATED_NO_ENTER"},
            reason_code=code,
        )
    database.conn.commit()

    path = write_session_journal(database, sid, tmp_path / "reports")
    content = path.read_text()

    # Counts
    assert "shortlisted: 3" in content
    assert "filled:      2" in content
    # P&L = 7.5 - 5.0 = 2.5
    assert "realised_pnl_gbp: £+2.50" in content
    # Fill rows
    assert "AAPL" in content and "150.00" in content
    assert "NVDA" in content and "500.00" in content
    assert "TRAIL_EXIT" in content
    assert "STOP_HIT" in content
    # Reject histogram
    assert "- R_CHASE_LIMIT: 2" in content
    assert "- R_GATE_FLIPPED: 1" in content
    # Exit-reason breakdown
    assert "- TRAIL_EXIT: 1" in content
    assert "- STOP_HIT: 1" in content


def test_journal_raises_on_missing_session(database, tmp_path):
    with pytest.raises(ValueError, match="No sessions row"):
        write_session_journal(database, "nonexistent", tmp_path)


def test_journal_flags_open_at_close(database, tmp_path):
    sid = "sess-open"
    scan = "scan-2"
    cand_open = "cand-open"

    _insert_session(
        database,
        sid,
        opened=datetime(2026, 4, 17, 14, 30),
        closed=datetime(2026, 4, 17, 21, 0),
    )
    _insert_scan(database, scan, sid)
    _insert_shortlist(database, cand_open, scan, sid, symbol="MSFT")
    _insert_event(
        database,
        session_id=sid,
        candidate_id=cand_open,
        event_type="FILLED",
        payload={
            "kind": "FILLED",
            "ig_deal_id": "DIOPEN",
            "fill_price": 400.0,
            "fill_ts_utc": "2026-04-17T15:00:00",
            "stake_gbp_per_pt": 0.5,
            "initial_stop_price": 395.0,
            "initial_risk_gbp": 2.5,
        },
    )
    database.conn.commit()

    path = write_session_journal(database, sid, tmp_path / "reports")
    content = path.read_text()

    # Fill table shows OPEN (no exit row)
    assert "OPEN" in content
    # Open-at-close table has MSFT
    assert "MSFT" in content
    assert "DIOPEN" in content
    assert "open at close:    1" in content
