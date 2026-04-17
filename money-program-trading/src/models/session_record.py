"""
SessionRecord — one row per trading session.

Opens at scan-ingest time, closes at session end. All downstream snapshot and
event rows carry this ``session_id`` plus the ``broker_mode`` denormalised.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .log_enums import BrokerMode, SessionLabel

LOG_SCHEMA_VERSION = 1
"""Observability log schema version. Bump on any breaking change to the five
log record models below. Old rows remain readable — queries partition on this.
"""


class SessionRecord(BaseModel):
    """One row per trading session.

    Writable fields are populated at session open; ``closed_at_utc`` is updated
    on clean shutdown.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(default_factory=lambda: str(uuid4()))
    session_date: date
    session_label: SessionLabel
    broker_mode: BrokerMode = Field(
        description="DEMO or LIVE. Required — denormalised onto every downstream row."
    )

    account_size_gbp: float = Field(
        ge=0.0,
        description=(
            "Account size snapshot at session open. Stamped — does not update intra-session."
        ),
    )

    opened_at_utc: datetime = Field(default_factory=datetime.utcnow)
    closed_at_utc: datetime | None = None

    scan_id: str | None = Field(
        default=None,
        description=(
            "FK to the one ScanRecord ingested for this session. "
            "None until the scan is ingested."
        ),
    )

    rule_set_version: str = Field(
        description=(
            "Short git SHA of entry-rules at session open. "
            "Lets us partition analysis on rule-tweak boundaries."
        ),
    )
    schema_version: int = LOG_SCHEMA_VERSION

    # Free-form label, e.g. "morning-run" / "post-maintenance-restart".
    # Not used by queries — audit-only.
    notes: str = ""
