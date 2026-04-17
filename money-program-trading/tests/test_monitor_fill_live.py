"""
Live integration test for the full fill path: trigger → IG order → fill →
close.

Hits IG DEMO. Auto-skipped when ``IG_USERNAME`` / ``IG_PASSWORD`` /
``IG_API_KEY`` are unset (see ``conftest.py``).

Why a separate file from ``test_monitor_live.py``
-------------------------------------------------
``test_monitor_live.py`` covers the pre-trigger path only (trigger far from
live price, never fires). This file covers the *fill path* — a real LONG on
a broad-market DFB epic, followed by an immediate ``close_position`` so we
don't leave anything open on DEMO.

What this validates
-------------------
- ``Broker.place_open_position`` talks to IG REST, gets a deal reference,
  polls the confirm, returns a populated ``OrderResult``.
- ``MonitorLoop._handle_fire`` emits ``ORDER_PLACED`` + ``FILLED`` and marks
  the candidate state as fired with a real deal_id.
- ``Broker.close_position`` round-trips the close successfully.

Size is deliberately the smallest we can get away with on DEMO (0.50 GBP/pt
on the S&P 500 DFB) — big enough to pass IG's minimum, small enough that a
runaway loop would only lose a small amount before manual intervention.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime
from pathlib import Path

import pytest

from src.auth.ig_auth import IGSession
from src.data.market_data import MarketData
from src.engine.broker import Broker
from src.engine.monitor import CandidatePlan, MonitorLoop
from src.engine.trail_manager import ExitConfig
from src.logging_mod.db import Database
from src.logging_mod.session_writer import SessionWriter
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import (
    BrokerMode,
    CandidateGrade,
    EventType,
    SessionLabel,
)

pytestmark = pytest.mark.integration


# Same broad-market epic pattern as test_monitor_live.py. Override per
# account via ``IG_TEST_EPIC`` if SPTRD isn't enabled.
DEFAULT_TEST_EPIC = os.getenv("IG_TEST_EPIC", "IX.D.SPTRD.DAILY.IP")

# DEMO fill-path test stake. Tiny but above IG's minimum for index DFBs.
FILL_TEST_STAKE_GBP_PER_PT = float(os.getenv("IG_FILL_TEST_STAKE", "0.50"))


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


@pytest.fixture(scope="module")
def broker(ig_session: IGSession) -> Broker:
    return Broker(ig_session)


@pytest.fixture
def database(tmp_path: Path):
    db = Database(db_path=str(tmp_path / "fill_live.db"))
    db.initialize()
    try:
        yield db
    finally:
        db.close()


def _insert_prereq_rows(
    database: Database, writer: SessionWriter, plan: CandidatePlan
) -> None:
    """Insert a minimal scan + shortlist row so snapshot FKs don't fail."""
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
        ) VALUES (?, ?, ?, ?, 'US', 'LONG', 'L-A', 'B', ?, ?, ?, ?, ?, 0.005,
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


def test_fire_places_real_order_and_close_roundtrips(
    database: Database,
    market_data: MarketData,
    broker: Broker,
):
    """End-to-end DEMO: trigger → place order → FILLED → close → verify.

    The test deliberately sets the LONG trigger *below* the current live
    price so a single tick fires the order. The stop is set 50% below
    current price so the initial stop can't be breached intra-tick. After
    FILL we immediately call ``broker.close_position`` — the test's goal is
    to verify the REST roundtrip, not to hold an overnight position.
    """
    snap = market_data.get_market_snapshot(DEFAULT_TEST_EPIC)
    assert snap, "Prereq: IG snapshot must be non-empty"
    last = snap["last_traded"]
    assert last and last > 0, f"Invalid last_traded in snapshot: {snap}"

    # Trigger just below current so LONG fires immediately on this tick.
    trigger = last * 0.999
    plan = CandidatePlan(
        candidate_id=str(uuid.uuid4()),
        scan_id=str(uuid.uuid4()),
        session_id="filled-later",  # hydrated below
        symbol="LIVE-FILL",
        market=Market.US,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.B,
        trigger_low=trigger,
        trigger_high=trigger * 1.0001,
        stop_price=last * 0.50,
        target_price=None,
        ig_epic=DEFAULT_TEST_EPIC,
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=FILL_TEST_STAKE_GBP_PER_PT,
        planned_risk_gbp=(last * 0.50) * FILL_TEST_STAKE_GBP_PER_PT,
    )

    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="fill-live-test",
        session_date=date.today(),
    ) as writer:
        plan = CandidatePlan(**{**plan.__dict__, "session_id": writer.session_id})
        _insert_prereq_rows(database, writer, plan)

        loop = MonitorLoop(
            writer=writer,
            market_data=market_data,
            plans=[plan],
            tick_interval_seconds=1,
            broker=broker,
            exit_config=ExitConfig(),
        )
        loop.run_one_tick()

        # Locate the state for our candidate — MonitorLoop keys runtime
        # state by candidate_id.
        state = loop._runtime[plan.candidate_id]

        # On a successful fill we expect: fired=True, a real deal_id, and
        # ORDER_PLACED + FILLED events in the DB.
        events = database.conn.execute(
            "SELECT event_type FROM candidate_events "
            "WHERE candidate_id = ? ORDER BY id",
            (plan.candidate_id,),
        ).fetchall()
        event_types = [r["event_type"] for r in events]
        assert EventType.TRIGGER_FIRED.value in event_types
        assert EventType.ORDER_PLACED.value in event_types

        if state.deal_id:
            # Order went through — we have an open position on IG DEMO.
            assert state.fired, "Candidate with deal_id should be marked fired"
            assert EventType.FILLED.value in event_types
            assert state.fill_price is not None

            # Close it immediately so we don't leave a DEMO position open.
            close = broker.close_position(
                deal_id=state.deal_id,
                direction=plan.direction,
                epic=plan.ig_epic,
                size=state.stake_gbp_per_pt,
            )
            assert close.success, (
                f"Close failed for deal_id={state.deal_id}: "
                f"{close.reason_code} — manual cleanup required!"
            )
        else:
            # Broker call returned !success — likely outside market hours or
            # minimum-size breach on this account. Assert we logged it
            # cleanly rather than silent-failing.
            assert EventType.ENTRY_EVALUATED_NO_ENTER.value in event_types, (
                "No fill and no ENTRY_EVALUATED_NO_ENTER — fill path silently dropped"
            )
            pytest.skip(
                "IG DEMO rejected the order (out of session hours or size). "
                "Fill events were logged correctly; rerun during US session."
            )
