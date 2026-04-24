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
from ..utils.time_utils import utc_now
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
    # Broker-enforced £ take-profit (added 2026-04-21 after the DEMO
    # TERMINAL-monitor silent-failure on JNJ). Optional so replay of
    # pre-2026-04-21 events from the DB doesn't fail validation.
    # - limit_level_scan: the price in scan units we asked IG to close at
    # - limit_level_ig:   the same price after scalingFactor conversion
    # - target_gbp:       £ P&L the limit was computed to lock
    # - grade_used:       the grade whose target drove the £ figure
    limit_level_scan: float | None = None
    limit_level_ig: float | None = None
    target_gbp: float | None = None
    grade_used: str | None = None


class FilledPayload(_PayloadBase):
    kind: Literal["FILLED"] = "FILLED"
    ig_deal_id: str
    fill_price: float
    fill_ts_utc: datetime
    stake_gbp_per_pt: float
    initial_stop_price: float
    initial_risk_gbp: float
    # Mirrors OrderPlacedPayload — carried on the FILLED event so the
    # journal shows "opened at X, stop at Y, limit at Z for £target"
    # on every fill, without cross-joining ORDER_PLACED + FILLED.
    limit_level_scan: float | None = None
    limit_level_ig: float | None = None
    target_gbp: float | None = None
    grade_used: str | None = None


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


class HardCloseExitPayload(_PayloadBase):
    """Emitted when the SessionClock force-closes an open position before
    session end.

    Distinct from TIMESTOP_HIT: timestop is a multi-session rule (held ≥ N
    sessions); hard-close fires within the *same* session as fill, once
    ``now`` crosses ``hard_close_utc`` (session end minus buffer). Recording
    it as its own event avoids polluting the timestop metric.
    """

    kind: Literal["HARD_CLOSE_EXIT"] = "HARD_CLOSE_EXIT"
    last_price: float
    fill_price: float
    mins_to_session_end: int
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


class PriceDivergenceSkipPayload(_PayloadBase):
    """S-4 interim gate skip event. Emitted when the monitor refused to
    evaluate exit logic because its cached price disagreed with the
    broker's live deal price (or no deal price was available).

    ``reason_code`` is one of:
      - ``DIVERGENCE_OVER_THRESHOLD`` — both prices present but
        |monitor - deal| / deal * 10000 > threshold_bps
      - ``NO_DEAL_PRICE`` — broker.get_deal_price returned None
        (fail-safe: we can't verify the feed, so we don't act on it)

    ``delta_bps`` and ``threshold_bps`` are present for
    ``DIVERGENCE_OVER_THRESHOLD`` and may be None for ``NO_DEAL_PRICE``.
    """

    kind: Literal["PRICE_DIVERGENCE_SKIP"] = "PRICE_DIVERGENCE_SKIP"
    reason: str
    monitor_price: float | None = None
    deal_price: float | None = None
    delta_bps: float | None = None
    threshold_bps: float | None = None
    consecutive_skips: int = 0


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


# --- S-3 Phase 4a price-feed staleness payloads --------------------------


class PriceStalePayload(_PayloadBase):
    """Emitted every tick the monitor skipped evaluation because the feed's
    ``latest()`` raised ``StalePriceError``. Short-glitch observability; not
    paired with any terminal or close action on its own.

    See docs/specs/S3_LIGHTSTREAMER_SPEC.md §7.2.
    """

    kind: Literal["PRICE_STALE"] = "PRICE_STALE"
    epic: str
    tick_age_seconds: float = Field(
        ge=0.0,
        description=(
            "Age of the most recent cached tick, from the StalePriceError the "
            "feed raised. 0 is permitted — callers may clamp +inf at emit time."
        ),
    )
    stale_duration_seconds: float = Field(
        ge=0.0,
        description=(
            "How long staleness has persisted on this epic since the first "
            "stale tick of the current episode. Resets when a fresh tick "
            "arrives (RECOVERED) or when escalated (DEGRADED)."
        ),
    )
    has_open_position: bool = Field(
        description="True if a position is open on the epic at this tick.",
    )


class PriceFeedDegradedPayload(_PayloadBase):
    """Emitted once per degradation episode when stale duration exceeds
    ``Settings.price_feed_degraded_seconds`` (default 60s). Not emitted per
    tick — spec §7.2 table row for the 60s+ threshold.
    """

    kind: Literal["PRICE_FEED_DEGRADED"] = "PRICE_FEED_DEGRADED"
    epic: str
    stale_duration_seconds: float = Field(ge=0.0)
    degraded_threshold_seconds: float = Field(ge=0.0)
    had_open_position: bool = Field(
        description=(
            "Whether a position was open on this epic when degradation was "
            "declared. When true, a POSITION_CLOSED_DEGRADED_FEED event "
            "follows (if broker close succeeds)."
        ),
    )


class PriceFeedRecoveredPayload(_PayloadBase):
    """Emitted on the first fresh tick after a degradation episode. We do
    NOT emit RECOVERED after transient (<60s) stale periods that never
    escalated to DEGRADED — that would be log spam.
    """

    kind: Literal["PRICE_FEED_RECOVERED"] = "PRICE_FEED_RECOVERED"
    epic: str
    stale_duration_seconds: float = Field(
        ge=0.0,
        description="How long the degradation episode lasted (close-out).",
    )


class PositionClosedDegradedFeedPayload(_PayloadBase):
    """Terminal close event fired by the defensive-close path when the
    feed degrades while a position is open. Distinct from STOP_HIT /
    TARGET_HIT / TRAIL_EXIT because the trigger was the *feed*, not the
    price. Realised P&L is computed from the broker's actual close fill,
    same as other terminal events.
    """

    kind: Literal["POSITION_CLOSED_DEGRADED_FEED"] = "POSITION_CLOSED_DEGRADED_FEED"
    epic: str
    deal_id: str
    close_fill_price: float | None = None
    realised_pnl_gbp: float | None = None
    stale_duration_seconds: float = Field(ge=0.0)


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
        | HardCloseExitPayload
        | RegimeChangedPayload
        | RejectedRiskBudgetPayload
        | PriceDivergenceSkipPayload
        | GateBypassActivePayload
        | PriceStalePayload
        | PriceFeedDegradedPayload
        | PriceFeedRecoveredPayload
        | PositionClosedDegradedFeedPayload
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
    ts_utc: datetime = Field(default_factory=utc_now)

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
