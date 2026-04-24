"""
Unit tests for ``LightstreamerPriceFeed`` — Phase 2 of the S-3 migration.

We do NOT connect to a real Lightstreamer server. All LS client and
subscription classes are replaced with ``unittest.mock`` doubles. What we
validate is our own logic:

* ``start()`` reads CST/XST from the existing IG session and constructs
  a ``LightstreamerClient`` without triggering a second REST auth.
* ``subscribe()`` pre-loads instrument metadata and opens a
  MARKET:{epic} subscription.
* ``_on_tick()`` parses an ``ItemUpdate``-shaped payload into a ``Tick``
  with correctly scaled fields.
* ``latest()`` raises ``StalePriceError`` when no tick has arrived or the
  cached tick is older than ``max_age_seconds``.
* ``stop()`` unsubscribes every subscription and disconnects cleanly.
* Lifecycle is idempotent — double ``start()`` / ``stop()`` don't explode.

Spec reference: ``docs/specs/S3_LIGHTSTREAMER_SPEC.md`` §§4.2, 7.2, 9.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.data.price_feed import (
    LightstreamerPriceFeed,
    StalePriceError,
    Tick,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_fake_session(
    cst: str = "FAKECST",
    xst: str = "FAKEXST",
    ls_endpoint: str = "https://lightstreamer.fake.ig.com",
    account_id: str = "TEST-ACC",
    instrument_response: dict | None = None,
) -> SimpleNamespace:
    """Build a fake ``IGSession`` with the bare surface
    ``LightstreamerPriceFeed`` actually touches: ``service.session.headers``,
    ``service.read_session``, ``service.fetch_market_by_epic``, and
    ``account_id``."""
    default_instrument = {
        "instrument": {"scalingFactor": 100, "name": "FakeEquity"},
        "snapshot": {"bid": 12345, "offer": 12350},
    }
    service = MagicMock()
    service.session = MagicMock()
    service.session.headers = {"CST": cst, "X-SECURITY-TOKEN": xst}
    service.read_session = MagicMock(
        return_value={"lightstreamerEndpoint": ls_endpoint}
    )
    service.fetch_market_by_epic = MagicMock(
        return_value=instrument_response or default_instrument
    )
    return SimpleNamespace(service=service, account_id=account_id)


def _make_item_update(fields: dict) -> MagicMock:
    """Build a fake ``ItemUpdate`` whose ``getValue(field)`` returns
    ``fields[field]`` (as string, matching Lightstreamer conventions)."""
    update = MagicMock()
    update.getValue = MagicMock(side_effect=lambda f: fields.get(f))
    return update


# ---------------------------------------------------------------------------
# start() — token sharing + endpoint resolution
# ---------------------------------------------------------------------------


def test_start_reads_tokens_from_existing_session_no_second_auth():
    """start() must NOT call ``create_session`` — that would be a second
    REST auth and trip DEMO's rate limit. It must read CST/XST from the
    existing session and resolve the endpoint via ``read_session``."""
    session = _make_fake_session(cst="TOKEN-A", xst="TOKEN-B")
    feed = LightstreamerPriceFeed(session)

    with patch("src.data.price_feed.LightstreamerClient") as LSClient:
        feed.start()

    # LightstreamerClient was constructed with the endpoint from read_session
    LSClient.assert_called_once()
    endpoint = LSClient.call_args[0][0]
    assert endpoint == "https://lightstreamer.fake.ig.com"

    # Password combines CST and XST with the IG convention
    client = LSClient.return_value
    client.connectionDetails.setPassword.assert_called_with("CST-TOKEN-A|XST-TOKEN-B")
    client.connectionDetails.setUser.assert_called_with("TEST-ACC")
    client.connect.assert_called_once()

    # read_session was called (to resolve the LS endpoint)
    session.service.read_session.assert_called_once_with(fetch_session_tokens="true")
    # create_session must NOT have been touched
    assert not session.service.create_session.called, (
        "start() must not re-authenticate; expected read_session only"
    )


def test_start_raises_when_cst_missing():
    """An IG session that hasn't been connect()'d yet has no CST header;
    start() must refuse rather than silently open a broken LS connection."""
    session = _make_fake_session()
    session.service.session.headers = {}  # strip CST/XST
    feed = LightstreamerPriceFeed(session)

    with patch("src.data.price_feed.LightstreamerClient"):
        with pytest.raises(RuntimeError, match="no CST"):
            feed.start()


def test_start_raises_when_endpoint_missing():
    """read_session returning no lightstreamerEndpoint must fail loudly."""
    session = _make_fake_session()
    session.service.read_session.return_value = {"other_field": "x"}
    feed = LightstreamerPriceFeed(session)

    with patch("src.data.price_feed.LightstreamerClient"):
        with pytest.raises(RuntimeError, match="lightstreamerEndpoint"):
            feed.start()


def test_start_is_idempotent():
    """Calling start() twice must be a no-op on the second call."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)

    with patch("src.data.price_feed.LightstreamerClient") as LSClient:
        feed.start()
        feed.start()
    # Only one LightstreamerClient constructed.
    assert LSClient.call_count == 1


# ---------------------------------------------------------------------------
# subscribe() — preload metadata + open MARKET subscription
# ---------------------------------------------------------------------------


def test_subscribe_preloads_instrument_metadata():
    """subscribe() fetches the instrument block once via REST so the
    first incoming LS tick can resolve scalingFactor without another
    network round-trip."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)

    with patch("src.data.price_feed.LightstreamerClient"), \
         patch("src.data.price_feed.Subscription") as SubscriptionCls:
        feed.start()
        feed.subscribe("SC.D.FDX.DAILY.IP")

    session.service.fetch_market_by_epic.assert_called_once_with("SC.D.FDX.DAILY.IP")
    # Subscription constructed with mode=MERGE and MARKET:{epic} item
    _, kwargs = SubscriptionCls.call_args
    assert kwargs["mode"] == "MERGE"
    assert kwargs["items"] == ["MARKET:SC.D.FDX.DAILY.IP"]
    assert "BID" in kwargs["fields"] and "OFFER" in kwargs["fields"]


def test_subscribe_refuses_before_start():
    """subscribe() before start() must raise — order matters."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)

    with pytest.raises(RuntimeError, match="start\\(\\) before"):
        feed.subscribe("ANY.EPIC")


def test_subscribe_is_idempotent():
    """Re-subscribing to the same epic is a no-op (no double subscription)."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)

    with patch("src.data.price_feed.LightstreamerClient") as LSClient, \
         patch("src.data.price_feed.Subscription"):
        feed.start()
        feed.subscribe("UA.D.AAPL.DAILY.IP")
        feed.subscribe("UA.D.AAPL.DAILY.IP")

    # client.subscribe was called exactly once
    assert LSClient.return_value.subscribe.call_count == 1


# ---------------------------------------------------------------------------
# _on_tick() — parsing + scaling
# ---------------------------------------------------------------------------


def test_on_tick_parses_and_scales_fields():
    """An incoming ItemUpdate with IG minor-unit prices + scalingFactor=100
    in the cached instrument must emerge as dollar-scaled Tick fields."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)

    # Pre-seed the instrument cache with scalingFactor=100 so we don't
    # need to go through subscribe()'s REST call.
    feed._instrument_cache["UA.D.FDX.DAILY.IP"] = {
        "scalingFactor": 100, "name": "FedEx", "type": "SHARES",
    }

    update = _make_item_update({
        "BID": "38190",
        "OFFER": "38510",
        "HIGH": "38600",
        "LOW": "38050",
        "CHANGE": "125",
        "CHANGE_PCT": "0.33",
        "MARKET_STATE": "TRADEABLE",
        "UPDATE_TIME": "13:25:36",
    })
    feed._on_tick("UA.D.FDX.DAILY.IP", update)

    cached = feed._tick_cache["UA.D.FDX.DAILY.IP"]
    assert cached.scaling_factor == 100.0
    assert cached.bid == pytest.approx(381.90)
    assert cached.ask == pytest.approx(385.10)
    # LS MARKET subscriptions don't include a last-traded field — we
    # derive it from the mid.
    assert cached.last_traded == pytest.approx((381.90 + 385.10) / 2)
    assert cached.high == pytest.approx(386.00)
    assert cached.low == pytest.approx(380.50)
    assert cached.net_change == pytest.approx(1.25)
    assert cached.pct_change == pytest.approx(0.33)  # unit-less, not scaled
    assert cached.market_status == "TRADEABLE"


def test_on_tick_applies_us_equity_scaling_fallback_when_factor_missing():
    """When the instrument block has no scalingFactor but the bid/ask look
    like minor units, the 100.0 equity fallback still kicks in on LS ticks
    the same way it does on REST snapshots."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)
    feed._instrument_cache["SC.D.FDX.DAILY.IP"] = {
        "type": "SHARES", "name": "FedEx"  # NO scalingFactor
    }

    update = _make_item_update({
        "BID": "39080.5", "OFFER": "39120.0", "MARKET_STATE": "TRADEABLE",
    })
    feed._on_tick("SC.D.FDX.DAILY.IP", update)

    cached = feed._tick_cache["SC.D.FDX.DAILY.IP"]
    assert cached.scaling_factor == 100.0
    assert cached.bid == pytest.approx(390.805)
    assert cached.ask == pytest.approx(391.20)


# ---------------------------------------------------------------------------
# latest() — StalePriceError semantics
# ---------------------------------------------------------------------------


def test_latest_raises_when_no_tick_yet():
    """Before any tick arrives, latest() raises StalePriceError with age=inf."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)
    with pytest.raises(StalePriceError) as exc:
        feed.latest("UA.D.AAPL.DAILY.IP")
    assert exc.value.epic == "UA.D.AAPL.DAILY.IP"
    assert exc.value.age_seconds == float("inf")


def test_latest_returns_tick_when_fresh():
    """A tick less than max_age_seconds old comes back unmodified."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session, stale_seconds=10.0)
    fresh = Tick(
        epic="UA.D.AAPL.DAILY.IP", bid=100.0, ask=100.1, last_traded=100.05,
        market_status="TRADEABLE", high=101.0, low=99.5, net_change=0.5,
        pct_change=0.5, update_time_utc="14:00:00", scaling_factor=1.0,
        updated_at_utc=datetime.utcnow(),
    )
    feed._tick_cache["UA.D.AAPL.DAILY.IP"] = fresh
    result = feed.latest("UA.D.AAPL.DAILY.IP")
    assert result is fresh


def test_latest_raises_when_tick_older_than_max_age():
    """A cached tick with updated_at_utc beyond max_age triggers
    StalePriceError carrying the actual age."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session, stale_seconds=5.0)
    stale = Tick(
        epic="UA.D.AAPL.DAILY.IP", bid=100.0, ask=100.1, last_traded=100.05,
        market_status="TRADEABLE", high=None, low=None, net_change=None,
        pct_change=None, update_time_utc="14:00:00", scaling_factor=1.0,
        updated_at_utc=datetime.utcnow() - timedelta(seconds=30),
    )
    feed._tick_cache["UA.D.AAPL.DAILY.IP"] = stale
    with pytest.raises(StalePriceError) as exc:
        feed.latest("UA.D.AAPL.DAILY.IP")
    assert exc.value.age_seconds >= 30


def test_latest_max_age_override_extends_tolerance():
    """Callers can override the feed-level stale_seconds per call — useful
    if a specific epic has slower market hours."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session, stale_seconds=5.0)
    older = Tick(
        epic="UK.D.FTSE.DAILY.IP", bid=8100.0, ask=8101.0, last_traded=8100.5,
        market_status="TRADEABLE", high=None, low=None, net_change=None,
        pct_change=None, update_time_utc="14:00:00", scaling_factor=1.0,
        updated_at_utc=datetime.utcnow() - timedelta(seconds=20),
    )
    feed._tick_cache["UK.D.FTSE.DAILY.IP"] = older
    # Default stale_seconds=5 would raise; override to 120 returns the tick
    result = feed.latest("UK.D.FTSE.DAILY.IP", max_age_seconds=120.0)
    assert result is older


# ---------------------------------------------------------------------------
# stop() — cleanup
# ---------------------------------------------------------------------------


def test_stop_unsubscribes_and_disconnects():
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)

    with patch("src.data.price_feed.LightstreamerClient") as LSClient, \
         patch("src.data.price_feed.Subscription"):
        feed.start()
        feed.subscribe("UA.D.AAPL.DAILY.IP")
        feed.subscribe("UA.D.MSFT.DAILY.IP")
        client = LSClient.return_value
        assert client.subscribe.call_count == 2
        feed.stop()

    # Both subscriptions unsubscribed, client disconnected
    assert client.unsubscribe.call_count == 2
    client.disconnect.assert_called_once()


def test_stop_is_idempotent():
    """Double stop() is a no-op on the second call."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)

    with patch("src.data.price_feed.LightstreamerClient") as LSClient:
        feed.start()
        feed.stop()
        feed.stop()  # must not raise

    assert LSClient.return_value.disconnect.call_count == 1


def test_stop_swallows_unsubscribe_errors():
    """If unsubscribe raises on one epic, stop() still disconnects the
    client cleanly (we don't want a bad subscription to leak an open
    LS connection)."""
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)

    with patch("src.data.price_feed.LightstreamerClient") as LSClient, \
         patch("src.data.price_feed.Subscription"):
        feed.start()
        feed.subscribe("UA.D.AAPL.DAILY.IP")
        LSClient.return_value.unsubscribe.side_effect = RuntimeError("boom")
        feed.stop()  # must not raise

    LSClient.return_value.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# get_scaling_factor() — cache accessor
# ---------------------------------------------------------------------------


def test_get_scaling_factor_returns_cached_value_after_tick():
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)
    feed._instrument_cache["UA.D.FDX.DAILY.IP"] = {"scalingFactor": 100}
    feed._on_tick("UA.D.FDX.DAILY.IP", _make_item_update({"BID": "38000", "OFFER": "38050"}))
    assert feed.get_scaling_factor("UA.D.FDX.DAILY.IP") == 100.0


def test_get_scaling_factor_defaults_to_one_for_unseen_epic():
    session = _make_fake_session()
    feed = LightstreamerPriceFeed(session)
    assert feed.get_scaling_factor("UNSEEN.EPIC") == 1.0
