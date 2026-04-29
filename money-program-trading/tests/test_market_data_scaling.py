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


def _make_market_data(ig_service, tmp_dir=None) -> MarketData:
    """Build a MarketData without touching the real IG session or disk
    cache. We skip ``__init__`` so we don't hit the parquet directory.

    ``tmp_dir`` is set to an in-memory-ish location so resolve_epic can
    persist its cache without hitting the production data/cache/ path.
    """
    import tempfile
    from pathlib import Path
    md = MarketData.__new__(MarketData)
    md._session = SimpleNamespace(service=ig_service)
    md._epic_cache = {}
    md._bar_cache = {}
    md._scale_cache = {}
    # _cache_dir is only used by _save_epic_cache when resolve_epic
    # successfully resolves a new ticker. Tests that only call
    # _search_market never hit it, but tests that exercise the public
    # resolve_epic path do.
    md._cache_dir = Path(tmp_dir or tempfile.mkdtemp(prefix="md_test_"))
    md._cache_dir.mkdir(parents=True, exist_ok=True)
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


def test_snapshot_fallback_to_100_for_us_equity_with_missing_factor():
    """2026-04-20 DEMO Day-1 regression: IG's /markets/{epic} response for
    ``SC.D.FDX.DAILY.IP`` and ``SA.D.AMD.DAILY.IP`` came back with the
    instrument block present (type=SHARES) but ``scalingFactor`` absent.
    Raw minor-unit prices leaked through (39100.50 for $391 FDX) and
    scan_anchor then rejected every entry as ~99% drift. The fallback:
    when type is SHARES AND bid/ask look like minor units (>1500), assume
    scalingFactor=100."""
    ig = FakeIGService(
        markets_response={
            "instrument": {"type": "SHARES", "name": "FedEx Corp"},  # no scalingFactor
            "snapshot": {
                "bid": 39080.5,
                "offer": 39120.0,
                "lastTraded": 39100.5,
                "marketStatus": "TRADEABLE",
            },
        }
    )
    md = _make_market_data(ig)

    snap = md.get_market_snapshot("SC.D.FDX.DAILY.IP")

    # Fallback must kick in, not leave raw minor units in place.
    assert snap["scaling_factor"] == 100.0
    assert snap["last_traded"] == pytest.approx(391.005)
    assert snap["bid"] == pytest.approx(390.805)
    assert snap["ask"] == pytest.approx(391.20)


def test_snapshot_fallback_not_triggered_for_non_shares():
    """FTSE-shape fixture: instrument type absent (or INDICES) must NOT
    trigger the ×100 fallback even if prices happen to be above 1500.
    FTSE legitimately trades at 8100 in native units."""
    ig = FakeIGService(
        markets_response={
            "instrument": {"type": "INDICES", "name": "FTSE 100"},
            "snapshot": {
                "bid": 8100.5,
                "offer": 8101.5,
                "lastTraded": 8101.0,
                "marketStatus": "TRADEABLE",
            },
        }
    )
    md = _make_market_data(ig)

    snap = md.get_market_snapshot("IX.D.FTSE.DAILY.IP")

    assert snap["scaling_factor"] == 1.0
    assert snap["last_traded"] == pytest.approx(8101.0)
    assert snap["bid"] == pytest.approx(8100.5)


def test_snapshot_fallback_not_triggered_when_prices_normal():
    """type=SHARES but raw bid/ask already in dollar range (< 1500) must
    NOT trigger the ×100 fallback — falling through to 1.0 is correct when
    the price already looks like a dollar figure (e.g. a $50 stock)."""
    ig = FakeIGService(
        markets_response={
            "instrument": {"type": "SHARES", "name": "Small Cap Corp"},
            "snapshot": {
                "bid": 49.5,
                "offer": 50.5,
                "lastTraded": 50.0,
                "marketStatus": "TRADEABLE",
            },
        }
    )
    md = _make_market_data(ig)

    snap = md.get_market_snapshot("UA.D.SMCP.DAILY.IP")

    # No fallback triggered — prices already in dollars, no evidence of
    # minor-unit quoting. Default 1.0 is correct.
    assert snap["scaling_factor"] == 1.0
    assert snap["last_traded"] == pytest.approx(50.0)


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


# ---------------------------------------------------------------------------
# 2026-04-29 RKT regression — UK pence-quoted shares above 1500p
# ---------------------------------------------------------------------------
#
# The bug: KA.D.RB.DAILY.IP (Reckitt Benckiser) returns bid 4682.3 in
# pence with snapshot.scalingFactor=1, but the instrument block omits
# scalingFactor. The old fallback path kicked in (bid > 1500 + type
# SHARES) and divided every price by 100, so the live monitor saw
# Reckitt at 46.82 instead of 4682p. Rule 4-Chase then rejected with
# R23 (open way below pivot).
#
# Fix is layered:
#   (a) resolve_scaling_factor now consults snapshot.scalingFactor first,
#       which is IG's authoritative answer.
#   (b) the >1500 fallback heuristic is suppressed when instrument
#       currency is GBP/GBX, so even if the snapshot path were bypassed
#       the heuristic wouldn't fire on UK shares.


def test_snapshot_scaling_factor_takes_precedence_over_instrument():
    """RKT-shape: snapshot says scalingFactor=1, instrument is empty. The
    correct answer is 1.0 — IG's snapshot is authoritative. Without this
    branch the fallback heuristic would run on bid=4682.3 and incorrectly
    return 100."""
    ig = FakeIGService(
        markets_response={
            "instrument": {
                "type": "SHARES",
                "name": "Reckitt Benckiser Group PLC",
                "currencies": [{"code": "GBP", "symbol": "£", "isDefault": True}],
            },
            "snapshot": {
                "bid": 4682.3,
                "offer": 4694.7,
                "lastTraded": 4688.5,
                "high": 4760.7,
                "low": 4682.3,
                "scalingFactor": 1,
                "decimalPlacesFactor": 1,
                "marketStatus": "TRADEABLE",
            },
        }
    )
    md = _make_market_data(ig)

    snap = md.get_market_snapshot("KA.D.RB.DAILY.IP")

    assert snap["scaling_factor"] == 1.0, (
        "RB has snapshot.scalingFactor=1; resolver must honour it and NOT "
        "divide by 100 via the fallback heuristic."
    )
    assert snap["bid"] == pytest.approx(4682.3)
    assert snap["ask"] == pytest.approx(4694.7)
    assert snap["last_traded"] == pytest.approx(4688.5)


def test_uk_share_above_1500_not_scaled_when_currency_gbp():
    """Defence-in-depth: even if the snapshot path is bypassed (no
    snapshot.scalingFactor at all), a GBP share with bid > 1500p must
    NOT trip the ×100 fallback. Catches the RKT class of bug if any
    future epic family ships without snapshot.scalingFactor."""
    ig = FakeIGService(
        markets_response={
            "instrument": {
                "type": "SHARES",
                "name": "Reckitt Benckiser Group PLC",
                "currencies": [{"code": "GBP", "symbol": "£", "isDefault": True}],
                # Note: no scalingFactor anywhere on instrument
            },
            "snapshot": {
                "bid": 4682.3,
                "offer": 4694.7,
                "lastTraded": 4688.5,
                "marketStatus": "TRADEABLE",
                # Note: no scalingFactor on snapshot either
            },
        }
    )
    md = _make_market_data(ig)

    snap = md.get_market_snapshot("KA.D.RB.DAILY.IP")

    assert snap["scaling_factor"] == 1.0, (
        "GBP share above 1500p must not trip the US-equity ×100 fallback; "
        "those are pence, not cents."
    )
    assert snap["bid"] == pytest.approx(4682.3)


def test_us_equity_above_1500_still_scaled_when_currency_usd():
    """Belt-and-braces for the original FDX/AMD case: USD currency with
    bid > 1500 must STILL hit the ×100 fallback. The new currency-aware
    guard only suppresses the heuristic for GBP/GBX, not USD."""
    ig = FakeIGService(
        markets_response={
            "instrument": {
                "type": "SHARES",
                "name": "FedEx Corp",
                "currencies": [{"code": "USD", "symbol": "$", "isDefault": True}],
                # No scalingFactor on instrument — historical FDX bug shape
            },
            "snapshot": {
                "bid": 39080.5,
                "offer": 39120.0,
                "lastTraded": 39100.5,
                "marketStatus": "TRADEABLE",
                # No scalingFactor on snapshot — forces fallback path
            },
        }
    )
    md = _make_market_data(ig)

    snap = md.get_market_snapshot("SC.D.FDX.DAILY.IP")

    assert snap["scaling_factor"] == 100.0, (
        "USD equity above 1500 must still hit the ×100 fallback — that's "
        "the original FDX/AMD bug the heuristic exists for."
    )
    assert snap["last_traded"] == pytest.approx(391.005)


def test_resolve_scaling_factor_unit_snapshot_path():
    """Direct unit test of resolve_scaling_factor: snapshot.scalingFactor
    short-circuits everything else."""
    from src.data.scaling import resolve_scaling_factor

    sf = resolve_scaling_factor(
        epic="KA.D.RB.DAILY.IP",
        instrument={"type": "SHARES", "name": "Reckitt"},
        bid_raw=4682.3,
        ask_raw=4694.7,
        snapshot={"scalingFactor": 1},
    )
    assert sf == 1.0


def test_resolve_scaling_factor_unit_gbp_guard():
    """Direct unit test: GBP share with no scaling info anywhere returns
    1.0 even when bid > 1500. The currency guard wins over the heuristic."""
    from src.data.scaling import resolve_scaling_factor

    sf = resolve_scaling_factor(
        epic="KA.D.RB.DAILY.IP",
        instrument={
            "type": "SHARES",
            "currencies": [{"code": "GBP", "isDefault": True}],
        },
        bid_raw=4682.3,
        ask_raw=4694.7,
        snapshot=None,
    )
    assert sf == 1.0


def test_resolve_scaling_factor_unit_usd_fallback_still_works():
    """Direct unit test: USD share with no scaling info but bid > 1500
    still gets the ×100 treatment (preserves the original behaviour)."""
    from src.data.scaling import resolve_scaling_factor

    sf = resolve_scaling_factor(
        epic="SC.D.FDX.DAILY.IP",
        instrument={
            "type": "SHARES",
            "currencies": [{"code": "USD", "isDefault": True}],
        },
        bid_raw=39080.5,
        ask_raw=39120.0,
        snapshot=None,
    )
    assert sf == 100.0


def test_resolve_scaling_factor_back_compat_no_snapshot_arg():
    """Old call sites that don't pass snapshot still work. Defaults to
    None which falls through to the instrument/heuristic path."""
    from src.data.scaling import resolve_scaling_factor

    # USD share, no scalingFactor anywhere, bid > 1500 → fallback ×100.
    sf = resolve_scaling_factor(
        epic="SC.D.FDX.DAILY.IP",
        instrument={
            "type": "SHARES",
            "currencies": [{"code": "USD", "isDefault": True}],
        },
        bid_raw=39080.5,
        ask_raw=39120.0,
    )
    assert sf == 100.0


# ---------------------------------------------------------------------------
# 2026-04-29 UK epic resolution — Tasks #67 (LN-suffix retry) + #68 (UK gate)
# ---------------------------------------------------------------------------
#
# Captured from today's debug_uk_search.py diagnostic against IG:
#   MNG search → returns KA.D.MNGLN.DAILY.IP, "M&G PLC"
#                (no "LSE" in instrumentName)
#   RKT search → returns SG.D.RKTUS.DAILY.IP, "Rocket Companies (24 Hours)"
#                (US ticker collision; correctly rejected)
#   Reckitt    → returns KA.D.RB.DAILY.IP, "Reckitt Benckiser Group PLC"
#                (no "LSE" in instrumentName)
#
# Old behaviour:
#   resolve_epic("MNG", "UK") → bare-ticker search finds the MNGLN epic
#   row but rejects it via _ticker_match (MNG ≠ MNGLN as whole token).
#   No fallback. NO_EPIC.
#
# New behaviour (this fix):
#   resolve_epic("MNG", "UK") → bare-ticker search empty → retry with
#   "MNGLN" → finds KA.D.MNGLN.DAILY.IP. UK gate accepts because the
#   retry ticker ends in "LN".


def test_resolve_epic_uk_ln_suffix_retry_finds_mng():
    """MNG (bare) → empty → retry with MNGLN → finds KA.D.MNGLN.DAILY.IP."""
    # IG returns the same row regardless of which ticker we search for.
    # The first call (with "MNG") fails because "MNG" is not a whole
    # token in "KA.D.MNGLN.DAILY.IP". The retry with "MNGLN" succeeds.
    mng_row = {
        "epic": "KA.D.MNGLN.DAILY.IP",
        "instrumentName": "M&G PLC",
        "instrumentType": "SHARES",
    }
    ig = FakeIGService(search_response=pd.DataFrame([mng_row]))
    md = _make_market_data(ig)

    epic = md.resolve_epic("MNG", "UK")
    assert epic == "KA.D.MNGLN.DAILY.IP", (
        f"expected MNGLN epic via LN-suffix retry, got {epic!r}"
    )


def test_resolve_epic_caches_under_original_ticker_after_ln_retry():
    """After the LN-suffix retry succeeds, the cache key uses the
    ORIGINAL ticker (MNG:UK), not MNGLN:UK. Callers don't need to know
    about the LN convention."""
    mng_row = {
        "epic": "KA.D.MNGLN.DAILY.IP",
        "instrumentName": "M&G PLC",
        "instrumentType": "SHARES",
    }
    ig = FakeIGService(search_response=pd.DataFrame([mng_row]))
    md = _make_market_data(ig)

    md.resolve_epic("MNG", "UK")
    assert md._epic_cache.get("MNG:UK") == "KA.D.MNGLN.DAILY.IP"
    assert "MNGLN:UK" not in md._epic_cache


def test_resolve_epic_skips_ln_retry_when_ticker_already_ends_in_ln():
    """Direct MNGLN lookup: bare-ticker search finds it, no retry needed."""
    mng_row = {
        "epic": "KA.D.MNGLN.DAILY.IP",
        "instrumentName": "M&G PLC",
        "instrumentType": "SHARES",
    }
    ig = FakeIGService(search_response=pd.DataFrame([mng_row]))
    md = _make_market_data(ig)

    epic = md.resolve_epic("MNGLN", "UK")
    assert epic == "KA.D.MNGLN.DAILY.IP"


def test_search_market_accepts_uk_share_with_ln_ticker_suffix():
    """The relaxed UK gate (Task #68) accepts rows where the ticker
    ends in "LN" — IG's internal LSE convention. No "LSE" substring
    needed in the instrumentName."""
    mng_row = {
        "epic": "KA.D.MNGLN.DAILY.IP",
        "instrumentName": "M&G PLC",
        "instrumentType": "SHARES",
    }
    md = _make_market_data(
        FakeIGService(search_response=pd.DataFrame([mng_row]))
    )
    # Simulate the LN-suffix retry path by calling _search_market directly
    # with the LN ticker. The gate must accept.
    epic = md._search_market("MNGLN", "UK")
    assert epic == "KA.D.MNGLN.DAILY.IP"


def test_search_market_uk_still_rejects_us_collision_ticker():
    """RKT-shape: search returns SG.D.RKTUS.DAILY.IP "Rocket Companies".
    With market=UK, the gate must reject this US ticker. The whole-token
    match alone catches it (RKT not in epic segments), but defence-in-
    depth: even if it matched, the UK gate would reject (no LSE / .L /
    LN-suffix marker on this row's instrumentName or ticker)."""
    rocket_row = {
        "epic": "SG.D.RKTUS.DAILY.IP",
        "instrumentName": "Rocket Companies Inc (24 Hours)",
        "instrumentType": "SHARES",
    }
    md = _make_market_data(
        FakeIGService(search_response=pd.DataFrame([rocket_row]))
    )
    # Whole-token match fails (RKT not a segment in RKTUS). Plus the
    # UK gate would reject if it ever got there.
    assert md._search_market("RKT", "UK") == ""


def test_search_market_uk_accepts_legacy_lse_in_name():
    """Back-compat: rows whose instrumentName explicitly contains "LSE"
    still pass the gate (some older epic families do)."""
    legacy_row = {
        "epic": "KA.D.SBRY.DAILY.IP",
        "instrumentName": "Sainsbury (J) PLC (LSE)",
        "instrumentType": "SHARES",
    }
    md = _make_market_data(
        FakeIGService(search_response=pd.DataFrame([legacy_row]))
    )
    epic = md._search_market("SBRY", "UK")
    assert epic == "KA.D.SBRY.DAILY.IP"


