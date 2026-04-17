"""
ShortlistEntry — one row per A+/A/B candidate selected from a scan.

This is the set we track per-minute during the session. Contains the plan
(trigger level, stop, target, planned sizing) that the entry-rules engine
will attempt to execute.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import Direction, EntryType, Market
from .log_enums import BrokerMode, CandidateGrade
from .session_record import LOG_SCHEMA_VERSION


class PillarVotes(BaseModel):
    """Which of the six pillars passed for this candidate.

    Duplicated from :class:`UniverseScoreEntry.pillar_bitmap` in a typed shape
    so it's queryable without bit-twiddling.
    """

    model_config = ConfigDict(extra="forbid")

    livermore: bool = False
    oneil: bool = False
    minervini: bool = False
    darvas: bool = False
    raschke: bool = False
    weinstein: bool = False

    @property
    def count(self) -> int:
        return sum(
            (self.livermore, self.oneil, self.minervini, self.darvas, self.raschke, self.weinstein)
        )

    @property
    def bitmap(self) -> int:
        return (
            (1 if self.livermore else 0)
            | (2 if self.oneil else 0)
            | (4 if self.minervini else 0)
            | (8 if self.darvas else 0)
            | (16 if self.raschke else 0)
            | (32 if self.weinstein else 0)
        )


class ShortlistEntry(BaseModel):
    """One candidate from the morning's A+/A/B shortlist.

    Written by swing-committee into ``scan_YYYYMMDD.json``; ingested by
    ``session_init.py`` into SQLite. Immutable after ingest — later state lives
    on ``CandidateSnapshot`` rows.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(default_factory=lambda: str(uuid4()))
    scan_id: str = Field(description="FK to ScanRecord.scan_id.")
    session_id: str | None = Field(
        default=None, description="Filled by the ingester (swing-committee does not know it)."
    )

    symbol: str
    market: Market
    direction: Direction
    setup_type: EntryType
    grade: CandidateGrade

    # Plan (what we'd execute if the trigger fires)
    trigger_low: float = Field(description="Low end of the entry trigger zone.")
    trigger_high: float = Field(description="High end of the entry trigger zone.")
    stop_price: float
    target_price: float | None = Field(
        default=None,
        description="Indicative target (informational only — v1 exits use the stepped £-trail).",
    )

    # Sizing — intent at scan time. Actuals land on snapshot / event rows.
    planned_stake_gbp_per_pt: float = Field(
        ge=0.0,
        description="£ per point. IG minimum is £0.10/pt; 0 would indicate a skip.",
    )
    planned_risk_gbp: float = Field(
        ge=0.0,
        description="abs(entry − stop) × stake_gbp_per_pt. The risk we'd book on fill.",
    )
    planned_risk_pct_account: float = Field(
        ge=0.0,
        le=1.0,
        description="Planned risk as fraction of account (A+=0.01, A=0.0075, B=0.005).",
    )

    # Scorer context
    pillar_votes: PillarVotes = Field(default_factory=PillarVotes)
    committee_stance: str = Field(
        default="", description="Free-text committee stance from /api/analyze (audit-only)."
    )
    day1_score: float | None = None
    day1_tier: str | None = None

    # Provenance
    broker_mode: BrokerMode
    created_at_utc: datetime = Field(default_factory=datetime.utcnow)
    schema_version: int = LOG_SCHEMA_VERSION
    rule_set_version: str = ""

    # Forward-compat bag for scanner-side fields we haven't typed yet.
    extras: dict[str, Any] = Field(default_factory=dict)

    notes: str = ""

    @model_validator(mode="after")
    def _validate_trigger_and_direction(self) -> ShortlistEntry:
        if self.trigger_low > self.trigger_high:
            raise ValueError(
                f"trigger_low ({self.trigger_low}) must be <= trigger_high ({self.trigger_high})"
            )

        # Direction must agree with setup_type.
        if self.direction != self.setup_type.direction:
            raise ValueError(
                f"direction={self.direction.value} contradicts setup_type={self.setup_type.value} "
                f"(which implies {self.setup_type.direction.value})"
            )

        # Stop must be on the correct side of the trigger zone.
        if self.direction == Direction.LONG and self.stop_price >= self.trigger_low:
            raise ValueError(
                f"LONG: stop_price ({self.stop_price}) must be < trigger_low ({self.trigger_low})"
            )
        if self.direction == Direction.SHORT and self.stop_price <= self.trigger_high:
            raise ValueError(
                f"SHORT: stop_price ({self.stop_price}) must be > "
                f"trigger_high ({self.trigger_high})"
            )
        return self

    @property
    def risk_per_pt(self) -> float:
        """Distance (in points) between entry trigger and stop. 1R = this × stake."""
        if self.direction == Direction.LONG:
            return self.trigger_low - self.stop_price
        return self.stop_price - self.trigger_high
