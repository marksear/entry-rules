"""
resume — rehydrate CandidateRuntimeState for positions left live on IG
from a prior trading session.

Why this exists
---------------
The monitor loop deliberately leaves open positions on IG at session close
(2–3 day hold cap per Masterclass v2). The next session needs to pick up
exactly where the last one left off — same deal_id, same peak P&L, same
trail step count, same sessions_held. If we start from zero every morning,
the trail ratchet resets and the timestop forgets how old the position is.

The resume flow
---------------
1. Query IG ``fetch_open_positions`` to get live dealIds (source of truth).
2. For each live dealId, find the matching FILLED event in the local DB
   (``candidate_events`` with ``event_type='FILLED'`` and matching
   ``ig_deal_id`` in the JSON payload).
3. Reconstruct ``CandidatePlan`` from the ``shortlist_entries`` row.
4. Reconstruct ``CandidateRuntimeState`` from:
   - FILLED event (fill_price, stake, fill_ts, initial_stop)
   - latest STOP_MOVED event (current_stop, peak_pnl_gbp, trail_step_count)
   - session count (distinct session_ids that have written snapshots for
     this candidate — a reasonable proxy for ``sessions_held``).
5. Skip any candidate that already has a terminal event (belt-and-braces —
   if we've already emitted STOP_HIT or TIMESTOP_HIT, the IG position
   shouldn't still be live; if it is, something's desynced and we log).

All DB work here is read-only. The caller (``session_init``) is
responsible for seeding the MonitorLoop with the returned states.

Design notes
------------
- ``rehydrate_open_positions`` is deliberately split from
  ``fetch_live_positions`` so the rule logic can be unit-tested without an
  IG session — feed in a fake positions list and a pre-populated SQLite.
- If a live IG position has no matching FILLED event in our DB, we log a
  WARNING and skip it. This shouldn't happen in practice (we only ever
  open positions through ``Broker.place_open_position`` which writes FILLED)
  but the log makes manual-intervention positions visible.
- Stop-level from IG is authoritative when it differs from our DB's most
  recent STOP_MOVED. That can happen if someone nudged the stop via the
  IG web UI between sessions. We trust IG for ``current_stop_price``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from ..models.candidate_event import CandidateEvent, PositionResumedPayload
from ..models.common import Direction, EntryType, Market
from ..models.log_enums import (
    ActorKind,
    BrokerMode,
    CandidateGrade,
    EventType,
    StopSource,
)
from .monitor import CandidatePlan, CandidateRuntimeState

if TYPE_CHECKING:
    from ..auth.ig_auth import IGSession
    from ..data.market_data import MarketData
    from ..logging_mod.db import Database
    from ..logging_mod.session_writer import SessionWriter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResumeMeta:
    """Per-candidate metadata for emitting a POSITION_RESUMED event.

    Carries fields the runtime state doesn't track but the event payload needs
    (e.g. the session_id where the FILLED was originally written, and which
    source the current_stop_price ultimately came from).
    """

    fill_session_id: str
    source_of_stop: StopSource


def fetch_live_positions(ig_session: IGSession) -> list[dict]:
    """Query IG for all currently-open positions.

    Returns a list of plain dicts (not a pandas DataFrame) with the IG
    field names preserved so the caller can correlate by ``dealId``.
    """
    # Ask trading_ig for raw dict form — dataframe conversion is flaky when
    # the positions list is empty and we don't need it here.
    svc = ig_session.service
    # Temporarily flip return_dataframe off so we get the parsed dict back.
    prev = getattr(svc, "return_dataframe", True)
    try:
        svc.return_dataframe = False
        data = svc.fetch_open_positions()
    finally:
        svc.return_dataframe = prev

    if not data:
        return []
    positions = data.get("positions", []) if isinstance(data, dict) else []
    normalised = []
    for p in positions:
        pos = p.get("position", {}) if isinstance(p, dict) else {}
        mkt = p.get("market", {}) if isinstance(p, dict) else {}
        normalised.append(
            {
                "deal_id": pos.get("dealId", ""),
                "deal_reference": pos.get("dealReference", ""),
                "size": float(pos.get("size", 0) or 0),
                "direction": pos.get("direction", ""),
                "fill_price": float(pos.get("level", 0) or 0) or None,
                "stop_level": (
                    float(pos["stopLevel"]) if pos.get("stopLevel") is not None else None
                ),
                "created_date_utc": pos.get("createdDateUTC", ""),
                "epic": mkt.get("epic", ""),
                "currency": pos.get("currency", ""),
            }
        )
    return normalised


def rehydrate_open_positions(
    database: Database,
    live_positions: list[dict],
    new_session_id: str,
    market_data: MarketData,
    writer: SessionWriter | None = None,
    broker_mode: BrokerMode | None = None,
    rule_set_version: str = "",
) -> list[tuple[CandidatePlan, CandidateRuntimeState]]:
    """Build (CandidatePlan, CandidateRuntimeState) tuples for resumed positions.

    When ``writer`` is supplied, also emits one ``POSITION_RESUMED`` event per
    resumed candidate so re-attachment is first-class in the event log (not
    just a WARNING/INFO line).

    Args:
        database: Connected SQLite database.
        live_positions: Output of ``fetch_live_positions``. Pass an empty
            list for the trivial case (no overnights).
        new_session_id: The session_id we're opening now — used only to set
            ``plan.session_id`` so events written during this session route
            correctly.
        market_data: Used to resolve an IG epic per symbol. Resumed plans
            must have a live epic so the monitor can fetch snapshots.
        writer: Optional SessionWriter. When present, a POSITION_RESUMED
            event is persisted for each resumed candidate. Tests that don't
            need the emission can omit it.
        broker_mode: Required when ``writer`` is provided — stamped onto the
            events. If omitted, falls back to each plan's own broker_mode.
        rule_set_version: Short git SHA of entry-rules at session open;
            stamped onto the emitted events. Optional.

    Returns:
        Zero or more (plan, state) tuples ready for ``MonitorLoop.seed_resumed_position``.
    """
    if not live_positions:
        return []

    live_deal_ids = {p["deal_id"] for p in live_positions if p["deal_id"]}
    if not live_deal_ids:
        return []

    live_by_deal_id = {p["deal_id"]: p for p in live_positions if p["deal_id"]}

    placeholders = ",".join("?" for _ in live_deal_ids)
    fill_rows = database.conn.execute(
        f"""
        SELECT e.candidate_id, e.session_id, e.ts_utc, e.payload_json,
               e.rule_set_version
        FROM candidate_events e
        WHERE e.event_type = 'FILLED'
          AND json_extract(e.payload_json, '$.ig_deal_id') IN ({placeholders})
          AND NOT EXISTS (
            SELECT 1 FROM candidate_events t
            WHERE t.candidate_id = e.candidate_id
              AND t.terminal_reason IS NOT NULL
          )
        ORDER BY e.ts_utc DESC
        """,
        tuple(live_deal_ids),
    ).fetchall()

    resumed: list[tuple[CandidatePlan, CandidateRuntimeState]] = []
    resumed_meta: list[tuple[CandidatePlan, CandidateRuntimeState, dict, _ResumeMeta]] = []
    fills_seen: set[str] = set()

    for fill_row in fill_rows:
        candidate_id = fill_row["candidate_id"]
        if candidate_id in fills_seen:
            continue  # defensive — one FILLED per candidate; take the newest
        fills_seen.add(candidate_id)

        fill_payload = json.loads(fill_row["payload_json"])
        deal_id = fill_payload.get("ig_deal_id", "")
        live_pos = live_by_deal_id.get(deal_id)
        if not live_pos:
            continue  # shouldn't happen — we filtered by live_deal_ids above

        shortlist_row = database.conn.execute(
            """
            SELECT candidate_id, scan_id, symbol, market, direction, setup_type,
                   grade, trigger_low, trigger_high, stop_price, target_price,
                   planned_stake_gbp_per_pt, planned_risk_gbp,
                   broker_mode, rule_set_version
            FROM shortlist_entries
            WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if shortlist_row is None:
            logger.warning(
                "Resume: FILLED for candidate_id=%s has no shortlist_entries row. Skipping.",
                candidate_id,
            )
            continue

        epic = market_data.resolve_epic(shortlist_row["symbol"], shortlist_row["market"])
        if not epic:
            logger.warning(
                "Resume: could not resolve epic for %s (%s). Skipping.",
                shortlist_row["symbol"],
                shortlist_row["market"],
            )
            continue

        plan = CandidatePlan(
            candidate_id=candidate_id,
            scan_id=shortlist_row["scan_id"],
            session_id=new_session_id,
            symbol=shortlist_row["symbol"],
            market=Market(shortlist_row["market"]),
            direction=Direction(shortlist_row["direction"]),
            setup_type=EntryType(shortlist_row["setup_type"]),
            grade=CandidateGrade(shortlist_row["grade"]),
            trigger_low=shortlist_row["trigger_low"],
            trigger_high=shortlist_row["trigger_high"],
            stop_price=shortlist_row["stop_price"],
            target_price=shortlist_row["target_price"],
            ig_epic=epic,
            broker_mode=BrokerMode(shortlist_row["broker_mode"]),
            rule_set_version=shortlist_row["rule_set_version"] or "",
            planned_stake_gbp_per_pt=shortlist_row["planned_stake_gbp_per_pt"] or 0.0,
            planned_risk_gbp=shortlist_row["planned_risk_gbp"] or 0.0,
        )

        state, meta = _build_resumed_state(
            database=database,
            candidate_id=candidate_id,
            fill_payload=fill_payload,
            live_pos=live_pos,
            new_session_id=new_session_id,
            fill_session_id=fill_row["session_id"],
        )
        logger.info(
            "Resume: re-attaching to %s deal_id=%s fill=%.4f stop=%.4f "
            "peak=£%.2f step=%d sessions_held=%d",
            plan.symbol,
            deal_id,
            state.fill_price or 0.0,
            state.current_stop_price or 0.0,
            state.peak_pnl_gbp,
            state.trail_step_count,
            state.sessions_held,
        )
        resumed.append((plan, state))
        resumed_meta.append((plan, state, fill_payload, meta))

    # Warn on live positions with no matching FILLED in DB — likely manual
    # orders or an earlier session whose DB was cleared.
    unmatched = live_deal_ids - {
        json.loads(r["payload_json"]).get("ig_deal_id", "") for r in fill_rows
    }
    for deal_id in unmatched:
        logger.warning(
            "Resume: IG live position deal_id=%s has no matching FILLED event "
            "in the DB — not re-attaching.",
            deal_id,
        )

    # Emit POSITION_RESUMED events so re-attachment is first-class in the
    # timeline, not just an INFO line. Only when a writer is supplied —
    # unit tests that don't need persistence can omit it.
    if writer is not None and resumed_meta:
        events: list[CandidateEvent] = []
        for plan, state, fill_payload, meta in resumed_meta:
            event_broker_mode = broker_mode or plan.broker_mode
            fill_ts_raw = fill_payload.get("fill_ts_utc")
            fill_ts_utc: datetime | None = None
            if fill_ts_raw:
                try:
                    fill_ts_utc = datetime.fromisoformat(str(fill_ts_raw).replace("Z", ""))
                except ValueError:
                    fill_ts_utc = None
            payload = PositionResumedPayload(
                ig_deal_id=state.deal_id or "",
                fill_price=float(fill_payload.get("fill_price", 0.0)),
                fill_ts_utc=fill_ts_utc,
                stake_gbp_per_pt=float(fill_payload.get("stake_gbp_per_pt", 0.0)),
                initial_stop_price=float(fill_payload.get("initial_stop_price", 0.0)),
                current_stop_price=state.current_stop_price,
                source_of_stop=meta.source_of_stop,
                peak_pnl_gbp_at_resume=state.peak_pnl_gbp,
                trail_step_count=state.trail_step_count,
                trail_mode_activated=state.trail_mode_activated,
                sessions_held=state.sessions_held,
                prior_fill_session_id=meta.fill_session_id,
            )
            events.append(
                CandidateEvent(
                    id=str(uuid4()),
                    session_id=new_session_id,
                    candidate_id=plan.candidate_id,
                    ts_utc=datetime.utcnow(),
                    event_type=EventType.POSITION_RESUMED,
                    actor=ActorKind.EXECUTOR,
                    payload=payload,
                    broker_mode=event_broker_mode,
                    rule_set_version=rule_set_version or plan.rule_set_version or "",
                )
            )
        writer.write_events(events)
        logger.info("Emitted POSITION_RESUMED x %d", len(events))

    return resumed


def _build_resumed_state(
    database: Database,
    candidate_id: str,
    fill_payload: dict,
    live_pos: dict,
    new_session_id: str,
    fill_session_id: str,
) -> tuple[CandidateRuntimeState, _ResumeMeta]:
    """Pull the latest STOP_MOVED + session count to reconstruct runtime.

    Returns the runtime state plus metadata useful for emitting a
    POSITION_RESUMED event (notably where the stop came from).
    """
    stop_moved_row = database.conn.execute(
        """
        SELECT payload_json FROM candidate_events
        WHERE candidate_id = ? AND event_type = 'STOP_MOVED'
        ORDER BY ts_utc DESC LIMIT 1
        """,
        (candidate_id,),
    ).fetchone()

    if stop_moved_row is not None:
        sm = json.loads(stop_moved_row["payload_json"])
        peak_pnl_gbp = float(sm.get("peak_pnl_gbp_at_move", 0.0))
        trail_step_count = int(sm.get("new_trail_step_count", 0))
        trail_mode_activated = trail_step_count >= 1
    else:
        peak_pnl_gbp = 0.0
        trail_step_count = 0
        trail_mode_activated = False

    # Prefer IG's stopLevel (source of truth). Fall back to DB's latest
    # STOP_MOVED new_stop, then initial_stop from the FILLED payload.
    if live_pos.get("stop_level") is not None:
        current_stop_price = float(live_pos["stop_level"])
        source_of_stop = StopSource.IG_STOP_LEVEL
    elif stop_moved_row is not None:
        current_stop_price = float(
            json.loads(stop_moved_row["payload_json"]).get("new_stop", 0.0)
        ) or None
        source_of_stop = StopSource.DB_STOP_MOVED
    else:
        current_stop_price = float(fill_payload.get("initial_stop_price", 0.0)) or None
        source_of_stop = StopSource.FILL_INITIAL

    # sessions_held = distinct prior sessions with snapshots for this
    # candidate + 1 (for this new session). Excludes the current new session
    # since no snapshots have been written yet.
    prior_sessions_row = database.conn.execute(
        """
        SELECT COUNT(DISTINCT session_id) AS n FROM candidate_snapshots
        WHERE candidate_id = ? AND session_id != ?
        """,
        (candidate_id, new_session_id),
    ).fetchone()
    prior_sessions = int(prior_sessions_row["n"]) if prior_sessions_row else 0
    sessions_held = prior_sessions + 1

    fill_ts_utc: datetime | None = None
    fill_ts_raw = fill_payload.get("fill_ts_utc")
    if fill_ts_raw:
        try:
            fill_ts_utc = datetime.fromisoformat(fill_ts_raw.replace("Z", ""))
        except ValueError:
            logger.warning(
                "Resume: could not parse fill_ts_utc %r for candidate_id=%s",
                fill_ts_raw,
                candidate_id,
            )

    state = CandidateRuntimeState(
        fired=True,
        deal_id=live_pos["deal_id"],
        deal_reference=live_pos.get("deal_reference") or None,
        fill_price=float(fill_payload.get("fill_price", 0.0)) or None,
        fill_ts_utc=fill_ts_utc,
        stake_gbp_per_pt=float(fill_payload.get("stake_gbp_per_pt", 0.0)) or None,
        initial_stop_price=float(fill_payload.get("initial_stop_price", 0.0)) or None,
        current_stop_price=current_stop_price,
        peak_pnl_gbp=peak_pnl_gbp,
        trail_step_count=trail_step_count,
        trail_mode_activated=trail_mode_activated,
        sessions_held=sessions_held,
    )
    meta = _ResumeMeta(
        fill_session_id=fill_session_id,
        source_of_stop=source_of_stop,
    )
    return state, meta


__all__ = [
    "fetch_live_positions",
    "rehydrate_open_positions",
]
