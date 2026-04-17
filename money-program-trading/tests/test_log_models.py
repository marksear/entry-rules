"""
Round-trip JSON tests for the observability log record types.

These models are the contract between swing-committee (scan artifact writer)
and entry-rules (session ingester + SessionWriter). Any break in field names
or validation tightens quickly turns into a production ingest failure, so the
tests are the first line of defence.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

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
    SessionRecord,
    ShortlistEntry,
    StopMovedPayload,
    StopMoveReason,
    TargetHitPayload,
    TargetHitReason,
    TrailModeActivatedPayload,
    TriggerFiredPayload,
    UniverseScoreEntry,
)

# ----------------------------- SessionRecord -----------------------------


def test_session_record_roundtrip():
    rec = SessionRecord(
        session_date=date(2026, 4, 16),
        session_label=SessionLabel.US_REGULAR,
        broker_mode=BrokerMode.DEMO,
        account_size_gbp=5000.0,
        rule_set_version="abc1234",
    )
    restored = SessionRecord.model_validate_json(rec.model_dump_json())
    assert restored == rec
    # Schema version must be stamped.
    assert restored.schema_version == 1


def test_session_record_requires_broker_mode():
    with pytest.raises(ValidationError):
        SessionRecord(
            session_date=date(2026, 4, 16),
            session_label=SessionLabel.US_REGULAR,
            # broker_mode missing
            account_size_gbp=5000.0,
            rule_set_version="abc1234",
        )


# ------------------------------- ScanRecord ------------------------------


def _make_scan() -> ScanRecord:
    return ScanRecord(
        scanned_at_utc=datetime(2026, 4, 16, 8, 0, 0),
        universe_size=2,
        broker_mode=BrokerMode.DEMO,
        regime=RegimeSnapshot(regime=RegimeState.GREEN, regime_score=1.2, vix_level=15.0),
        scored_universe=[
            UniverseScoreEntry(
                symbol="AVGO",
                market="US",
                price=1400.0,
                currency="USD",
                pillar_pass_count=5,
                pillar_bitmap=0b011111,
                grade="A+",
                shortlisted=True,
            ),
            UniverseScoreEntry(
                symbol="XYZ",
                market="US",
                price=10.0,
                pillar_pass_count=1,
                pillar_bitmap=0b000001,
                shortlisted=False,
                rejection_reason="below B grade",
            ),
        ],
    )


def test_scan_record_roundtrip():
    scan = _make_scan()
    restored = ScanRecord.model_validate_json(scan.model_dump_json())
    assert restored == scan
    assert len(restored.scored_universe) == 2
    assert restored.scored_universe[0].shortlisted is True


def test_scan_record_rejects_unknown_fields():
    raw = _make_scan().model_dump()
    raw["this_is_garbage"] = True
    with pytest.raises(ValidationError):
        ScanRecord.model_validate(raw)


# ---------------------------- ShortlistEntry -----------------------------


def _make_shortlist(direction: Direction = Direction.LONG) -> ShortlistEntry:
    if direction == Direction.LONG:
        return ShortlistEntry(
            scan_id=str(uuid4()),
            symbol="AVGO",
            market=Market.US,
            direction=Direction.LONG,
            setup_type=EntryType.L_A,
            grade=CandidateGrade.A_PLUS,
            trigger_low=1400.0,
            trigger_high=1402.0,
            stop_price=1380.0,
            target_price=1440.0,
            planned_stake_gbp_per_pt=0.50,
            planned_risk_gbp=50.0,
            planned_risk_pct_account=0.01,
            pillar_votes=PillarVotes(
                livermore=True, oneil=True, minervini=True, darvas=True, raschke=True
            ),
            broker_mode=BrokerMode.DEMO,
        )
    return ShortlistEntry(
        scan_id=str(uuid4()),
        symbol="TSLA",
        market=Market.US,
        direction=Direction.SHORT,
        setup_type=EntryType.S_A,
        grade=CandidateGrade.A,
        trigger_low=180.0,
        trigger_high=182.0,
        stop_price=195.0,
        target_price=160.0,
        planned_stake_gbp_per_pt=0.25,
        planned_risk_gbp=32.5,
        planned_risk_pct_account=0.0075,
        broker_mode=BrokerMode.DEMO,
    )


def test_shortlist_entry_long_roundtrip():
    se = _make_shortlist(Direction.LONG)
    restored = ShortlistEntry.model_validate_json(se.model_dump_json())
    assert restored == se
    assert restored.pillar_votes.count == 5
    assert restored.pillar_votes.bitmap == 0b011111
    # risk_per_pt for LONG = trigger_low − stop
    assert restored.risk_per_pt == pytest.approx(20.0)


def test_shortlist_entry_short_roundtrip():
    se = _make_shortlist(Direction.SHORT)
    restored = ShortlistEntry.model_validate_json(se.model_dump_json())
    assert restored == se
    # risk_per_pt for SHORT = stop − trigger_high
    assert restored.risk_per_pt == pytest.approx(13.0)


def test_shortlist_entry_direction_must_match_setup_type():
    with pytest.raises(ValidationError, match="contradicts setup_type"):
        ShortlistEntry(
            scan_id=str(uuid4()),
            symbol="AVGO",
            market=Market.US,
            direction=Direction.LONG,  # but setup is a short
            setup_type=EntryType.S_A,
            grade=CandidateGrade.A,
            trigger_low=100.0,
            trigger_high=102.0,
            stop_price=90.0,
            planned_stake_gbp_per_pt=0.10,
            planned_risk_gbp=1.0,
            planned_risk_pct_account=0.005,
            broker_mode=BrokerMode.DEMO,
        )


def test_shortlist_entry_stop_on_wrong_side_rejected_long():
    with pytest.raises(ValidationError, match="LONG: stop_price"):
        ShortlistEntry(
            scan_id=str(uuid4()),
            symbol="AVGO",
            market=Market.US,
            direction=Direction.LONG,
            setup_type=EntryType.L_A,
            grade=CandidateGrade.A,
            trigger_low=100.0,
            trigger_high=102.0,
            stop_price=105.0,  # above the trigger — nonsense for LONG
            planned_stake_gbp_per_pt=0.10,
            planned_risk_gbp=1.0,
            planned_risk_pct_account=0.005,
            broker_mode=BrokerMode.DEMO,
        )


def test_shortlist_entry_stop_on_wrong_side_rejected_short():
    with pytest.raises(ValidationError, match="SHORT: stop_price"):
        ShortlistEntry(
            scan_id=str(uuid4()),
            symbol="TSLA",
            market=Market.US,
            direction=Direction.SHORT,
            setup_type=EntryType.S_A,
            grade=CandidateGrade.A,
            trigger_low=180.0,
            trigger_high=182.0,
            stop_price=175.0,  # below the trigger — nonsense for SHORT
            planned_stake_gbp_per_pt=0.10,
            planned_risk_gbp=1.0,
            planned_risk_pct_account=0.005,
            broker_mode=BrokerMode.DEMO,
        )


def test_shortlist_entry_trigger_low_must_not_exceed_high():
    with pytest.raises(ValidationError, match="trigger_low"):
        ShortlistEntry(
            scan_id=str(uuid4()),
            symbol="AVGO",
            market=Market.US,
            direction=Direction.LONG,
            setup_type=EntryType.L_A,
            grade=CandidateGrade.A,
            trigger_low=102.0,
            trigger_high=100.0,  # inverted
            stop_price=90.0,
            planned_stake_gbp_per_pt=0.10,
            planned_risk_gbp=1.0,
            planned_risk_pct_account=0.005,
            broker_mode=BrokerMode.DEMO,
        )


# ---------------------------- CandidateSnapshot --------------------------


def test_candidate_snapshot_pending_trigger_roundtrip():
    snap = CandidateSnapshot(
        session_id=str(uuid4()),
        candidate_id=str(uuid4()),
        scan_id=str(uuid4()),
        symbol="AVGO",
        market=Market.US,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.A_PLUS,
        ts_utc=datetime(2026, 4, 16, 14, 17, 0),
        minute_bucket=202604161417,
        status=CandidateStatus.PENDING_TRIGGER,
        broker_mode=BrokerMode.DEMO,
        last_price=1398.5,
        bid=1398.4,
        ask=1398.6,
        spread_pts=0.2,
        dist_to_trigger_pts=1.5,
        adx_14=32.1,
        rs_percentile=87.0,
        ma_alignment_bitmap=0b111,
        mcl_regime=RegimeState.GREEN,
        pillar_pass_count=5,
        pillar_bitmap=0b011111,
        would_enter_now=False,
        rejection_code="R10",  # portfolio heat
    )
    restored = CandidateSnapshot.model_validate_json(snap.model_dump_json())
    assert restored == snap


def test_candidate_snapshot_triggered_open_with_trail_roundtrip():
    snap = CandidateSnapshot(
        session_id=str(uuid4()),
        candidate_id=str(uuid4()),
        scan_id=str(uuid4()),
        symbol="AVGO",
        market=Market.US,
        direction=Direction.LONG,
        setup_type=EntryType.L_A,
        grade=CandidateGrade.A_PLUS,
        ts_utc=datetime(2026, 4, 16, 15, 5, 0),
        minute_bucket=202604161505,
        status=CandidateStatus.TRIGGERED_OPEN,
        broker_mode=BrokerMode.DEMO,
        last_price=1415.0,
        fill_price=1400.5,
        fill_ts_utc=datetime(2026, 4, 16, 14, 35, 0),
        elapsed_mins_in_position=30,
        elapsed_sessions_in_position=1,
        unrealised_pnl_gbp=30.0,
        peak_unrealised_pnl_gbp=32.0,
        unrealised_r=1.5,
        current_stop_price=1401.7,
        current_locked_profit_gbp=6.0,  # band 2 after £30 peak
        stop_moved_count=2,
        trail_step_count=2,
        invalidation_window_active=False,
        trail_mode_active=True,
        mins_to_timestop=2430,
    )
    restored = CandidateSnapshot.model_validate_json(snap.model_dump_json())
    assert restored == snap


def test_candidate_snapshot_trail_step_count_out_of_range():
    with pytest.raises(ValidationError):
        CandidateSnapshot(
            session_id=str(uuid4()),
            candidate_id=str(uuid4()),
            scan_id=str(uuid4()),
            symbol="AVGO",
            market=Market.US,
            direction=Direction.LONG,
            setup_type=EntryType.L_A,
            grade=CandidateGrade.A_PLUS,
            ts_utc=datetime(2026, 4, 16, 14, 0, 0),
            minute_bucket=202604161400,
            status=CandidateStatus.TRIGGERED_OPEN,
            broker_mode=BrokerMode.DEMO,
            trail_step_count=6,  # invalid — max is 5
        )


# ----------------------------- CandidateEvent ----------------------------


def _common_event_fields():
    return {
        "session_id": str(uuid4()),
        "candidate_id": str(uuid4()),
        "broker_mode": BrokerMode.DEMO,
    }


def test_event_trigger_fired_roundtrip():
    evt = CandidateEvent(
        **_common_event_fields(),
        event_type=EventType.TRIGGER_FIRED,
        actor=ActorKind.EXECUTOR,
        payload=TriggerFiredPayload(
            last_price=1401.0, trigger_low=1400.0, trigger_high=1402.0
        ),
    )
    restored = CandidateEvent.model_validate_json(evt.model_dump_json())
    assert restored == evt
    assert restored.payload.kind == "TRIGGER_FIRED"


def test_event_stop_moved_trail_step_roundtrip():
    """Single event when a fast tick crosses multiple £5 bands.

    peak went 27 → 43, so step_count 1 → 4, locked 1 → 16. A single
    STOP_MOVED(reason=TRAIL_STEP) event captures it.
    """
    evt = CandidateEvent(
        **_common_event_fields(),
        event_type=EventType.STOP_MOVED,
        actor=ActorKind.TRAIL_MANAGER,
        reason_code="TRAIL_STEP",
        payload=StopMovedPayload(
            reason=StopMoveReason.TRAIL_STEP,
            old_stop=1401.0,
            new_stop=1416.0,
            old_trail_step_count=1,
            new_trail_step_count=4,
            old_locked_gbp=1.0,
            new_locked_gbp=16.0,
            peak_pnl_gbp_at_move=43.0,
        ),
    )
    restored = CandidateEvent.model_validate_json(evt.model_dump_json())
    assert restored == evt
    assert restored.payload.new_trail_step_count - restored.payload.old_trail_step_count == 3


def test_event_trail_mode_activated_roundtrip():
    evt = CandidateEvent(
        **_common_event_fields(),
        event_type=EventType.TRAIL_MODE_ACTIVATED,
        actor=ActorKind.TRAIL_MANAGER,
        payload=TrailModeActivatedPayload(
            peak_pnl_gbp=25.30,
            trail_activation_gbp=25.0,
            initial_locked_gbp=1.0,
        ),
    )
    restored = CandidateEvent.model_validate_json(evt.model_dump_json())
    assert restored == evt


def test_event_hard_target_hit_roundtrip():
    evt = CandidateEvent(
        **_common_event_fields(),
        event_type=EventType.TARGET_HIT,
        actor=ActorKind.TRAIL_MANAGER,
        reason_code="HARD_TARGET_GBP",
        payload=TargetHitPayload(
            reason=TargetHitReason.HARD_TARGET_GBP,
            peak_pnl_gbp=50.10,
            realised_pnl_gbp=50.00,
        ),
    )
    restored = CandidateEvent.model_validate_json(evt.model_dump_json())
    assert restored == evt
    assert restored.payload.reason == TargetHitReason.HARD_TARGET_GBP


def test_event_payload_discriminator_rejects_mismatch():
    """If event_type=TRIGGER_FIRED but payload is a StopMovedPayload, Pydantic
    should accept (since event_type is independent of the discriminator),
    BUT the payload's own 'kind' discriminator must match its class. The real
    safety net is at write time: SessionWriter asserts event_type ↔ payload.kind
    agreement before persistence."""
    evt = CandidateEvent(
        **_common_event_fields(),
        event_type=EventType.TRIGGER_FIRED,  # lie
        actor=ActorKind.EXECUTOR,
        payload=StopMovedPayload(
            reason=StopMoveReason.TRAIL_ARM,
            old_stop=100.0,
            new_stop=101.0,
            old_trail_step_count=0,
            new_trail_step_count=1,
            old_locked_gbp=0.0,
            new_locked_gbp=1.0,
            peak_pnl_gbp_at_move=25.5,
        ),
    )
    # Model accepts — enforcement is at write-time (SessionWriter in session 3).
    # Document the known gap here so it doesn't get lost.
    assert evt.event_type == EventType.TRIGGER_FIRED
    assert evt.payload.kind == "STOP_MOVED"


def test_shortlist_added_payload_from_json_via_discriminator():
    """Round-trip through JSON picks the right payload class from 'kind'."""
    raw = {
        **{
            "id": str(uuid4()),
            "session_id": str(uuid4()),
            "candidate_id": str(uuid4()),
            "ts_utc": "2026-04-16T08:05:00",
            "event_type": "SHORTLIST_ADDED",
            "actor": "INGESTER",
            "broker_mode": "DEMO",
            "schema_version": 1,
            "rule_set_version": "",
            "reason_code": None,
            "terminal_reason": None,
        },
        "payload": {
            "kind": "SHORTLIST_ADDED",
            "grade": "A+",
            "planned_stake_gbp_per_pt": 0.50,
            "planned_risk_gbp": 50.0,
        },
    }
    evt = CandidateEvent.model_validate(raw)
    assert evt.payload.kind == "SHORTLIST_ADDED"
    assert evt.payload.grade == "A+"
