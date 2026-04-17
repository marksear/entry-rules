"""
Unit tests for src/engine/scan_anchor.py

Context
-------
Scan anchoring exists because the swing-committee LLM pipeline occasionally
fabricates price levels that don't match the real tape (the 2026-04-17 DEMO
shakedown saw AMD shortlisted at $278 when IG quoted $155). We reject such
entries rather than re-anchor them, because re-anchoring silently shifts the
setup thesis (a Darvas box top at $185 becomes meaningless if rescaled to $195).

These tests pin down the behaviour:

* Entries within ``max_drift_pct`` of IG's ``last_traded`` are accepted.
* Entries outside that window are rejected as ``drift_too_high``.
* Missing epic / missing quote / exceptions are rejected with the matching reason.
* When ``last_traded`` is missing we fall back to the bid/ask midpoint.
* The ``AnchorReport`` summary/accepted/rejected helpers match the underlying
  results.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.engine.scan_anchor import (
    AnchorReport,
    AnchorResult,
    anchor_shortlist_to_ig,
)
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import BrokerMode, CandidateGrade
from src.models.shortlist_entry import ShortlistEntry


pytestmark = pytest.mark.unit


# ──────────────────────────────────────────────────────────────────────────
# Fixtures / builders
# ──────────────────────────────────────────────────────────────────────────


def _make_entry(
    symbol: str = "AMD",
    *,
    trigger_low: float = 275.0,
    trigger_high: float = 281.0,
    stop_price: float = 270.0,
    direction: Direction = Direction.LONG,
    setup_type: EntryType = EntryType.L_A,
    grade: CandidateGrade = CandidateGrade.A_PLUS,
    market: Market = Market.US,
) -> ShortlistEntry:
    """Build a minimal valid ShortlistEntry for anchoring tests."""
    return ShortlistEntry(
        scan_id="scan-test",
        symbol=symbol,
        market=market,
        direction=direction,
        setup_type=setup_type,
        grade=grade,
        trigger_low=trigger_low,
        trigger_high=trigger_high,
        stop_price=stop_price,
        planned_stake_gbp_per_pt=1.0,
        planned_risk_gbp=5.0,
        planned_risk_pct_account=0.01,
        broker_mode=BrokerMode.DEMO,
    )


class FakeMarketData:
    """Stand-in for ``MarketData`` — only implements what scan_anchor uses.

    ``epic_map`` maps ticker → epic (or None to trigger ``no_epic``).
    ``snapshots`` maps epic → dict (as get_market_snapshot would return)
        or an Exception to raise for that epic.
    ``resolve_errors`` maps ticker → Exception to raise during resolve_epic.
    """

    def __init__(
        self,
        *,
        epic_map: dict[str, str | None] | None = None,
        snapshots: dict[str, Any] | None = None,
        resolve_errors: dict[str, Exception] | None = None,
    ) -> None:
        self._epic_map = epic_map or {}
        self._snapshots = snapshots or {}
        self._resolve_errors = resolve_errors or {}
        self.resolve_calls: list[tuple[str, str]] = []
        self.snapshot_calls: list[str] = []

    def resolve_epic(self, ticker: str, market: str = "US") -> str:
        self.resolve_calls.append((ticker, market))
        if ticker in self._resolve_errors:
            raise self._resolve_errors[ticker]
        return self._epic_map.get(ticker, "") or ""

    def get_market_snapshot(self, epic: str) -> dict[str, Any]:
        self.snapshot_calls.append(epic)
        snap = self._snapshots.get(epic)
        if isinstance(snap, Exception):
            raise snap
        return snap if snap is not None else {}


# ──────────────────────────────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────────────────────────────


def test_anchor_accepts_entry_within_drift_threshold():
    # Scan ref midpoint = 278, IG last 276 → drift ~0.7% < 15%
    entry = _make_entry(trigger_low=275.0, trigger_high=281.0, stop_price=270.0)
    md = FakeMarketData(
        epic_map={"AMD": "SA.D.AMD.DAILY.IP"},
        snapshots={"SA.D.AMD.DAILY.IP": {"last_traded": 276.0}},
    )

    report = anchor_shortlist_to_ig([entry], md)

    assert report.accepted_count == 1
    assert report.rejected_count == 0
    assert report.accepted == [entry]
    [result] = report.results
    assert result.accepted is True
    assert result.reason == "ok"
    assert result.epic == "SA.D.AMD.DAILY.IP"
    assert result.scan_ref_price == pytest.approx(278.0)
    assert result.ig_last_price == pytest.approx(276.0)
    assert result.drift_pct == pytest.approx(abs(278.0 - 276.0) / 276.0)


def test_anchor_rejects_entry_with_excessive_drift():
    # AMD-at-278 vs IG-155 replay of the 2026-04-17 incident.
    entry = _make_entry(trigger_low=275.0, trigger_high=281.0, stop_price=270.0)
    md = FakeMarketData(
        epic_map={"AMD": "SA.D.AMD.DAILY.IP"},
        snapshots={"SA.D.AMD.DAILY.IP": {"last_traded": 155.0}},
    )

    report = anchor_shortlist_to_ig([entry], md)

    assert report.accepted_count == 0
    assert report.rejected_count == 1
    [result] = report.results
    assert result.accepted is False
    assert result.reason == "drift_too_high"
    # ~79% drift — well outside the 15% window
    assert result.drift_pct is not None and result.drift_pct > 0.5


def test_anchor_rejects_when_epic_cannot_be_resolved():
    entry = _make_entry(symbol="BRK.B")
    md = FakeMarketData(epic_map={"BRK.B": ""})

    report = anchor_shortlist_to_ig([entry], md)

    [result] = report.results
    assert result.accepted is False
    assert result.reason == "no_epic"
    assert result.epic == ""
    assert result.ig_last_price is None
    # We must NOT hit get_market_snapshot when epic resolution fails.
    assert md.snapshot_calls == []


def test_anchor_rejects_when_resolve_epic_raises():
    entry = _make_entry(symbol="AMD")
    md = FakeMarketData(
        epic_map={"AMD": "SA.D.AMD.DAILY.IP"},
        resolve_errors={"AMD": RuntimeError("IG down")},
    )

    report = anchor_shortlist_to_ig([entry], md)

    [result] = report.results
    assert result.accepted is False
    assert result.reason == "error"
    assert "RuntimeError" in result.notes


def test_anchor_rejects_when_snapshot_raises():
    entry = _make_entry(symbol="AMD")
    md = FakeMarketData(
        epic_map={"AMD": "SA.D.AMD.DAILY.IP"},
        snapshots={"SA.D.AMD.DAILY.IP": RuntimeError("snapshot 500")},
    )

    report = anchor_shortlist_to_ig([entry], md)

    [result] = report.results
    assert result.accepted is False
    assert result.reason == "no_quote"
    assert result.epic == "SA.D.AMD.DAILY.IP"
    assert result.ig_last_price is None


def test_anchor_rejects_when_snapshot_lacks_usable_price():
    """No last_traded AND no bid/ask → cannot anchor, reject."""
    entry = _make_entry(symbol="AMD")
    md = FakeMarketData(
        epic_map={"AMD": "SA.D.AMD.DAILY.IP"},
        snapshots={
            "SA.D.AMD.DAILY.IP": {
                "last_traded": None,
                "bid": None,
                "ask": None,
                "market_status": "CLOSED",
            }
        },
    )

    report = anchor_shortlist_to_ig([entry], md)

    [result] = report.results
    assert result.accepted is False
    assert result.reason == "no_quote"
    assert "CLOSED" in result.notes


def test_anchor_falls_back_to_bid_ask_midpoint_when_last_traded_missing():
    """Out-of-hours CASH epics omit last_traded but still carry bid/ask."""
    entry = _make_entry(
        symbol="AMD", trigger_low=275.0, trigger_high=281.0, stop_price=270.0
    )
    md = FakeMarketData(
        epic_map={"AMD": "SA.D.AMD.DAILY.IP"},
        snapshots={
            "SA.D.AMD.DAILY.IP": {
                "last_traded": None,
                "bid": 275.5,
                "ask": 276.5,
                "market_status": "EDITS_ONLY",
            }
        },
    )

    report = anchor_shortlist_to_ig([entry], md)

    [result] = report.results
    assert result.accepted is True
    assert result.reason == "ok"
    # Fallback midpoint = (275.5 + 276.5) / 2 = 276.0
    assert result.ig_last_price == pytest.approx(276.0)


def test_anchor_reports_rejections_by_reason_and_summary_line():
    ok_entry = _make_entry(symbol="AAPL")
    drift_entry = _make_entry(symbol="AMD")
    missing_epic_entry = _make_entry(symbol="BRK.B")

    md = FakeMarketData(
        epic_map={
            "AAPL": "UA.D.AAPL.DAILY.IP",
            "AMD": "SA.D.AMD.DAILY.IP",
            "BRK.B": "",
        },
        snapshots={
            "UA.D.AAPL.DAILY.IP": {"last_traded": 278.0},
            "SA.D.AMD.DAILY.IP": {"last_traded": 155.0},
        },
    )

    report = anchor_shortlist_to_ig(
        [ok_entry, drift_entry, missing_epic_entry], md
    )

    assert isinstance(report, AnchorReport)
    assert report.accepted_count == 1
    assert report.rejected_count == 2
    assert report.accepted == [ok_entry]
    by_reason = report.rejections_by_reason()
    assert by_reason == {"drift_too_high": 1, "no_epic": 1}

    summary = report.summary_line()
    assert "1/3" in summary
    assert "drift_too_high=1" in summary
    assert "no_epic=1" in summary


def test_anchor_threshold_respects_max_drift_pct_override():
    """Caller can widen/tighten the drift tolerance."""
    entry = _make_entry(trigger_low=95.0, trigger_high=105.0, stop_price=90.0)
    # scan midpoint = 100, ig last = 120 → drift = 0.1666...
    md = FakeMarketData(
        epic_map={"AMD": "SA.D.AMD.DAILY.IP"},
        snapshots={"SA.D.AMD.DAILY.IP": {"last_traded": 120.0}},
    )

    tight = anchor_shortlist_to_ig([entry], md, max_drift_pct=0.10)
    loose = anchor_shortlist_to_ig([entry], md, max_drift_pct=0.20)

    assert tight.accepted_count == 0
    assert tight.results[0].reason == "drift_too_high"
    assert loose.accepted_count == 1
    assert loose.results[0].reason == "ok"


def test_anchor_result_as_log_dict_shape():
    entry = _make_entry(symbol="AAPL")
    md = FakeMarketData(
        epic_map={"AAPL": "UA.D.AAPL.DAILY.IP"},
        snapshots={"UA.D.AAPL.DAILY.IP": {"last_traded": 278.0}},
    )

    report = anchor_shortlist_to_ig([entry], md)
    log = report.results[0].as_log_dict()

    # Shape check — keys used by session_writer to persist the decision.
    assert set(log.keys()) >= {
        "candidate_id",
        "symbol",
        "market",
        "accepted",
        "reason",
        "scan_ref_price",
        "ig_last_price",
        "drift_pct",
        "epic",
        "notes",
    }
    assert log["symbol"] == "AAPL"
    assert log["market"] == "US"
    assert log["accepted"] is True
    assert log["reason"] == "ok"


def test_anchor_preserves_input_order_for_accepted_entries():
    a = _make_entry(symbol="AAPL")
    b = _make_entry(symbol="AMD")  # will be rejected
    c = _make_entry(symbol="ABT")

    md = FakeMarketData(
        epic_map={
            "AAPL": "UA.D.AAPL.DAILY.IP",
            "AMD": "SA.D.AMD.DAILY.IP",
            "ABT": "SA.D.ABT.DAILY.IP",
        },
        snapshots={
            "UA.D.AAPL.DAILY.IP": {"last_traded": 278.0},
            "SA.D.AMD.DAILY.IP": {"last_traded": 100.0},  # 64% drift
            "SA.D.ABT.DAILY.IP": {"last_traded": 279.0},
        },
    )

    report = anchor_shortlist_to_ig([a, b, c], md)
    assert [e.symbol for e in report.accepted] == ["AAPL", "ABT"]


def test_anchor_empty_input_returns_empty_report():
    md = FakeMarketData()
    report = anchor_shortlist_to_ig([], md)
    assert report.results == []
    assert report.accepted == []
    assert report.accepted_count == 0
    assert report.rejected_count == 0
    assert "no entries" in report.summary_line()


def test_anchor_result_dataclass_round_trips_basic_fields():
    # Cheap sanity check for the dataclass helpers so we don't regress the
    # log_dict keys silently.
    entry = _make_entry(symbol="AMD")
    result = AnchorResult(
        entry=entry,
        accepted=False,
        reason="drift_too_high",
        scan_ref_price=278.0,
        ig_last_price=155.0,
        drift_pct=(278 - 155) / 155,
        epic="SA.D.AMD.DAILY.IP",
        notes="replay",
    )
    log = result.as_log_dict()
    assert log["drift_pct"] == pytest.approx(round(((278 - 155) / 155) * 100, 2))
    assert log["epic"] == "SA.D.AMD.DAILY.IP"
