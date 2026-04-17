"""
Audit log — every decision logged with full context.

This is the weekly review surface. The only human touchpoint
in the entire system.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Any

from ..models.audit_entry import AuditEntry
from ..config.settings import get_settings
from .db import Database

logger = logging.getLogger(__name__)


class AuditLog:
    """
    Dual-output audit logger:
    1. SQLite database (queryable, for analytics)
    2. Daily JSON files (human-readable, for review)
    """

    def __init__(self, db: Database, json_dir: str | None = None):
        self._db = db
        self._json_dir = Path(json_dir or get_settings().audit_log_dir)
        self._json_dir.mkdir(parents=True, exist_ok=True)

    def log(self, entry: AuditEntry) -> None:
        """Log a decision to both SQLite and JSON file."""
        self._log_to_db(entry)
        self._log_to_json(entry)
        logger.info(
            "AUDIT | %s | %s | %s %s | %s | %s",
            entry.ticker,
            entry.direction.value,
            entry.entry_type.value,
            entry.entry_type.display_name if hasattr(entry.entry_type, 'display_name') else '',
            entry.decision.value,
            entry.reason_code.value if entry.reason_code else "—",
        )

    def _log_to_db(self, entry: AuditEntry) -> None:
        """Insert into the audit_log table."""
        try:
            self._db.conn.execute(
                """
                INSERT INTO audit_log (
                    timestamp, signal_id, ticker, market, direction,
                    entry_type, decision, reason_code, reason_detail,
                    gates_json, levels_json, volume_json, tranche_json,
                    short_specific_json, uk_specific_json,
                    ig_deal_reference, ig_deal_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.timestamp.isoformat(),
                    entry.signal_id,
                    entry.ticker,
                    entry.market.value,
                    entry.direction.value,
                    entry.entry_type.value,
                    entry.decision.value,
                    entry.reason_code.value if entry.reason_code else None,
                    entry.reason_detail,
                    entry.gates.model_dump_json(),
                    entry.levels.model_dump_json(),
                    entry.volume.model_dump_json(),
                    entry.tranche.model_dump_json(),
                    entry.short_specific.model_dump_json() if entry.short_specific else None,
                    entry.uk_specific.model_dump_json() if entry.uk_specific else None,
                    entry.ig_deal_reference,
                    entry.ig_deal_id,
                ),
            )
            self._db.conn.commit()
        except Exception as e:
            logger.error("Failed to log to database: %s", e)

    def _log_to_json(self, entry: AuditEntry) -> None:
        """Append to the daily JSON log file."""
        today = date.today().isoformat()
        path = self._json_dir / f"audit_{today}.json"

        try:
            # Load existing entries
            entries: list[dict] = []
            if path.exists():
                with open(path) as f:
                    entries = json.load(f)

            # Append new entry
            entries.append(json.loads(entry.model_dump_json()))

            # Write back
            with open(path, "w") as f:
                json.dump(entries, f, indent=2, default=str)

        except Exception as e:
            logger.error("Failed to log to JSON file: %s", e)

    # ── Query Methods (for weekly review) ─────────────────────

    def get_entries(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        ticker: str | None = None,
        decision: str | None = None,
        direction: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query audit log entries with optional filters."""
        query = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []

        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date)
        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date)
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        if decision:
            query += " AND decision = ?"
            params.append(decision)
        if direction:
            query += " AND direction = ?"
            params.append(direction)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = self._db.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_rejection_summary(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> list[dict]:
        """Aggregate rejection reasons for analysis."""
        query = """
            SELECT reason_code, COUNT(*) as count, direction
            FROM audit_log
            WHERE decision IN ('REJECT', 'SKIP')
        """
        params: list[Any] = []

        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date)
        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date)

        query += " GROUP BY reason_code, direction ORDER BY count DESC"

        cursor = self._db.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_entry_count(self, start_date: str | None = None) -> dict:
        """Quick stats: how many entries, skips, rejects."""
        query = "SELECT decision, COUNT(*) as count FROM audit_log"
        params: list[Any] = []

        if start_date:
            query += " WHERE timestamp >= ?"
            params.append(start_date)

        query += " GROUP BY decision"
        cursor = self._db.conn.execute(query, params)
        return {row["decision"]: row["count"] for row in cursor.fetchall()}
