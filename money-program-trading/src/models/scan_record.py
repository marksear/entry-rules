"""
ScanRecord — one row per morning scan.

Emitted by swing-committee as a rich artifact alongside the execution-contract
``data/trades.json``. Captures the full universe scoring so we can answer
"why wasn't X shortlisted?" retrospectively, long after the scan has run.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .log_enums import BrokerMode, RegimeState
from .session_record import LOG_SCHEMA_VERSION


class RegimeSnapshot(BaseModel):
    """MCL regime state captured at scan time.

    The MCL policy lives in swing-committee (``lib/mclPolicy.js``). We mirror
    its output here as structured fields so we don't need to re-run the policy
    to interpret an old scan.
    """

    model_config = ConfigDict(extra="forbid")

    regime: RegimeState
    regime_score: float | None = Field(
        default=None,
        description="Composite MCL score at scan time (None if the policy does not surface one).",
    )
    vix_level: float | None = None
    breadth_us: float | None = Field(default=None, description="US market breadth reading")
    breadth_uk: float | None = Field(default=None, description="UK market breadth reading")
    notes: str = ""


class UniverseScoreEntry(BaseModel):
    """One row per ticker in the scanned universe.

    This is the raw scorer output — per-pillar votes, per-gate pass/fail, grade
    if any, rejection reason if any. Stored verbatim so we can retrospectively
    ask questions like "what did the O'Neil pillar vote on OXY on 2026-04-12?".
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: str = Field(description="US or UK (string rather than enum for ingest tolerance).")
    price: float | None = None
    currency: str | None = None

    pillar_pass_count: int = Field(ge=0, le=6, description="How many of the six pillars passed.")
    pillar_bitmap: int = Field(
        ge=0,
        le=0b111111,
        description=(
            "6-bit field, 1 per pillar. Bit order: Livermore=0, O'Neil=1, "
            "Minervini=2, Darvas=3, Raschke=4, Weinstein=5."
        ),
    )

    day1_score: float | None = Field(
        default=None, description="Day-1 capture scorer output (dayTradeScorer.js)."
    )
    day1_tier: str | None = Field(
        default=None, description='"A-GRADE" / "B-GRADE" / None if day-trade not scored.'
    )

    grade: str | None = Field(
        default=None, description='"A+" / "A" / "B" / None if below B / not shortlisted.'
    )
    shortlisted: bool = False
    rejection_reason: str | None = None
    rejection_code: str | None = Field(
        default=None, description="Rejection code string if rejected by a gate-style check."
    )


class EmissionRejection(BaseModel):
    """One shortlist entry dropped by swing-committee's emission-side
    price-anchor pass. The counterpart to ``scan_anchor`` ingest-side
    rejections — see docs/ig_price_grounding_spec.md §5.2.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    direction: str
    grade: str
    reason: str = Field(
        description=(
            "DRIFT_OVER_THRESHOLD | NO_REFERENCE_QUOTE | STALE_QUOTE | "
            "CURRENCY_MISMATCH"
        )
    )
    llm_trigger_mid: float | None = None
    reference_last_traded: float | None = None
    drift_pct: float | None = None
    price_source: str | None = None
    price_as_of_utc: datetime | None = None


class ScanRecord(BaseModel):
    """One row per morning scan. Emitted by swing-committee.

    Paired with the executable ``data/trades.json`` — same scan, two files.
    """

    model_config = ConfigDict(extra="forbid")

    scan_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str | None = Field(
        default=None,
        description=(
            "Set by entry-rules at ingest (swing-committee does not know the session_id). "
            "swing-committee writes this as None; session_init.py fills it in."
        ),
    )

    scanned_at_utc: datetime
    universe_size: int = Field(ge=0)

    broker_mode: BrokerMode = Field(
        description=(
            "Whether this scan targets DEMO or LIVE execution. Separate from entry-rules' "
            "own broker_mode so that a DEMO-sourced scan never gets wired to a LIVE session "
            "by mistake — session_init.py asserts agreement."
        )
    )

    regime: RegimeSnapshot = Field(description="MCL regime snapshot captured at scan time.")

    scored_universe: list[UniverseScoreEntry] = Field(
        default_factory=list,
        description="Full scorer output for the universe. Can be large (~175 rows).",
    )

    scanner_version: str = Field(
        default="",
        description="Short git SHA of swing-committee at scan time, if available.",
    )
    rule_set_version: str = Field(
        default="",
        description="Short git SHA of entry-rules known to swing-committee (if shared); "
        "else populated by the ingester at session_init time.",
    )
    schema_version: int = LOG_SCHEMA_VERSION

    # ── Gate bypass (mechanics-test mode) ─────────────────────
    # When swing-committee emits a user-curated shortlist (user ticked rows from
    # the Trade Signals tab), it stamps gate_bypass=True and a bypass_until date.
    # entry-rules refuses to ingest the scan after bypass_until expires — a hard
    # stop against drift. Exit management and %-of-account sizing are NOT
    # affected; only the pre-trade entry filters (pillars / grade / MCL regime)
    # are treated as informational under bypass.
    gate_bypass: bool = Field(
        default=False,
        description=(
            "If True, this scan is a user-curated mechanics-test override. "
            "Pre-trade entry gates are informational only; the shortlist is "
            "trusted as-is. Requires bypass_until."
        ),
    )
    bypass_until: date | None = Field(
        default=None,
        description=(
            "Expiry date (UTC) for gate_bypass. session_init refuses to ingest "
            "a bypass scan after this date. Required if gate_bypass=True."
        ),
    )

    emission_rejections: list[EmissionRejection] | None = Field(
        default=None,
        description=(
            "Entries dropped by swing-committee's emission-side anchor pass. "
            "None = pre-grounding (v1) scan; [] = grounding ran with no drops. "
            "Counterpart to scan_anchor's ingest-side rejections."
        ),
    )

    extras: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form bag for forward-compatible fields. Production queries should never "
            "rely on values here — promote to a typed field and bump schema_version."
        ),
    )

    @model_validator(mode="after")
    def _check_bypass_fields(self) -> ScanRecord:
        """gate_bypass=True must carry a bypass_until date; expiry is enforced
        at ingest time by session_init, not here (we accept historical scans
        with past bypass_until dates for replay / backtest)."""
        if self.gate_bypass and self.bypass_until is None:
            raise ValueError(
                "gate_bypass=True requires bypass_until to be set "
                "(the expiry date past which ingest is refused)."
            )
        return self
