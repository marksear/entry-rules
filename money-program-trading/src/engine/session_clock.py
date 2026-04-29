"""SessionClock — time-of-day gates for the intraday-preferred exit model.

Background
----------
Mark's mandate for Session 9 (2026-04-17): *keep* the swing-committee's
Livermore/O'Neil/Minervini/Darvas/Raschke/Weinstein signals, but **manage
every trade to exit the same day where possible**. Most swing setups move
in the direction of the thesis on day 1 — holding overnight just donates
the first day's edge back to the next session's gap risk.

This module is the time-of-day policy layer. It does not own any clock of
its own; callers pass ``now`` into the query methods so the same object is
safe across tests, backtests, and live runs.

Three cutoffs, evaluated in UTC
-------------------------------
* ``session_end_utc`` — the absolute wall-time the underlying market session
  ends (e.g. NYSE 16:00 ET ≈ 21:00 UK during BST → 20:00 UTC).
* ``hard_close_utc`` = ``session_end_utc - hard_close_buffer_minutes``
  (default 10 min). After this, open positions are force-closed by
  ``evaluate_exit`` returning ``EXIT(HARD_CLOSE)``.
* ``entries_cutoff_utc`` = min(``session_end_utc - no_new_entries_buffer_minutes``,
  ``last_entry_cutoff_utc``). After this, ``classify_tick`` suppresses
  FIRE so we don't open a position we immediately have to close.

Locked decisions (2026-04-17)
-----------------------------
* US session: hard-close buffer **10 min** (close at 20:50 UK / 19:50 UTC BST).
* US session: last-entry cutoff **19:30 UK** (2h before NY close — strictly
  earlier than "60 min before close", so it wins under min()).
* Intraday-preferred, NOT intraday-only — positions carried overnight are
  still legitimate; HARD_CLOSE just fires when the clock crosses the buffer.

Why this isn't folded into ``ExitConfig``
----------------------------------------
``ExitConfig`` is a frozen dataclass of numeric thresholds shared across
every candidate. The session clock is *per session*: it depends on today's
UK local date (DST flips between BST and GMT shift session_end_utc by an
hour) and on whether we're running a US or UK session. Building it at
``session_init`` keeps the exit rule pure — ``evaluate_exit`` just asks
"is now past hard_close?" without knowing about timezones.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from ..utils.time_utils import utc_now

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

if TYPE_CHECKING:
    pass


# ─── Defaults from locked 2026-04-17 decisions ────────────────────────────
DEFAULT_HARD_CLOSE_BUFFER_MINUTES: int = 10
DEFAULT_NO_NEW_ENTRIES_BUFFER_MINUTES: int = 60

# 19:30 UK local. During BST this is 18:30 UTC; during GMT it's 19:30 UTC.
# The helper resolves to UTC using the session's local date.
DEFAULT_US_LAST_ENTRY_CUTOFF_UK_LOCAL: time = time(hour=19, minute=30)

# Opening-buffer minutes — the amount of time after the market open
# during which classify_tick MUST NOT fire new entries. The opening
# 15 minutes is amateur-hour chop; the Masterclass (§3 entry window)
# and every rule author (Livermore, Minervini, Raschke) says wait for
# the noise to settle. Matches Settings.entry_window_start (09:45 ET).
# See feedback_entry_window_lower_bound.
DEFAULT_OPENING_BUFFER_MINUTES: int = 15

# NYSE regular hours close: 16:00 America/New_York → 21:00 UK year-round
# (US DST and UK BST transitions are aligned on all the days we'd run).
US_SESSION_CLOSE_ET_LOCAL: time = time(hour=16, minute=0)

# NYSE regular hours open: 09:30 America/New_York → 14:30 UK.
US_SESSION_OPEN_ET_LOCAL: time = time(hour=9, minute=30)

# FTSE regular hours close: 16:30 Europe/London.
UK_SESSION_CLOSE_UK_LOCAL: time = time(hour=16, minute=30)

# FTSE regular hours open: 08:00 Europe/London.
UK_SESSION_OPEN_UK_LOCAL: time = time(hour=8, minute=0)

# ─── LBR day-trade entry cutoff (Task 2026-04-29 — Rule S5) ──────────────
# Linda Bradford Raschke / standard intraday discipline: directional day
# trades should be initiated in the first ~1.5 hours after the market open.
# After this point the morning's directional regime has typically played
# out — afternoon entries chase mature moves with deteriorated R:R.
#
# This is a SECOND, EARLIER entries cutoff than the existing
# ``entries_cutoff_utc`` (which is risk-management-driven: stop opening
# positions in the closing window). The day-trade cutoff is entry-quality-
# driven: stop opening positions when the morning's signal is stale.
DEFAULT_US_DAY_TRADE_CUTOFF_ET_LOCAL: time = time(hour=11, minute=15)
DEFAULT_UK_DAY_TRADE_CUTOFF_UK_LOCAL: time = time(hour=10, minute=30)


@dataclass(frozen=True)
class SessionClock:
    """Absolute UTC cutoffs for one trading session.

    All fields are timezone-aware UTC. Build via :meth:`for_us_session` or
    :meth:`for_uk_session` so DST is resolved from the local date once,
    rather than every tick.
    """

    session_end_utc: datetime
    hard_close_utc: datetime
    entries_cutoff_utc: datetime
    entries_open_utc: datetime
    # Rule S5 / Task #71 (LBR day-trade cutoff): a second, earlier
    # entries-cutoff for intraday-managed entries. None disables the
    # gate (back-compat with old fixtures + tests). When set, an
    # entry that would otherwise FIRE past this UTC time returns
    # REJECT(R_DAY_TRADE_CUTOFF) instead.
    day_trade_cutoff_utc: datetime | None = None

    # Retained for observability / debugging; not needed by the decision path.
    hard_close_buffer_minutes: int = DEFAULT_HARD_CLOSE_BUFFER_MINUTES
    no_new_entries_buffer_minutes: int = DEFAULT_NO_NEW_ENTRIES_BUFFER_MINUTES
    opening_buffer_minutes: int = DEFAULT_OPENING_BUFFER_MINUTES
    market_label: str = ""

    # ──────────────────────────────────────────────────────────────────
    # Queries — called on every tick
    # ──────────────────────────────────────────────────────────────────

    def must_hard_close(self, now: datetime) -> bool:
        """True once ``now`` has crossed ``hard_close_utc``.

        After this returns True, ``evaluate_exit`` returns
        ``EXIT(HARD_CLOSE)`` on any subsequent tick for an open position.
        """
        return _as_utc(now) >= self.hard_close_utc

    def is_past_entries_cutoff(self, now: datetime) -> bool:
        """True once ``now`` has crossed ``entries_cutoff_utc``.

        After this returns True, ``classify_tick`` suppresses FIRE so we
        don't open a new position within the closing window.
        """
        return _as_utc(now) >= self.entries_cutoff_utc

    def is_before_entries_open(self, now: datetime) -> bool:
        """True until ``now`` reaches ``entries_open_utc``.

        The opening 15 minutes after session open is amateur-hour chop —
        every rule author in the Masterclass (Livermore, Minervini,
        Raschke) waits for the noise to settle before entering. When
        this returns True, ``classify_tick`` suppresses FIRE with
        ``R_SESSION_PREMATURE`` even if the technical trigger has fired.
        See feedback_entry_window_lower_bound and the TMUS 2026-04-24
        incident that motivated the gate.
        """
        return _as_utc(now) < self.entries_open_utc

    def is_past_day_trade_cutoff(self, now: datetime) -> bool:
        """True once ``now`` has crossed ``day_trade_cutoff_utc``
        (Rule S5, LBR-aligned 2026-04-29).

        Returns False when ``day_trade_cutoff_utc`` is None — the gate
        is opt-in via the builder argument so older fixtures and tests
        keep their existing behaviour.

        When this returns True, ``classify_tick`` returns
        ``REJECT(R_DAY_TRADE_CUTOFF)`` even if the strict-break trigger
        has fired. Rationale: directional day-trade entries should
        initiate in the first ~1.5h post-open; later entries chase
        mature moves with deteriorated R:R and are rule-violations of
        the intraday-managed profile.
        """
        if self.day_trade_cutoff_utc is None:
            return False
        return _as_utc(now) >= self.day_trade_cutoff_utc

    def minutes_to_session_end(self, now: datetime) -> int:
        """Signed minutes remaining until ``session_end_utc``.

        Positive before close, negative after. Handy for log lines and for
        the HARD_CLOSE_EXIT payload.
        """
        delta = self.session_end_utc - _as_utc(now)
        # Round toward zero — we never care about sub-minute accuracy.
        return int(delta.total_seconds() // 60)

    # ──────────────────────────────────────────────────────────────────
    # Builders
    # ──────────────────────────────────────────────────────────────────

    @classmethod
    def for_us_session(
        cls,
        session_date_local: date,
        *,
        hard_close_buffer_minutes: int = DEFAULT_HARD_CLOSE_BUFFER_MINUTES,
        no_new_entries_buffer_minutes: int = DEFAULT_NO_NEW_ENTRIES_BUFFER_MINUTES,
        opening_buffer_minutes: int = DEFAULT_OPENING_BUFFER_MINUTES,
        last_entry_cutoff_uk_local: time | None = DEFAULT_US_LAST_ENTRY_CUTOFF_UK_LOCAL,
        day_trade_cutoff_local: time | None = DEFAULT_US_DAY_TRADE_CUTOFF_ET_LOCAL,
    ) -> "SessionClock":
        """Build a clock for a NYSE regular-hours session.

        ``session_date_local`` is the UK local date the session runs on.
        We anchor the 16:00 ET close via the America/New_York zone so DST
        transitions are handled correctly on mismatched days (rare but real
        when US/UK DST boundaries fall apart, e.g. mid-March).

        ``last_entry_cutoff_uk_local`` is the *absolute* UK wall-clock
        cutoff (default 19:30). Setting it to None disables the absolute
        cutoff; only the relative "session_end - no_new_entries_buffer"
        rule applies.

        ``opening_buffer_minutes`` defaults to 15 — no new entries before
        09:45 ET. See :attr:`is_before_entries_open` for rationale.

        ``day_trade_cutoff_local`` is the LBR-aligned intraday-managed
        entries cutoff (Rule S5, default 11:15 ET). Setting to None
        disables the gate — back-compat for fixtures that don't expect
        the new rejection.
        """
        session_end_utc = _local_time_to_utc(
            session_date_local, US_SESSION_CLOSE_ET_LOCAL, "America/New_York"
        )
        session_open_utc = _local_time_to_utc(
            session_date_local, US_SESSION_OPEN_ET_LOCAL, "America/New_York"
        )
        day_trade_cutoff_utc: datetime | None = None
        if day_trade_cutoff_local is not None:
            day_trade_cutoff_utc = _local_time_to_utc(
                session_date_local, day_trade_cutoff_local, "America/New_York"
            )
        return cls._build(
            session_end_utc=session_end_utc,
            session_open_utc=session_open_utc,
            session_date_local=session_date_local,
            hard_close_buffer_minutes=hard_close_buffer_minutes,
            no_new_entries_buffer_minutes=no_new_entries_buffer_minutes,
            opening_buffer_minutes=opening_buffer_minutes,
            last_entry_cutoff_uk_local=last_entry_cutoff_uk_local,
            day_trade_cutoff_utc=day_trade_cutoff_utc,
            market_label="US",
        )

    @classmethod
    def for_uk_session(
        cls,
        session_date_local: date,
        *,
        hard_close_buffer_minutes: int = DEFAULT_HARD_CLOSE_BUFFER_MINUTES,
        no_new_entries_buffer_minutes: int = DEFAULT_NO_NEW_ENTRIES_BUFFER_MINUTES,
        opening_buffer_minutes: int = DEFAULT_OPENING_BUFFER_MINUTES,
        last_entry_cutoff_uk_local: time | None = None,
        day_trade_cutoff_local: time | None = DEFAULT_UK_DAY_TRADE_CUTOFF_UK_LOCAL,
    ) -> "SessionClock":
        """Build a clock for an LSE regular-hours session (FTSE 15).

        The FTSE day is short enough that we don't hard-code an absolute
        cutoff — just the relative buffer. Callers that want a "no entries
        after X UK" rule pass ``last_entry_cutoff_uk_local``.

        ``opening_buffer_minutes`` defaults to 15 — no new entries before
        08:15 UK. Same rationale as the US session.

        ``day_trade_cutoff_local`` is the LBR-aligned intraday-managed
        entries cutoff (Rule S5, default 10:30 UK = 2.5 h post-open).
        Setting to None disables the gate.
        """
        session_end_utc = _local_time_to_utc(
            session_date_local, UK_SESSION_CLOSE_UK_LOCAL, "Europe/London"
        )
        session_open_utc = _local_time_to_utc(
            session_date_local, UK_SESSION_OPEN_UK_LOCAL, "Europe/London"
        )
        day_trade_cutoff_utc: datetime | None = None
        if day_trade_cutoff_local is not None:
            day_trade_cutoff_utc = _local_time_to_utc(
                session_date_local, day_trade_cutoff_local, "Europe/London"
            )
        return cls._build(
            session_end_utc=session_end_utc,
            session_open_utc=session_open_utc,
            session_date_local=session_date_local,
            hard_close_buffer_minutes=hard_close_buffer_minutes,
            no_new_entries_buffer_minutes=no_new_entries_buffer_minutes,
            opening_buffer_minutes=opening_buffer_minutes,
            last_entry_cutoff_uk_local=last_entry_cutoff_uk_local,
            day_trade_cutoff_utc=day_trade_cutoff_utc,
            market_label="UK",
        )

    # ──────────────────────────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────────────────────────

    @classmethod
    def _build(
        cls,
        *,
        session_end_utc: datetime,
        session_open_utc: datetime,
        session_date_local: date,
        hard_close_buffer_minutes: int,
        no_new_entries_buffer_minutes: int,
        opening_buffer_minutes: int,
        last_entry_cutoff_uk_local: time | None,
        market_label: str,
        day_trade_cutoff_utc: datetime | None = None,
    ) -> "SessionClock":
        hard_close_utc = session_end_utc - timedelta(
            minutes=hard_close_buffer_minutes
        )

        entries_open_utc = session_open_utc + timedelta(
            minutes=opening_buffer_minutes
        )

        relative_cutoff = session_end_utc - timedelta(
            minutes=no_new_entries_buffer_minutes
        )

        cutoffs: list[datetime] = [relative_cutoff]
        if last_entry_cutoff_uk_local is not None:
            absolute_cutoff_utc = _local_time_to_utc(
                session_date_local,
                last_entry_cutoff_uk_local,
                "Europe/London",
            )
            cutoffs.append(absolute_cutoff_utc)

        # Stricter (earlier) cutoff wins.
        entries_cutoff_utc = min(cutoffs)

        if entries_cutoff_utc > hard_close_utc:
            # A cutoff after hard-close would be meaningless — clamp.
            entries_cutoff_utc = hard_close_utc

        return cls(
            session_end_utc=session_end_utc,
            hard_close_utc=hard_close_utc,
            entries_cutoff_utc=entries_cutoff_utc,
            entries_open_utc=entries_open_utc,
            day_trade_cutoff_utc=day_trade_cutoff_utc,
            hard_close_buffer_minutes=hard_close_buffer_minutes,
            no_new_entries_buffer_minutes=no_new_entries_buffer_minutes,
            opening_buffer_minutes=opening_buffer_minutes,
            market_label=market_label,
        )


def _as_utc(dt: datetime) -> datetime:
    """Coerce naive datetimes (assumed UTC) to tz-aware UTC.

    ``utc_now()`` returns naive datetimes everywhere else in the
    codebase; accept those as UTC rather than forcing a cascade of
    rewrites.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _local_time_to_utc(
    session_date_local: date, local_time: time, zone_name: str
) -> datetime:
    """Resolve a wall-clock (date, time, zone) to UTC."""
    zone = ZoneInfo(zone_name)
    local_dt = datetime.combine(session_date_local, local_time).replace(tzinfo=zone)
    return local_dt.astimezone(timezone.utc)


__all__ = [
    "DEFAULT_HARD_CLOSE_BUFFER_MINUTES",
    "DEFAULT_NO_NEW_ENTRIES_BUFFER_MINUTES",
    "DEFAULT_OPENING_BUFFER_MINUTES",
    "DEFAULT_US_LAST_ENTRY_CUTOFF_UK_LOCAL",
    "SessionClock",
]
