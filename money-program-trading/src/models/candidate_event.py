"""
CandidateEvent — event-driven row, written on state transitions and rule-based
decisions (enter / don't-enter / move-stop / close).

This is the decision log. No free text, no Claude narrative — every field is
structured or enum. Per-event payload lives in a Pydantic-discriminated union
rather than a generic dict so queries can rely on field types.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .common import Direction
from .log_enums import (
    ActorKind,
    BrokerMode,
    EventType,
    RegimeState,
    StopMoveReason,
    StopSource,
    TargetHitReason,
    TerminalReason,
)
from .session_record import LOG_SCHEMA_VERSION

# --- Payload types ---------------------------------------------------------


class _PayloadBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShortlistAddedPayload(_PayloadBase):
    kind: Literal["SHORTLIST_ADDED"] = "SHORTLIST_ADDED"
    grade: str
    planned_stake_gbp_per_pt: float
    planned_risk_gbp: float


class GateFlippedPayload(_PayloadBase):
    kind: Literal["GATE_FLIPPED"] = "GATE_FLIPPED"
    gate_name: str
    old_passed: bool
    new_passed: bool
    value: float | int | bool | None = None


class TriggerArmedPayload(_PayloadBase):
    kind: Literal["TRIGGER_ARMED"] = "TRIGGER_ARMED"
    trigger_low: float
    trigger_high: float
    stop_price: float
    direction: Direction


class EntryEvaluatedNoEnterPayload(_PayloadBase):
    """Per-minute 'we looked, we did not enter' record."""

    kind: Literal["ENTRY_EVALUATED_NO_ENTER"] = "ENTRY_EVALUATED_NO_ENTER"
    rejection_code: str = Field(description="R01–R19 or an internal code.")
    last_price: float | None = None
    would_enter_if_rejection_lifted: bool | None = None


class InvalidatedPreTriggerPayload(_PayloadBase):
    kind: Literal["INVALIDATED_PRE_TRIGGER"] = "INVALIDATED_PRE_TRIGGER"
    reason: str
    last_price: float | None = None


class SessionEndedNoTriggerPayload(_PayloadBase):
    kind: Literal["SESSION_ENDED_NO_TRIGGER"] = "SESSION_ENDED_NO_TRIGGER"


class TriggerFiredPayload(_PayloadBase):
    kind: Literal["TRIGGER_FIRED"] = "TRIGGER_FIRED"
    last_price: float
    trigger_low: float
    trigger_high: float


class OrderPlacedPayload(_PayloadBase):
    kind: Literal["ORDER_PLACED"] = "ORDER_PLACED"
    ig_deal_reference: str
    order_type: str
    stake_gbp_per_pt: float
    stop_price: float


class FilledPayload(_PayloadBase):
    kind: Literal["FILLED"] = "FILLED"
    ig_deal_id: str
    fill_price: float
    fill_ts_utc: datetime
    stake_gbp_per_pt: float
    initial_stop_price: float
    initial_risk_gbp: float


class PositionResumedPayload(_PayloadBase):
    """Emitted when the monitor re-attaches to an IG position that was left
    open at the end of a prior session.

    One event per resumed candidate at session open. The payload records
    exactly what the monitor assumed on re-attachment so the timeline shows
    where trail state / stop authority / sessions_held came from.
    """

    kind: Literal["POSITION_RESUMED"] = "POSITION_RESUMED"
    ig_deal_id: str
    fill_price: float
    fill_ts_utc: datetime | None = None
    stake_gbp_per_pt: float
    initial_stop_price: float
    current_stop_price: float | None = None
    source_of_stop: StopSource
    peak_pnl_gbp_at_resume: float
    trail_step_count: int = Field(ge=0, le=5)
    trail_mode_activated: bool
    sessions_held: int = Field(ge=1)
    prior_fill_session_id: str = Field(
        description="session_id of the session where the FILLED event was originally written."
    )


class StopMovedPayload(_PayloadBase):
    """Emitted once per STOP_MOVED event — even when a fast tick crosses
    multiple £5 bands at once. ``new_trail_step_count − old_trail_step_count``
    is the 'bands crossed in one tick' signal for log analysis."""

    kind: Literal["STOP_MOVED"] = "STOP_MOVED"
    reason: StopMoveReason
    old_stop: float
    new_stop: float
    old_trail_step_count: int = Field(ge=0, le=5)
    new_trail_step_count: int = Field(ge=0, le=5)
    old_locked_gbp: float
    new_locked_gbp: float
    peak_pnl_gbp_at_move: float


class TrailModeActivatedPayload(_PayloadBase):
    kind: Literal["TRAIL_MODE_ACTIVATED"] = "TRAIL_MODE_ACTIVATED"
    peak_pnl_gbp: float
    trail_activation_gbp: float
    initial_locked_gbp: float


class InvalidationExitPayload(_PayloadBase):
    kind: Literal["INVALIDATION_EXIT"] = "INVALIDATION_EXIT"
    last_price: float
    mins_since_fill: int
    fill_price: float


class StopHitPayload(_PayloadBase):
    kind: Literal["STOP_HIT"] = "STOP_HIT"
    stop_price: float
    fill_price: float
    realised_pnl_gbp: float


class TargetHitPayload(_PayloadBase):
    kind: Literal["TARGET_HIT"] = "TARGET_HIT"
    reason: TargetHitReason
    target_price: float | None = Field(
        default=None,
        description=(
            "Price at which the target fired. Absent for HARD_TARGET_GBP — use peak_pnl_gbp."
        ),
    )
    peak_pnl_gbp: float
    realised_pnl_gbp: float


class TimestopHitPayload(_PayloadBase):
    kind: Literal["TIMESTOP_HIT"] = "TIMESTOP_HIT"
    last_price: float
    sessions_held: int
    realised_pnl_gbp: float


class TrailExitPayload(_PayloadBase):
    kind: Literal["TRAIL_EXIT"] = "TRAIL_EXIT"
    trail_stop_price: float
    fill_price: float
    locked_gbp: float
    trail_step_count: int = Field(ge=0, le=5)
    realised_pnl_gbp: float


class RegimeChangedPayload(_PayloadBase):
    kind: Literal["REGIME_CHANGED"] = "REGIME_CHANGED"
    old_regime: RegimeState
    new_regime: RegimeState
    mcl_score: float | None = None


class RejectedRiskBudgetPayload(_PayloadBase):
    kind: Literal["REJECTED_RISK_BUDGET"] = "REJECTED_RISK_BUDGET"
    would_be_risk_gbp: float
    budget_remaining_gbp: float
    portfolio_heat_pct: float


class GateBypassActivePayload(_PayloadBase):
    """One emitted per session at session_init time when the ingested scan
    carries ``gate_bypass=True``. Makes the bypass prominent in the journal so
    a mechanics-test session can never be mistaken for a fully-gated one.
    """

    kind: Literal["GATE_BYPASS_ACTIVE"] = "GATE_BYPASS_ACTIVE"
    bypass_until: date
    selected_candidate_count: int = Field(
        ge=1,
        description="How many candidates the user curated into this scan.",
    )
    scan_id: str


# Discriminated union of all payload shapes. `kind` field drives discrimination.
EventPayload = Annotated[
    (
        ShortlistAddedPayload
        | GateFlippedPayload
        | TriggerArmedPayload
        | EntryEvaluatedNoEnterPayload
        | InvalidatedPreTriggerPayload
        | SessionEndedNoTriggerPayload
        | TriggerFiredPayload
        | OrderPlacedPayload
        | FilledPayload
        | PositionResumedPayload
        | StopMovedPayload
        | TrailModeActivatedPayload
        | InvalidationExitPayload
        | StopHitPayload
        | TargetHitPayload
        | TimestopHitPayload
        | TrailExitPayload
        | RegimeChangedPayload
        | RejectedRiskBudgetPayload
        | GateBypassActivePayload
    ),
    Field(discriminator="kind"),
]


# --- Event row -------------------------------------------------------------


class CandidateEvent(BaseModel):
    """One row per rule-based decision or state transition.

    Stored in SQLite as: scalar columns + payload_json (the serialised union).
    Queries against typed payload fields go through DuckDB's JSON extraction
    or via a view that unnests per-event-type.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    candidate_id: str | None = Field(
        default=None,
        description=(
            "Most events attach to a candidate. Session-wide events "
            "(REGIME_CHANGED with no candidate context) leave this as None."
        ),
    )
    ts_utc: datetime = Field(default_factory=datetime.utcnow)

    event_type: EventType
    actor: ActorKind
    reason_code: str | None = Field(
        default=None,
        description=(
            "R01–R19 for gate-rejections; enum-derived string for exit reasons "
            "(e.g. 'TRAIL_STEP', 'HARD_TARGET_GBP', 'TIMESTOP'). Optional — payload "
            "also carries the reason in typed form for most events."
        ),
    )

    payload: EventPayload

    terminal_reason: TerminalReason | None = Field(
        default=None,
        description="Set only on the event that terminates a candidate's lifecycle.",
    )

    broker_mode: BrokerMode
    schema_version: int = LOG_SCHEMA_VERSION
    rule_set_version: str = ""
