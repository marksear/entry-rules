"""
Unit tests for the broker-enforced £ take-profit limit attached at
order-open (2026-04-21).

After the 2026-04-20 DEMO Day-1 shakedown (JNJ SHORT hit +£37.68 peak
P&L but the monitor marked the plan TERMINAL while the position was
still open at IG — trail ladder stopped evaluating, manual close
required), every opening order must attach a ``limit_level`` set to
the price where unrealised P&L equals the grade-scaled £ hard target
(A+ £62.50, A/B/C £50). The limit sits at IG so the cap fires even
when the monitor misses a tick.

Test shape mirrors ``test_broker_scaling.py`` — stub trading_ig,
assert on the exact kwargs passed to ``create_open_position``. The
fifth test covers the None-limit fallback path so we know legacy
callers (no MarketData) still open orders without a limit attached.
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
        self._min_deal_size = min_deal_size

    def create_open_position(self, **kwargs):
        self.open_kwargs = kwargs
        return {"dealReference": "REF-OPEN"}

    def close_open_position(self, **kwargs):
        return {"dealReference": "REF-CLOSE"}

    def fetch_deal_by_deal_reference(self, deal_reference):
        return {
            "dealReference": deal_reference,
            "dealStatus": "ACCEPTED",
            "dealId": "DL-99",
            "level": 16710,
            "reason": "SUCCESS",
        }

    def fetch_market_by_epic(self, epic):
        rules: dict = {}
        if self._min_deal_size is not None:
            rules = {"minDealSize": {"value": self._min_deal_size}}
        return {"dealingRules": rules}


class FakeMarketData:
    def __init__(self, scale: float = 100.0):
        self._scale = scale

    def get_scaling_factor(self, epic):
        return self._scale

    def to_ig_units(self, epic, value):
        return None if value is None else float(value) * self._scale


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import src.engine.broker as broker_mod

    monkeypatch.setattr(broker_mod.time, "sleep", lambda *_a, **_kw: None)


def _make_broker(ig_service, market_data=None) -> Broker:
    session = SimpleNamespace(service=ig_service)
    return Broker(session, market_data=market_data)


def _assemble_order(broker: Broker, *, direction, epic, entry_scan, size_scan,
                    target_gbp):
    """Mimic what monitor._handle_fire does: ask the broker for the limit
    price, then place the order with it attached."""
    limit_scan = broker.compute_limit_price_for_target(
        epic=epic,
        direction=direction,
        entry_price=entry_scan,
        size=size_scan,
        target_gbp=target_gbp,
    )
    broker.place_open_position(
        epic=epic,
        direction=direction,
        size=size_scan,
        stop_price=entry_scan - 5.0 if direction == Direction.LONG else entry_scan + 5.0,
        limit_level=limit_scan,
    )
    return limit_scan


# ---------------------------------------------------------------------------
# Test A — Grade-A LONG, £50 target
# ---------------------------------------------------------------------------


def test_grade_a_long_locks_fifty_pounds():
    """Entry $167.10, scan stake £50/pt → descaled to £0.50 IG.
    Target £50, effective_scan_stake = 0.50 × 100 = 50.
    delta_scan = 50/50 = 1.00; limit_scan = 168.10; IG units = 16810.
    Verify target recoverable: (16810 − 16710) × 0.50 = £50.00.
    """
    ig = FakeIGService(min_deal_size=0.05)
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    limit_scan = _assemble_order(
        broker,
        direction=Direction.LONG,
        epic="UA.D.ARM.CASH.IP",
        entry_scan=167.10,
        size_scan=50.0,
        target_gbp=50.0,
    )

    assert limit_scan == pytest.approx(168.10)
    assert ig.open_kwargs is not None
    assert ig.open_kwargs["limit_level"] == pytest.approx(16810.0)
    # Target recovers exactly:
    recovered_gbp = (ig.open_kwargs["limit_level"] - 16710) * ig.open_kwargs["size"]
    assert recovered_gbp == pytest.approx(50.00)


# ---------------------------------------------------------------------------
# Test B — Grade-A+ LONG, £62.50 target
# ---------------------------------------------------------------------------


def test_grade_a_plus_long_locks_sixty_two_fifty():
    """Same setup as Test A but A+ target (£62.50). delta_scan = 1.25;
    limit_scan = 168.35; IG units = 16835. Matches the spec's worked
    example exactly."""
    ig = FakeIGService(min_deal_size=0.05)
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    limit_scan = _assemble_order(
        broker,
        direction=Direction.LONG,
        epic="UA.D.AMD.CASH.IP",
        entry_scan=167.10,
        size_scan=50.0,
        target_gbp=62.50,
    )

    assert limit_scan == pytest.approx(168.35)
    assert ig.open_kwargs["limit_level"] == pytest.approx(16835.0)
    recovered_gbp = (ig.open_kwargs["limit_level"] - 16710) * ig.open_kwargs["size"]
    assert recovered_gbp == pytest.approx(62.50)


# ---------------------------------------------------------------------------
# Test C — Grade-A SHORT, £50 target (limit below entry)
# ---------------------------------------------------------------------------


def test_grade_a_short_locks_fifty_pounds_below_entry():
    """SHORT at $234.00, £50/pt scan stake, £50 target.
    delta_scan = 1.00; limit_scan = 233.00; IG units = 23300.
    Direction flip: limit is BELOW entry for a SHORT."""
    ig = FakeIGService(min_deal_size=0.05)
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    limit_scan = _assemble_order(
        broker,
        direction=Direction.SHORT,
        epic="UA.D.JNJ.CASH.IP",
        entry_scan=234.00,
        size_scan=50.0,
        target_gbp=50.0,
    )

    assert limit_scan == pytest.approx(233.00)
    assert ig.open_kwargs["limit_level"] == pytest.approx(23300.0)
    # For SHORT: target recovers as (entry_ig - limit_ig) × size.
    recovered_gbp = (23400 - ig.open_kwargs["limit_level"]) * ig.open_kwargs["size"]
    assert recovered_gbp == pytest.approx(50.00)


# ---------------------------------------------------------------------------
# Test D — Grade-C LONG with minDealSize clamp
# ---------------------------------------------------------------------------


def test_grade_c_long_limit_uses_clamped_stake():
    """Scan stake £5/pt → descaled 0.05 → clamped up to minDealSize 0.24.
    effective_scan_stake = 0.24 × 100 = 24.  delta_scan = 50/24 ≈ 2.0833.
    Limit computed off the CLAMPED stake (not the pre-clamp 0.05) so
    (limit_ig − entry_ig) × 0.24 == £50.00 exactly."""
    ig = FakeIGService(min_deal_size=0.24)
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    limit_scan = _assemble_order(
        broker,
        direction=Direction.LONG,
        epic="UA.D.ARM.CASH.IP",
        entry_scan=167.10,
        size_scan=5.0,  # scan stake tiny enough to trigger clamp
        target_gbp=50.0,
    )

    # delta_scan = 50 / 24 = 2.0833...; limit_scan ≈ 169.1833
    assert limit_scan == pytest.approx(167.10 + 50.0 / 24.0, rel=1e-6)
    assert ig.open_kwargs["size"] == pytest.approx(0.24)  # clamped
    # Target recovers exactly at the clamped stake, not the pre-clamp one:
    recovered_gbp = (
        ig.open_kwargs["limit_level"] - 16710
    ) * ig.open_kwargs["size"]
    assert recovered_gbp == pytest.approx(50.00, rel=1e-6)


# ---------------------------------------------------------------------------
# Test E — edge case: no MarketData → limit cannot be computed
# ---------------------------------------------------------------------------


def test_limit_is_none_when_no_market_data():
    """Legacy broker (no MarketData attached) can't see the scaling
    factor, so compute_limit_price_for_target returns None. The
    order still opens — monitor logs a warning but the trail ladder
    remains the exit path."""
    ig = FakeIGService()
    broker = _make_broker(ig, market_data=None)

    limit_scan = broker.compute_limit_price_for_target(
        epic="IX.D.FTSE.DAILY.IP",
        direction=Direction.LONG,
        entry_price=8080.0,
        size=1.0,
        target_gbp=50.0,
    )

    assert limit_scan is None

    # Order must still be placeable with limit_level=None:
    broker.place_open_position(
        epic="IX.D.FTSE.DAILY.IP",
        direction=Direction.LONG,
        size=1.0,
        stop_price=8075.0,
        limit_level=None,
    )
    assert ig.open_kwargs is not None
    assert ig.open_kwargs["limit_level"] is None


def test_limit_is_none_when_target_is_zero_or_negative():
    """Defensive: target_gbp <= 0 returns None so monitor logs and
    opens without a take-profit rather than attaching a limit AT entry."""
    ig = FakeIGService()
    broker = _make_broker(ig, market_data=FakeMarketData(scale=100.0))

    assert (
        broker.compute_limit_price_for_target(
            epic="UA.D.ARM.CASH.IP",
            direction=Direction.LONG,
            entry_price=167.10,
            size=50.0,
            target_gbp=0.0,
        )
        is None
    )
    assert (
        broker.compute_limit_price_for_target(
            epic="UA.D.ARM.CASH.IP",
            direction=Direction.LONG,
            entry_price=167.10,
            size=50.0,
            target_gbp=-1.0,
        )
        is None
    )
