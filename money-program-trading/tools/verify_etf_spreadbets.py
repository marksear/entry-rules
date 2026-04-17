"""
Verify which US ETFs are tradable on the IG spread-bet account.

Session 9 prerequisite. Before hard-coding SPY/QQQ/IWM/DIA/XLK into
the 200-name universe we need to confirm each one has at least one
spread-bet-eligible CASH or DFB epic on this account.

Usage
-----
    python -m tools.verify_etf_spreadbets          # writes data/cache/etf_verification.json
    python -m tools.verify_etf_spreadbets --tickers SPY QQQ IWM DIA XLK VOO

Output
------
JSON at ``data/cache/etf_verification.json`` shaped as::

    {
      "generated_utc": "2026-04-17T12:34:56Z",
      "acc_type": "DEMO",
      "results": {
        "SPY": {
          "status": "ok" | "no_match" | "cfd_only" | "error",
          "epic": "UA.D.SPY.CASH.IP",
          "instrument_name": "SPDR S&P 500 ETF Trust",
          "instrument_type": "SHARES",
          "scaling_factor": 100.0,
          "market_status": "TRADEABLE",
          "currency": "USD",
          "margin_factor_unit": "PERCENTAGE",
          "margin_factor": 20.0,
          "notes": ""
        },
        ...
      }
    }

Design notes
------------
* The search is whole-token (ticker appears as an epic segment or a
  name word). No substring fallback — we inherit the same discipline
  from ``MarketData._search_market``.
* "cfd_only" is inferred when search finds the ticker but no CASH or
  DFB epic is returned. Spread-bet accounts receive CASH/DFB; CFD
  accounts receive MINI or CFD epics.
* We deliberately keep this script dependency-light so it can run
  before the rest of the Session 9 scanner package exists.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python -m tools.verify_etf_spreadbets` from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.auth.ig_auth import IGSession  # noqa: E402
from src.config.settings import get_settings  # noqa: E402

logger = logging.getLogger("verify_etf_spreadbets")

DEFAULT_TICKERS = ("SPY", "QQQ", "IWM", "DIA", "XLK")


def _whole_token_match(ticker: str, epic: str, instrument_name: str) -> bool:
    """Mirror of MarketData._search_market._ticker_match.

    Whole-token match only. Rejects options epics like
    ``ON.D.AMDsa15500P6.CASH.IP`` where the ticker is a substring of
    one segment.
    """
    import re

    ticker_u = ticker.upper()
    epic_segments = [s.upper() for s in epic.split(".")]
    if ticker_u in epic_segments:
        return True
    name_tokens = [
        t.upper() for t in re.split(r"[^A-Za-z0-9]+", instrument_name) if t
    ]
    return ticker_u in name_tokens


def _pick_best_epic(ticker: str, results) -> tuple[str, str, str]:
    """Walk search_markets results, return (epic, instrumentName, reason).

    Preference: CASH > DFB. Ignore anything else (MINI = CFD-only).
    """
    if results is None or results.empty:
        return "", "", "no_results"

    cash_hit = None
    dfb_hit = None
    cfd_hit = None

    for _, row in results.iterrows():
        epic = str(row.get("epic", ""))
        name = str(row.get("instrumentName", ""))
        if not epic:
            continue
        if not _whole_token_match(ticker, epic, name):
            continue
        if "CASH" in epic and cash_hit is None:
            cash_hit = (epic, name)
        elif "DFB" in epic and dfb_hit is None:
            dfb_hit = (epic, name)
        elif ("MINI" in epic or "CFD" in epic) and cfd_hit is None:
            cfd_hit = (epic, name)

    if cash_hit:
        return cash_hit[0], cash_hit[1], "cash"
    if dfb_hit:
        return dfb_hit[0], dfb_hit[1], "dfb"
    if cfd_hit:
        return cfd_hit[0], cfd_hit[1], "cfd_only"
    return "", "", "no_ticker_match"


def verify(tickers: tuple[str, ...]) -> dict:
    settings = get_settings()
    session = IGSession(settings)
    session.connect()
    ig = session.service

    results: dict[str, dict] = {}
    for ticker in tickers:
        entry: dict = {"status": "error", "epic": "", "notes": ""}
        try:
            logger.info("Searching IG for %s...", ticker)
            search = ig.search_markets(ticker)
            epic, name, reason = _pick_best_epic(ticker, search)

            if not epic:
                entry["status"] = "no_match" if reason == "no_ticker_match" else reason
                entry["notes"] = f"search returned {0 if search is None else len(search)} rows; reason={reason}"
                results[ticker] = entry
                continue

            if reason == "cfd_only":
                entry["status"] = "cfd_only"
                entry["epic"] = epic
                entry["instrument_name"] = name
                entry["notes"] = "No CASH/DFB epic — not tradable on spread-bet account"
                results[ticker] = entry
                continue

            logger.info("  → candidate epic: %s (%s)", epic, reason)
            md = ig.fetch_market_by_epic(epic)
            instrument = md.get("instrument", {}) if isinstance(md, dict) else {}
            snapshot = md.get("snapshot", {}) if isinstance(md, dict) else {}
            dealing = md.get("dealingRules", {}) if isinstance(md, dict) else {}

            margin_factor_block = dealing.get("marginFactor", {}) or {}

            entry.update(
                {
                    "status": "ok",
                    "epic": epic,
                    "instrument_name": instrument.get("name", name),
                    "instrument_type": instrument.get("type", ""),
                    "scaling_factor": float(instrument.get("scalingFactor", 1) or 1),
                    "market_status": snapshot.get("marketStatus", ""),
                    "currency": (
                        instrument.get("currencies", [{}])[0].get("code", "")
                        if instrument.get("currencies")
                        else ""
                    ),
                    "margin_factor_unit": margin_factor_block.get("unit", ""),
                    "margin_factor": margin_factor_block.get("value", None),
                    "notes": f"resolved via {reason}",
                }
            )
        except Exception as exc:  # noqa: BLE001 — log everything, don't crash the run
            logger.exception("Verification failed for %s", ticker)
            entry["status"] = "error"
            entry["notes"] = f"{type(exc).__name__}: {exc}"
        results[ticker] = entry

    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "acc_type": settings.ig_acc_type.value,
        "results": results,
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=list(DEFAULT_TICKERS),
        help="Tickers to verify (default: SPY QQQ IWM DIA XLK)",
    )
    parser.add_argument(
        "--out",
        default="data/cache/etf_verification.json",
        help="Output path (default: data/cache/etf_verification.json)",
    )
    args = parser.parse_args()

    report = verify(tuple(t.upper() for t in args.tickers))

    out_path = _REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Human-readable summary to stdout
    print(f"\nETF spread-bet verification ({report['acc_type']}):")
    for ticker, entry in report["results"].items():
        status = entry["status"]
        marker = {
            "ok": "✓",
            "cfd_only": "✗ (CFD only)",
            "no_match": "✗ (not found)",
            "error": "! error",
        }.get(status, status)
        epic = entry.get("epic", "") or "—"
        name = entry.get("instrument_name", "") or ""
        print(f"  {marker:20s} {ticker:6s} {epic:30s} {name}")
    print(f"\nFull report: {out_path}\n")

    errors = [t for t, e in report["results"].items() if e["status"] == "error"]
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
