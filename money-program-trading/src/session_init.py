"""
session_init — CLI entry point that opens a trading session, ingests the
morning scan from swing-committee, and runs the per-minute monitor loop.

Usage::

    python -m src.session_init --scan data/scans/scan_20260416.json \\
        --duration-mins 390 --account-size 1000 --label US_REGULAR

The workflow
------------
1. Connect to IG (via ``IGSession`` — DEMO or LIVE is driven by config).
2. Open the observability database (SQLite v2 schema).
3. Open a ``SessionWriter`` — inserts the ``sessions`` row.
4. Ingest the scan handoff file — validates via pydantic, writes scan +
   scan_universe + shortlist_entries, stamps ``sessions.scan_id``.
5. Resolve an IG epic for each shortlisted candidate (cached on disk by
   ``MarketData``).
6. Emit one ``SHORTLIST_ADDED`` event per candidate.
7. Build :class:`CandidatePlan` objects and hand them to :class:`MonitorLoop`.
8. Run the loop for ``--duration-mins`` minutes (or until Ctrl-C).
9. Close the session cleanly.

The loop hits IG DEMO (or LIVE — same code path). No synthetic prices are
ever used. If IG is unreachable, the script bails early with a clear error
rather than silently writing empty snapshots.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .auth.ig_auth import IGSession
from .config.settings import get_settings
from .data.market_data import MarketData
from .engine.broker import Broker
from .engine.monitor import CandidatePlan, MonitorLoop
from .engine.resume import fetch_live_positions, rehydrate_open_positions
from .engine.trail_manager import ExitConfig
from .logging_mod.db import Database
from .logging_mod.session_writer import SessionWriter
from .models.candidate_event import (
    CandidateEvent,
    GateBypassActivePayload,
    ShortlistAddedPayload,
)
from .models.common import Direction, EntryType, Market
from .models.log_enums import (
    ActorKind,
    BrokerMode,
    CandidateGrade,
    EventType,
    SessionLabel,
)
from .reporting.journal import write_session_journal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session_init",
        description="Open a trading session, ingest the morning scan, run the monitor loop.",
    )
    parser.add_argument(
        "--scan",
        type=Path,
        required=True,
        help="Path to scan_YYYYMMDD.json (produced by swing-committee's browser download).",
    )
    parser.add_argument(
        "--account-size",
        type=float,
        required=True,
        help="Account size in GBP at session open. Stamped to the sessions row.",
    )
    parser.add_argument(
        "--label",
        type=str,
        default=SessionLabel.US_REGULAR.value,
        choices=[label.value for label in SessionLabel],
        help="Which session this run covers.",
    )
    parser.add_argument(
        "--duration-mins",
        type=int,
        default=390,  # US regular session ~6.5h
        help="How long to run the monitor loop (default: one US regular session).",
    )
    parser.add_argument(
        "--tick-seconds",
        type=int,
        default=60,
        help="Seconds between ticks (default: 60 — one per minute).",
    )
    parser.add_argument(
        "--rule-set-version",
        type=str,
        default="",
        help="Short git SHA of entry-rules at session open.",
    )
    parser.add_argument(
        "--notes",
        type=str,
        default="",
        help="Free-form label attached to the sessions row.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ingest the scan and emit SHORTLIST_ADDED events, then exit without running the loop.",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports"),
        help="Directory to write end-of-session markdown journals. Default: ./reports",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


# ---------------------------------------------------------------------------
# Session orchestration
# ---------------------------------------------------------------------------


def _resolve_candidates(
    writer: SessionWriter,
    market_data: MarketData,
) -> list[CandidatePlan]:
    """Load the shortlist from SQLite + resolve an IG epic per candidate.

    Raises if any candidate's epic cannot be resolved — without an epic we
    can't fetch live prices, and writing blind snapshots would be a worse
    outcome than bailing.
    """
    if writer.scan_id is None:
        raise RuntimeError("SessionWriter has no scan_id — did ingest_scan run?")

    rows = writer.database.conn.execute(
        """
        SELECT candidate_id, scan_id, symbol, market, direction, setup_type,
               grade, trigger_low, trigger_high, stop_price, target_price,
               planned_stake_gbp_per_pt, planned_risk_gbp,
               broker_mode, rule_set_version
        FROM shortlist_entries
        WHERE scan_id = ?
        ORDER BY symbol
        """,
        (writer.scan_id,),
    ).fetchall()

    plans: list[CandidatePlan] = []
    for row in rows:
        symbol = row["symbol"]
        market = row["market"]
        epic = market_data.resolve_epic(symbol, market)
        if not epic:
            raise RuntimeError(
                f"Could not resolve IG epic for {symbol} ({market}). "
                "Refusing to open the loop without a price source."
            )
        plans.append(
            CandidatePlan(
                candidate_id=row["candidate_id"],
                scan_id=row["scan_id"],
                session_id=writer.session_id,
                symbol=symbol,
                market=Market(market),
                direction=Direction(row["direction"]),
                setup_type=EntryType(row["setup_type"]),
                grade=CandidateGrade(row["grade"]),
                trigger_low=row["trigger_low"],
                trigger_high=row["trigger_high"],
                stop_price=row["stop_price"],
                target_price=row["target_price"],
                ig_epic=epic,
                broker_mode=BrokerMode(row["broker_mode"]),
                rule_set_version=row["rule_set_version"] or "",
                planned_stake_gbp_per_pt=row["planned_stake_gbp_per_pt"] or 0.0,
                planned_risk_gbp=row["planned_risk_gbp"] or 0.0,
                gate_bypass=writer.gate_bypass,
            )
        )
    return plans


def _emit_gate_bypass_active_event(writer: SessionWriter) -> None:
    """If the ingested scan carries gate_bypass=True, emit a session-level
    GATE_BYPASS_ACTIVE event so the journal makes the bypass unmissable.

    No-op when bypass is off. Safe to call unconditionally.
    """
    if not writer.gate_bypass:
        return
    if writer.bypass_until is None or writer.session_id is None or writer.scan_id is None:
        # Paranoia: ingest_scan sets all three before flipping gate_bypass on.
        logger.warning(
            "gate_bypass=True but bypass_until/session_id/scan_id not populated; "
            "skipping GATE_BYPASS_ACTIVE event emission."
        )
        return
    event = CandidateEvent(
        id=str(uuid4()),
        session_id=writer.session_id,
        candidate_id=None,  # session-level, not per-candidate
        ts_utc=datetime.utcnow(),
        event_type=EventType.GATE_BYPASS_ACTIVE,
        actor=ActorKind.INGESTER,
        payload=GateBypassActivePayload(
            bypass_until=writer.bypass_until,
            selected_candidate_count=writer.bypass_candidate_count,
            scan_id=writer.scan_id,
        ),
        broker_mode=writer.broker_mode,
        rule_set_version=writer.rule_set_version,
    )
    writer.write_events([event])
    logger.warning(
        "GATE_BYPASS_ACTIVE emitted: bypass_until=%s, %d curated candidate(s). "
        "Pre-trade entry gates are informational for this session only.",
        writer.bypass_until.isoformat(),
        writer.bypass_candidate_count,
    )


def _emit_shortlist_added_events(
    writer: SessionWriter, plans: list[CandidatePlan]
) -> None:
    """One SHORTLIST_ADDED event per candidate, written in a single batch."""
    if not plans:
        return
    now = datetime.utcnow()
    events = [
        CandidateEvent(
            id=str(uuid4()),
            session_id=plan.session_id,
            candidate_id=plan.candidate_id,
            ts_utc=now,
            event_type=EventType.SHORTLIST_ADDED,
            actor=ActorKind.INGESTER,
            payload=ShortlistAddedPayload(
                grade=plan.grade.value,
                planned_stake_gbp_per_pt=0.0,  # populated from shortlist row if needed
                planned_risk_gbp=0.0,
            ),
            broker_mode=plan.broker_mode,
            rule_set_version=plan.rule_set_version,
        )
        for plan in plans
    ]
    # Hydrate planned_stake_gbp_per_pt + planned_risk_gbp from the DB rows so
    # events carry the real numbers — not zeros.
    sizing_rows = writer.database.conn.execute(
        """
        SELECT candidate_id, planned_stake_gbp_per_pt, planned_risk_gbp
        FROM shortlist_entries WHERE scan_id = ?
        """,
        (plans[0].scan_id,),
    ).fetchall()
    sizing = {r["candidate_id"]: r for r in sizing_rows}
    for ev in events:
        s = sizing.get(ev.candidate_id)
        if s is not None:
            ev.payload.planned_stake_gbp_per_pt = s["planned_stake_gbp_per_pt"]
            ev.payload.planned_risk_gbp = s["planned_risk_gbp"]
    writer.write_events(events)
    logger.info("Emitted SHORTLIST_ADDED x %d", len(events))


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = get_settings()
    broker_mode = BrokerMode(settings.ig_acc_type.value)
    session_label = SessionLabel(args.label)

    scan_path: Path = args.scan
    if not scan_path.exists():
        logger.error("Scan file does not exist: %s", scan_path)
        return 2

    # Connect to IG up-front. If this fails we never open a session row —
    # we refuse to open a session we couldn't actually monitor.
    ig_session = IGSession(settings)
    ig_session.connect()
    market_data = MarketData(ig_session)

    session_id_for_journal: str | None = None
    try:
        with Database() as db:
            with SessionWriter(
                db,
                broker_mode=broker_mode,
                session_label=session_label,
                account_size_gbp=args.account_size,
                rule_set_version=args.rule_set_version,
                notes=args.notes,
            ) as writer:
                session_id_for_journal = writer.session_id
                logger.info("Session opened: id=%s", writer.session_id)

                # Resume overnight positions FIRST so that if the scan ingest
                # or epic resolution fails, we at least re-attached to any
                # live IG positions and will continue managing them.
                try:
                    live_positions = fetch_live_positions(ig_session)
                except Exception as e:  # noqa: BLE001 — defensive at IG edge
                    logger.warning(
                        "fetch_live_positions failed (%s) — starting with no resumed state.",
                        e,
                    )
                    live_positions = []
                resumed = rehydrate_open_positions(
                    database=db,
                    live_positions=live_positions,
                    new_session_id=writer.session_id,
                    market_data=market_data,
                    writer=writer,
                    broker_mode=broker_mode,
                    rule_set_version=args.rule_set_version,
                )
                logger.info(
                    "Resumed %d position(s) from prior session(s).", len(resumed)
                )

                scan_id = writer.ingest_scan(scan_path)
                logger.info("Scan ingested: scan_id=%s", scan_id)

                # If the scan was curated under gate_bypass, stamp a prominent
                # session-level event so the journal can't miss it. No-op when
                # bypass is off, so safe to call unconditionally.
                _emit_gate_bypass_active_event(writer)

                plans = _resolve_candidates(writer, market_data)
                logger.info("Resolved %d candidate epic(s).", len(plans))

                _emit_shortlist_added_events(writer, plans)

                if args.dry_run:
                    logger.info("--dry-run: exiting after ingest + SHORTLIST_ADDED events.")
                    return 0

                end_time = datetime.utcnow() + timedelta(minutes=args.duration_mins)
                # market_data is passed to Broker so order-placement and
                # stop-modify calls scale stop_level/stop_distance/limit_level
                # into IG's quoted units using each epic's scalingFactor.
                broker = Broker(ig_session, market_data=market_data)
                exit_config = ExitConfig(
                    trail_activation_gbp=settings.trail_activation_gbp,
                    trail_initial_lock_gbp=settings.trail_initial_lock_gbp,
                    trail_step_trigger_gbp=settings.trail_step_trigger_gbp,
                    trail_step_size_gbp=settings.trail_step_size_gbp,
                    trail_hard_target_gbp=settings.trail_hard_target_gbp,
                    invalidation_window_minutes=settings.invalidation_window_minutes,
                    timestop_sessions=settings.timestop_sessions,
                )
                loop = MonitorLoop(
                    writer=writer,
                    market_data=market_data,
                    plans=plans,
                    tick_interval_seconds=args.tick_seconds,
                    broker=broker,
                    exit_config=exit_config,
                )
                for resumed_plan, resumed_state in resumed:
                    loop.seed_resumed_position(resumed_plan, resumed_state)
                logger.info(
                    "Starting monitor loop: %d candidates (%d resumed), "
                    "%d min duration, %ds tick, broker=Broker(%s), exit_config=%s",
                    len(loop.plans),
                    len(resumed),
                    args.duration_mins,
                    args.tick_seconds,
                    settings.ig_acc_type.value,
                    exit_config,
                )
                loop.run_until(end_time)
                logger.info("Monitor loop finished cleanly.")
            # SessionWriter.__exit__ has now stamped closed_at_utc. Write
            # the journal outside the writer scope so the report captures
            # the closed timestamp.
            try:
                path = write_session_journal(
                    database=db,
                    session_id=session_id_for_journal,
                    output_dir=args.reports_dir,
                )
                logger.info("Session journal written: %s", path)
            except Exception as e:  # noqa: BLE001
                logger.exception("Failed to write session journal: %s", e)
            return 0
    finally:
        ig_session.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
