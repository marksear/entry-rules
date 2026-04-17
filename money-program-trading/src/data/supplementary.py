"""
Supplementary data sources — earnings calendar, short interest, catalysts.

These are STUBBED until API keys are obtained. The interfaces are defined
so the engine can work without them (safe defaults: block shorts without SI data,
warn on missing earnings dates).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Protocol

logger = logging.getLogger(__name__)


# ── Interfaces (for future implementations) ──────────────────

class EarningsProvider(Protocol):
    """Interface for earnings calendar data."""

    def get_next_earnings_date(self, ticker: str) -> date | None:
        """Return the next earnings date, or None if unknown."""
        ...

    def has_earnings_within(self, ticker: str, days: int) -> bool | None:
        """Return True if earnings are within N days, None if unknown."""
        ...


class ShortInterestProvider(Protocol):
    """Interface for short interest data."""

    def get_short_interest(self, ticker: str) -> float | None:
        """Return SI% (e.g., 15.5 for 15.5%), or None if unavailable."""
        ...

    def get_days_to_cover(self, ticker: str) -> float | None:
        """Return days to cover, or None if unavailable."""
        ...

    def get_borrow_fee(self, ticker: str) -> float | None:
        """Return annual borrow fee as decimal (0.05 = 5%), or None."""
        ...


class CatalystProvider(Protocol):
    """Interface for catalyst calendar (FDA, legal, etc.)."""

    def get_next_catalyst(self, ticker: str) -> tuple[date, str] | None:
        """Return (date, description) of next catalyst, or None."""
        ...

    def has_catalyst_within(self, ticker: str, days: int) -> bool | None:
        """Return True if catalyst within N days, None if unknown."""
        ...


# ── Stub Implementations ─────────────────────────────────────

class StubEarningsProvider:
    """
    Stub earnings provider.
    Returns None for everything — the engine will log warnings
    but continue operating (Rule 11 cannot fire without data).

    TODO: Replace with FMP or Alpha Vantage implementation.
    """

    def __init__(self):
        logger.warning(
            "Using STUB earnings provider — Rule 11 (auto-exit before earnings) "
            "is DISABLED until a real provider is configured"
        )
        self._cache: dict[str, date | None] = {}

    def get_next_earnings_date(self, ticker: str) -> date | None:
        return self._cache.get(ticker)

    def has_earnings_within(self, ticker: str, days: int) -> bool | None:
        earnings_date = self.get_next_earnings_date(ticker)
        if earnings_date is None:
            return None  # Unknown
        return earnings_date <= date.today() + timedelta(days=days)

    def set_earnings_date(self, ticker: str, earnings_date: date) -> None:
        """Manual override — for testing or manual entry."""
        self._cache[ticker] = earnings_date
        logger.info("Manually set earnings for %s: %s", ticker, earnings_date)


class StubShortInterestProvider:
    """
    Stub short interest provider.
    Returns None — the engine's gates will allow shorts on CFDs
    (where borrow isn't a concern) but log warnings.

    TODO: Replace with ORTEX or Fintel implementation.
    """

    def __init__(self):
        logger.warning(
            "Using STUB short interest provider — Gate S4 will proceed "
            "with caution (CFD model) until real data is connected"
        )

    def get_short_interest(self, ticker: str) -> float | None:
        return None

    def get_days_to_cover(self, ticker: str) -> float | None:
        return None

    def get_borrow_fee(self, ticker: str) -> float | None:
        return None


class StubCatalystProvider:
    """
    Stub catalyst provider.
    Returns None — binary catalyst protection (Rule 11) is disabled.

    TODO: Replace with Biopharmcatalyst / news API.
    """

    def __init__(self):
        logger.warning(
            "Using STUB catalyst provider — binary catalyst protection "
            "is DISABLED until a real provider is configured"
        )
        self._cache: dict[str, tuple[date, str]] = {}

    def get_next_catalyst(self, ticker: str) -> tuple[date, str] | None:
        return self._cache.get(ticker)

    def has_catalyst_within(self, ticker: str, days: int) -> bool | None:
        catalyst = self.get_next_catalyst(ticker)
        if catalyst is None:
            return None
        return catalyst[0] <= date.today() + timedelta(days=days)

    def set_catalyst(self, ticker: str, catalyst_date: date, description: str) -> None:
        """Manual override."""
        self._cache[ticker] = (catalyst_date, description)
        logger.info("Manually set catalyst for %s: %s — %s", ticker, catalyst_date, description)
