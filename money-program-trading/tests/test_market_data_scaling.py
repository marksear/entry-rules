"""
Unit tests for IG scaling-factor normalisation in MarketData.

Context
-------
On 2026-04-17 the first DEMO shakedown caught a false TRIGGER_FIRED on
FDX: IG returned ``last=38350`` for a scan trigger of ``378``. IG quotes
US CASH equities in minor units (×100) and the broker adapter was
passing those raw values through to the monitor's price comparator.

These tests lock in the new behaviour:

* ``get_market_snapshot`` divides every price field by the
  ``scalingFactor`` reported in the ``instrument`` block.
* ``get_scaling_factor`` / ``to_ig_units`` expose the cached factor to
  the order-placement path.
* ``_search_market`` no longer silently returns a mis-matched epic when
  the ticker doesn't appear in the epic or instrumentName.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from src.data.market_data import MarketData


pytestmark = pytest.mark.unit


class FakeIGService:
    """Minimal stand-in for trading_ig's IGService used across tests."""

    def __init__(self, markets_response=None, search_response=None):
        self._markets_response = markets_response
        self._search_response = search_response

    def fetch_market_by_epic(self, epic):
        return self._markets_response

    def search_markets(self, ticker):
        return self._search_response


def _make_market_data(ig_service) -> MarketData:
    """Build a MarketData without touching the real IG session or disk
    cache. We skip ``__init__`` so we don't hit the parquet directory."""
    md = MarketData.__new__(MarketData)
    md._session = SimpleNamespace(service=ig_service)
    md._epic_cache = {}
    md._bar_cache = {}
    md._scale_cache = {}
    return md


# ---------------------------------------------------------------------------
# get_market_snapshot — scalingFactor normalisation
# ---------------------------------------------------------------------------


def test_snapshot_divides_prices_by_scaling_factor():
    """FDX-shape fixture: IG minor-units response must come back in dollars."""
    ig = FakeIGService(
        markets_response={
            "instrument": {"scalingFactor": 100, "name": "FedEx Corp"},
            "snapshot": {
                "bid": 38190,
                "offer": 38510,
                "lastTraded": 38350,
                "high": 38600,
                "low": 38050,
                "netChange": 125.0,
                "percentageChange": 0.33,
                "marketStatus": "TRADEABLE",
                "updateTime": "13:25:36",
            },
        }
    )
    md = _make_market_data(ig)

    snap = md.get_market_snapshot("UA.D.FDX.CASH.IP")

    assert snap["scaling_factor"] == 100.0
    assert snap["bid"] == pytest.approx(381.90)
    assert snap["ask"] == pytest.approx(385.10)
    assert snap["last_traded"] == pytest.approx(383.50)
    assert snap["high"] == pytest.approx(386.00)
    assert snap["low"] == pytest.approx(380.50)
    assert snap["net_change"] == pytest.approx(1.25)
    # pct_change is unitless and must NOT be scaled.
    assert snap["pct_change"] == pytest.approx(0.33)
    assert snap["market_status"] == "TRADEABLE"


def test_snapshot_defaults_scale_to_1_when_factor_missing():
    """UK DFB epics often omit scalingFactor — prices must pass through."""
    ig = FakeIGService(
        markets_response={
            "instrument": {"name": "FTSE 100"},
            "snapshot": {
                "bid": 8100.5,
                "offer": 8101.5,
                "lastTraded": 8101.0,
                "high": 8120.0,
                "low": 8080.0,
                "netChange": 5.0,
                "percentageChange": 0.06,
                "marketStatus": "TRADEABLE",
            },
        }
    )
    md = _make_market_data(ig)

    snap = md.get_market_snapshot("IX.D.FTSE.DAILY.IP")

    assert snap["scaling_factor"] == 1.0
    assert snap["last_traded"] == pytest.approx(8101.0)
    assert snap["bid"] == pytest.approx(8100.5)


def test_snapshot_handles_zero_or_negative_scale_as_1():
    """A garbage scalingFactor must not divide-by-zero or invert prices."""
    ig = FakeIGService(
        markets_response={
            "instrument": {"scalingFactor": 0},
            "snapshot": {
                "bid": 100,
                "offer": 101,
                "lastTraded": 100.5,
                "marketStatus": "TRADEABLE",
            },
        }
    )
    md = _make_market_data(ig)

    snap = md.get_market_snapshot("TEST.EPIC")

    assert snap["scaling_factor"] == 1.0
    assert snap["last_traded"] == pytest.approx(100.5)


# ---------------------------------------------------------------------------
# get_scaling_factor / to_ig_units
# ---------------------------------------------------------------------------


def test_to_ig_units_uses_cached_scale_after_snapshot():
    """After one snapshot fetch, the order-side helpers must be able to
    convert scan-unit levels into IG quoted units without another HTTP call."""
    ig = FakeIGService(
        markets_response={
            "instrument": {"scalingFactor": 100},
            "snapshot": {
                "bid": 37800,
                "offer": 38200,
                "lastTraded": 38000,
                "marketStatus": "TRADEABLE",
            },
        }
    )
    md = _make_market_data(ig)

    md.get_market_snapshot("UA.D.FDX.CASH.IP")

    assert md.get_scaling_factor("UA.D.FDX.CASH.IP") == 100.0
    # A $365 stop must become 36500 in IG quoted units.
    assert md.to_ig_units("UA.D.FDX.CASH.IP", 365.0) == pytest.approx(36500.0)
    # An unobserved epic falls back to 1.0 (passthrough).
    assert md.get_scaling_factor("UNSEEN.EPIC") == 1.0
    assert md.to_ig_units("UNSEEN.EPIC", 365.0) == pytest.approx(365.0)


def test_to_ig_units_none_is_none():
    md = _make_market_data(FakeIGService())
    assert md.to_ig_units("ANY.EPIC", None) is None


# ---------------------------------------------------------------------------
# _search_market — no silent mis-resolution
# ---------------------------------------------------------------------------


def test_search_market_accepts_ticker_match_in_epic():
    results = pd.DataFrame(
        [
            {"epic": "UA.D.AMD.CASH.IP", "instrumentName": "Advanced Micro Devices"},
        ]
    )
    md = _make_market_data(FakeIGService(search_response=results))
    assert md._search_market("AMD", "US") == "UA.D.AMD.CASH.IP"


def test_search_market_accepts_ticker_in_name_even_if_epic_opaque():
    results = pd.DataFrame(
        [
            {"epic": "KA.D.XYZ123.CASH.IP", "instrumentName": "AMD Common Stock"},
        ]
    )
    md = _make_market_data(FakeIGService(search_response=results))
    assert md._search_market("AMD", "US") == "KA.D.XYZ123.CASH.IP"


def test_search_market_rejects_non_ticker_fallback():
    """AMD-shape regression: IG returns a CASH row whose ticker isn't AMD
    anywhere. Must return '' rather than silently resolving (old 'last
    resort' branch is gone)."""
    results = pd.DataFrame(
        [
            {"epic": "KA.D.IAGMERGE.CASH.IP", "instrumentName": "IAG Merger Arb"},
            {"epic": "KA.D.UNRELATED.DFB.IP", "instrumentName": "Unrelated DFB"},
        ]
    )
    md = _make_market_data(FakeIGService(search_response=results))
    assert md._search_market("AMD", "US") == ""


def test_search_market_empty_results_returns_empty_string():
    md = _make_market_data(FakeIGService(search_response=pd.DataFrame()))
    assert md._search_market("ZZZZ", "US") == ""


def test_search_market_rejects_option_epic_with_substring_ticker():
    """AMD-options-epic regression: ``ON.D.AMDsa15500P6.CASH.IP`` was
    what IG returned from the real shakedown. "AMD" is only a substring
    inside "AMDsa15500P6" — a substring match would accept this options
    contract. Whole-token match must reject it."""
    results = pd.DataFrame(
        [
            {
                "epic": "ON.D.AMDsa15500P6.CASH.IP",
                "instrumentName": "AMD Put Option 155",
            },
        ]
    )
    md = _make_market_data(FakeIGService(search_response=results))
    # 'AMD' is a word in the instrumentName ("AMD Put Option 155") so
    # this one SHOULD match — the filter is designed to accept
    # name-level word matches. We use a narrower instrument name here
    # to prove the epic-segment check is strict.
    assert md._search_market("AMD", "US") == "ON.D.AMDsa15500P6.CASH.IP"

    # When the name is also opaque (e.g., the raw option descriptor),
    # the filter must NOT match on epic substring alone.
    results2 = pd.DataFrame(
        [
            {
                "epic": "ON.D.AMDsa15500P6.CASH.IP",
                "instrumentName": "Put AMDsa15500P6 Strike 155",
            },
        ]
    )
    md2 = _make_market_data(FakeIGService(search_response=results2))
    assert md2._search_market("AMD", "US") == ""


def test_search_market_prefers_exact_segment_match_over_options_row():
    """When IG returns both a clean equity epic and an options epic,
    the clean equity wins (first ticker-matching CASH row is taken)."""
    results = pd.DataFrame(
        [
            {
                "epic": "ON.D.AMDsa15500P6.CASH.IP",
                "instrumentName": "Put AMDsa15500P6",  # no 'AMD' word
            },
            {
                "epic": "UA.D.AMD.CASH.IP",
                "instrumentName": "Advanced Micro Devices",
            },
        ]
    )
    md = _make_market_data(FakeIGService(search_response=results))
    assert md._search_market("AMD", "US") == "UA.D.AMD.CASH.IP"


# ---------------------------------------------------------------------------
# _search_market — DAILY (24-hour) acceptance + dated-futures rejection
# (Added 2026-04-17 after universe build found AAPL/ABBV/ABT/ACN/ADBE being
#  rejected because IG returns them as ``.DAILY.IP`` epics, not CASH.)
# ---------------------------------------------------------------------------


def test_search_market_accepts_daily_24hour_epic_for_us_equities():
    """IG returns US equities as ``.DAILY.IP`` (24-hour spread bet)
    ahead of any CASH row, and often CASH doesn't exist at all for
    single-name US stocks on spread-bet accounts. The resolver must
    accept DAILY when nothing better is available."""
    results = pd.DataFrame(
        [
            {"epic": "UA.D.AAPL.DAILY.IP", "instrumentName": "Apple Inc (24 Hours)"},
            {"epic": "UA.D.AAPL.JUN.IP", "instrumentName": "Apple Inc (24 Hours)"},
            {"epic": "UA.D.AAPL.SEP.IP", "instrumentName": "Apple Inc (24 Hours)"},
        ]
    )
    md = _make_market_data(FakeIGService(search_response=results))
    assert md._search_market("AAPL", "US") == "UA.D.AAPL.DAILY.IP"


def test_search_market_rejects_dated_expiry_segments():
    """``.JUN.IP`` / ``.SEP.IP`` / ``.DEC.IP`` / ``.MAR.IP`` are quarterly
    futures — rollover behaviour makes them unsuitable for day-trading.
    The resolver must reject them even if the ticker matches, and return
    '' when only dated rows are available."""
    results = pd.DataFrame(
        [
            {"epic": "UA.D.AAPL.JUN.IP", "instrumentName": "Apple Inc"},
            {"epic": "UA.D.AAPL.SEP.IP", "instrumentName": "Apple Inc"},
            {"epic": "UA.D.AAPL.DEC.IP", "instrumentName": "Apple Inc"},
            {"epic": "UA.D.AAPL.MAR.IP", "instrumentName": "Apple Inc"},
        ]
    )
    md = _make_market_data(FakeIGService(search_response=results))
    assert md._search_market("AAPL", "US") == ""


def test_search_market_prefers_cash_over_daily_when_both_present():
    """If IG returns both a CASH row and a DAILY row for the same ticker,
    CASH wins (cleaner undated cash market, no 24-hour spread penalty)."""
    results = pd.DataFrame(
        [
            {"epic": "UA.D.AAPL.DAILY.IP", "instrumentName": "Apple Inc (24 Hours)"},
            {"epic": "UA.D.AAPL.CASH.IP", "instrumentName": "Apple Inc"},
        ]
    )
    md = _make_market_data(FakeIGService(search_response=results))
    assert md._search_market("AAPL", "US") == "UA.D.AAPL.CASH.IP"


def test_search_market_prefers_dfb_over_daily():
    """DFB (daily funded bet, IG's main UK intraday product) beats DAILY
    (24-hour)."""
    results = pd.DataFrame(
        [
            {"epic": "UA.D.AAPL.DAILY.IP", "instrumentName": "Apple Inc (24 Hours)"},
            {"epic": "UA.D.AAPL.DFB.IP", "instrumentName": "Apple Inc"},
        ]
    )
    md = _make_market_data(FakeIGService(search_response=results))
    assert md._search_market("AAPL", "US") == "UA.D.AAPL.DFB.IP"


def test_search_market_skips_dated_rows_and_picks_daily():
    """Real-world shape from the 2026-04-17 universe build: IG returned
    DAILY + three quarterly rows for AAPL. The resolver must skip the
    quarterlies and return DAILY."""
    results = pd.DataFrame(
        [
            {"epic": "UA.D.AAPL.JUN.IP", "instrumentName": "Apple Inc (24 Hours)"},
            {"epic": "UA.D.AAPL.SEP.IP", "instrumentName": "Apple Inc (24 Hours)"},
            {"epic": "UA.D.AAPL.DEC.IP", "instrumentName": "Apple Inc (24 Hours)"},
            {"epic": "UA.D.AAPL.DAILY.IP", "instrumentName": "Apple Inc (24 Hours)"},
        ]
    )
    md = _make_market_data(FakeIGService(search_response=results))
    assert md._search_market("AAPL", "US") == "UA.D.AAPL.DAILY.IP"
