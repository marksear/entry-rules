"""
SessionWriter — context manager that persists a trading session's observability
log streams to SQLite (see db.py, v2 schema).

Usage::

    with Database() as db:
        with SessionWriter(
            db,
            broker_mode=BrokerMode.DEMO,
            session_label=SessionLabel.US_REGULAR,
            account_size_gbp=1000.0,
            rule_set_version="abc1234",
        ) as writer:
            writer.ingest_scan(Path("data/scans/scan_20260416.json"))
            writer.write_snapshot(snapshot)
            writer.write_event(event)

Design notes
------------
- The SessionWriter does NOT own the :class:`Database`. The caller opens and
  closes the DB; the writer only opens and closes a *logical* session row.
- ``__enter__`` inserts a fresh ``sessions`` row and stamps the session_id.
  ``__exit__`` stamps ``closed_at_utc`` (unless the caller already called
  :meth:`close_session`).
- ``ingest_scan`` reads a handoff file of shape
  ``{schema_version, scan_record, shortlist_entries}`` produced by
  swing-committee's ``lib/scanEmission.js`` (see project_deployment.md memory).
  It validates via pydantic before touching the DB and uses a single
  transaction so a bad shortlist row rolls back the whole scan ingest.
- ``write_snapshot`` / ``write_event`` accept validated pydantic instances.
  They do NOT re-validate — that would waste cycles in the hot per-minute
  loop. Callers should build the model once and pass it.
- Enum values are persisted as their string ``.value`` (matches scan_universe
  queries that compare against free-text "LONG" / "A+" / etc).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..models.candidate_event import CandidateEvent
from ..models.candidate_snapshot import CandidateSnapshot
from ..models.log_enums import BrokerMode, SessionLabel
from ..models.scan_record import ScanRecord
from ..models.session_record import LOG_SCHEMA_VERSION, SessionRecord
from ..models.shortlist_entry import ShortlistEntry
from .db import Database
from ..utils.time_utils import utc_now

if TYPE_CHECKING:
    from ..data.market_data import MarketData

logger = logging.getLogger(__name__)


def _iso(dt: datetime | None) -> str | None:
    """ISO-format a datetime (or pass through None)."""
    return dt.isoformat() if dt is not None else None


def _enum_value(v: Any) -> Any:
    """Return enum ``.value`` when ``v`` is an Enum, otherwise pass through."""
    return v.value if hasattr(v, "value") else v


def _json_or_null(v: Any) -> str | None:
    """Serialise a dict to JSON, or return None if empty/falsy.

    None and empty dicts both become NULL in SQLite so queries can use
    ``WHERE extras_json IS NULL``.
    """
    if not v:
        return None
    return json.dumps(v, default=str)


def _as_int_bool(v: Any) -> int | None:
    """True/False → 1/0, None → None. SQLite stores booleans as INTEGER."""
    if v is None:
        return None
    return 1 if v else 0


class SessionWriter:
    """Persists observability rows for one trading session.

    The writer is a context manager. Opening inserts a ``sessions`` row;
    closing stamps ``closed_at_utc``. All writes commit per-call so that a
    crash mid-session still leaves an intact log.
    """

    def __init__(
        self,
        database: Database,
        *,
        broker_mode: BrokerMode,
        session_label: SessionLabel,
        account_size_gbp: float,
        rule_set_version: str,
        notes: str = "",
        session_date: date | None = None,
    ):
        self.database = database
        self.broker_mode = broker_mode
        self.session_label = session_label
        self.account_size_gbp = account_size_gbp
        self.rule_set_version = rule_set_version
        self.notes = notes
        self.session_date = session_date or date.today()

        self.session_id: str | None = None
        self.scan_id: str | None = None
        # Populated by ingest_scan() if the scan carries gate_bypass=True.
        # session_init reads these to emit GATE_BYPASS_ACTIVE and to warn loudly.
        self.gate_bypass: bool = False
        self.bypass_until: date | None = None
        self.bypass_candidate_count: int = 0
        # Populated by ingest_scan() from scan_record.emission_rejections.
        # session_init reads these to log a summary line alongside the
        # ingest-side scan_anchor rejections — see
        # docs/ig_price_grounding_spec.md §7.
        self.emission_rejections: list = []
        self._opened_at: datetime | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> SessionWriter:
        self.database.initialize()
        self._opened_at = utc_now()
        self.session_id = str(uuid4())
        record = SessionRecord(
            session_id=self.session_id,
            session_date=self.session_date,
            session_label=self.session_label,
            broker_mode=self.broker_mode,
            account_size_gbp=self.account_size_gbp,
            opened_at_utc=self._opened_at,
            rule_set_version=self.rule_set_version,
            notes=self.notes,
        )
        self._insert_session(record)
        logger.info(
            "Session opened: id=%s date=%s broker=%s label=%s",
            self.session_id,
            self.session_date.isoformat(),
            self.broker_mode.value,
            self.session_label.value,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if not self._closed:
            self.close_session()
        return False

    # ------------------------------------------------------------------
    # Session row
    # ------------------------------------------------------------------

    def _insert_session(self, record: SessionRecord) -> None:
        self.database.conn.execute(
            """
            INSERT INTO sessions (
                session_id, session_date, session_label, broker_mode,
                account_size_gbp, opened_at_utc, closed_at_utc, scan_id,
                rule_set_version, schema_version, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.session_id,
                record.session_date.isoformat(),
                record.session_label.value,
                record.broker_mode.value,
                record.account_size_gbp,
                record.opened_at_utc.isoformat(),
                _iso(record.closed_at_utc),
                record.scan_id,
                record.rule_set_version,
                record.schema_version,
                record.notes,
            ),
        )
        self.database.conn.commit()

    def close_session(self, notes: str | None = None) -> None:
        """Stamp ``closed_at_utc`` (and optionally append to notes)."""
        if self._closed:
            return
        if self.session_id is None:
            # Never opened. Nothing to close.
            self._closed = True
            return

        closed_at = utc_now().isoformat()
        if notes is not None:
            self.database.conn.execute(
                """
                UPDATE sessions
                SET closed_at_utc = ?, notes = ?
                WHERE session_id = ?
                """,
                (closed_at, notes, self.session_id),
            )
        else:
            self.database.conn.execute(
                "UPDATE sessions SET closed_at_utc = ? WHERE session_id = ?",
                (closed_at, self.session_id),
            )
        self.database.conn.commit()
        self._closed = True
        logger.info("Session closed: id=%s at=%s", self.session_id, closed_at)

    # ------------------------------------------------------------------
    # Scan ingest
    # ------------------------------------------------------------------

    def ingest_scan(
        self,
        scan_json_path: Path | str,
        *,
        market_data: "MarketData | None" = None,
        anchor_max_drift_pct: float = 0.15,
    ) -> str:
        """Load a swing-committee scan handoff and persist it.

        Inserts rows into ``scans``, ``scan_universe``, and
        ``shortlist_entries`` in a single transaction, then stamps
        ``sessions.scan_id``. Returns the ``scan_id``.

        Price grounding
        ---------------
        When ``market_data`` is provided, every shortlist entry is
        checked against IG's current snapshot via
        :func:`src.engine.scan_anchor.anchor_shortlist_to_ig`. Entries
        whose trigger-zone midpoint drifts more than
        ``anchor_max_drift_pct`` from IG's ``last_traded`` are **dropped
        before insert** and the rejection is logged. This is the fix
        for the 2026-04-17 DEMO shakedown where swing-committee's LLM
        emitted AMD at $278 (IG ~$155) and FDX at $380 (IG $383.50
        after scaling). If ``market_data`` is ``None``, grounding is
        skipped — callers doing unit tests or offline replay opt out
        that way.

        Args:
            scan_json_path: Path to ``scan_YYYYMMDD.json``.
            market_data: Optional authenticated ``MarketData``. Required
                for price grounding; pass ``None`` to skip grounding
                (unit tests, offline replay).
            anchor_max_drift_pct: Maximum tolerated drift between scan
                reference price and IG last_traded (0.15 = 15%).

        Raises:
            FileNotFoundError: if the handoff file is missing.
            ValueError: if broker_mode in the scan disagrees with the session.
            pydantic.ValidationError: if the handoff shape is wrong.
            RuntimeError: if grounding drops every entry and ``gate_bypass``
                is not set (we refuse to ingest an empty shortlist in
                normal mode — safer to fail loud than open a session
                with no candidates).
        """
        if self.session_id is None:
            raise RuntimeError("SessionWriter not opened — use 'with' or call __enter__ first.")

        path = Path(scan_json_path)
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        if "scan_record" not in payload or "shortlist_entries" not in payload:
            raise ValueError(
                f"Handoff file {path} missing 'scan_record' or 'shortlist_entries' keys"
            )

        scan = ScanRecord.model_validate(payload["scan_record"])
        entries = [ShortlistEntry.model_validate(e) for e in payload["shortlist_entries"]]

        if scan.broker_mode != self.broker_mode:
            raise ValueError(
                f"Scan broker_mode ({scan.broker_mode.value}) does not match session "
                f"broker_mode ({self.broker_mode.value}). Refusing to ingest to prevent "
                "DEMO/LIVE co-mingling."
            )

        # Gate bypass expiry — hard stop against drift. A mechanics-test scan
        # stamped with a bypass_until date must still be within that window.
        # (The pydantic validator already enforced bypass_until-required-if-on.)
        if scan.gate_bypass:
            if scan.bypass_until is None:  # belt; validator already covers this
                raise ValueError(
                    "Scan has gate_bypass=True but no bypass_until date; refusing ingest."
                )
            today = date.today()
            if scan.bypass_until < today:
                raise ValueError(
                    f"Scan bypass expired on {scan.bypass_until.isoformat()} "
                    f"(today is {today.isoformat()}). Refusing ingest — re-curate the "
                    "shortlist in swing-committee with a fresh bypass_until window."
                )
            # Reject DEMO-only — we never let bypass touch LIVE.
            if scan.broker_mode != BrokerMode.DEMO:
                raise ValueError(
                    "gate_bypass=True is only permitted with broker_mode=DEMO. "
                    "Refusing ingest — this is a mechanics-test mode."
                )

        # Price grounding — reject LLM-hallucinated levels before they
        # poison the session. Skipped when:
        #   * market_data is None (unit tests / offline replay)
        #   * scan.gate_bypass is True — a mechanics-test scan explicitly
        #     says "trust these levels as-is for DEMO exercise". Running
        #     scan_anchor against IG would only add a new failure surface
        #     (transient IG snapshot issues, 401s, rate-limits) that
        #     defeats the purpose of bypass. Bypass already disables
        #     pre-trade entry gates downstream; anchor is conceptually the
        #     same class of check and should follow the same switch.
        #
        # `curated_count` preserves the pre-anchor size — the number of
        # entries the user (or swing-committee) actually curated. That's
        # what the GATE_BYPASS_ACTIVE event's `selected_candidate_count`
        # field is supposed to report (see models.candidate_event). The
        # post-anchor `len(entries)` is a different number and is logged
        # separately as "grounded candidate count".
        anchor_report = None
        curated_count = len(entries)
        if scan.gate_bypass:
            logger.info(
                "scan_anchor skipped: gate_bypass=True — trusting %d "
                "curated entries without IG price grounding.",
                curated_count,
            )
        elif market_data is not None and entries:
            from ..engine.scan_anchor import anchor_shortlist_to_ig

            anchor_report = anchor_shortlist_to_ig(
                entries,
                market_data,
                max_drift_pct=anchor_max_drift_pct,
            )
            pre_count = len(entries)
            entries = anchor_report.accepted
            logger.info(
                "Scan price grounding: %s (kept %d/%d)",
                anchor_report.summary_line(),
                len(entries),
                pre_count,
            )
            if not entries:
                raise RuntimeError(
                    "Scan price grounding dropped all shortlist entries. "
                    "Refusing to open a session with no candidates. "
                    f"Rejection reasons: {anchor_report.rejections_by_reason()}. "
                    "Either regenerate the scan, relax --anchor-max-drift, or "
                    "set gate_bypass=True for a mechanics-test run."
                )

        # Stamp the session_id onto the scan + shortlist rows before insert so
        # every downstream table links back to this session cleanly.
        scan = scan.model_copy(update={"session_id": self.session_id})
        entries = [e.model_copy(update={"session_id": self.session_id}) for e in entries]

        conn = self.database.conn
        try:
            conn.execute("BEGIN")
            self._insert_scan(scan)
            for u in scan.scored_universe:
                self._insert_scan_universe(scan.scan_id, u)
            for entry in entries:
                self._insert_shortlist_entry(entry)
            conn.execute(
                "UPDATE sessions SET scan_id = ? WHERE session_id = ?",
                (scan.scan_id, self.session_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        self.scan_id = scan.scan_id
        self.gate_bypass = scan.gate_bypass
        self.bypass_until = scan.bypass_until
        # `bypass_candidate_count` feeds the GATE_BYPASS_ACTIVE event's
        # `selected_candidate_count`, which the pydantic model constrains
        # to >= 1. Report the **curated** count (pre-anchor) per the
        # field's documented meaning — the post-anchor count is a
        # different concept (grounding survival) and is logged above.
        self.bypass_candidate_count = curated_count
        self.emission_rejections = list(scan.emission_rejections or [])
        logger.info(
            "Scan ingested: scan_id=%s universe=%d shortlist=%d",
            scan.scan_id,
            len(scan.scored_universe),
            len(entries),
        )
        if scan.gate_bypass:
            logger.warning(
                "GATE BYPASS ACTIVE — shortlist trusted as-is. Pre-trade entry gates "
                "are informational only. Exits + sizing remain on. bypass_until=%s "
                "(scan=%s, candidates=%d)",
                scan.bypass_until.isoformat() if scan.bypass_until else "?",
                scan.scan_id,
                len(entries),
            )
        return scan.scan_id

    def _insert_scan(self, scan: ScanRecord) -> None:
        self.database.conn.execute(
            """
            INSERT INTO scans (
                scan_id, session_id, scanned_at_utc, universe_size, broker_mode,
                regime, regime_score, vix_level, breadth_us, breadth_uk, regime_notes,
                scanner_version, rule_set_version, schema_version, extras_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan.scan_id,
                scan.session_id,
                scan.scanned_at_utc.isoformat(),
                scan.universe_size,
                scan.broker_mode.value,
                scan.regime.regime.value,
                scan.regime.regime_score,
                scan.regime.vix_level,
                scan.regime.breadth_us,
                scan.regime.breadth_uk,
                scan.regime.notes,
                scan.scanner_version,
                scan.rule_set_version,
                scan.schema_version,
                _json_or_null(scan.extras),
            ),
        )

    def _insert_scan_universe(self, scan_id: str, u: Any) -> None:
        self.database.conn.execute(
            """
            INSERT INTO scan_universe (
                scan_id, symbol, market, price, currency,
                pillar_pass_count, pillar_bitmap, day1_score, day1_tier, grade,
                shortlisted, rejection_reason, rejection_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                u.symbol,
                u.market,
                u.price,
                u.currency,
                u.pillar_pass_count,
                u.pillar_bitmap,
                u.day1_score,
                u.day1_tier,
                u.grade,
                _as_int_bool(u.shortlisted),
                u.rejection_reason,
                u.rejection_code,
            ),
        )

    def _insert_shortlist_entry(self, entry: ShortlistEntry) -> None:
        self.database.conn.execute(
            """
            INSERT INTO shortlist_entries (
                candidate_id, scan_id, session_id,
                symbol, market, direction, setup_type, grade,
                trigger_low, trigger_high, stop_price, target_price,
                planned_stake_gbp_per_pt, planned_risk_gbp, planned_risk_pct_account,
                pv_livermore, pv_oneil, pv_minervini, pv_darvas, pv_raschke, pv_weinstein,
                committee_stance, day1_score, day1_tier,
                broker_mode, created_at_utc, schema_version, rule_set_version,
                extras_json, notes
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                entry.candidate_id,
                entry.scan_id,
                entry.session_id,
                entry.symbol,
                entry.market.value,
                entry.direction.value,
                entry.setup_type.value,
                entry.grade.value,
                entry.trigger_low,
                entry.trigger_high,
                entry.stop_price,
                entry.target_price,
                entry.planned_stake_gbp_per_pt,
                entry.planned_risk_gbp,
                entry.planned_risk_pct_account,
                _as_int_bool(entry.pillar_votes.livermore),
                _as_int_bool(entry.pillar_votes.oneil),
                _as_int_bool(entry.pillar_votes.minervini),
                _as_int_bool(entry.pillar_votes.darvas),
                _as_int_bool(entry.pillar_votes.raschke),
                _as_int_bool(entry.pillar_votes.weinstein),
                entry.committee_stance,
                entry.day1_score,
                entry.day1_tier,
                entry.broker_mode.value,
                entry.created_at_utc.isoformat(),
                entry.schema_version,
                entry.rule_set_version,
                _json_or_null(entry.extras),
                entry.notes,
            ),
        )

    # ------------------------------------------------------------------
    # Snapshot writes
    # ------------------------------------------------------------------

    _SNAPSHOT_INSERT_SQL = """
        INSERT INTO candidate_snapshots (
            session_id, candidate_id, ts_utc, symbol, status,
            last_price, bid, ask,
            gate_hard_mask, gate_soft_mask, trigger_armed,
            entry_price, fill_ts_utc, stake_gbp_per_pt, initial_stop, current_stop,
            unrealised_pnl_gbp, peak_unrealised_pnl_gbp, current_locked_profit_gbp,
            trail_step_count, trail_mode_active,
            invalidation_window_active, mins_since_fill, mins_to_timestop,
            regime, broker_mode, schema_version, rule_set_version
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """

    @staticmethod
    def _snapshot_row(s: CandidateSnapshot) -> tuple:
        """Build the positional param tuple for ``_SNAPSHOT_INSERT_SQL``.

        Some DB columns (``gate_hard_mask``, ``gate_soft_mask``,
        ``initial_stop``) are not carried on the ``CandidateSnapshot`` model
        today — they are stored as NULL so queries can still reconstruct them
        by joining to ``shortlist_entries`` / ``candidate_events``. The
        ``trigger_armed`` column is derived from ``status`` so analysts don't
        need the pydantic struct to query "was this candidate armed?".
        """
        is_pending = s.status.value == "PENDING_TRIGGER"
        return (
            s.session_id,
            s.candidate_id,
            s.ts_utc.isoformat(),
            s.symbol,
            s.status.value,
            s.last_price,
            s.bid,
            s.ask,
            None,  # gate_hard_mask — not on pydantic snapshot
            None,  # gate_soft_mask — not on pydantic snapshot
            0 if is_pending else 1,  # trigger_armed (derived)
            s.fill_price,
            _iso(s.fill_ts_utc),
            s.current_stake_gbp_per_pt,
            None,  # initial_stop — derive by joining to shortlist_entries
            s.current_stop_price,
            s.unrealised_pnl_gbp,
            s.peak_unrealised_pnl_gbp,
            s.current_locked_profit_gbp,
            s.trail_step_count,
            _as_int_bool(s.trail_mode_active) or 0,
            _as_int_bool(s.invalidation_window_active) or 0,
            s.elapsed_mins_in_position,
            s.mins_to_timestop,
            _enum_value(s.mcl_regime),
            s.broker_mode.value,
            s.schema_version,
            s.rule_set_version,
        )

    def _assert_session(self, obj_session_id: str, kind: str) -> None:
        if self.session_id is None:
            raise RuntimeError("SessionWriter not opened.")
        if obj_session_id != self.session_id:
            raise ValueError(
                f"{kind}.session_id ({obj_session_id}) does not match "
                f"writer.session_id ({self.session_id})"
            )

    def write_snapshot(self, snapshot: CandidateSnapshot) -> None:
        """INSERT one candidate_snapshots row."""
        self._assert_session(snapshot.session_id, "Snapshot")
        self.database.conn.execute(self._SNAPSHOT_INSERT_SQL, self._snapshot_row(snapshot))
        self.database.conn.commit()

    def write_snapshots(self, snapshots: list[CandidateSnapshot]) -> None:
        """Batch snapshot insert. Single commit at the end."""
        if self.session_id is None:
            raise RuntimeError("SessionWriter not opened.")
        # Validate session_ids up front so we fail before BEGIN.
        for s in snapshots:
            self._assert_session(s.session_id, "Snapshot")
        conn = self.database.conn
        try:
            conn.execute("BEGIN")
            conn.executemany(
                self._SNAPSHOT_INSERT_SQL,
                [self._snapshot_row(s) for s in snapshots],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Event writes
    # ------------------------------------------------------------------

    _EVENT_INSERT_SQL = """
        INSERT INTO candidate_events (
            event_id, session_id, candidate_id, ts_utc,
            event_type, actor, reason_code,
            payload_json, terminal_reason,
            broker_mode, schema_version, rule_set_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    @staticmethod
    def _event_row(event: CandidateEvent) -> tuple:
        return (
            event.id,
            event.session_id,
            event.candidate_id,
            event.ts_utc.isoformat(),
            event.event_type.value,
            event.actor.value,
            event.reason_code,
            json.dumps(event.payload.model_dump(mode="json"), default=str),
            _enum_value(event.terminal_reason),
            event.broker_mode.value,
            event.schema_version,
            event.rule_set_version,
        )

    def write_event(self, event: CandidateEvent) -> None:
        """INSERT one candidate_events row."""
        self._assert_session(event.session_id, "Event")
        self.database.conn.execute(self._EVENT_INSERT_SQL, self._event_row(event))
        self.database.conn.commit()

    def write_events(self, events: list[CandidateEvent]) -> None:
        """Batch event insert. Single commit at the end."""
        if self.session_id is None:
            raise RuntimeError("SessionWriter not opened.")
        for event in events:
            self._assert_session(event.session_id, "Event")
        conn = self.database.conn
        try:
            conn.execute("BEGIN")
            conn.executemany(self._EVENT_INSERT_SQL, [self._event_row(e) for e in events])
            conn.commit()
        except Exception:
            conn.rollback()
            raise


__all__ = ["SessionWriter", "LOG_SCHEMA_VERSION"]
