"""
Integration tests for SessionWriter — end-to-end write-path coverage against
a temporary SQLite DB. The goal is to catch schema drift between the pydantic
models and the v2 tables in db.py the moment it happens.

Each test exercises:
- DB initialisation + idempotent re-open
- Session insert on __enter__
- Session close stamp on __exit__
- Scan ingest from a swing-committee-shaped JSON file (via pydantic round-trip)
- Snapshot + event writes, including discriminated-payload JSON round-trip
- Refusal to co-mingle DEMO/LIVE scans with a mismatched session
- Transaction rollback on bad batch input
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from src.logging_mod import Database, SessionWriter
from src.models import (
    ActorKind,
    BrokerMode,
    CandidateEvent,
    CandidateGrade,
    CandidateSnapshot,
    CandidateStatus,
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
    StopMovedPayload,
    StopMoveReason,
    TriggerFiredPayload,
    UniverseScoreEntry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_trading.db")


@pytest.fixture
def database(db_path: str):
    db = Database(db_path=db_path)
    db.initialize()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def scan_json_path(tmp_path: Path) -> Path:
    """A minimal but valid scan handoff file, matching the shape emitted by
    swing-committee's `lib/scanEmission.js buildScanPayload`."""
    scan = ScanRecord(
        scanned_at_utc=datetime(2026, 4, 16, 13, 30, 0),
        universe_size=3,
        broker_mode=BrokerMode.DEMO,
        regime=RegimeSnapshot(
            regime=RegimeState.GREEN,
            regime_score=0.72,
            vix_level=14.5,
            breadth_us=0.61,
            breadth_uk=0.54,
            notes="",
        ),
        scored_universe=[
            UniverseScoreEntry(
                symbol="OXY",
                market="US",
                price=62.40,
                currency="USD",
                pillar_pass_count=5,
                pillar_bitmap=0b011111,
                day1_score=0.82,
                day1_tier="A-GRADE",
                grade="A",
                shortlisted=True,
            ),
            UniverseScoreEntry(
                symbol="TSLA",
                market="US",
                price=180.0,
                currency="USD",
                pillar_pass_count=2,
                pillar_bitmap=0b000011,
                grade=None,
                shortlisted=False,
                rejection_reason="Insufficient pillars",
                rejection_code="R_PILLARS_BELOW_3",
            ),
            UniverseScoreEntry(
                symbol="VOD.L",
                market="UK",
                price=75.0,
                currency="GBP",
                pillar_pass_count=6,
                pillar_bitmap=0b111111,
                day1_score=0.91,
                day1_tier="A-GRADE",
                grade="A+",
                shortlisted=True,
            ),
        ],
        scanner_version="abc1234",
    )

    shortlist = [
        ShortlistEntry(
            scan_id=scan.scan_id,
            symbol="OXY",
            market=Market.US,
            direction=Direction.LONG,
            setup_type=EntryType.L_A,
            grade=CandidateGrade.A,
            trigger_low=62.40,
            trigger_high=62.55,
            stop_price=60.10,
            target_price=67.0,
            planned_stake_gbp_per_pt=0.10,
            planned_risk_gbp=0.23,
            planned_risk_pct_account=0.0075,
            pillar_votes=PillarVotes(
                livermore=True,
                oneil=True,
                minervini=True,
                darvas=True,
                raschke=True,
                weinstein=False,
            ),
            committee_stance="aligned",
            day1_score=0.82,
            day1_tier="A-GRADE",
            broker_mode=BrokerMode.DEMO,
        ),
        ShortlistEntry(
            scan_id=scan.scan_id,
            symbol="VOD.L",
            market=Market.UK,
            direction=Direction.SHORT,
            setup_type=EntryType.S_E,
            grade=CandidateGrade.A_PLUS,
            trigger_low=75.0,
            trigger_high=75.3,
            stop_price=77.0,
            target_price=70.0,
            planned_stake_gbp_per_pt=0.10,
            planned_risk_gbp=0.17,
            planned_risk_pct_account=0.01,
            pillar_votes=PillarVotes(
                livermore=True,
                oneil=True,
                minervini=True,
                darvas=True,
                raschke=True,
                weinstein=True,
            ),
            broker_mode=BrokerMode.DEMO,
        ),
    ]

    payload = {
        "schema_version": 1,
        "scan_record": scan.model_dump(mode="json"),
        "shortlist_entries": [e.model_dump(mode="json") for e in shortlist],
    }

    path = tmp_path / "scan_20260416.json"
    path.write_text(json.dumps(payload, default=str), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Session open/close
# ---------------------------------------------------------------------------


def test_session_open_inserts_row(database: Database):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
        session_date=date(2026, 4, 16),
    ) as writer:
        assert writer.session_id is not None

        row = database.conn.execute(
            "SELECT session_id, session_date, session_label, broker_mode, "
            "account_size_gbp, opened_at_utc, closed_at_utc, rule_set_version, "
            "schema_version FROM sessions WHERE session_id = ?",
            (writer.session_id,),
        ).fetchone()
        assert row is not None
        assert row["session_date"] == "2026-04-16"
        assert row["session_label"] == "US_REGULAR"
        assert row["broker_mode"] == "DEMO"
        assert row["account_size_gbp"] == 1000.0
        assert row["opened_at_utc"] is not None
        assert row["closed_at_utc"] is None
        assert row["rule_set_version"] == "abc1234"
        assert row["schema_version"] == 1


def test_session_close_stamps_closed_at(database: Database):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        session_id = writer.session_id

    row = database.conn.execute(
        "SELECT closed_at_utc FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert row["closed_at_utc"] is not None


def test_manual_close_idempotent(database: Database):
    writer = SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    )
    with writer:
        writer.close_session(notes="manual close")
        # A second close is a no-op — should not throw, should not change notes.
        writer.close_session()

    row = database.conn.execute(
        "SELECT closed_at_utc, notes FROM sessions WHERE session_id = ?",
        (writer.session_id,),
    ).fetchone()
    assert row["closed_at_utc"] is not None
    assert row["notes"] == "manual close"


# ---------------------------------------------------------------------------
# Scan ingest
# ---------------------------------------------------------------------------


def test_ingest_scan_populates_scans_universe_and_shortlist(
    database: Database, scan_json_path: Path
):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        scan_id = writer.ingest_scan(scan_json_path)
        assert scan_id

        # scans row stamped with session_id
        scan_row = database.conn.execute(
            "SELECT scan_id, session_id, regime, regime_score, universe_size, broker_mode "
            "FROM scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        assert scan_row["session_id"] == writer.session_id
        assert scan_row["regime"] == "GREEN"
        assert scan_row["regime_score"] == pytest.approx(0.72)
        assert scan_row["universe_size"] == 3
        assert scan_row["broker_mode"] == "DEMO"

        # scan_universe has 3 rows, 2 shortlisted
        universe = database.conn.execute(
            "SELECT symbol, shortlisted, grade, rejection_code "
            "FROM scan_universe WHERE scan_id = ? ORDER BY symbol",
            (scan_id,),
        ).fetchall()
        assert len(universe) == 3
        by_symbol = {r["symbol"]: r for r in universe}
        assert by_symbol["OXY"]["shortlisted"] == 1
        assert by_symbol["OXY"]["grade"] == "A"
        assert by_symbol["TSLA"]["shortlisted"] == 0
        assert by_symbol["TSLA"]["rejection_code"] == "R_PILLARS_BELOW_3"
        assert by_symbol["VOD.L"]["shortlisted"] == 1
        assert by_symbol["VOD.L"]["grade"] == "A+"

        # shortlist_entries has 2 rows, both stamped with session_id
        shortlist = database.conn.execute(
            "SELECT symbol, direction, setup_type, grade, session_id, scan_id, "
            "pv_livermore, pv_weinstein, planned_risk_pct_account "
            "FROM shortlist_entries WHERE scan_id = ? ORDER BY symbol",
            (scan_id,),
        ).fetchall()
        assert len(shortlist) == 2
        by_symbol = {r["symbol"]: r for r in shortlist}
        assert by_symbol["OXY"]["direction"] == "LONG"
        assert by_symbol["OXY"]["setup_type"] == "L-A"
        assert by_symbol["OXY"]["pv_weinstein"] == 0
        assert by_symbol["OXY"]["planned_risk_pct_account"] == pytest.approx(0.0075)
        assert by_symbol["VOD.L"]["direction"] == "SHORT"
        assert by_symbol["VOD.L"]["setup_type"] == "S-E"
        assert by_symbol["VOD.L"]["pv_weinstein"] == 1
        assert by_symbol["VOD.L"]["session_id"] == writer.session_id

        # sessions.scan_id has been stamped
        sess_scan_id = database.conn.execute(
            "SELECT scan_id FROM sessions WHERE session_id = ?",
            (writer.session_id,),
        ).fetchone()["scan_id"]
        assert sess_scan_id == scan_id


def test_ingest_scan_rejects_broker_mode_mismatch(database: Database, scan_json_path: Path):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.LIVE,  # session is LIVE, scan is DEMO
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        with pytest.raises(ValueError, match="broker_mode"):
            writer.ingest_scan(scan_json_path)

        # Nothing persisted — rollback worked
        scan_count = database.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        assert scan_count == 0
        universe_count = database.conn.execute(
            "SELECT COUNT(*) FROM scan_universe"
        ).fetchone()[0]
        assert universe_count == 0


def test_ingest_scan_rejects_malformed_handoff(database: Database, tmp_path: Path):
    bad = tmp_path / "broken.json"
    bad.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        with pytest.raises(ValueError, match="missing"):
            writer.ingest_scan(bad)


def test_ingest_scan_roundtrips_shortlist_via_pydantic(
    database: Database, scan_json_path: Path
):
    """Round-trip: JSON -> ingest -> read back columns -> rebuild ShortlistEntry."""
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        scan_id = writer.ingest_scan(scan_json_path)

        row = database.conn.execute(
            "SELECT * FROM shortlist_entries WHERE scan_id = ? AND symbol = 'OXY'",
            (scan_id,),
        ).fetchone()

        rebuilt = ShortlistEntry(
            candidate_id=row["candidate_id"],
            scan_id=row["scan_id"],
            session_id=row["session_id"],
            symbol=row["symbol"],
            market=row["market"],
            direction=row["direction"],
            setup_type=row["setup_type"],
            grade=row["grade"],
            trigger_low=row["trigger_low"],
            trigger_high=row["trigger_high"],
            stop_price=row["stop_price"],
            target_price=row["target_price"],
            planned_stake_gbp_per_pt=row["planned_stake_gbp_per_pt"],
            planned_risk_gbp=row["planned_risk_gbp"],
            planned_risk_pct_account=row["planned_risk_pct_account"],
            pillar_votes=PillarVotes(
                livermore=bool(row["pv_livermore"]),
                oneil=bool(row["pv_oneil"]),
                minervini=bool(row["pv_minervini"]),
                darvas=bool(row["pv_darvas"]),
                raschke=bool(row["pv_raschke"]),
                weinstein=bool(row["pv_weinstein"]),
            ),
            broker_mode=row["broker_mode"],
            created_at_utc=datetime.fromisoformat(row["created_at_utc"]),
            schema_version=row["schema_version"],
        )
        # Reconstructed model should validate without errors.
        assert rebuilt.symbol == "OXY"
        assert rebuilt.direction == Direction.LONG
        assert rebuilt.setup_type == EntryType.L_A
        assert rebuilt.pillar_votes.count == 5


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def _make_snapshot(session_id: str, candidate_id: str, scan_id: str, **overrides):
    defaults = dict(
        session_id=session_id,
        candidate_id=candidate_id,
        scan_id=scan_id,
        symbol="OXY",
        market=Market.US,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.A,
        ts_utc=datetime(2026, 4, 16, 13, 31, 0),
        minute_bucket=202604161331,
        status=CandidateStatus.PENDING_TRIGGER,
        broker_mode=BrokerMode.DEMO,
        last_price=62.25,
        bid=62.24,
        ask=62.26,
    )
    defaults.update(overrides)
    return CandidateSnapshot(**defaults)


def test_write_snapshot_persists_fields(database: Database, scan_json_path: Path):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        writer.ingest_scan(scan_json_path)
        candidate_id = database.conn.execute(
            "SELECT candidate_id FROM shortlist_entries WHERE symbol = 'OXY'"
        ).fetchone()["candidate_id"]
        scan_id = writer.scan_id
        snap = _make_snapshot(
            writer.session_id,
            candidate_id,
            scan_id,
            status=CandidateStatus.TRIGGERED_OPEN,
            fill_price=62.48,
            fill_ts_utc=datetime(2026, 4, 16, 13, 45, 0),
            current_stake_gbp_per_pt=0.10,
            current_stop_price=60.10,
            unrealised_pnl_gbp=2.5,
            peak_unrealised_pnl_gbp=3.1,
            trail_step_count=1,
            trail_mode_active=True,
            invalidation_window_active=False,
            elapsed_mins_in_position=14,
            mcl_regime=RegimeState.GREEN,
        )
        writer.write_snapshot(snap)

        row = database.conn.execute(
            "SELECT symbol, status, trigger_armed, entry_price, current_stop, "
            "unrealised_pnl_gbp, peak_unrealised_pnl_gbp, trail_step_count, "
            "trail_mode_active, invalidation_window_active, regime "
            "FROM candidate_snapshots WHERE session_id = ?",
            (writer.session_id,),
        ).fetchone()
        assert row["symbol"] == "OXY"
        assert row["status"] == "TRIGGERED_OPEN"
        assert row["trigger_armed"] == 1
        assert row["entry_price"] == pytest.approx(62.48)
        assert row["current_stop"] == pytest.approx(60.10)
        assert row["unrealised_pnl_gbp"] == pytest.approx(2.5)
        assert row["peak_unrealised_pnl_gbp"] == pytest.approx(3.1)
        assert row["trail_step_count"] == 1
        assert row["trail_mode_active"] == 1
        assert row["invalidation_window_active"] == 0
        assert row["regime"] == "GREEN"


def test_write_snapshots_batch(database: Database, scan_json_path: Path):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        writer.ingest_scan(scan_json_path)
        candidate_id = database.conn.execute(
            "SELECT candidate_id FROM shortlist_entries WHERE symbol = 'OXY'"
        ).fetchone()["candidate_id"]
        scan_id = writer.scan_id
        base_ts = datetime(2026, 4, 16, 13, 31, 0)
        snaps = [
            _make_snapshot(
                writer.session_id,
                candidate_id,
                scan_id,
                ts_utc=base_ts + timedelta(minutes=i),
                minute_bucket=202604161331 + i,
            )
            for i in range(5)
        ]
        writer.write_snapshots(snaps)

        count = database.conn.execute(
            "SELECT COUNT(*) FROM candidate_snapshots WHERE session_id = ?",
            (writer.session_id,),
        ).fetchone()[0]
        assert count == 5


def test_write_snapshot_rejects_foreign_session(database: Database):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        snap = _make_snapshot(
            session_id=str(uuid4()),  # mismatched
            candidate_id=str(uuid4()),
            scan_id=str(uuid4()),
        )
        with pytest.raises(ValueError, match="session_id"):
            writer.write_snapshot(snap)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _make_event(session_id: str, candidate_id: str, payload):
    is_stop_moved = payload.kind == "STOP_MOVED"
    event_type = EventType.STOP_MOVED if is_stop_moved else EventType.TRIGGER_FIRED
    actor = ActorKind.TRAIL_MANAGER if is_stop_moved else ActorKind.EXECUTOR
    return CandidateEvent(
        session_id=session_id,
        candidate_id=candidate_id,
        ts_utc=datetime(2026, 4, 16, 13, 46, 0),
        event_type=event_type,
        actor=actor,
        payload=payload,
        broker_mode=BrokerMode.DEMO,
    )


def test_write_event_roundtrips_payload_json(database: Database, scan_json_path: Path):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        writer.ingest_scan(scan_json_path)
        candidate_id = database.conn.execute(
            "SELECT candidate_id FROM shortlist_entries WHERE symbol = 'OXY'"
        ).fetchone()["candidate_id"]

        payload = StopMovedPayload(
            reason=StopMoveReason.TRAIL_STEP,
            old_stop=60.10,
            new_stop=60.60,
            old_trail_step_count=1,
            new_trail_step_count=2,
            old_locked_gbp=1.0,
            new_locked_gbp=6.0,
            peak_pnl_gbp_at_move=26.5,
        )
        event = _make_event(writer.session_id, candidate_id, payload)
        writer.write_event(event)

        row = database.conn.execute(
            "SELECT event_id, event_type, actor, payload_json, broker_mode "
            "FROM candidate_events WHERE session_id = ?",
            (writer.session_id,),
        ).fetchone()
        assert row["event_type"] == "STOP_MOVED"
        assert row["actor"] == "TRAIL_MANAGER"
        assert row["broker_mode"] == "DEMO"

        payload_dict = json.loads(row["payload_json"])
        assert payload_dict["kind"] == "STOP_MOVED"
        assert payload_dict["reason"] == "TRAIL_STEP"
        assert payload_dict["new_trail_step_count"] == 2
        assert payload_dict["new_locked_gbp"] == 6.0


def test_write_events_batch(database: Database, scan_json_path: Path):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        writer.ingest_scan(scan_json_path)
        candidate_id = database.conn.execute(
            "SELECT candidate_id FROM shortlist_entries WHERE symbol = 'OXY'"
        ).fetchone()["candidate_id"]

        fired = TriggerFiredPayload(last_price=62.50, trigger_low=62.40, trigger_high=62.55)
        moved = StopMovedPayload(
            reason=StopMoveReason.TRAIL_ARM,
            old_stop=60.10,
            new_stop=60.10,
            old_trail_step_count=0,
            new_trail_step_count=1,
            old_locked_gbp=0.0,
            new_locked_gbp=1.0,
            peak_pnl_gbp_at_move=25.0,
        )
        events = [
            _make_event(writer.session_id, candidate_id, fired),
            _make_event(writer.session_id, candidate_id, moved),
        ]
        writer.write_events(events)

        count = database.conn.execute(
            "SELECT COUNT(*) FROM candidate_events WHERE session_id = ?",
            (writer.session_id,),
        ).fetchone()[0]
        assert count == 2


def test_write_events_rollback_on_foreign_session(database: Database, scan_json_path: Path):
    """Batch should validate session_ids up front and not write any rows."""
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ) as writer:
        writer.ingest_scan(scan_json_path)
        candidate_id = database.conn.execute(
            "SELECT candidate_id FROM shortlist_entries WHERE symbol = 'OXY'"
        ).fetchone()["candidate_id"]

        good_payload = TriggerFiredPayload(
            last_price=62.50, trigger_low=62.40, trigger_high=62.55
        )
        good = _make_event(writer.session_id, candidate_id, good_payload)
        bad = _make_event(str(uuid4()), candidate_id, good_payload)  # foreign session

        with pytest.raises(ValueError, match="session_id"):
            writer.write_events([good, bad])

        count = database.conn.execute(
            "SELECT COUNT(*) FROM candidate_events"
        ).fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# Database ownership
# ---------------------------------------------------------------------------


def test_exit_does_not_close_database(database: Database):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="abc1234",
    ):
        pass
    # DB is still usable — caller owns it.
    row = database.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    assert row[0] == 1


def test_schema_version_row_inserted(database: Database):
    version = database.conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert version == 2


def test_foreign_key_enforced(database: Database):
    """Inserting a shortlist row with a non-existent scan_id must fail."""
    # raw sqlite so we bypass SessionWriter's transactional ingest
    with pytest.raises(sqlite3.IntegrityError):
        database.conn.execute(
            """
            INSERT INTO shortlist_entries (
                candidate_id, scan_id, symbol, market, direction, setup_type, grade,
                trigger_low, trigger_high, stop_price,
                planned_stake_gbp_per_pt, planned_risk_gbp, planned_risk_pct_account,
                broker_mode, created_at_utc, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cand-1",
                "nonexistent-scan",
                "OXY",
                "US",
                "LONG",
                "L-A",
                "A",
                62.40,
                62.55,
                60.10,
                0.10,
                0.23,
                0.0075,
                "DEMO",
                datetime.utcnow().isoformat(),
                1,
            ),
        )
        database.conn.commit()
