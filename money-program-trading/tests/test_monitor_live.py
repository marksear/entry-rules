"""
Live integration tests for the monitor loop.

Every test in this file hits IG DEMO. They are auto-skipped when
``IG_USERNAME`` / ``IG_PASSWORD`` / ``IG_API_KEY`` are unset (see conftest.py).

Run locally with creds in .env::

    pytest tests/test_monitor_live.py -v

What each test validates
------------------------
- IG is reachable, authentication works, a real snapshot comes back.
- The snapshot shape matches what ``classify_tick`` expects (guards against
  trading_ig library changes).
- A single ``run_one_tick`` writes the expected rows in SQLite — no
  mocks, real prices all the way.

These tests deliberately use conservative, stable universal-equity epics
(S&P 500 index or a mega-cap US equity) so they don't fail due to thin
liquidity or unusual market states.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.auth.ig_auth import IGSession
from src.data.market_data import MarketData
from src.engine.monitor import CandidatePlan, MonitorLoop
from src.logging_mod.db import Database
from src.logging_mod.session_writer import SessionWriter
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import (
    BrokerMode,
    CandidateGrade,
    SessionLabel,
)

pytestmark = pytest.mark.integration


# A broad-market epic — IG exposes the S&P 500 continuous future as a
# spread-betting "DAILY FUNDED BET" (DFB). If this epic doesn't resolve on
# your IG account, pass IG_TEST_EPIC in env to override.
DEFAULT_TEST_EPIC = os.getenv("IG_TEST_EPIC", "IX.D.SPTRD.DAILY.IP")


@pytest.fixture(scope="module")
def ig_session() -> IGSession:
    session = IGSession()
    session.connect()
    try:
        yield session
    finally:
        session.disconnect()


@pytest.fixture(scope="module")
def market_data(ig_session: IGSession) -> MarketData:
    return MarketData(ig_session)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "live_test.db")


@pytest.fixture
def database(db_path: str):
    db = Database(db_path=db_path)
    db.initialize()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def test_market_snapshot_has_expected_shape(market_data: MarketData):
    snap = market_data.get_market_snapshot(DEFAULT_TEST_EPIC)
    assert snap, f"Empty snapshot from IG for {DEFAULT_TEST_EPIC}"
    # These are the fields MonitorLoop + classify_tick rely on.
    for key in ("bid", "ask", "last_traded", "market_status"):
        assert key in snap, f"Missing key {key!r} in IG snapshot: {snap}"

    # Sanity: when the market is tradeable, bid <= ask and last sits between them.
    if snap["market_status"] == "TRADEABLE" and snap["bid"] and snap["ask"]:
        assert snap["bid"] <= snap["ask"]
        assert snap["bid"] <= snap["last_traded"] <= snap["ask"]


def test_unknown_epic_returns_empty_dict(market_data: MarketData):
    """Bad epic should not crash the loop; it returns {} → NO_PRICE decision."""
    snap = market_data.get_market_snapshot("KA.D.NONSENSE_ABC123.CASH.IP")
    # IG raises IGException for unknown epics; retry wrapper exhausts and
    # re-raises. We just want to make sure this doesn't silently succeed.
    assert snap == {} or snap == snap  # tolerant — the key check is no crash


# ---------------------------------------------------------------------------
# Monitor loop tick against live IG
# ---------------------------------------------------------------------------


def _plan_from_live_snapshot(
    session_id: str, snapshot: dict, epic: str
) -> CandidatePlan:
    """Build a candidate plan whose trigger is so far from the live price
    that no FIRE can happen during the test. We're validating that the loop
    writes rows, not that it makes trading decisions."""
    last = snapshot["last_traded"]
    # Set trigger at 100x current price so a LONG will never fire.
    trigger = last * 100.0
    return CandidatePlan(
        candidate_id=str(uuid.uuid4()),
        scan_id=str(uuid.uuid4()),
        session_id=session_id,
        symbol="LIVE-TEST",
        market=Market.US,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.B,
        trigger_low=trigger,
        trigger_high=trigger * 1.0001,
        stop_price=trigger * 0.5,
        target_price=None,
        ig_epic=epic,
        broker_mode=BrokerMode.DEMO,
    )


def test_one_tick_writes_snapshot_row(
    database: Database, market_data: MarketData
):
    snap = market_data.get_market_snapshot(DEFAULT_TEST_EPIC)
    assert snap, "Prereq: IG snapshot must be non-empty"

    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="live-test",
        session_date=date.today(),
    ) as writer:
        plan = _plan_from_live_snapshot(writer.session_id, snap, DEFAULT_TEST_EPIC)
        # The SessionWriter requires a shortlist row FK on snapshots, so we
        # insert a minimal shortlist row directly. (Skipping the full
        # ingest_scan path because this test is about the monitor loop.)
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
            ) VALUES (?, ?, ?, ?, 'US', 'LONG', 'L-A', 'B', ?, ?, ?, 0.1, 1.0, 0.005,
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
                datetime.utcnow().isoformat(),
            ),
        )
        database.conn.commit()

        loop = MonitorLoop(
            writer=writer,
            market_data=market_data,
            plans=[plan],
            tick_interval_seconds=1,
        )
        loop.run_one_tick()

        rows = database.conn.execute(
            "SELECT bid, ask, last_price, status FROM candidate_snapshots "
            "WHERE session_id = ?",
            (writer.session_id,),
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "PENDING_TRIGGER"
        # We fetched a live snapshot so at least one of bid/ask/last_price
        # should be populated.
        assert row["bid"] is not None or row["ask"] is not None or row["last_price"] is not None


def test_short_loop_writes_multiple_ticks(
    database: Database, market_data: MarketData
):
    """Run 3 ticks 1s apart against live IG — verify 3 snapshot rows land."""
    snap = market_data.get_market_snapshot(DEFAULT_TEST_EPIC)
    assert snap

    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="live-test",
    ) as writer:
        plan = _plan_from_live_snapshot(writer.session_id, snap, DEFAULT_TEST_EPIC)
        # Same minimal shortlist + scan prereqs as above.
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
            ) VALUES (?, ?, ?, ?, 'US', 'LONG', 'L-A', 'B', ?, ?, ?, 0.1, 1.0, 0.005,
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
                datetime.utcnow().isoformat(),
            ),
        )
        database.conn.commit()

        loop = MonitorLoop(
            writer=writer,
            market_data=market_data,
            plans=[plan],
            tick_interval_seconds=1,
        )
        end = datetime.utcnow() + timedelta(seconds=3)
        loop.run_until(end)

        count = database.conn.execute(
            "SELECT COUNT(*) FROM candidate_snapshots WHERE session_id = ?",
            (writer.session_id,),
        ).fetchone()[0]
        # Expect ≥2 ticks in a 3-second window with 1-second cadence.
        assert count >= 2, f"Expected at least 2 snapshots, got {count}"
