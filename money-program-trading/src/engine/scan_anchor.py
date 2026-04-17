"""Scan price grounding — reject shortlist entries whose prices don't
match IG's current market snapshot.

Motivation
----------
swing-committee produces shortlists via an LLM pipeline. The LLM
occasionally fabricates price levels that look plausible but don't
correspond to the actual tape — the 2026-04-17 DEMO shakedown hit
this with AMD shortlisted at $278 (IG quoted ~$155) and FDX at $380
(IG quoted $383.50 after scaling). Without grounding, the engine
either (a) never triggers because the fabricated level is far from
reality, or (b) triggers on a false premise and attaches a stop IG
will reject.

This module walks each shortlist entry, fetches the real IG snapshot
via ``MarketData.get_market_snapshot`` (which already normalises by
``scalingFactor``), and classifies the entry:

* **accepted** — scan reference price (trigger zone midpoint) is
  within ``max_drift_pct`` of IG's current ``last_traded``. Entry
  is persisted as-is; the session can replay it through the monitor
  normally.
* **rejected (drift_too_high)** — the LLM's price is off enough that
  we can't trust the rest of the setup. The entry is dropped and the
  rejection logged.
* **rejected (no_epic)** — the symbol doesn't resolve to an IG epic
  (e.g. ``BRK.B`` under IG's naming). Without an epic we can't
  execute anyway, so drop it.
* **rejected (no_quote)** — the epic exists but the snapshot call
  fails or comes back without a usable ``last_traded``. Drop it;
  a retry next session may succeed.

Design notes
------------
* We don't attempt to re-anchor prices (rescale trigger/stop/target
  to IG's level). Re-anchoring preserves the R ratio but silently
  shifts absolute levels, which changes whether the setup is still
  valid (e.g. a Darvas box top at $185 becomes meaningless if
  rescaled to $195). Rejection is safer than quietly correcting.
* Drift is computed against ``(trigger_low + trigger_high) / 2`` —
  the trigger *zone* midpoint, not the stop. Stops sit outside the
  zone by design and would inflate drift numbers.
* ``max_drift_pct`` default is 15%. This tolerates normal intraday
  drift between scan emission and ingestion (scans often generated
  at UK open, ingested a few hours later when US session starts)
  while rejecting the "LLM invented a 2023 price" case.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..data.market_data import MarketData
    from ..models.shortlist_entry import ShortlistEntry

logger = logging.getLogger(__name__)

# Default drift threshold. Configurable via Settings.price_anchor_max_drift_pct.
DEFAULT_MAX_DRIFT_PCT: float = 0.15


@dataclass
class AnchorResult:
    """Outcome of grounding a single shortlist entry against IG."""

    entry: "ShortlistEntry"
    accepted: bool
    reason: str  # ok | no_epic | no_quote | drift_too_high | error
    scan_ref_price: float
    ig_last_price: float | None = None
    drift_pct: float | None = None
    epic: str = ""
    notes: str = ""

    def as_log_dict(self) -> dict[str, Any]:
        """Shape for structured logging / SQLite event insert."""
        return {
            "candidate_id": self.entry.candidate_id,
            "symbol": self.entry.symbol,
            "market": self.entry.market.value,
            "accepted": self.accepted,
            "reason": self.reason,
            "scan_ref_price": round(self.scan_ref_price, 4),
            "ig_last_price": (
                round(self.ig_last_price, 4) if self.ig_last_price is not None else None
            ),
            "drift_pct": (
                round(self.drift_pct * 100, 2) if self.drift_pct is not None else None
            ),
            "epic": self.epic,
            "notes": self.notes,
        }


@dataclass
class AnchorReport:
    """Aggregate outcome of grounding a full shortlist."""

    results: list[AnchorResult] = field(default_factory=list)

    @property
    def accepted(self) -> list["ShortlistEntry"]:
        return [r.entry for r in self.results if r.accepted]

    @property
    def rejected_count(self) -> int:
        return sum(1 for r in self.results if not r.accepted)

    @property
    def accepted_count(self) -> int:
        return sum(1 for r in self.results if r.accepted)

    def rejections_by_reason(self) -> dict[str, int]:
        bucket: dict[str, int] = {}
        for r in self.results:
            if not r.accepted:
                bucket[r.reason] = bucket.get(r.reason, 0) + 1
        return bucket

    def summary_line(self) -> str:
        total = len(self.results)
        if total == 0:
            return "scan grounding: no entries"
        if self.rejected_count == 0:
            return f"scan grounding: {self.accepted_count}/{total} accepted, no drift rejections"
        by_reason = ", ".join(f"{k}={v}" for k, v in sorted(self.rejections_by_reason().items()))
        return (
            f"scan grounding: {self.accepted_count}/{total} accepted; "
            f"rejections — {by_reason}"
        )


def _scan_reference_price(entry: "ShortlistEntry") -> float:
    """The price we use to compare against IG's ``last_traded``.

    Midpoint of the entry trigger zone. Matches what the committee
    "thinks the stock is near" — drift against this captures the
    LLM-hallucination case (e.g. AMD trigger 278.00 vs IG 155.00).
    """
    return (entry.trigger_low + entry.trigger_high) / 2.0


def _classify_drift(
    scan_price: float,
    ig_last: float,
    max_drift_pct: float,
) -> tuple[bool, float]:
    """Return ``(accept, drift_pct)``.

    drift_pct is ``abs(scan_price - ig_last) / ig_last`` — we normalise
    by the IG quote, not the scan quote, because IG is the ground truth.
    """
    if ig_last <= 0:
        # Can't compute a ratio — treat as a quote failure upstream.
        return False, float("inf")
    drift = abs(scan_price - ig_last) / ig_last
    return drift <= max_drift_pct, drift


def anchor_shortlist_to_ig(
    entries: list["ShortlistEntry"],
    market_data: "MarketData",
    *,
    max_drift_pct: float = DEFAULT_MAX_DRIFT_PCT,
) -> AnchorReport:
    """Ground each entry against IG's current snapshot.

    Parameters
    ----------
    entries:
        Shortlist entries as loaded from the swing-committee handoff.
        Order is preserved for accepted entries.
    market_data:
        An authenticated ``MarketData`` instance.
    max_drift_pct:
        Maximum tolerable distance between the trigger-zone midpoint
        and IG's ``last_traded``, expressed as a fraction (0.15 = 15%).

    Returns
    -------
    AnchorReport
        Carries per-entry results plus helpers to pull out the accepted
        subset and a summary line for logging.
    """
    results: list[AnchorResult] = []
    for entry in entries:
        scan_price = _scan_reference_price(entry)
        result = AnchorResult(
            entry=entry,
            accepted=False,
            reason="error",
            scan_ref_price=scan_price,
        )

        # ── 1. Resolve epic ──────────────────────────────────────────
        try:
            epic = market_data.resolve_epic(entry.symbol, market=entry.market.value)
        except Exception as exc:  # noqa: BLE001
            result.reason = "error"
            result.notes = f"resolve_epic raised {type(exc).__name__}: {exc}"
            logger.warning(
                "anchor: %s — resolve_epic error: %s", entry.symbol, result.notes
            )
            results.append(result)
            continue

        if not epic:
            result.reason = "no_epic"
            result.notes = "IG search returned no whole-token match for symbol"
            logger.warning("anchor: %s — no IG epic, dropping", entry.symbol)
            results.append(result)
            continue

        result.epic = epic

        # ── 2. Fetch snapshot ────────────────────────────────────────
        try:
            snap = market_data.get_market_snapshot(epic)
        except Exception as exc:  # noqa: BLE001
            result.reason = "no_quote"
            result.notes = f"get_market_snapshot raised {type(exc).__name__}: {exc}"
            logger.warning(
                "anchor: %s (%s) — snapshot error: %s",
                entry.symbol,
                epic,
                result.notes,
            )
            results.append(result)
            continue

        ig_last = snap.get("last_traded")
        if ig_last is None or ig_last <= 0:
            # Fall back to bid/ask midpoint if last_traded is missing
            # (outside regular hours on CASH epics, for example).
            bid = snap.get("bid")
            ask = snap.get("ask")
            if bid and ask and bid > 0 and ask > 0:
                ig_last = (bid + ask) / 2.0
            else:
                result.reason = "no_quote"
                result.notes = (
                    f"snapshot had no usable last_traded/bid/ask "
                    f"(status={snap.get('market_status')!r})"
                )
                logger.warning(
                    "anchor: %s (%s) — %s", entry.symbol, epic, result.notes
                )
                results.append(result)
                continue

        result.ig_last_price = float(ig_last)

        # ── 3. Classify drift ────────────────────────────────────────
        accept, drift = _classify_drift(scan_price, float(ig_last), max_drift_pct)
        result.drift_pct = drift

        if accept:
            result.accepted = True
            result.reason = "ok"
            result.notes = (
                f"scan_ref={scan_price:.2f} vs ig_last={ig_last:.2f} "
                f"(drift={drift*100:.2f}%)"
            )
            logger.info("anchor: %s accepted — %s", entry.symbol, result.notes)
        else:
            result.accepted = False
            result.reason = "drift_too_high"
            result.notes = (
                f"scan_ref={scan_price:.2f} vs ig_last={ig_last:.2f} "
                f"(drift={drift*100:.2f}% > max {max_drift_pct*100:.0f}%)"
            )
            logger.warning(
                "anchor: %s REJECTED — %s", entry.symbol, result.notes
            )

        results.append(result)

    report = AnchorReport(results=results)
    logger.info(report.summary_line())
    return report
