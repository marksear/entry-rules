"""
Contract tests for the ``PriceFeed`` abstraction.

Both ``RestPriceFeed`` and ``LightstreamerPriceFeed`` must honour the same
public surface so ``MarketData`` can swap between them via env flag. These
tests parametrise across both implementations and assert the shared
contract — specifically:

* ``Tick`` shape is uniform (both feeds produce the same 10 fields).
* ``is_empty()`` semantics are uniform.
* ``get_scaling_factor(epic)`` returns 1.0 for unseen epics on both.
* Lifecycle methods (``start``/``stop``/``subscribe``/``unsubscribe``)
  exist on both and don't raise when called sensibly.
* ``build_price_feed`` factory dispatches correctly for each mode.

Spec reference: ``docs/specs/S3_LIGHTSTREAMER_SPEC.md`` §4 + §9.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.config.settings import PriceFeedMode
from src.data.price_feed import (
    LightstreamerPriceFeed,
    ParallelPriceFeed,
    PriceFeed,
    RestPriceFeed,
    StalePriceError,
    Tick,
    build_price_feed,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fake_session(snapshot=None):
    """A SimpleNamespace standing in for IGSession with the few attributes
    each feed actually touches."""
    default = {
        "instrument": {"scalingFactor": 100, "name": "X"},
        "snapshot": {
            "bid": 10000, "offer": 10050, "lastTraded": 10025,
            "marketStatus": "TRADEABLE",
        },
    }
    service = MagicMock()
    service.session = MagicMock()
    service.session.headers = {"CST": "C", "X-SECURITY-TOKEN": "X"}
    service.read_session = MagicMock(
        return_value={"lightstreamerEndpoint": "https://ls.fake"}
    )
    service.fetch_market_by_epic = MagicMock(return_value=snapshot or default)
    return SimpleNamespace(service=service, account_id="ACC")


# ---------------------------------------------------------------------------
# Parametrised contract assertions
# ---------------------------------------------------------------------------


@pytest.fixture(params=["rest", "lightstreamer"])
def feed(request):
    """Yields a fully-constructed PriceFeed of each flavour, with any LS
    plumbing mocked. The test function receives ``(feed, flavour)``."""
    session = _fake_session()
    if request.param == "rest":
        yield RestPriceFeed(session), "rest"
    else:
        with patch("src.data.price_feed.LightstreamerClient"), \
             patch("src.data.price_feed.Subscription"):
            f = LightstreamerPriceFeed(session)
            f.start()
            yield f, "lightstreamer"
            f.stop()


def test_both_feeds_implement_price_feed_interface(feed):
    feed_obj, _flavour = feed
    assert isinstance(feed_obj, PriceFeed), (
        f"{type(feed_obj).__name__} must inherit from PriceFeed"
    )
    # Every method called by MarketData / session_init exists.
    for method in ("latest", "get_scaling_factor", "start", "stop",
                   "subscribe", "unsubscribe"):
        assert hasattr(feed_obj, method), (
            f"{type(feed_obj).__name__} missing method '{method}'"
        )


def test_scaling_factor_defaults_to_one_on_unseen_epic(feed):
    feed_obj, _flavour = feed
    assert feed_obj.get_scaling_factor("NEVER.SEEN.EPIC") == 1.0


def test_unsubscribe_unknown_epic_is_no_op(feed):
    """Unsubscribing an epic we never subscribed to must not raise —
    simplifies teardown paths in the monitor loop that may retry."""
    feed_obj, _flavour = feed
    feed_obj.unsubscribe("NEVER.SEEN.EPIC")  # must not raise


# ---------------------------------------------------------------------------
# Tick shape uniformity
# ---------------------------------------------------------------------------


_EXPECTED_TICK_KEYS = {
    "bid", "ask", "last_traded", "market_status", "high", "low",
    "net_change", "pct_change", "update_time_utc", "scaling_factor",
}


def test_tick_snapshot_dict_has_stable_keys_when_populated():
    """Tick.as_snapshot_dict() must produce the same 10 keys regardless
    of which feed produced it — that's the contract MarketData relies on."""
    from datetime import datetime
    t = Tick(
        epic="X.Y.Z", bid=1.0, ask=1.01, last_traded=1.005,
        market_status="TRADEABLE", high=1.02, low=0.99, net_change=0.01,
        pct_change=0.5, update_time_utc="09:30:00", scaling_factor=100.0,
        updated_at_utc=datetime.utcnow(),
    )
    d = t.as_snapshot_dict()
    assert set(d.keys()) == _EXPECTED_TICK_KEYS


def test_tick_snapshot_dict_empty_returns_empty_dict():
    """An empty Tick must render as {} to preserve the pre-refactor
    return contract of MarketData.get_market_snapshot on IG empty-snap."""
    from src.data.price_feed import _empty_tick
    assert _empty_tick("X.Y.Z").as_snapshot_dict() == {}


# ---------------------------------------------------------------------------
# Factory (build_price_feed)
# ---------------------------------------------------------------------------


def test_factory_returns_rest_feed_for_rest_mode():
    session = _fake_session()
    cfg = SimpleNamespace(price_feed_mode=PriceFeedMode.REST)
    feed_obj = build_price_feed(session, cfg)
    assert isinstance(feed_obj, RestPriceFeed)


def test_factory_returns_ls_feed_for_lightstreamer_mode():
    session = _fake_session()
    cfg = SimpleNamespace(
        price_feed_mode=PriceFeedMode.LIGHTSTREAMER,
        price_feed_stale_seconds=7.0,
        price_feed_degraded_seconds=42.0,
    )
    feed_obj = build_price_feed(session, cfg)
    assert isinstance(feed_obj, LightstreamerPriceFeed)
    # Factory must thread the thresholds through.
    assert feed_obj._stale_seconds == 7.0
    assert feed_obj._degraded_seconds == 42.0


def test_factory_returns_parallel_feed_for_parallel_mode():
    """Phase 3: parallel mode returns a ParallelPriceFeed wrapping both
    inner feeds. Validation-only per the spec; not meant for LIVE."""
    session = _fake_session()
    cfg = SimpleNamespace(
        price_feed_mode=PriceFeedMode.PARALLEL,
        price_feed_stale_seconds=10.0,
        price_feed_degraded_seconds=60.0,
    )
    feed_obj = build_price_feed(session, cfg)
    assert isinstance(feed_obj, ParallelPriceFeed)
    # Must be carrying both inner feeds, properly typed.
    assert isinstance(feed_obj._rest, RestPriceFeed)
    assert isinstance(feed_obj._ls, LightstreamerPriceFeed)


def test_factory_rejects_unknown_mode():
    session = _fake_session()
    cfg = SimpleNamespace(price_feed_mode="nonsense")
    with pytest.raises(ValueError, match="Unknown price_feed_mode"):
        build_price_feed(session, cfg)


def test_factory_accepts_string_mode_as_well_as_enum():
    """``SimpleNamespace(price_feed_mode='rest')`` should work too, for
    callers that construct a fake settings object without importing the
    enum (tests mostly)."""
    session = _fake_session()
    cfg = SimpleNamespace(price_feed_mode="rest")
    feed_obj = build_price_feed(session, cfg)
    assert isinstance(feed_obj, RestPriceFeed)


# ---------------------------------------------------------------------------
# StalePriceError: construction + attributes
# ---------------------------------------------------------------------------


def test_stale_price_error_carries_epic_and_age():
    err = StalePriceError(epic="X.Y.Z", age_seconds=42.5)
    assert err.epic == "X.Y.Z"
    assert err.age_seconds == 42.5
    assert "42.5" in str(err) and "X.Y.Z" in str(err)
