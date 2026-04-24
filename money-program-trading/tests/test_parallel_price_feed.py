"""
Unit tests for ``ParallelPriceFeed`` — Phase 3 of the S-3 migration.

ParallelPriceFeed is a validation wrapper: it runs ``RestPriceFeed`` and
``LightstreamerPriceFeed`` side-by-side so we can prove LS and REST
disagree in exactly the staleness pattern we suspect (2026-04-23 BA
scenario). These tests validate the wrapper's own logic — both inner
feeds are swapped for ``MagicMock`` instances.

Spec reference: ``docs/specs/S3_LIGHTSTREAMER_SPEC.md`` §5 + §6 Phase 3.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.data.price_feed import (
    ParallelPriceFeed,
    StalePriceError,
    Tick,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tick(epic: str, bid: float, ask: float, age_s: float = 0.0) -> Tick:
    return Tick(
        epic=epic, bid=bid, ask=ask, last_traded=(bid + ask) / 2,
        market_status="TRADEABLE", high=None, low=None, net_change=None,
        pct_change=None, update_time_utc=None, scaling_factor=1.0,
        updated_at_utc=datetime.utcnow() - timedelta(seconds=age_s),
    )


def _fresh_parallel(rest_tick: Tick | None = None, ls_tick: Tick | None = None,
                    ls_stale: bool = False) -> tuple[ParallelPriceFeed, MagicMock, MagicMock]:
    """Build a ParallelPriceFeed backed by two MagicMock inner feeds."""
    rest = MagicMock()
    ls = MagicMock()
    if rest_tick is not None:
        rest.latest.return_value = rest_tick
    if ls_stale:
        ls.latest.side_effect = StalePriceError(epic="X", age_seconds=30.0)
    elif ls_tick is not None:
        ls.latest.return_value = ls_tick
    feed = ParallelPriceFeed(rest=rest, ls=ls)
    return feed, rest, ls


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_start_starts_both_feeds():
    feed, rest, ls = _fresh_parallel()
    feed.start()
    ls.start.assert_called_once()
    rest.start.assert_called_once()


def test_stop_stops_both_feeds():
    feed, rest, ls = _fresh_parallel()
    feed.stop()
    ls.stop.assert_called_once()
    rest.stop.assert_called_once()


def test_stop_swallows_ls_error_still_stops_rest():
    """If LS teardown raises, REST teardown must still happen — we can't
    let a bad LS disconnect leak a REST session."""
    feed, rest, ls = _fresh_parallel()
    ls.stop.side_effect = RuntimeError("ls boom")
    feed.stop()  # must not raise
    rest.stop.assert_called_once()


def test_subscribe_propagates_to_both():
    feed, rest, ls = _fresh_parallel()
    feed.subscribe("UA.D.AAPL.DAILY.IP")
    ls.subscribe.assert_called_with("UA.D.AAPL.DAILY.IP")
    rest.subscribe.assert_called_with("UA.D.AAPL.DAILY.IP")


def test_unsubscribe_propagates_to_both():
    feed, rest, ls = _fresh_parallel()
    feed.unsubscribe("UA.D.AAPL.DAILY.IP")
    ls.unsubscribe.assert_called_with("UA.D.AAPL.DAILY.IP")
    rest.unsubscribe.assert_called_with("UA.D.AAPL.DAILY.IP")


# ---------------------------------------------------------------------------
# latest() — LS preferred, REST fallback, divergence logged
# ---------------------------------------------------------------------------


def test_latest_returns_ls_tick_when_fresh():
    """Happy path: LS returns a fresh tick; ParallelPriceFeed returns it.
    REST is still called (for divergence comparison) but its tick is NOT
    returned to the caller."""
    rest_tick = _tick("X", 99.9, 100.1)
    ls_tick = _tick("X", 100.0, 100.2)  # fresh
    feed, rest, ls = _fresh_parallel(rest_tick=rest_tick, ls_tick=ls_tick)

    result = feed.latest("X")
    assert result is ls_tick
    # REST was still consulted for comparison
    rest.latest.assert_called_once()


def test_latest_falls_back_to_rest_on_ls_stale():
    """LS raises StalePriceError → ParallelPriceFeed returns the REST
    tick instead of propagating the error. The monitor must not halt
    mid-session just because LS had a hiccup during validation."""
    rest_tick = _tick("X", 99.9, 100.1)
    feed, rest, ls = _fresh_parallel(rest_tick=rest_tick, ls_stale=True)

    result = feed.latest("X")
    assert result is rest_tick


def test_latest_logs_divergence_when_mids_differ_above_threshold(caplog):
    """REST mid = 100.0, LS mid = 100.5 → diff = 50bps (above 30bps
    threshold). Must emit a PRICE_DIVERGENCE WARNING line."""
    rest_tick = _tick("X", 99.95, 100.05)   # mid = 100.00
    ls_tick = _tick("X", 100.45, 100.55)    # mid = 100.50  → 50bps
    feed, _, _ = _fresh_parallel(rest_tick=rest_tick, ls_tick=ls_tick)

    with caplog.at_level(logging.WARNING, logger="src.data.price_feed"):
        feed.latest("X")

    msgs = [r.message for r in caplog.records if "PRICE_DIVERGENCE" in r.message]
    assert len(msgs) == 1, f"Expected one PRICE_DIVERGENCE log, got: {msgs}"
    assert "diff=50.0bps" in msgs[0]


def test_latest_does_not_log_when_divergence_below_threshold(caplog):
    """REST mid = 100.0, LS mid = 100.05 → 5bps (below threshold).
    No PRICE_DIVERGENCE warning should appear."""
    rest_tick = _tick("X", 99.95, 100.05)   # mid = 100.00
    ls_tick = _tick("X", 100.00, 100.10)    # mid = 100.05  → 5bps
    feed, _, _ = _fresh_parallel(rest_tick=rest_tick, ls_tick=ls_tick)

    with caplog.at_level(logging.WARNING, logger="src.data.price_feed"):
        feed.latest("X")

    msgs = [r.message for r in caplog.records if "PRICE_DIVERGENCE" in r.message]
    assert msgs == [], f"Unexpected divergence log: {msgs}"


def test_latest_skips_divergence_check_when_ls_stale():
    """No LS tick → no divergence comparison possible (and we already
    logged the fallback-to-REST event). Specifically: must NOT emit a
    PRICE_DIVERGENCE warning in this case."""
    rest_tick = _tick("X", 99.95, 100.05)
    feed, _, _ = _fresh_parallel(rest_tick=rest_tick, ls_stale=True)

    caplog_records = []
    logger = logging.getLogger("src.data.price_feed")
    handler = logging.Handler()
    handler.emit = lambda record: caplog_records.append(record)
    logger.addHandler(handler)
    try:
        feed.latest("X")
    finally:
        logger.removeHandler(handler)

    divergence = [r for r in caplog_records if "PRICE_DIVERGENCE" in r.getMessage()]
    assert divergence == []


def test_latest_skips_divergence_when_rest_has_no_mid():
    """If REST returns bid=None ask=None (empty tick), we can't compute a
    mid — the divergence guard must bail silently rather than dividing
    by zero or comparing None."""
    from src.data.price_feed import _empty_tick
    rest_tick = _empty_tick("X")
    ls_tick = _tick("X", 100.0, 100.5)
    feed, _, _ = _fresh_parallel(rest_tick=rest_tick, ls_tick=ls_tick)

    # Must not raise — and must return the LS tick since LS was fresh.
    result = feed.latest("X")
    assert result is ls_tick


# ---------------------------------------------------------------------------
# get_scaling_factor()
# ---------------------------------------------------------------------------


def test_get_scaling_factor_prefers_ls():
    """LS's scaling cache wins if populated; REST is the fallback."""
    feed, rest, ls = _fresh_parallel()
    ls.get_scaling_factor.return_value = 100.0
    rest.get_scaling_factor.return_value = 1.0
    assert feed.get_scaling_factor("X") == 100.0


def test_get_scaling_factor_falls_back_to_rest_when_ls_unknown():
    feed, rest, ls = _fresh_parallel()
    # LS default for unseen epic is 1.0, but let's have it report 0 (falsy)
    # to check the fallback.
    ls.get_scaling_factor.return_value = 0
    rest.get_scaling_factor.return_value = 42.0
    assert feed.get_scaling_factor("X") == 42.0
