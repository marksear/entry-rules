"""
Unit tests for the Broker → IG price-scaling bridge.

After the 2026-04-17 DEMO shakedown (FDX sent a $365 stop to an IG epic
quoted at ×100, tripping ATTACHED_ORDER_LEVEL_ERROR), Broker now scales
every outgoing price-unit argument through MarketData.to_ig_units. These
tests stub the IG REST client and assert on the exact kwargs passed to
``create_open_position`` and ``update_open_position``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.engine.broker import Broker
from src.models.common import Direction


pytestmark = pytest.mark.unit


class FakeConfirm(dict):
    pass


class FakeIGService:
    """Stubs the trading_ig surface touched by Broker."""

    def __init__(self):
        self.open_kwargs: dict | None = None
        self.modify_kwargs: dict | None = None
        self.close_kwargs: dict | None = None

    def create_open_position(self, **kwargs):
        self.open_kwargs = kwargs
        return {"dealReference": "REF-OPEN"}

    def update_open_position(self, **kwargs):
        self.modify_kwargs = kwargs
        return {"dealReference": "REF-MODIFY"}

    def close_open_position(self, **kwargs):
        self.close_kwargs = kwargs
        return {"dealReference": "REF-CLOSE"}

    def fetch_deal_by_deal_reference(self, deal_reference):
        # Return a scaled-up ``level`` so the Broker's descaling path is
        # exercised as part of the same test.
        return {
            "dealReference": deal_reference,
            "dealStatus": "ACCEPTED",
            "dealId": "DL-42",
            "level": 38250,  # $382.50 ×100 — the IG quoted-unit form
            "reason": "SUCCESS",
        }


class FakeMarketData:
    """Minimal MarketData surface — just the two methods Broker calls."""

    def __init__(self, scale: float = 100.0):
        self._scale = scale

    def get_scaling_factor(self, epic):
        return self._scale

    def to_ig_units(self, epic, value):
        return None if value is None else float(value) * self._scale


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Broker._poll_confirmation sleeps between polls — not needed here."""
    import src.engine.broker as broker_mod

    monkeypatch.setattr(broker_mod.time, "sleep", lambda *_a, **_kw: None)


def _make_broker(ig_service, market_data=None) -> Broker:
    session = SimpleNamespace(service=ig_service)
    return Broker(session, market_data=market_data)


# ---------------------------------------------------------------------------
# place_open_position
# ---------------------------------------------------------------------------


def test_place_open_position_scales_stop_level_to_ig_units():
    ig = FakeIGService()
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    result = broker.place_open_position(
        epic="UA.D.FDX.CASH.IP",
        direction=Direction.LONG,
        size=5.77,
        stop_price=365.0,
    )

    assert ig.open_kwargs is not None
    # The scan's $365 stop must have been scaled to 36500 for IG.
    assert ig.open_kwargs["stop_level"] == pytest.approx(36500.0)
    assert ig.open_kwargs["epic"] == "UA.D.FDX.CASH.IP"
    # OrderResult should still surface the ORIGINAL scan-unit stop_price
    # for the monitor's P&L math + audit log.
    assert result.stop_price == pytest.approx(365.0)
    # Fill price returned from IG is in ×100 units; Broker must descale.
    assert result.fill_price == pytest.approx(382.50)
    assert result.success is True


def test_place_open_position_passes_through_without_market_data():
    """Legacy call path — no market_data = no scaling (preserves old tests)."""
    ig = FakeIGService()
    broker = _make_broker(ig, market_data=None)

    broker.place_open_position(
        epic="IX.D.FTSE.DAILY.IP",
        direction=Direction.LONG,
        size=1.0,
        stop_price=8080.0,
    )

    assert ig.open_kwargs["stop_level"] == pytest.approx(8080.0)


def test_place_open_position_scales_stop_distance_and_limit_level():
    ig = FakeIGService()
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    broker.place_open_position(
        epic="UA.D.AMD.CASH.IP",
        direction=Direction.LONG,
        size=9.09,
        stop_distance=13.0,
        limit_level=295.0,
    )

    assert ig.open_kwargs["stop_distance"] == pytest.approx(1300.0)
    assert ig.open_kwargs["limit_level"] == pytest.approx(29500.0)


# ---------------------------------------------------------------------------
# modify_stop — epic-aware scaling
# ---------------------------------------------------------------------------


def test_modify_stop_scales_new_stop_when_epic_supplied():
    ig = FakeIGService()
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    result = broker.modify_stop(
        deal_id="DL-42", new_stop_price=275.0, epic="UA.D.AMD.CASH.IP"
    )

    assert ig.modify_kwargs["stop_level"] == pytest.approx(27500.0)
    # The returned StopModifyResult keeps the scan-unit value for the monitor.
    assert result.new_stop_price == pytest.approx(275.0)


def test_modify_stop_without_epic_skips_scaling():
    """MockBroker-compatible path: no epic → no scaling."""
    ig = FakeIGService()
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    broker.modify_stop(deal_id="DL-42", new_stop_price=275.0)

    assert ig.modify_kwargs["stop_level"] == pytest.approx(275.0)
