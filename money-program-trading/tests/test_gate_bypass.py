"""
Tests for the gate_bypass path — a user-curated, mechanics-test shortlist.

Covers:

- ScanRecord pydantic validation (bypass=True requires bypass_until)
- ingest_scan refuses scans where bypass_until has expired
- ingest_scan refuses bypass=True on a LIVE session (DEMO-only)
- SessionWriter exposes bypass attrs on the instance after ingest
- GateBypassActivePayload is emittable via the discriminated union
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.logging_mod import Database, SessionWriter
from src.models import (
    ActorKind,
    BrokerMode,
    CandidateEvent,
    CandidateGrade,
    Direction,
    EntryType,
    EventType,
    Market,
    PillarVotes,
    RegimeSnapshot,
    RegimeState,
    ScanRecord,
    SessionLabel,
    ShortlistEntry,
)
from src.models.candidate_event import GateBypassActivePayload

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def database(tmp_path: Path):
    db = Database(db_path=str(tmp_path / "test_bypass.db"))
    db.initialize()
    try:
        yield db
    finally:
        db.close()


def _make_scan_payload(
    *,
    gate_bypass: bool,
    bypass_until: date | None,
    broker_mode: BrokerMode = BrokerMode.DEMO,
    symbols: tuple[str, ...] = ("OXY",),
) -> tuple[ScanRecord, list[ShortlistEntry]]:
    scan = ScanRecord(
        scanned_at_utc=datetime(2026, 4, 17, 13, 30, 0),
        universe_size=len(symbols),
        broker_mode=broker_mode,
        regime=RegimeSnapshot(regime=RegimeState.GREEN, regime_score=0.7),
        scored_universe=[],  # not needed for these tests
        scanner_version="sha-test",
        gate_bypass=gate_bypass,
        bypass_until=bypass_until,
    )
    entries = [
        ShortlistEntry(
            scan_id=scan.scan_id,
            symbol=sym,
            market=Market.US,
            direction=Direction.LONG,
            setup_type=EntryType.L_A,
            grade=CandidateGrade.B,
            trigger_low=100.0,
            trigger_high=100.5,
            stop_price=98.0,
            target_price=105.0,
            planned_stake_gbp_per_pt=0.50,
            planned_risk_gbp=1.0,
            planned_risk_pct_account=0.005,
            pillar_votes=PillarVotes(
                livermore=True,
                oneil=True,
                minervini=True,
                darvas=False,
                raschke=False,
                weinstein=False,
            ),
            broker_mode=broker_mode,
        )
        for sym in symbols
    ]
    return scan, entries


def _write_payload(tmp_path: Path, scan: ScanRecord, entries: list[ShortlistEntry]) -> Path:
    payload = {
        "schema_version": 1,
        "scan_record": scan.model_dump(mode="json"),
        "shortlist_entries": [e.model_dump(mode="json") for e in entries],
    }
    path = tmp_path / f"scan_{scan.scan_id[:8]}.json"
    path.write_text(json.dumps(payload, default=str), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Pydantic-level validation
# ---------------------------------------------------------------------------


def test_scan_bypass_true_requires_bypass_until():
    with pytest.raises(ValidationError) as exc_info:
        ScanRecord(
            scanned_at_utc=datetime(2026, 4, 17, 13, 30),
            universe_size=0,
            broker_mode=BrokerMode.DEMO,
            regime=RegimeSnapshot(regime=RegimeState.GREEN),
            gate_bypass=True,
            bypass_until=None,
        )
    assert "bypass_until" in str(exc_info.value)


def test_scan_bypass_false_default_shape_validates():
    # Baseline: no bypass fields supplied → scan validates fine, both fields
    # default to their off values. Proves we didn't break existing scans.
    scan = ScanRecord(
        scanned_at_utc=datetime(2026, 4, 17, 13, 30),
        universe_size=0,
        broker_mode=BrokerMode.DEMO,
        regime=RegimeSnapshot(regime=RegimeState.GREEN),
    )
    assert scan.gate_bypass is False
    assert scan.bypass_until is None


def test_scan_bypass_true_with_future_date_validates():
    scan = ScanRecord(
        scanned_at_utc=datetime(2026, 4, 17, 13, 30),
        universe_size=0,
        broker_mode=BrokerMode.DEMO,
        regime=RegimeSnapshot(regime=RegimeState.GREEN),
        gate_bypass=True,
        bypass_until=date(2026, 5, 7),
    )
    assert scan.gate_bypass is True
    assert scan.bypass_until == date(2026, 5, 7)


# ---------------------------------------------------------------------------
# ingest_scan enforcement
# ---------------------------------------------------------------------------


def test_ingest_bypass_active_populates_writer_attrs(database: Database, tmp_path: Path):
    scan, entries = _make_scan_payload(
        gate_bypass=True,
        bypass_until=date.today() + timedelta(days=5),
        symbols=("OXY", "CVX"),
    )
    scan_path = _write_payload(tmp_path, scan, entries)

    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=10000.0,
        rule_set_version="sha-test",
    ) as writer:
        writer.ingest_scan(scan_path)
        assert writer.gate_bypass is True
        assert writer.bypass_until == date.today() + timedelta(days=5)
        assert writer.bypass_candidate_count == 2


def test_ingest_refuses_expired_bypass(database: Database, tmp_path: Path):
    scan, entries = _make_scan_payload(
        gate_bypass=True,
        bypass_until=date.today() - timedelta(days=1),  # expired yesterday
    )
    scan_path = _write_payload(tmp_path, scan, entries)

    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=10000.0,
        rule_set_version="sha-test",
    ) as writer:
        with pytest.raises(ValueError, match="bypass expired"):
            writer.ingest_scan(scan_path)


def test_ingest_refuses_bypass_on_live_session(database: Database, tmp_path: Path):
    # bypass=True is DEMO-only. If someone manages to emit a LIVE scan with
    # gate_bypass on (they shouldn't — swing-committee's UI gates this), we
    # refuse at ingest as a belt-and-braces defence.
    scan, entries = _make_scan_payload(
        gate_bypass=True,
        bypass_until=date.today() + timedelta(days=5),
        broker_mode=BrokerMode.LIVE,
    )
    scan_path = _write_payload(tmp_path, scan, entries)

    with SessionWriter(
        database,
        broker_mode=BrokerMode.LIVE,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=10000.0,
        rule_set_version="sha-test",
    ) as writer:
        with pytest.raises(ValueError, match="only permitted with broker_mode=DEMO"):
            writer.ingest_scan(scan_path)


def test_ingest_bypass_off_leaves_writer_defaults(database: Database, tmp_path: Path):
    scan, entries = _make_scan_payload(gate_bypass=False, bypass_until=None)
    scan_path = _write_payload(tmp_path, scan, entries)

    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=10000.0,
        rule_set_version="sha-test",
    ) as writer:
        writer.ingest_scan(scan_path)
        assert writer.gate_bypass is False
        assert writer.bypass_until is None


# ---------------------------------------------------------------------------
# Event payload round-trip
# ---------------------------------------------------------------------------


def test_gate_bypass_active_payload_round_trip():
    """GateBypassActivePayload must serialise/deserialise through the
    discriminated union on its `kind` field, alongside every other payload."""
    payload = GateBypassActivePayload(
        bypass_until=date(2026, 5, 7),
        selected_candidate_count=3,
        scan_id=str(uuid4()),
    )
    event = CandidateEvent(
        id=str(uuid4()),
        session_id=str(uuid4()),
        candidate_id=None,  # session-level event
        ts_utc=datetime.utcnow(),
        event_type=EventType.GATE_BYPASS_ACTIVE,
        actor=ActorKind.INGESTER,
        payload=payload,
        broker_mode=BrokerMode.DEMO,
    )
    dumped = event.model_dump(mode="json")
    rehydrated = CandidateEvent.model_validate(dumped)
    assert rehydrated.event_type == EventType.GATE_BYPASS_ACTIVE
    assert rehydrated.payload.kind == "GATE_BYPASS_ACTIVE"
    assert rehydrated.payload.bypass_until == date(2026, 5, 7)
    assert rehydrated.payload.selected_candidate_count == 3
