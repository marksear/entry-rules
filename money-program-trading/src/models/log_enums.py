"""
Enums shared across the observability log record types.

Keeps values stable — these enum strings are persisted to disk (SQLite / Parquet)
and referenced by log-replay queries, so adding a value is safe but changing or
removing one is a schema-breaking change.
"""

from __future__ import annotations

from enum import Enum

# --- Session / broker context ---


class BrokerMode(str, Enum):
    """Which broker environment the session is running against.

    Required on every session, denormalised to every snapshot/event row so that
    DEMO and LIVE data never get co-mingled during analysis.
    """

    DEMO = "DEMO"
    LIVE = "LIVE"


class SessionLabel(str, Enum):
    """The trading session this run covers."""

    US_REGULAR = "US_REGULAR"
    UK_REGULAR = "UK_REGULAR"
    US_EXTENDED = "US_EXTENDED"


# --- Regime / candidate state ---


class RegimeState(str, Enum):
    """MCL (Market Condition Layer) regime at a point in time."""

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class CandidateStatus(str, Enum):
    """Lifecycle state of a shortlisted candidate.

    PENDING_TRIGGER  — shortlisted at scan open, trigger not yet fired
    TRIGGERED_OPEN   — filled, managing the open position
    TERMINAL         — closed out (exit event emitted; see :class:`TerminalReason`)
    """

    PENDING_TRIGGER = "PENDING_TRIGGER"
    TRIGGERED_OPEN = "TRIGGERED_OPEN"
    TERMINAL = "TERMINAL"


class TerminalReason(str, Enum):
    """Why a candidate reached terminal state.

    Exactly one of these is set on the final snapshot row / the final event's
    payload for a candidate. Mirrors the exit hierarchy in
    ``entry-rules/Exit_Management_v1.md``.
    """

    STOPPED_OUT = "STOPPED_OUT"
    TARGET_HIT = "TARGET_HIT"
    HARD_TARGET_HIT = "HARD_TARGET_HIT"  # £50 peak-P&L market exit
    TIMESTOP_HIT = "TIMESTOP_HIT"
    TRAIL_EXIT = "TRAIL_EXIT"
    INVALIDATION_EXIT = "INVALIDATION_EXIT"
    INVALIDATED_PRE_TRIGGER = "INVALIDATED_PRE_TRIGGER"
    SESSION_ENDED_NO_TRIGGER = "SESSION_ENDED_NO_TRIGGER"
    # Intraday-preferred exit: open position carried into the last N minutes of
    # the session and was force-closed to avoid an overnight hold. Set by
    # ``evaluate_exit`` when ``SessionClock.hard_close_utc`` is crossed.
    HARD_CLOSE = "HARD_CLOSE"
    # S-3 Phase 4a: price feed went silent (no fresh ticks) for longer than
    # ``Settings.price_feed_degraded_seconds`` while a position was open.
    # Monitor force-closed via broker REST rather than hold a position it
    # couldn't see prices for. See docs/specs/S3_LIGHTSTREAMER_SPEC.md §7.2.
    DEGRADED_FEED = "DEGRADED_FEED"


# --- Grades (matches swing-committee's scorer output) ---


class CandidateGrade(str, Enum):
    """Committee-assigned grade.

    A+/A/B are the production ladder. C is accepted ONLY on DEMO bypass
    (mechanics-test) runs — real trading policy (per
    ``feedback_small_account_sizing``) never sizes C, and
    ``backtest/trade_management.risk_percent_for_grade`` returns 0.0 for it.
    Having C in the enum lets scan_YYYYMMDD.json validate when the
    swing-committee UI has enabled bypass and the user has picked a C to
    shake down the pipeline end-to-end.
    """

    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"


# --- Event taxonomy ---


class ActorKind(str, Enum):
    """Which rule-engine component emitted an event."""

    SCANNER = "SCANNER"
    INGESTER = "INGESTER"
    GATE_ENGINE = "GATE_ENGINE"
    RISK_MANAGER = "RISK_MANAGER"
    EXECUTOR = "EXECUTOR"
    TRAIL_MANAGER = "TRAIL_MANAGER"
    TIMESTOP_MONITOR = "TIMESTOP_MONITOR"
    REGIME_MONITOR = "REGIME_MONITOR"


class EventType(str, Enum):
    """The event taxonomy from Observability_Design_v1.md §3.5."""

    # Pre-entry
    SHORTLIST_ADDED = "SHORTLIST_ADDED"
    GATE_FLIPPED = "GATE_FLIPPED"
    TRIGGER_ARMED = "TRIGGER_ARMED"
    ENTRY_EVALUATED_NO_ENTER = "ENTRY_EVALUATED_NO_ENTER"
    INVALIDATED_PRE_TRIGGER = "INVALIDATED_PRE_TRIGGER"
    SESSION_ENDED_NO_TRIGGER = "SESSION_ENDED_NO_TRIGGER"

    # Entry
    TRIGGER_FIRED = "TRIGGER_FIRED"
    ORDER_PLACED = "ORDER_PLACED"
    FILLED = "FILLED"

    # Position lifecycle — resume (re-attach to an overnight IG position)
    POSITION_RESUMED = "POSITION_RESUMED"

    # In-position
    STOP_MOVED = "STOP_MOVED"
    TRAIL_MODE_ACTIVATED = "TRAIL_MODE_ACTIVATED"
    INVALIDATION_EXIT = "INVALIDATION_EXIT"
    STOP_HIT = "STOP_HIT"
    TARGET_HIT = "TARGET_HIT"
    TIMESTOP_HIT = "TIMESTOP_HIT"
    TRAIL_EXIT = "TRAIL_EXIT"
    # Intraday-preferred: force-close triggered by SessionClock, not by price.
    HARD_CLOSE_EXIT = "HARD_CLOSE_EXIT"

    # Environmental
    REGIME_CHANGED = "REGIME_CHANGED"
    REJECTED_RISK_BUDGET = "REJECTED_RISK_BUDGET"

    # S-4 interim price-divergence gate. Monitor refused to evaluate exit
    # logic this tick because its last_traded disagreed with the broker's
    # deal price by more than the configured threshold (or no deal price
    # was available). See ADD_DIVERGENCE_GATE_SPEC.md.
    PRICE_DIVERGENCE_SKIP = "PRICE_DIVERGENCE_SKIP"

    # S-3 Phase 4a — price feed staleness escalation.
    # See docs/specs/S3_LIGHTSTREAMER_SPEC.md §7.2.
    #
    # PRICE_STALE — feed's latest() raised StalePriceError this tick
    # (cached tick older than the feed's ``stale_seconds`` threshold,
    # typically 10s). Monitor skipped trigger/exit evaluation for this
    # tick; emitted every stale tick for observability. Harmless short
    # glitches (<60s) don't escalate.
    PRICE_STALE = "PRICE_STALE"
    # PRICE_FEED_DEGRADED — staleness exceeded the feed's
    # ``degraded_seconds`` threshold (typically 60s). Emitted once per
    # degradation episode (not per tick) to avoid log spam. When a
    # position is open on the epic, this event is paired with a
    # defensive close + POSITION_CLOSED_DEGRADED_FEED.
    PRICE_FEED_DEGRADED = "PRICE_FEED_DEGRADED"
    # PRICE_FEED_RECOVERED — fresh tick arrived after a degradation
    # episode. Emitted once on the transition so post-session analysis
    # can pair DEGRADED → RECOVERED intervals. Not emitted for stale
    # periods that never escalated to DEGRADED (to keep the event
    # stream quiet on routine <60s glitches).
    PRICE_FEED_RECOVERED = "PRICE_FEED_RECOVERED"
    # POSITION_CLOSED_DEGRADED_FEED — terminal close event emitted when
    # defensive-close fires at the 60s threshold. Uses broker REST
    # (independent of the stale feed). Carries the same realised_pnl_gbp
    # + close_fill_price fields as other terminal events so journals
    # treat it uniformly.
    POSITION_CLOSED_DEGRADED_FEED = "POSITION_CLOSED_DEGRADED_FEED"

    # Session-level — governance / bypass
    GATE_BYPASS_ACTIVE = "GATE_BYPASS_ACTIVE"


class StopMoveReason(str, Enum):
    """Why a STOP_MOVED event fired.

    TRAIL_ARM    — first move, on peak P&L crossing ``trail_activation_gbp``
    TRAIL_STEP   — subsequent band advance (peak P&L crossed another £5 band)
    MANUAL       — explicit operator override (not used by v1 automation)
    """

    TRAIL_ARM = "TRAIL_ARM"
    TRAIL_STEP = "TRAIL_STEP"
    MANUAL = "MANUAL"


class TargetHitReason(str, Enum):
    """Why a TARGET_HIT event fired.

    HARD_TARGET_GBP — peak P&L reached ``trail_hard_target_gbp`` (£50 default)
    R_MULTIPLE      — reserved for future fixed-R target support (not v1)
    """

    HARD_TARGET_GBP = "HARD_TARGET_GBP"
    R_MULTIPLE = "R_MULTIPLE"


class StopSource(str, Enum):
    """Where the ``current_stop_price`` on a resumed position came from.

    IG_STOP_LEVEL   — IG's ``stopLevel`` on the open position (authoritative
                      when a stop was moved via the IG web UI between sessions)
    DB_STOP_MOVED   — latest ``STOP_MOVED`` event in the local DB (trail ratchet)
    FILL_INITIAL    — initial stop from the FILLED event (no STOP_MOVED yet)
    """

    IG_STOP_LEVEL = "IG_STOP_LEVEL"
    DB_STOP_MOVED = "DB_STOP_MOVED"
    FILL_INITIAL = "FILL_INITIAL"
