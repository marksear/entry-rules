"""
Unit tests for the Broker → IG stake-descaling bridge.

After the 2026-04-20 DEMO Day-1 shakedown (ARM LONG and JNJ SHORT both
rejected INSUFFICIENT_FUNDS on a £11,277 DEMO balance despite £50
intended risk), Broker now descales ``size`` from scan units into IG's
quoted units — symmetric with the price-level descaling already done
for ``stop_level`` / ``stop_distance`` / ``limit_level``. A floor at
``dealingRules.minDealSize.value`` prevents sub-minimum IG rejects and
surfaces a WARNING when effective risk diverges from plan.

Test shape mirrors ``tests/test_broker_scaling.py`` — stub the
``trading_ig`` surface, assert on the exact kwargs passed to
``create_open_position`` / ``close_open_position``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.engine.broker import Broker
from src.models.common import Direction


pytestmark = pytest.mark.unit


class FakeIGService:
    """Stubs the trading_ig surface touched by Broker."""

    def __init__(self, min_deal_size: float | None = 0.05):
        self.open_kwargs: dict | None = None
        self.close_kwargs: dict | None = None
        self._min_deal_size = min_deal_size

    def create_open_position(self, **kwargs):
        self.open_kwargs = kwargs
        return {"dealReference": "REF-OPEN"}

    def close_open_position(self, **kwargs):
        self.close_kwargs = kwargs
        return {"dealReference": "REF-CLOSE"}

    def fetch_deal_by_deal_reference(self, deal_reference):
        return {
            "dealReference": deal_reference,
            "dealStatus": "ACCEPTED",
            "dealId": "DL-99",
            "level": 16581,  # ARM $165.81 on ×100 epic — descales to 165.81
            "reason": "SUCCESS",
        }

    def fetch_market_by_epic(self, epic):
        """Return a minimal market snapshot with dealingRules.minDealSize.

        ``min_deal_size=None`` simulates a fetch that returns no rules
        (exercises the fallback-to-0.10 path inside Broker).
        """
        rules: dict = {}
        if self._min_deal_size is not None:
            rules = {"minDealSize": {"value": self._min_deal_size}}
        return {"dealingRules": rules}


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
# place_open_position — size descaling
# ---------------------------------------------------------------------------


def test_place_open_position_descales_size_on_scaled_epic():
    """The bug: ARM £7.12/pt stake was sent to IG unchanged on a ×100 epic,
    so IG saw £712/$1-move and rejected for notional. With the fix
    ``size`` is divided by scalingFactor on the way into IG, so IG sees
    the intended £0.0712/pt (which then maps to £7.12 per $1 display-move
    as expected).
    """
    ig = FakeIGService(min_deal_size=0.05)  # well below the descaled result
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    broker.place_open_position(
        epic="UA.D.ARM.CASH.IP",
        direction=Direction.LONG,
        size=7.12,
        stop_price=157.98,
    )

    assert ig.open_kwargs is not None
    # 7.12 / 100 = 0.0712 — no clamp because min_deal_size=0.05.
    assert ig.open_kwargs["size"] == pytest.approx(0.0712)
    # Confirm prices were also scaled (existing behaviour preserved).
    assert ig.open_kwargs["stop_level"] == pytest.approx(15798.0)


def test_place_open_position_clamps_to_min_deal_size():
    """When the descaled size falls under IG's per-epic minDealSize,
    Broker floors it to the minimum so IG accepts the order. Caller
    must see a WARNING so the journal records the plan-vs-actual
    risk divergence.
    """
    ig = FakeIGService(min_deal_size=0.10)  # above the 0.0712 descaled value
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    broker.place_open_position(
        epic="UA.D.ARM.CASH.IP",
        direction=Direction.LONG,
        size=7.12,
        stop_price=157.98,
    )

    # Descaled to 0.0712, clamped up to the 0.10 floor.
    assert ig.open_kwargs["size"] == pytest.approx(0.10)


def test_place_open_position_no_scale_passes_size_unchanged():
    """Index / FX epics typically have scalingFactor=1.0 — in that
    regime the descale branch is a no-op and ``size`` reaches IG
    unchanged (plus the minDealSize floor, which at FTSE £1/pt is
    irrelevant).
    """
    ig = FakeIGService(min_deal_size=0.10)
    broker = _make_broker(ig, market_data=FakeMarketData(scale=1.0))

    broker.place_open_position(
        epic="IX.D.FTSE.DAILY.IP",
        direction=Direction.LONG,
        size=7.12,
        stop_price=8080.0,
    )

    # scale=1.0 means ig_size == size; 7.12 > 0.10 floor so no clamp.
    assert ig.open_kwargs["size"] == pytest.approx(7.12)


def test_place_open_position_without_market_data_passes_through():
    """Legacy path — no MarketData attached means no scaling and no
    minDealSize fetch. Preserves pre-fix behaviour for unit tests that
    mock at the trading_ig layer directly.
    """
    ig = FakeIGService()
    broker = _make_broker(ig, market_data=None)

    broker.place_open_position(
        epic="IX.D.FTSE.DAILY.IP",
        direction=Direction.LONG,
        size=1.0,
        stop_price=8080.0,
    )

    assert ig.open_kwargs["size"] == pytest.approx(1.0)
    assert ig.open_kwargs["stop_level"] == pytest.approx(8080.0)


def test_place_open_position_uses_fallback_when_min_deal_size_missing():
    """If IG returns no dealingRules.minDealSize, Broker falls back to
    0.10. A descaled size of 0.0712 would be clamped up.
    """
    ig = FakeIGService(min_deal_size=None)  # simulates missing rule
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    broker.place_open_position(
        epic="UA.D.ARM.CASH.IP",
        direction=Direction.LONG,
        size=7.12,
        stop_price=157.98,
    )

    assert ig.open_kwargs["size"] == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# close_position — mirror path
# ---------------------------------------------------------------------------


def test_close_position_descales_size_symmetrically():
    """Close crosses the same REST boundary as open — descale and
    clamp the same way so a future divergence between the two paths
    doesn't re-introduce the INSUFFICIENT_FUNDS class of bug.
    """
    ig = FakeIGService(min_deal_size=0.05)
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    broker.close_position(
        deal_id="DL-42",
        direction=Direction.LONG,
        epic="UA.D.ARM.CASH.IP",
        size=7.12,
    )

    assert ig.close_kwargs is not None
    assert ig.close_kwargs["size"] == pytest.approx(0.0712)


def test_close_position_clamps_to_min_deal_size():
    ig = FakeIGService(min_deal_size=0.10)
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    broker.close_position(
        deal_id="DL-42",
        direction=Direction.LONG,
        epic="UA.D.ARM.CASH.IP",
        size=7.12,
    )

    assert ig.close_kwargs["size"] == pytest.approx(0.10)


def test_close_position_without_market_data_passes_through():
    ig = FakeIGService()
    broker = _make_broker(ig, market_data=None)

    broker.close_position(
        deal_id="DL-42",
        direction=Direction.LONG,
        epic="IX.D.FTSE.DAILY.IP",
        size=1.0,
    )

    assert ig.close_kwargs["size"] == pytest.approx(1.0)
