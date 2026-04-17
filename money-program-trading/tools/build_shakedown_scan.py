"""
Build a shakedown scan handoff file (``data/scans/scan_YYYYMMDD.json``).

This is a one-off fixture for session 8 item #1 — the live DEMO shakedown.
It's NOT a replacement for swing-committee's scanner. swing-committee produces
a full A+/A/B shortlist with pillar votes and MCL regime; this tool produces a
minimal fixture with one or two hand-chosen broad-market candidates so the
execution pipeline can be exercised end-to-end.

Usage
-----
Sample mode (offline — produces a scan with synthetic prices for dry-run):

    python -m tools.build_shakedown_scan --sample

Live mode (queries IG for current prices and builds trigger zones off them):

    python -m tools.build_shakedown_scan \\
        --broker-mode DEMO \\
        --candidate SPTRD:US:LONG:0.50 \\
        --candidate NASDAQ:US:LONG:0.50

Each ``--candidate`` is ``SYMBOL:MARKET:DIRECTION:STAKE_GBP_PER_PT``.
Setup_type defaults to L-A for LONG and S-A for SHORT. Grade is B.

Output goes to ``data/scans/scan_YYYYMMDD.json`` (overwrites).

Trigger / stop placement (LONG):
    spot → trigger_low = spot + trigger_offset_pts
    trigger_high = trigger_low + 1
    stop = trigger_low - stop_offset_pts

For a shakedown we want a tight trigger (2pts above spot for SPTRD, ~5 for
NASDAQ) so the fill fires within minutes of market open, not hours.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.common import Direction, EntryType, Market  # noqa: E402
from src.models.log_enums import BrokerMode, CandidateGrade, RegimeState  # noqa: E402
from src.models.scan_record import (  # noqa: E402
    RegimeSnapshot,
    ScanRecord,
    UniverseScoreEntry,
)
from src.models.shortlist_entry import (  # noqa: E402
    PillarVotes,
    ShortlistEntry,
)

logger = logging.getLogger(__name__)

# Default per-epic trigger / stop offsets (points). Tuned so the trigger fires
# within the first 30 min of typical intraday range.
_DEFAULTS: dict[str, dict[str, float]] = {
    "SPTRD": {"trigger_offset": 2.0, "stop_offset": 5.0},
    "NASDAQ": {"trigger_offset": 5.0, "stop_offset": 15.0},
    "FTSE": {"trigger_offset": 2.0, "stop_offset": 5.0},
}

_DEFAULT_SETUP_LONG = EntryType.L_A
_DEFAULT_SETUP_SHORT = EntryType.S_A


def _parse_candidate(s: str) -> tuple[str, str, str, float]:
    """Parse SYMBOL:MARKET:DIRECTION:STAKE into a tuple."""
    parts = s.split(":")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"--candidate must be SYMBOL:MARKET:DIRECTION:STAKE (got {s!r})"
        )
    sym, market, direction, stake = parts
    if market not in ("US", "UK"):
        raise argparse.ArgumentTypeError(f"market must be US or UK (got {market!r})")
    if direction not in ("LONG", "SHORT"):
        raise argparse.ArgumentTypeError(f"direction must be LONG or SHORT (got {direction!r})")
    try:
        stake_f = float(stake)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"stake must be a number (got {stake!r})") from e
    if stake_f <= 0:
        raise argparse.ArgumentTypeError(f"stake must be > 0 (got {stake!r})")
    return sym, market, direction, stake_f


def _build_shortlist_entry(
    *,
    scan_id: str,
    symbol: str,
    market: str,
    direction: str,
    stake_gbp_per_pt: float,
    spot_price: float,
    broker_mode: BrokerMode,
    account_size_gbp: float,
    rule_set_version: str,
) -> ShortlistEntry:
    """Build one ShortlistEntry given the symbol + live spot price."""
    defaults = _DEFAULTS.get(symbol, {"trigger_offset": 2.0, "stop_offset": 5.0})
    trig_off = defaults["trigger_offset"]
    stop_off = defaults["stop_offset"]

    if direction == "LONG":
        trigger_low = round(spot_price + trig_off, 2)
        trigger_high = round(trigger_low + 1.0, 2)
        stop_price = round(trigger_low - stop_off, 2)
        setup_type = _DEFAULT_SETUP_LONG
    else:
        trigger_high = round(spot_price - trig_off, 2)
        trigger_low = round(trigger_high - 1.0, 2)
        stop_price = round(trigger_high + stop_off, 2)
        setup_type = _DEFAULT_SETUP_SHORT

    # Risk per pt = distance between trigger entry and stop.
    if direction == "LONG":
        risk_pts = trigger_low - stop_price
    else:
        risk_pts = stop_price - trigger_high
    planned_risk_gbp = round(abs(risk_pts) * stake_gbp_per_pt, 2)
    planned_risk_pct = round(planned_risk_gbp / max(account_size_gbp, 1e-9), 6)

    return ShortlistEntry(
        candidate_id=str(uuid4()),
        scan_id=scan_id,
        session_id=None,  # filled by ingester
        symbol=symbol,
        market=Market(market),
        direction=Direction(direction),
        setup_type=setup_type,
        grade=CandidateGrade.B,
        trigger_low=trigger_low,
        trigger_high=trigger_high,
        stop_price=stop_price,
        target_price=None,
        planned_stake_gbp_per_pt=stake_gbp_per_pt,
        planned_risk_gbp=planned_risk_gbp,
        planned_risk_pct_account=planned_risk_pct,
        pillar_votes=PillarVotes(),  # no pillar votes for a shakedown fixture
        committee_stance="shakedown",
        day1_score=None,
        day1_tier=None,
        broker_mode=broker_mode,
        created_at_utc=datetime.utcnow(),
        rule_set_version=rule_set_version,
        notes=f"session-8 shakedown: spot={spot_price}, risk_pts={risk_pts}",
    )


def _build_universe_entry(symbol: str, market: str, spot: float) -> UniverseScoreEntry:
    """One UniverseScoreEntry per shortlisted symbol — kept minimal."""
    return UniverseScoreEntry(
        symbol=symbol,
        market=market,
        price=spot,
        currency="GBP",
        pillar_pass_count=0,
        pillar_bitmap=0,
        grade="B",
        shortlisted=True,
    )


def _fetch_spot_price(symbol: str, market: str) -> float:
    """Fetch live mid price for a symbol via IG. Requires creds in .env."""
    from src.auth.ig_auth import IGSession
    from src.config.settings import get_settings
    from src.data.market_data import MarketData

    settings = get_settings()
    session = IGSession(settings)
    session.connect()
    try:
        md = MarketData(session)
        epic = md.resolve_epic(symbol, market)
        if not epic:
            raise RuntimeError(f"Could not resolve epic for {symbol} ({market})")
        snap = md.get_market_snapshot(epic)
        bid = snap.get("bid")
        ask = snap.get("ask")
        if bid is None or ask is None:
            raise RuntimeError(
                f"IG returned no bid/ask for {symbol} ({epic}). Snapshot: {snap}"
            )
        mid = (bid + ask) / 2.0
        logger.info(
            "Resolved %s → %s; bid=%.2f ask=%.2f mid=%.2f status=%s",
            symbol,
            epic,
            bid,
            ask,
            mid,
            snap.get("market_status"),
        )
        return float(mid)
    finally:
        session.disconnect()


def build_scan(
    *,
    candidates: list[tuple[str, str, str, float]],
    broker_mode: BrokerMode,
    account_size_gbp: float,
    rule_set_version: str,
    spot_override: dict[str, float] | None = None,
) -> tuple[ScanRecord, list[ShortlistEntry]]:
    """Build (ScanRecord, list[ShortlistEntry]) for the given candidates.

    ``spot_override`` — if set, use these prices instead of querying IG. Keyed
    by symbol. Use for the --sample mode.
    """
    scan_id = str(uuid4())

    shortlist: list[ShortlistEntry] = []
    universe: list[UniverseScoreEntry] = []

    for symbol, market, direction, stake in candidates:
        if spot_override is not None and symbol in spot_override:
            spot = spot_override[symbol]
        else:
            spot = _fetch_spot_price(symbol, market)

        shortlist.append(
            _build_shortlist_entry(
                scan_id=scan_id,
                symbol=symbol,
                market=market,
                direction=direction,
                stake_gbp_per_pt=stake,
                spot_price=spot,
                broker_mode=broker_mode,
                account_size_gbp=account_size_gbp,
                rule_set_version=rule_set_version,
            )
        )
        universe.append(_build_universe_entry(symbol, market, spot))

    scan = ScanRecord(
        scan_id=scan_id,
        session_id=None,
        scanned_at_utc=datetime.utcnow(),
        universe_size=len(universe),
        broker_mode=broker_mode,
        regime=RegimeSnapshot(
            regime=RegimeState.GREEN,
            regime_score=None,
            notes="shakedown fixture — no MCL run",
        ),
        scored_universe=universe,
        scanner_version="shakedown",
        rule_set_version=rule_set_version,
    )
    return scan, shortlist


def _write_handoff(
    scan: ScanRecord,
    shortlist: list[ShortlistEntry],
    path: Path,
) -> None:
    """Write the scan handoff JSON in the shape session_init.ingest_scan expects."""
    payload = {
        "schema_version": 1,
        "scan_record": scan.model_dump(mode="json"),
        "shortlist_entries": [e.model_dump(mode="json") for e in shortlist],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote scan handoff: %s", path)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_shakedown_scan",
        description="Build a session-8 shakedown scan handoff file.",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        type=_parse_candidate,
        default=None,
        help=(
            "Repeatable. SYMBOL:MARKET:DIRECTION:STAKE — e.g. SPTRD:US:LONG:0.50. "
            "Default (sample mode): two LONGs — SPTRD and NASDAQ at £0.50/pt."
        ),
    )
    parser.add_argument(
        "--broker-mode",
        choices=["DEMO", "LIVE"],
        default="DEMO",
        help="Broker mode to stamp on the scan (must agree with the session).",
    )
    parser.add_argument(
        "--account-size",
        type=float,
        default=10000.0,
        help="Account size in GBP, used to derive planned_risk_pct_account.",
    )
    parser.add_argument(
        "--rule-set-version",
        type=str,
        default="",
        help="Short git SHA of entry-rules at scan time (optional).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override output path. Default: data/scans/scan_YYYYMMDD.json.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help=(
            "Offline mode: skip IG and use synthetic prices (SPTRD=5800, "
            "NASDAQ=20000, FTSE=8100). Use for dry-run smoke tests."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    candidates = args.candidate or [
        ("SPTRD", "US", "LONG", 0.50),
        ("NASDAQ", "US", "LONG", 0.50),
    ]

    spot_override: dict[str, float] | None = None
    if args.sample:
        spot_override = {
            "SPTRD": 5800.0,
            "NASDAQ": 20000.0,
            "FTSE": 8100.0,
        }
        logger.info("Sample mode — using synthetic prices: %s", spot_override)

    scan, shortlist = build_scan(
        candidates=candidates,
        broker_mode=BrokerMode(args.broker_mode),
        account_size_gbp=args.account_size,
        rule_set_version=args.rule_set_version,
        spot_override=spot_override,
    )

    if args.output is not None:
        out_path = args.output
    else:
        stamp = date.today().strftime("%Y%m%d")
        out_path = Path("data/scans") / f"scan_{stamp}.json"

    _write_handoff(scan, shortlist, out_path)

    # Brief summary so the operator can sanity-check trigger/stop before launching.
    print(f"\nShakedown scan written: {out_path}")
    print(f"  scan_id        = {scan.scan_id}")
    print(f"  broker_mode    = {scan.broker_mode.value}")
    print(f"  universe_size  = {scan.universe_size}")
    print(f"  shortlisted    = {len(shortlist)}")
    for e in shortlist:
        risk_pts = (
            e.trigger_low - e.stop_price
            if e.direction == Direction.LONG
            else e.stop_price - e.trigger_high
        )
        print(
            f"  - {e.symbol:<8} {e.direction.value:<5} "
            f"trigger=[{e.trigger_low:.2f},{e.trigger_high:.2f}] "
            f"stop={e.stop_price:.2f} risk={risk_pts:.1f}pts "
            f"stake=£{e.planned_stake_gbp_per_pt:.2f}/pt "
            f"risk=£{e.planned_risk_gbp:.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
