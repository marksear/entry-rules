"""Universe resolver — turn the hand-curated ticker lists into a
structured map of IG epics.

Design
------
* Single-pass over SP100 ∪ NDX ∪ FTSE15.
* Each ticker is resolved via ``MarketData.resolve_epic`` which already
  enforces whole-token matching and caches into ``data/cache/epic_map.json``.
* Index proxies are hard-coded — we don't search for them because IG
  doesn't return SPY/QQQ/etc. as spread bets at all (see
  ``reference_ig_index_proxies.md``).
* Paced at ~25 requests/min to stay under IG's 30/min non-trading cap
  with a safety margin.
* Resumable — unresolved tickers from a previous run are retried.

Output schema
-------------
``data/cache/universe.json``::

    {
      "generated_utc": "2026-04-17T17:42:00Z",
      "total_tickers_requested": 191,
      "resolved": 183,
      "unresolved": 8,
      "entries": [
        {
          "ticker": "AAPL",
          "epic": "UA.D.AAPL.CASH.IP",
          "market": "US",
          "source": "sp100|ndx",
          "status": "ok"
        },
        {
          "ticker": "BRK.B",
          "epic": "",
          "market": "US",
          "source": "sp100",
          "status": "unresolved",
          "notes": "IG doesn't list BRK.B under that ticker; manual epic required"
        },
        ...
        {
          "ticker": "US500",
          "epic": "IX.D.SPTRD.DAILY.IP",
          "market": "US",
          "source": "index_proxy",
          "status": "ok",
          "instrument_name": "US 500"
        }
      ]
    }
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..data.market_data import MarketData
from .universe_lists import (
    FTSE_TICKERS,
    INDEX_PROXIES,
    MANUAL_EPIC_OVERRIDES,
    UNAVAILABLE_ON_IG,
    combined_us_tickers,
)

logger = logging.getLogger(__name__)

# Honour IG's 30/min non-trading rate limit with headroom.
DEFAULT_REQUESTS_PER_MINUTE = 25
DEFAULT_OUTPUT_PATH = Path("data/cache/universe.json")


def _sources_for_ticker(ticker: str, sp100: set[str], ndx: set[str]) -> str:
    tags: list[str] = []
    if ticker in sp100:
        tags.append("sp100")
    if ticker in ndx:
        tags.append("ndx")
    return "|".join(tags) if tags else "us"


def build_universe(
    market_data: MarketData,
    *,
    include_ftse: bool = True,
    include_index_proxies: bool = True,
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
    max_tickers: int | None = None,
    skip_resolved: bool = True,
) -> dict[str, Any]:
    """Resolve the full universe and return a structured report.

    Parameters
    ----------
    market_data:
        Live ``MarketData`` instance with an authenticated IG session.
    include_ftse:
        Include the FTSE 15 block (default True).
    include_index_proxies:
        Include the 6 IG index products (default True).
    requests_per_minute:
        Pacing cap. ~25 keeps us under IG's 30/min non-trading limit.
    max_tickers:
        Optional cap on how many *unresolved* tickers to attempt this
        run. Useful for chunking within short time budgets.
    skip_resolved:
        If True, tickers whose epic is already in the cache are counted
        but not re-searched. Second-run calls are fast.
    """
    from .universe_lists import NDX_TICKERS, SP100_TICKERS

    sp100 = set(SP100_TICKERS)
    ndx = set(NDX_TICKERS)
    us_tickers = combined_us_tickers()

    # Seed the cache with manual overrides. ``MarketData.resolve_epic``
    # checks the cache before calling IG, so this short-circuits any
    # ticker whose IG epic wouldn't pass whole-token matching (e.g.
    # AbbVie's ``SA.D.ABBVWUS.DAILY.IP``).
    overrides_applied = 0
    for cache_key, epic in MANUAL_EPIC_OVERRIDES.items():
        if cache_key not in market_data._epic_cache:  # type: ignore[attr-defined]
            market_data._epic_cache[cache_key] = epic  # type: ignore[attr-defined]
            overrides_applied += 1
    if overrides_applied:
        market_data._save_epic_cache()  # type: ignore[attr-defined]
        logger.info("Applied %d manual epic overrides", overrides_applied)

    entries: list[dict[str, Any]] = []
    min_gap = 60.0 / requests_per_minute if requests_per_minute > 0 else 0
    attempts = 0
    last_call = 0.0

    def _maybe_throttle() -> None:
        """Pace calls so we don't burn the rate limit."""
        nonlocal last_call
        if min_gap <= 0:
            return
        elapsed = time.monotonic() - last_call
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        last_call = time.monotonic()

    def _resolve_one(ticker: str, market: str, source: str) -> dict[str, Any]:
        nonlocal attempts
        entry: dict[str, Any] = {
            "ticker": ticker,
            "market": market,
            "source": source,
        }

        # Known unavailable — skip without burning an IG call.
        if ticker in UNAVAILABLE_ON_IG:
            entry["epic"] = ""
            entry["status"] = "unavailable"
            entry["notes"] = "known not offered on IG UK retail spread-bet"
            return entry

        # Cache-hit short-circuit — no IG call burned.
        cache_key = f"{ticker}:{market}"
        cached = market_data._epic_cache.get(cache_key)  # type: ignore[attr-defined]
        if cached and skip_resolved:
            entry["epic"] = cached
            entry["status"] = "ok"
            entry["notes"] = (
                "manual override"
                if cache_key in MANUAL_EPIC_OVERRIDES
                else "cache hit"
            )
            return entry

        if max_tickers is not None and attempts >= max_tickers:
            entry["epic"] = ""
            entry["status"] = "skipped"
            entry["notes"] = "max_tickers reached this run"
            return entry

        _maybe_throttle()
        attempts += 1
        try:
            epic = market_data.resolve_epic(ticker, market=market)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Resolution error for %s: %s", ticker, exc)
            entry["epic"] = ""
            entry["status"] = "error"
            entry["notes"] = f"{type(exc).__name__}: {exc}"
            return entry

        entry["epic"] = epic
        if epic:
            entry["status"] = "ok"
        else:
            entry["status"] = "unresolved"
            entry["notes"] = "search returned no whole-token match"
        return entry

    # ── US single names ────────────────────────────────────────────────
    for ticker in us_tickers:
        source = _sources_for_ticker(ticker, sp100, ndx)
        entries.append(_resolve_one(ticker, "US", source))

    # ── FTSE 100 liquids ───────────────────────────────────────────────
    if include_ftse:
        for ticker in FTSE_TICKERS:
            entries.append(_resolve_one(ticker, "UK", "ftse"))

    # ── Index proxies (hard-coded, no search) ──────────────────────────
    if include_index_proxies:
        for key, cfg in INDEX_PROXIES.items():
            entries.append(
                {
                    "ticker": key,
                    "epic": cfg["epic"],
                    "market": cfg["market"],
                    "source": "index_proxy",
                    "status": "ok",
                    "instrument_name": cfg["instrument_name"],
                    "notes": f"hard-coded, replaces {cfg['replaces']}",
                }
            )

    resolved = sum(1 for e in entries if e["status"] == "ok")
    unresolved = sum(1 for e in entries if e["status"] == "unresolved")
    skipped = sum(1 for e in entries if e["status"] == "skipped")
    errored = sum(1 for e in entries if e["status"] == "error")
    unavailable = sum(1 for e in entries if e["status"] == "unavailable")

    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_tickers_requested": len(entries),
        "resolved": resolved,
        "unresolved": unresolved,
        "skipped": skipped,
        "errored": errored,
        "unavailable": unavailable,
        "attempts_this_run": attempts,
        "entries": entries,
    }


def write_universe(report: dict[str, Any], path: Path | None = None) -> Path:
    """Persist the resolver output to JSON."""
    out = path or DEFAULT_OUTPUT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out


def summarise_report(report: dict[str, Any]) -> str:
    """Human-readable summary suitable for stdout."""
    lines = [
        f"Universe resolver — {report['generated_utc']}",
        f"  requested:   {report['total_tickers_requested']}",
        f"  resolved:    {report['resolved']}",
        f"  unresolved:  {report['unresolved']}",
        f"  unavailable: {report.get('unavailable', 0)}",
        f"  skipped:     {report['skipped']}",
        f"  errored:     {report['errored']}",
        f"  IG calls:    {report['attempts_this_run']}",
    ]
    if report["unresolved"] or report["errored"]:
        lines.append("")
        lines.append("Failed tickers:")
        for e in report["entries"]:
            if e["status"] in ("unresolved", "error"):
                lines.append(
                    f"  {e['status']:10s} {e['ticker']:8s} {e.get('notes', '')}"
                )
    return "\n".join(lines)
