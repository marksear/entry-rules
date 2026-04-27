#!/usr/bin/env python3
"""
manual_entry_monitor.py — heads-up display for manual trade entry.

Reads a ``scan_YYYYMMDD.json`` (the same handoff entry-rules consumes),
polls **IG** every 60 seconds for live prices via the same MarketData
adapter the live monitor uses, runs the FULL Masterclass rule stack
against each shortlist symbol, and prints whether a manual entry is
permitted right now.

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
document is the law.

Why IG and not Yahoo
--------------------
The live monitor reads prices from IG. If this tool used Yahoo,
"FIRE" might fire at a moment when IG's quote hasn't yet broken the
trigger — and the human would enter at a different price than the
heads-up display suggested. By pulling from IG via the same
``MarketData`` adapter, the price the tool sees is the price the
operator will be filled at.

What this tool does NOT do
--------------------------
- Place orders. When a signal goes FIRE, you hit buy/sell in IG yourself.
- Manage exits. The print-out gives stop + target levels — set them
  manually in the broker after entry.

Usage
-----
::

    python tools/manual_entry_monitor.py [path/to/scan.json]

If no path is given, the most recent ``scan_*.json`` in
``data/scans/`` is picked automatically. IG credentials are read from
``.env`` exactly as the live monitor does.

Press Ctrl+C to stop.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Make ``src`` importable when run as ``python tools/manual_entry_monitor.py``.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Quiet the per-tick scalingFactor warnings from src/data/scaling.py. The
# DAILY.IP fallback is a known IG quirk (see feedback_ig_scaling_factor_daily_ip),
# documented once in the live monitor logs — no need to repeat it every poll
# in a heads-up tool.
logging.getLogger("src.data.scaling").setLevel(logging.ERROR)

from src.auth.ig_auth import IGSession  # noqa: E402
from src.data.market_data import MarketData  # noqa: E402
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
LONDON_TZ = ZoneInfo("Europe/London")
NY_TZ = ZoneInfo("America/New_York")


def fmt_local_time(utc_dt: datetime, tz: ZoneInfo = LONDON_TZ) -> str:
    """Format a UTC datetime as HH:MM in the given local zone."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(tz).strftime("%H:%M")


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


PILLAR_LABELS = {
    "livermore": "Livermore",
    "oneil": "O'Neil",
    "minervini": "Minervini",
    "darvas": "Darvas",
    "raschke": "Raschke",
    "weinstein": "Weinstein",
}


def _rr_ratio(plan: CandidatePlan) -> float | None:
    """Worst-case reward:risk at trigger entry.

    LONG: entry = trigger_high (worst entry price); reward = target - entry; risk = entry - stop.
    SHORT: entry = trigger_low; reward = entry - target; risk = stop - entry.
    """
    if plan.target_price is None:
        return None
    if plan.direction == Direction.LONG:
        entry = plan.trigger_high
        reward = plan.target_price - entry
        risk = entry - plan.stop_price
    else:
        entry = plan.trigger_low
        reward = entry - plan.target_price
        risk = plan.stop_price - entry
    if risk <= 0:
        return None
    return reward / risk


def _stop_pct(plan: CandidatePlan) -> float:
    """Stop distance as % of entry price (worst-case entry side)."""
    entry = plan.trigger_high if plan.direction == Direction.LONG else plan.trigger_low
    if entry == 0:
        return 0.0
    return abs(entry - plan.stop_price) / entry


def format_preflight(entry: dict, plan: CandidatePlan) -> str:
    """Per-candidate one-time block: every Layer-1 (pre-screen) and
    Layer-2 (pre-flight) rule from CANONICAL_ENTRY_RULES.md, with current
    pass/fail/drift/not-implemented status.

    Layer 1 rules ran in the scanner before this candidate was shortlisted;
    they're surfaced via grade + pillar_votes (the "why is this a
    candidate?" evidence). Layer 2 risk/sizing rules run in scanEmission
    when the shortlist entry is built — some are validated here against
    the scan JSON, some are flagged as NOT IMPLEMENTED.
    """
    grade = entry.get("grade", "?")
    pillar_votes = entry.get("pillar_votes") or {}
    pillars_passing = [
        PILLAR_LABELS.get(k, k) for k, v in pillar_votes.items() if v
    ]
    pillars_count = sum(1 for v in pillar_votes.values() if v)
    pillars_total = len(pillar_votes) or 6
    pillars_str = (
        f"{pillars_count}/{pillars_total} confirmed: " + ", ".join(pillars_passing)
        if pillars_passing
        else f"{pillars_count}/{pillars_total} confirmed"
    )
    verdict_raw = (entry.get("extras") or {}).get("verdict") or "?"
    setup = entry.get("setup_type") or "?"
    direction = plan.direction
    is_long = direction == Direction.LONG
    is_short = not is_long

    # ── Layer 1 — Pre-screen (Rules P1-P7) ─────────────────────────────
    pre_screen = ["    Layer 1 — Pre-screen (scanner gates, surfaced via grade):"]
    pre_screen.append(
        f"      • Rule P1 Trend Template (LONG):     "
        f"{'⏸ upstream — passed (implied by grade)' if is_long else '– N/A (SHORT candidate)'}"
    )
    pre_screen.append(
        f"      • Rule P2 Inverse Trend Tmpl (SHORT):"
        f"{' ⏸ upstream — passed (implied by grade)' if is_short else ' – N/A (LONG candidate)'}"
    )
    pre_screen.append(
        f"      • Rule P3 ADX(14) > 25 (BOTH):       ⏸ upstream — passed (implied by grade)"
    )
    pre_screen.append(
        f"      • Rule P4 Volume Dry-Up (LONG):      "
        f"{'⏸ upstream — passed (implied by grade)' if is_long else '– N/A (SHORT candidate)'}"
    )
    pre_screen.append(
        f"      • Rule P5 Distribution Days (SHORT): "
        f"{'⏸ upstream — passed (implied by grade)' if is_short else '– N/A (LONG candidate)'}"
    )
    pre_screen.append(
        f"      • Rule P6 Squeeze Check (SHORT):     "
        f"{'⏳ partial — needs SI/DTC/borrow data verification' if is_short else '– N/A (LONG candidate)'}"
    )
    if setup == "S-D":
        pre_screen.append(
            "      • Rule P7 Climax Top exception:      ⏳ partial — S-D-specific, thresholds need verification"
        )
    else:
        pre_screen.append(
            f"      • Rule P7 Climax Top exception:      – N/A (setup={setup}, not S-D)"
        )

    # ── Layer 2 — Pre-flight risk/sizing (Rules R1-R6) ─────────────────
    pre_flight = ["", "    Layer 2 — Pre-flight risk/sizing (scanEmission):"]
    risk_pct = entry.get("planned_risk_pct_account")
    if isinstance(risk_pct, (int, float)):
        actual_pct = risk_pct * 100
        if 0.95 <= actual_pct <= 1.05:
            r1 = f"✓ {actual_pct:.2f}% (matches spec ≤ 1%)"
        elif 0.45 <= actual_pct <= 0.55:
            r1 = f"⚠ {actual_pct:.2f}% — DRIFT (spec is 1%, Task #52)"
        elif actual_pct < 1.0:
            r1 = f"⚠ {actual_pct:.2f}% — under spec (≤ 1%, but not stepped 0.5%)"
        else:
            r1 = f"✗ {actual_pct:.2f}% — exceeds 1% cap"
    else:
        r1 = "? unknown (planned_risk_pct_account missing)"
    pre_flight.append(f"      • Rule R1 Risk ≤ 1% per trade:       {r1}")

    rr = _rr_ratio(plan)
    if rr is None:
        r2 = "? cannot compute (missing target or zero risk)"
    elif rr >= 3.0:
        r2 = f"✓ {rr:.2f}:1 (≥ 3:1)"
    else:
        r2 = f"✗ {rr:.2f}:1 — VIOLATION (spec ≥ 3:1, Task #53)"
    pre_flight.append(f"      • Rule R2 Reward:Risk ≥ 3:1:         {r2}")

    sp = _stop_pct(plan) * 100
    if sp <= 8.0:
        r3 = f"✓ {sp:.2f}% (≤ 8%)"
    else:
        r3 = f"✗ {sp:.2f}% — exceeds 8% cap"
    pre_flight.append(f"      • Rule R3 Stop distance ≤ 8%:        {r3}")
    pre_flight.append(
        "      • Rule R4 Single position cap (10/8%): ⚙ NOT IMPLEMENTED (needs portfolio context)"
    )
    pre_flight.append(
        "      • Rule R5 Total open risk ≤ 6%:      ⚙ NOT IMPLEMENTED (needs portfolio context)"
    )
    pre_flight.append(
        f"      • Rule R6 Short exposure ≤ 50%:      "
        f"{'⚙ NOT IMPLEMENTED (needs portfolio context)' if is_short else '– N/A (LONG candidate)'}"
    )

    # ── Setup metadata (informational) ─────────────────────────────────
    metadata = [
        "",
        "    Setup metadata (from scan JSON):",
        f"      • Setup type:      {setup}",
        f"      • Grade:           {grade}",
        f"      • Six pillars:     {pillars_str}",
        f"      • LLM verdict:     {verdict_raw}",
    ]

    return "\n".join(pre_screen + pre_flight + metadata)


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
        ig_epic=f"manual-{entry['symbol']}",  # overwritten on resolution below
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=float(entry.get("planned_stake_gbp_per_pt") or 1.0),
        planned_risk_gbp=float(entry.get("planned_risk_gbp") or 50.0),
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _check_session(clock: SessionClock, now: datetime) -> tuple[str, str]:
    """Return (entries_open_line, cutoff_line) — pretty pass/fail strings."""
    open_label = fmt_local_time(clock.entries_open_utc)
    cutoff_label = fmt_local_time(clock.entries_cutoff_utc)
    if clock.is_before_entries_open(now):
        open_test = f"    • Session entries open (≥ {open_label} BST):  ✗ wait"
    else:
        open_test = f"    • Session entries open (≥ {open_label} BST):  ✓"
    if clock.is_past_entries_cutoff(now):
        cutoff_test = (
            f"    • Session before cutoff (< {cutoff_label} BST):  ✗ past cutoff"
        )
    else:
        cutoff_test = f"    • Session before cutoff (< {cutoff_label} BST):  ✓"
    return open_test, cutoff_test


def _check_bgu(
    plan: CandidatePlan,
    runtime: CandidateRuntimeState,
    last: float | None,
    now: datetime,
    bgu_window_min: int = 15,
) -> str:
    """One-line summary of Rule 9A BGU state for this candidate."""
    if not runtime.gap_up_detected:
        return "    • Rule 9A BGU window:  – not engaged (no gap-up today)"
    if runtime.session_open_ts_utc is None:
        return "    • Rule 9A BGU window:  – engaged (no session-open timestamp)"
    elapsed = (now - runtime.session_open_ts_utc).total_seconds() / 60.0
    or_high = runtime.opening_range_high
    if elapsed < bgu_window_min:
        return (
            f"    • Rule 9A BGU window:  ⏱ {elapsed:.0f}/{bgu_window_min} min — OR forming "
            f"(current OR high: {or_high:.2f})"
        )
    # Window closed — gate on OR-high break.
    if last is None or or_high is None:
        return f"    • Rule 9A BGU break:  – cannot evaluate (no last/OR high)"
    if last > or_high:
        return f"    • Rule 9A BGU break:  ✓ last {last:.2f} > OR high {or_high:.2f}"
    return (
        f"    • Rule 9A BGU break:  ✗ wait — last {last:.2f} ≤ OR high {or_high:.2f} "
        f"(need +{or_high - last + 0.01:.2f})"
    )


def _check_r22(
    plan: CandidatePlan,
    runtime: CandidateRuntimeState,
    last: float | None,
) -> str | None:
    """Rule 22 only applies on non-gap days — return None if BGU engaged."""
    if runtime.gap_up_detected:
        return None
    if last is None:
        return "    • Rule 22 strict breakout:  – cannot evaluate (no last)"
    if plan.direction == Direction.LONG:
        bound = plan.trigger_high
        if last > bound:
            return f"    • Rule 22 breakout (> {bound:.2f}):  ✓ broken"
        if last >= plan.trigger_low:
            return (
                f"    • Rule 22 breakout (> {bound:.2f}):  ✗ inside zone "
                f"(need +{bound - last + 0.01:.2f})"
            )
        return (
            f"    • Rule 22 breakout (> {bound:.2f}):  · awaiting zone entry "
            f"(last {last:.2f} below trigger_low {plan.trigger_low:.2f})"
        )
    # SHORT
    bound = plan.trigger_low
    if last < bound:
        return f"    • Rule 22 breakdown (< {bound:.2f}):  ✓ broken"
    if last <= plan.trigger_high:
        return (
            f"    • Rule 22 breakdown (< {bound:.2f}):  ✗ inside zone "
            f"(need -{last - bound + 0.01:.2f})"
        )
    return (
        f"    • Rule 22 breakdown (< {bound:.2f}):  · awaiting zone entry "
        f"(last {last:.2f} above trigger_high {plan.trigger_high:.2f})"
    )


def _check_market_state(snapshot: dict | None) -> tuple[str, str]:
    """Return (tradeable_test, price_test) for the market-state gates."""
    if not snapshot:
        return (
            "    • Market tradeable:        ✗ no snapshot",
            "    • Last price available:    ✗ no snapshot",
        )
    status = (snapshot.get("market_status") or "").upper()
    tradeable_test = (
        f"    • Market tradeable:        ✓ ({status or 'status not set, treated as TRADEABLE'})"
        if (not status) or status == "TRADEABLE"
        else f"    • Market tradeable:        ✗ status={status}"
    )
    last = snapshot.get("last_traded")
    price_test = (
        f"    • Last price available:    ✓ ({last:.2f})"
        if last is not None
        else "    • Last price available:    ✗ last_traded is null"
    )
    return tradeable_test, price_test


def _check_arm_band(
    plan: CandidatePlan,
    last: float | None,
    arm_band_pct: float = 0.005,
) -> str:
    """Within arm_band_pct of the trigger threshold but not yet fired."""
    if last is None:
        return "    • Arm band (within 0.5%):  – cannot evaluate (no last)"
    if plan.direction == Direction.LONG:
        threshold = plan.trigger_low
        distance = last - threshold  # negative when below
    else:
        threshold = plan.trigger_high
        distance = threshold - last
    band = threshold * arm_band_pct
    if distance >= 0:
        return f"    • Arm band (within 0.5%):  – inside zone (arm bypassed)"
    if -distance <= band:
        return (
            f"    • Arm band (within 0.5%):  ✓ in band "
            f"(distance {distance:+.2f}, band ±{band:.2f})"
        )
    return (
        f"    • Arm band (within 0.5%):  – outside band "
        f"(distance {distance:+.2f}, band ±{band:.2f})"
    )


def _check_chase(plan: CandidatePlan, runtime: CandidateRuntimeState) -> str:
    """Rule 4-Chase: open > pivot + 3% (LONG) → SKIP. Mirror for SHORT."""
    open_price = runtime.session_open_price
    if open_price is None:
        return "    • Rule 4-Chase (open ≤ pivot+3%):    – session open not yet observed"
    if plan.direction == Direction.LONG:
        pivot = plan.trigger_low
        cap = pivot * 1.03
        if open_price <= cap:
            return f"    • Rule 4-Chase (open ≤ pivot+3%):    ✓ open {open_price:.2f} ≤ {cap:.2f}"
        return (
            f"    • Rule 4-Chase (open ≤ pivot+3%):    ✗ SKIP — open {open_price:.2f} > "
            f"pivot+3% {cap:.2f} (do not chase)"
        )
    # SHORT: open < pivot - 3% → SKIP
    pivot = plan.trigger_high
    floor = pivot * 0.97
    if open_price >= floor:
        return f"    • Rule 4-Chase (open ≥ pivot-3%):    ✓ open {open_price:.2f} ≥ {floor:.2f}"
    return (
        f"    • Rule 4-Chase (open ≥ pivot-3%):    ✗ SKIP — open {open_price:.2f} < "
        f"pivot-3% {floor:.2f} (do not chase)"
    )


def _check_volume_confirm(snapshot: dict | None) -> str:
    """Rule 5: volume ≥ 1.4× 50d on entry candle. Not implemented — needs intraday vol."""
    return (
        "    • Rule 5 Volume confirm 1.4×:        ⚙ NOT IMPLEMENTED "
        "(needs intraday volume + 50-day avg per symbol)"
    )


def _check_ema_pullback(plan: CandidatePlan) -> str:
    """Rule 6: L-B / S-B EMA-pullback entry — distinct logic from Rule 22 breakout."""
    setup = plan.setup_type
    if setup not in (EntryType.L_B, EntryType.S_B):
        return f"    • Rule 6 EMA Pullback (L-B/S-B):     – N/A (setup={setup.value})"
    return (
        f"    • Rule 6 EMA Pullback (L-B/S-B):     ⚙ NOT IMPLEMENTED "
        f"(setup is {setup.value} but classify_tick uses zone-breakout logic)"
    )


def _check_bgu_vwap(runtime: CandidateRuntimeState) -> str:
    """Rule 9B: BGU VWAP-pullback fallback — not yet wired into classify_tick."""
    if not runtime.gap_up_detected:
        return "    • Rule 9B BGU VWAP fallback:         – N/A (no gap-up today)"
    return (
        "    • Rule 9B BGU VWAP fallback:         ⚙ NOT IMPLEMENTED "
        "(vwap() helper exists in src/engine/rules.py but unused)"
    )


def _check_late_stage_gap(plan: CandidatePlan, runtime: CandidateRuntimeState) -> str:
    """Rule 9-Late: late-stage BGU = exhaustion → DO NOT BUY."""
    if plan.direction != Direction.LONG:
        return "    • Rule 9-Late late-stage gap:        – N/A (SHORT candidate)"
    if not runtime.gap_up_detected:
        return "    • Rule 9-Late late-stage gap:        – N/A (no gap-up today)"
    return (
        "    • Rule 9-Late late-stage gap:        ⚙ NOT IMPLEMENTED "
        "(needs base-stage classification from scan)"
    )


def _check_sgd(plan: CandidatePlan, runtime: CandidateRuntimeState) -> str:
    """Rule 10: SHORT mirror of Rule 9 — Shortable Gap Down."""
    if plan.direction != Direction.SHORT:
        return "    • Rule 10 SGD (SHORT mirror):        – N/A (LONG candidate)"
    return (
        "    • Rule 10 SGD (SHORT mirror):        ⚙ NOT IMPLEMENTED "
        "(Rule 9A's mirror not yet built — Task tomorrow)"
    )


def _check_uk_spread(plan: CandidatePlan, snapshot: dict | None) -> str:
    """Rule X3: UK spread > 0.3% reduce 25%, > 0.5% SKIP."""
    if plan.market != Market.UK:
        return "    • Rule X3 UK spread filter:          – N/A (US ticker)"
    if not snapshot or snapshot.get("bid") is None or snapshot.get("ask") is None:
        return "    • Rule X3 UK spread filter:          – cannot evaluate (no bid/ask)"
    bid = snapshot["bid"]
    ask = snapshot["ask"]
    if bid <= 0:
        return "    • Rule X3 UK spread filter:          – cannot evaluate (bid ≤ 0)"
    spread_pct = (ask - bid) / bid * 100
    if spread_pct > 0.5:
        return f"    • Rule X3 UK spread filter:          ✗ SKIP — spread {spread_pct:.2f}% > 0.5%"
    if spread_pct > 0.3:
        return f"    • Rule X3 UK spread filter:          ⚠ reduce stake 25% — spread {spread_pct:.2f}% > 0.3%"
    return f"    • Rule X3 UK spread filter:          ✓ spread {spread_pct:.2f}% ≤ 0.3%"


def _check_earnings_blackout() -> str:
    """Rule X4: earnings auto-exit. swing-committee event filter is OFF by default."""
    return (
        "    • Rule X4 Earnings blackout:         ⏳ off by default "
        "(EVENT_FILTER_ENABLED=0, see feedback_event_filter_design)"
    )


def _check_sector_correlation() -> str:
    """Rule X5: ≥3 same-sector positions → reduce 75%."""
    return (
        "    • Rule X5 Sector correlation cap:    ⚙ NOT IMPLEMENTED "
        "(needs portfolio + sector mapping)"
    )


def _check_position_caps() -> tuple[str, str]:
    """Rule X1 (budget) + Rule X2 (position cap) — need portfolio context."""
    return (
        "    • Rule X1 Budget capacity:           ⚙ NOT IMPLEMENTED (needs portfolio)",
        "    • Rule X2 Position size cap:         ⚙ NOT IMPLEMENTED (needs portfolio)",
    )


def format_block(
    plan: CandidatePlan,
    last: float | None,
    snapshot: dict | None,
    outcome,
    runtime: CandidateRuntimeState,
    clock: SessionClock,
    now: datetime,
) -> str:
    """Pretty-print one candidate's full Layer-3 + Layer-4 rule status,
    grouped into named sections matching CANONICAL_ENTRY_RULES.md."""
    direction = plan.direction.value
    target_str = (
        f"target {plan.target_price:.2f}"
        if plan.target_price is not None
        else "no target"
    )
    header = (
        f"{plan.symbol}  {direction}  "
        f"trigger {plan.trigger_low:.2f}–{plan.trigger_high:.2f}  "
        f"stop {plan.stop_price:.2f}  {target_str}"
    )
    last_str = f"  Last: {last:.2f}" if last is not None else "  Last: — (no price)"

    # Layer 3 — Entry-time gates
    tradeable_test, price_test = _check_market_state(snapshot)
    open_test, cutoff_test = _check_session(clock, now)
    chase_test = _check_chase(plan, runtime)
    bgu_test = _check_bgu(plan, runtime, last, now)
    bgu_vwap_test = _check_bgu_vwap(runtime)
    late_test = _check_late_stage_gap(plan, runtime)
    sgd_test = _check_sgd(plan, runtime)
    r22_test = _check_r22(plan, runtime, last)
    if r22_test is None:
        r22_test = "    • Rule 22 strict breakout:           – N/A (BGU engaged, see Rule 9A)"
    ema_test = _check_ema_pullback(plan)
    vol_test = _check_volume_confirm(snapshot)
    arm_test = _check_arm_band(plan, last)

    # Layer 4 — Pre-fire executor checks
    budget_test, pos_cap_test = _check_position_caps()
    spread_test = _check_uk_spread(plan, snapshot)
    earnings_test = _check_earnings_blackout()
    sector_test = _check_sector_correlation()

    layer3 = [
        "  Layer 3 — Entry-time gates (every tick):",
        "    Pre-trigger gates (must pass before entry can fire):",
        tradeable_test,
        price_test,
        open_test,
        cutoff_test,
        chase_test,
        "    Setup-specific entry triggers:",
        bgu_test,
        bgu_vwap_test,
        late_test,
        r22_test,
        ema_test,
        sgd_test,
        "    Heads-up:",
        vol_test,
        arm_test,
    ]

    layer4 = [
        "  Layer 4 — Pre-fire executor checks (visibility only — not yet wired):",
        budget_test,
        pos_cap_test,
        spread_test,
        earnings_test,
        sector_test,
    ]

    # Verdict + action
    if outcome.decision == Decision.FIRE:
        verb = "BUY" if plan.direction == Direction.LONG else "SELL"
        verdict = "  Verdict: 🔥 FIRE"
        action = (
            f"  Action:  {verb} {plan.symbol} @ {last:.2f} "
            f"(stop {plan.stop_price:.2f}, {target_str})"
        )
        verdict_block = [verdict, action]
    elif outcome.decision == Decision.REJECT:
        code = outcome.rejection_code or "?"
        verdict_block = [f"  Verdict: REJECT ({code})"]
    elif outcome.decision == Decision.NO_PRICE:
        verdict_block = ["  Verdict: NO PRICE — market not tradeable / no quote"]
    elif outcome.decision == Decision.ARM:
        verdict_block = ["  Verdict: ARMED — near trigger, watch closely"]
    elif outcome.decision == Decision.HOLD:
        verdict_block = ["  Verdict: HOLD — far from trigger"]
    else:
        verdict_block = [f"  Verdict: {outcome.decision}"]

    return "\n".join([header, last_str, *layer3, *layer4, *verdict_block])


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

    # ── IG auth + market data ──────────────────────────────────────
    print("Connecting to IG...")
    session = IGSession()
    session.connect()
    market_data = MarketData(session)
    print(f"  Connected ({session._settings.ig_acc_type.value} account).")

    # Resolve epic per symbol once at startup.
    print("Resolving IG epics:")
    epics: dict[str, str] = {}
    for plan in plans:
        market_label = plan.market.value if hasattr(plan.market, "value") else str(plan.market)
        try:
            epic = market_data.resolve_epic(plan.symbol, market=market_label)
            epics[plan.candidate_id] = epic
            print(f"  {plan.symbol:6} {market_label:3} → {epic}")
        except Exception as exc:
            print(f"  {plan.symbol:6} {market_label:3} → FAILED ({exc})")
            epics[plan.candidate_id] = None

    today = date.today()
    # All current shortlists are US-session; UK switch is a one-line change.
    clock = SessionClock.for_us_session(today)

    print(
        f"\nMonitoring {len(plans)} candidate"
        f"{'' if len(plans) == 1 else 's'}:"
    )
    entries_by_id = {e["candidate_id"]: e for e in entries}
    for p in plans:
        target_str = (
            f"target {p.target_price}"
            if p.target_price is not None
            else "no target"
        )
        print(
            f"\n  {p.symbol}  {p.direction.value}  "
            f"trigger {p.trigger_low}–{p.trigger_high}  "
            f"stop {p.stop_price}  {target_str}"
        )
        scan_entry = entries_by_id.get(p.candidate_id, {})
        print(format_preflight(scan_entry, p))

    print(
        f"\nSession entries open at {clock.entries_open_utc.isoformat()}"
        f"\nSession hard close   at {clock.hard_close_utc.isoformat()}"
        f"\n\nPolling IG every {POLL_SECONDS}s. Ctrl+C to stop."
        f"\nThis tool NEVER places orders. You hit buy/sell yourself when FIRE shows."
        f"\nRules in force: Rule 9A BGU + Rule 22 strict breakout + session-clock gates."
        f"\n"
    )

    try:
        while True:
            now = utc_now()
            now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
            ts_utc = now_utc.strftime("%H:%M:%S UTC")
            ts_bst = now_utc.astimezone(LONDON_TZ).strftime("%H:%M BST")
            ts_et = now_utc.astimezone(NY_TZ).strftime("%H:%M ET")
            print(f"\n=== {ts_utc}  ({ts_bst}  /  {ts_et}) ===\n")
            for plan in plans:
                epic = epics.get(plan.candidate_id)
                if not epic:
                    print(
                        f"{plan.symbol}  {plan.direction.value}  "
                        f"trigger {plan.trigger_low:.2f}–{plan.trigger_high:.2f}\n"
                        f"  Verdict: NO EPIC — resolution failed at startup\n"
                    )
                    continue
                try:
                    snapshot = market_data.get_market_snapshot(epic)
                except Exception as exc:
                    print(
                        f"{plan.symbol}  {plan.direction.value}  "
                        f"trigger {plan.trigger_low:.2f}–{plan.trigger_high:.2f}\n"
                        f"  Verdict: FETCH ERR — {type(exc).__name__}: {exc}\n"
                    )
                    continue
                state = states[plan.candidate_id]
                outcome = classify_tick(
                    plan,
                    snapshot,
                    state,
                    session_clock=clock,
                    now=now,
                )
                last = snapshot.get("last_traded") if snapshot else None
                print(format_block(plan, last, snapshot, outcome, state, clock, now))
                print()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
