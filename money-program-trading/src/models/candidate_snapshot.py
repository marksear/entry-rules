"""
CandidateSnapshot — one row per shortlisted candidate per minute during the session.

Flat column layout: everything you'd want to query ("was this trade working at
14:17 on day 2?") lives in a typed column on this row, not buried in JSON.
Compensates for single-entry sizing by making every "why did/didn't we enter?"
decision a SQL query away.

Schema: ~55 columns. See Observability_Design_v1.md §3.4.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .common import Direction, EntryType, Market
from .log_enums import BrokerMode, CandidateGrade, CandidateStatus, RegimeState
from .session_record import LOG_SCHEMA_VERSION


class CandidateSnapshot(BaseModel):
    """Per-minute state vector for a shortlisted candidate."""

    model_config = ConfigDict(extra="forbid")

    # -- Identity --------------------------------------------------------
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    candidate_id: str = Field(description="FK to ShortlistEntry.candidate_id.")
    scan_id: str
    symbol: str
    market: Market
    direction: Direction
    setup_type: EntryType
    grade: CandidateGrade

    ts_utc: datetime
    minute_bucket: int = Field(
        description=(
            "YYYYMMDDHHMM integer derived from ts_utc at write time — lets queries "
            "GROUP BY minute without date-parsing."
        ),
    )
    status: CandidateStatus
    broker_mode: BrokerMode
    schema_version: int = LOG_SCHEMA_VERSION
    rule_set_version: str = ""

    # -- Price state -----------------------------------------------------
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread_pts: float | None = None
    spread_pct: float | None = None
    dist_to_trigger_pts: float | None = Field(
        default=None, description="Signed — positive = price favourable toward trigger."
    )
    dist_to_stop_pts: float | None = None
    dist_to_target_pts: float | None = None

    # -- Indicators ------------------------------------------------------
    adx_14: float | None = None
    rs_percentile: float | None = Field(default=None, ge=0.0, le=100.0)
    ma_alignment_bitmap: int | None = Field(
        default=None,
        ge=0,
        le=0b111,
        description="3-bit: bit0=price>MA50, bit1=MA50>MA150, bit2=MA150>MA200.",
    )
    vol_pace_ratio: float | None = Field(
        default=None,
        description="Projected session volume / 50d avg. > 1.0 means hotter than normal.",
    )
    vwap_offset_pts: float | None = None
    atr_daily: float | None = None
    atr_intraday_est: float | None = None

    # -- Regime ----------------------------------------------------------
    mcl_regime: RegimeState | None = None
    mcl_score: float | None = None
    vix_level: float | None = None

    # -- Committee (copied from shortlist for row self-sufficiency) -------
    pillar_pass_count: int | None = Field(default=None, ge=0, le=6)
    pillar_bitmap: int | None = Field(default=None, ge=0, le=0b111111)
    committee_stance: str = ""

    # -- Risk / sizing live ----------------------------------------------
    current_stake_gbp_per_pt: float | None = None
    current_risk_gbp: float | None = None
    margin_required_gbp: float | None = None
    portfolio_heat_pct: float | None = Field(default=None, ge=0.0)
    fits_heat_limit: bool | None = None
    fits_margin: bool | None = None
    would_enter_now: bool | None = Field(
        default=None,
        description="True iff, at this minute, all gates pass and all budgets fit.",
    )
    rejection_code: str | None = Field(
        default=None,
        description="R01–R19 if would_enter_now is False due to a gate; None otherwise.",
    )

    # -- Position state (only when status == TRIGGERED_OPEN) -------------
    fill_price: float | None = None
    fill_ts_utc: datetime | None = None
    elapsed_mins_in_position: int | None = Field(default=None, ge=0)
    elapsed_sessions_in_position: int | None = Field(
        default=None,
        ge=0,
        description="Counts session-boundary crossings since fill (1-indexed vs 3-day cap).",
    )
    unrealised_pnl_gbp: float | None = None
    peak_unrealised_pnl_gbp: float | None = Field(
        default=None,
        description=(
            "One-way ratchet — max of itself and unrealised_pnl_gbp. Drives the stepped "
            "£-trail; never decreases intra-position."
        ),
    )
    unrealised_r: float | None = Field(
        default=None, description="Unrealised P&L divided by initial risk (in R-multiples)."
    )
    current_stop_price: float | None = None
    current_locked_profit_gbp: float | None = Field(
        default=None,
        description=(
            "£ above breakeven locked by the current trail band. 0 pre-arm; £1 / £6 / £11 / "
            "£16 / £21 after each trail step (mirrors Exit_Management_v1.md §2)."
        ),
    )
    stop_moved_count: int | None = Field(default=None, ge=0)
    trail_step_count: int | None = Field(
        default=None,
        ge=0,
        le=5,
        description=(
            "0 = not armed; 1 = armed at BE+£1; 2 = +£6; 3 = +£11; 4 = +£16; 5 = +£21. "
            "Hard exit at peak ≥ £50 terminates before a 6th step is possible."
        ),
    )
    invalidation_window_active: bool | None = Field(
        default=None,
        description="True iff now − fill_ts_utc < invalidation_window_minutes (default 30).",
    )
    trail_mode_active: bool | None = Field(
        default=None,
        description="True iff peak_unrealised_pnl_gbp ≥ trail_activation_gbp (default £25).",
    )
    mins_to_timestop: int | None = Field(
        default=None,
        description=(
            "Count-down in minutes to the 2–3 session hard close. Negative values mean "
            "timestop has passed (should not happen — a TIMESTOP_HIT should have fired)."
        ),
    )
