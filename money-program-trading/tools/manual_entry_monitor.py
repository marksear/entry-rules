#!/usr/bin/env python3
"""
manual_entry_monitor.py — heads-up display for manual trade entry.

Reads a ``scan_YYYYMMDD.json`` (the same handoff entry-rules consumes),
polls Yahoo every 60 seconds for live prices, runs the FULL Masterclass
rule stack against each shortlist symbol, and prints whether a manual
entry is permitted right now.

What rules run
--------------
The script imports ``src.engine.monitor.classify_tick`` directly so the
exact same logic that gates the live engine gates this tool. That means:

- **Rule 9A BGU** — gap-up-day 15-min opening-range gate (R20 / R21).
- **Rule 22 strict breakout** — non-gap day must break above
  ``trigger_high`` (LONG) or below ``trigger_low`` (SHORT). Inside-zone
  ticks return R22.
- **Session-clock gates** — R_SESSION_PREMATURE before 09:45 ET,
  R_SESSION_CUTOFF after the entries cutoff.

Per ``feedback_trigger_semantics``: the legacy "any tick in zone fires"
semantic is GARBAGE. This tool NEVER falls back to it. The Masterclass
document is the law — every entry decision in this script goes through
``classify_tick`` and respects every gate.

What this tool does NOT do
--------------------------
- Place orders. When a signal goes FIRE, you hit buy/sell in IG yourself.
- Manage exits. The print-out gives you stop + target levels — set them
  manually in the broker after entry.
- Pay attention to position sizing. The scan JSON already has stake
  computed; copy it into IG.

Usage
-----
::

    python tools/manual_entry_monitor.py [path/to/scan.json]

If no path is given, the most recent ``scan_*.json`` in
``data/scans/`` is picked automatically.

Press Ctrl+C to stop.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

import urllib.error
import urllib.request

# Make ``src`` importable when run as ``python tools/manual_entry_monitor.py``.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.engine.monitor import (  # noqa: E402
    CandidatePlan,
    CandidateRuntimeState,
    Decision,
    classify_tick,
)
from src.engine.session_clock import SessionClock  # noqa: E402
from src.models.common import Direction, EntryType, Market  # noqa: E402
from src.models.log_enums import BrokerMode, CandidateGrade  # noqa: E402
from src.utils.time_utils import utc_now  # noqa: E402


POLL_SECONDS = 60
YAHOO_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def latest_scan_path() -> Path:
    """Pick the newest ``scan_*.json`` in ``data/scans/``."""
    scans_dir = REPO_ROOT / "data" / "scans"
    files = sorted(scans_dir.glob("scan_*.json"), reverse=True)
    if not files:
        sys.exit(f"No scan files found in {scans_dir}")
    return files[0]


def make_plan(entry: dict, scan_id: str) -> CandidatePlan:
    """Translate a shortlist_entries row into a CandidatePlan."""
    return CandidatePlan(
        candidate_id=entry["candidate_id"],
        scan_id=scan_id,
        session_id="manual-monitor",
        symbol=entry["symbol"],
        market=Market.US if entry.get("market") == "US" else Market.UK,
        direction=Direction.LONG if entry["direction"] == "LONG" else Direction.SHORT,
        setup_type=EntryType(entry.get("setup_type", "L-A")),
        grade=CandidateGrade(entry.get("grade", "B")),
        trigger_low=float(entry["trigger_low"]),
        trigger_high=float(entry["trigger_high"]),
        stop_price=float(entry["stop_price"]),
        target_price=(
            float(entry["target_price"]) if entry.get("target_price") is not None else None
        ),
        ig_epic=f"manual-{entry['symbol']}",
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=float(entry.get("planned_stake_gbp_per_pt") or 1.0),
        planned_risk_gbp=float(entry.get("planned_risk_gbp") or 50.0),
    )


# ---------------------------------------------------------------------------
# Live price source — Yahoo Finance chart endpoint
# ---------------------------------------------------------------------------


def fetch_yahoo_price(symbol: str) -> float | None:
    """Latest price from Yahoo (regularMarketPrice → last 1m close)."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1m&range=1d"
    )
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=YAHOO_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None
    except json.JSONDecodeError:
        return None

    result = (data.get("chart") or {}).get("result")
    if not result:
        return None
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        # Fall back to last non-null 1-minute close on the chart.
        indicators = (result[0].get("indicators") or {}).get("quote") or [{}]
        closes = indicators[0].get("close") or []
        valid = [c for c in closes if c is not None]
        price = valid[-1] if valid else None
    if price is None:
        # Final fallback: previousClose so something useful prints during halts.
        price = meta.get("previousClose")
    return float(price) if price is not None else None


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


REJECTION_HINTS = {
    "R20": "BGU 15-min OR forming",
    "R21": "BGU OR-high not broken yet",
    "R22": "awaiting strict breakout",
    "R_SESSION_PREMATURE": "before 09:45 ET — wait",
    "R_SESSION_CUTOFF": "past entries cutoff — skip today",
}


def format_row(plan: CandidatePlan, last: float | None, outcome) -> str:
    sym = f"{plan.symbol:6}"
    direction = f"{plan.direction.value:5}"
    last_str = f"{last:>8.2f}" if last is not None else "       —"

    if last is None:
        status = "NO PRICE"
        action = "Yahoo price unavailable — skip this tick"
    elif outcome.decision == Decision.FIRE:
        status = "🔥 FIRE"
        verb = "BUY" if plan.direction == Direction.LONG else "SELL"
        target_str = (
            f"target {plan.target_price:.2f}"
            if plan.target_price is not None
            else "no target"
        )
        action = (
            f"{verb} {plan.symbol} @ {last:.2f} — "
            f"stop {plan.stop_price:.2f}, {target_str}"
        )
    elif outcome.decision == Decision.REJECT:
        code = outcome.rejection_code or "?"
        status = f"REJ({code})"
        action = REJECTION_HINTS.get(code, "wait")
    elif outcome.decision == Decision.ARM:
        status = "ARMED"
        action = "near trigger — watch closely"
    elif outcome.decision == Decision.HOLD:
        status = "HOLD"
        action = "far from trigger"
    elif outcome.decision == Decision.NO_PRICE:
        status = "NO PRICE"
        action = "skip"
    else:
        status = str(outcome.decision)
        action = "?"
    return f"  {sym} {direction} {last_str}  {status:<14} {action}"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) > 1:
        scan_path = Path(sys.argv[1]).expanduser().resolve()
    else:
        scan_path = latest_scan_path()

    print(f"Reading scan: {scan_path}")
    with open(scan_path) as fh:
        scan = json.load(fh)

    scan_id = scan["scan_record"]["scan_id"]
    entries = scan.get("shortlist_entries") or []
    if not entries:
        sys.exit("No shortlist entries to monitor — stand aside.")

    plans = [make_plan(e, scan_id) for e in entries]
    states = {p.candidate_id: CandidateRuntimeState() for p in plans}

    today = date.today()
    # All current shortlists are US-session; UK switch is trivial if needed.
    clock = SessionClock.for_us_session(today)

    print(
        f"Monitoring {len(plans)} candidate"
        f"{'' if len(plans) == 1 else 's'}:"
    )
    for p in plans:
        target_str = (
            f"target {p.target_price}"
            if p.target_price is not None
            else "no target"
        )
        print(
            f"  {p.symbol:6} {p.direction.value:5} "
            f"trigger {p.trigger_low}-{p.trigger_high}  "
            f"stop {p.stop_price}  {target_str}"
        )

    print(
        f"\nSession entries open at {clock.entries_open_utc.isoformat()}"
        f"\nSession hard close   at {clock.hard_close_utc.isoformat()}"
        f"\n\nPolling Yahoo every {POLL_SECONDS}s. Ctrl+C to stop."
        f"\nThis tool NEVER places orders. You hit buy/sell yourself when FIRE shows."
        f"\nRules in force: Rule 9A BGU + Rule 22 strict breakout + session-clock gates."
        f"\n"
    )

    try:
        while True:
            now = utc_now()
            ts = now.strftime("%Y-%m-%d %H:%M:%S UTC")
            print(f"=== {ts} ===")
            for plan in plans:
                last = fetch_yahoo_price(plan.symbol)
                snapshot = {
                    "last_traded": last,
                    "bid": last,
                    "ask": last,
                    "market_status": "TRADEABLE",
                    "high": None,
                    "low": None,
                    "net_change": None,
                    "pct_change": None,
                    "update_time_utc": None,
                    "scaling_factor": 1.0,
                }
                state = states[plan.candidate_id]
                outcome = classify_tick(
                    plan,
                    snapshot,
                    state,
                    session_clock=clock,
                    now=now,
                )
                print(format_row(plan, last, outcome))
            print()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
