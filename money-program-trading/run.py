#!/usr/bin/env python3
"""
Money Program — Trade Runner

Usage:
    python run.py                    # Monitor pending trades (dry run)
    python run.py --live             # Monitor and execute for real
    python run.py check AVGO         # Quick gate check on a ticker
    python run.py check VOD --uk     # Gate check on UK stock
    python run.py status             # Account balance + trade summary

Workflow:
    1. Swing Trader delivers a signal (symbol, entry zone, stop)
    2. You add it to data/trades.json with your stake
    3. Run this script — it monitors price every 60s for 20 min
    4. When price enters the zone → spread bet placed with stop
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from src.config.settings import get_settings
from src.engine.executor import EntryMonitor, PositionMonitor, TradeDaemon


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("trading_ig").setLevel(logging.WARNING)


def ig_login(settings, max_retries: int = 5) -> dict:
    """
    Login to IG with exponential backoff on rate limits.

    Retries up to max_retries times with increasing waits:
      Attempt 1: immediate
      Attempt 2: wait 30s
      Attempt 3: wait 60s
      Attempt 4: wait 120s
      Attempt 5: wait 240s
    """
    import time as _time

    base = settings.ig_base_url
    headers = {
        "X-IG-API-KEY": settings.ig_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json; charset=UTF-8",
        "VERSION": "2",
    }

    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            wait = 30 * (2 ** (attempt - 2))  # 30, 60, 120, 240s
            print(f"  Retry {attempt}/{max_retries} — waiting {wait}s...")
            _time.sleep(wait)

        print(f"Connecting to IG ({settings.ig_acc_type.value})..."
              + (f" (attempt {attempt})" if attempt > 1 else ""))

        try:
            r = requests.post(
                f"{base}/session",
                json={"identifier": settings.ig_username, "password": settings.ig_password},
                headers=headers,
                timeout=15,
            )
        except requests.exceptions.RequestException as e:
            print(f"  Connection error: {e}")
            continue

        if r.status_code == 200:
            break

        err = r.json().get("errorCode", "?")
        if "token" in err.lower() or "security" in err.lower():
            print(f"  Rate-limited: {err}")
            if attempt == max_retries:
                print(f"  Failed after {max_retries} attempts. Try again in a few minutes.")
                sys.exit(1)
            continue
        else:
            print(f"  Login failed: {err}")
            sys.exit(1)

    headers["CST"] = r.headers["CST"]
    headers["X-SECURITY-TOKEN"] = r.headers["X-SECURITY-TOKEN"]

    # Switch to spread bet account
    if settings.ig_account_id:
        headers["VERSION"] = "1"
        r2 = requests.put(
            f"{base}/session",
            json={"accountId": settings.ig_account_id, "defaultAccount": False},
            headers=headers,
        )
        if r2.status_code == 200:
            if "CST" in r2.headers:
                headers["CST"] = r2.headers["CST"]
            if "X-SECURITY-TOKEN" in r2.headers:
                headers["X-SECURITY-TOKEN"] = r2.headers["X-SECURITY-TOKEN"]
            print(f"Connected — account {settings.ig_account_id} (Spread Bet)")
        else:
            print(f"Warning: account switch failed ({r2.status_code})")

    return headers


def cmd_enter(args):
    """Monitor pending trades and execute when entry zone is hit."""
    settings = get_settings()
    setup_logging(settings.log_level)

    ig_headers = ig_login(settings)

    # --now flag: bypass time window for testing
    w_start = args.window_start if hasattr(args, 'window_start') and args.window_start else None
    w_end = args.window_end if hasattr(args, 'window_end') and args.window_end else None
    max_checks = 999  # Default: unlimited (window controls duration)

    if hasattr(args, 'now') and args.now:
        # Testing mode: window = now to now+30min, max 30 checks
        from datetime import datetime, timedelta
        try:
            from zoneinfo import ZoneInfo
            now_est = datetime.now(ZoneInfo("America/New_York"))
        except ImportError:
            from datetime import timezone
            now_est = datetime.now(timezone(timedelta(hours=-5)))
        w_start = now_est.strftime("%H:%M")
        w_end = (now_est + timedelta(minutes=30)).strftime("%H:%M")
        max_checks = 30
        print(f"  --now mode: window set to {w_start}–{w_end} EST")

    monitor = EntryMonitor(
        ig_headers=ig_headers,
        settings=settings,
        check_interval=args.interval,
        max_checks=max_checks,
        window_start=w_start,
        window_end=w_end,
        dry_run=not args.live,
    )
    filled_trades = monitor.run()

    # Seamless handoff: if any trades filled, auto-start position monitor
    filled = [t for t in filled_trades if t.status in ("FILLED", "FILLED_DRY")]
    if filled:
        pos_monitor = PositionMonitor(
            ig_headers=ig_headers,
            settings=settings,
            check_interval=args.interval,
            dry_run=not args.live,
        )
        pos_monitor.run()

    # Logout
    try:
        ig_headers["VERSION"] = "1"
        requests.delete(f"{settings.ig_base_url}/session", headers=ig_headers)
    except Exception:
        pass


def cmd_run(args):
    """
    Unified daemon — stalks entries AND manages open positions in a single loop.

    Handles up to --max-positions (default 6) concurrent fills. Runs until Ctrl+C.
    """
    settings = get_settings()
    setup_logging(settings.log_level)

    ig_headers = ig_login(settings)

    daemon = TradeDaemon(
        ig_headers=ig_headers,
        settings=settings,
        check_interval=args.interval,
        window_start=args.window_start,
        window_end=args.window_end,
        dry_run=not args.live,
        max_positions=args.max_positions,
    )
    daemon.run()

    try:
        ig_headers["VERSION"] = "1"
        requests.delete(f"{settings.ig_base_url}/session", headers=ig_headers)
    except Exception:
        pass


def cmd_monitor(args):
    """Monitor existing filled positions (stop management, P&L, emergency cover)."""
    settings = get_settings()
    setup_logging(settings.log_level)

    ig_headers = ig_login(settings)

    monitor = PositionMonitor(
        ig_headers=ig_headers,
        settings=settings,
        check_interval=args.interval,
        dry_run=not args.live,
    )
    monitor.run()

    try:
        ig_headers["VERSION"] = "1"
        requests.delete(f"{settings.ig_base_url}/session", headers=ig_headers)
    except Exception:
        pass


def cmd_check(args):
    """Quick gate check on a single ticker."""
    settings = get_settings()
    setup_logging(settings.log_level)

    ig_headers = ig_login(settings)

    ticker = args.ticker.upper()
    direction = args.direction.upper() if args.direction else "LONG"
    market = "UK" if args.uk else "US"
    base = settings.ig_base_url

    print(f"\nChecking {ticker} ({direction}, {market})...\n")

    # Search for the epic
    ig_headers["VERSION"] = "1"
    r = requests.get(f"{base}/markets?searchTerm={ticker}", headers=ig_headers)
    epic = ""
    if r.status_code == 200:
        for m in r.json().get("markets", []):
            if "CASH" in m.get("epic", "") and ticker in m.get("instrumentName", "").upper():
                epic = m["epic"]
                break
        if not epic and r.json().get("markets"):
            epic = r.json()["markets"][0].get("epic", "")

    if not epic:
        print(f"Could not find {ticker} on IG.")
        return

    print(f"Epic: {epic}")

    # Fetch current price
    ig_headers["VERSION"] = "3"
    r2 = requests.get(f"{base}/markets/{epic}", headers=ig_headers)
    if r2.status_code == 200:
        snap = r2.json().get("snapshot", {})
        print(f"Bid: {snap.get('bid')}  Ask: {snap.get('offer')}  "
              f"Status: {snap.get('marketStatus')}")

    # Fetch daily bars and run gates
    r3 = requests.get(f"{base}/prices/{epic}?resolution=DAY&max=260&pageSize=0", headers=ig_headers)
    if r3.status_code == 200:
        bars_raw = r3.json().get("prices", [])
        print(f"Bars: {len(bars_raw)}")

        if len(bars_raw) >= 50:
            import pandas as pd
            from src.engine.gates import evaluate_long_gates, evaluate_short_gates

            # Convert to DataFrame
            df = pd.DataFrame([{
                "close": b.get("closePrice", {}).get("bid", 0) or 0,
                "high": b.get("highPrice", {}).get("bid", 0) or 0,
                "low": b.get("lowPrice", {}).get("bid", 0) or 0,
                "open": b.get("openPrice", {}).get("bid", 0) or 0,
                "volume": b.get("lastTradedVolume", 0) or 0,
            } for b in bars_raw])

            print(f"Last close: {df['close'].iloc[-1]:.1f}")
            print()

            rs_pct = 50.0
            if direction == "LONG":
                result = evaluate_long_gates(df, rs_pct, settings)
            else:
                result = evaluate_short_gates(df, rs_pct, "S-A", settings=settings)

            print(f"{'Gate':<45s} {'Pass':>6s} {'Value':>10s} {'Need':>10s}")
            print("─" * 75)
            for g in result.gates:
                icon = "  ✓" if g.passed else "  ✗"
                val = f"{g.value}" if g.value is not None else "—"
                thr = f"{g.threshold}" if g.threshold is not None else "—"
                print(f"{icon} {g.name:<42s} {val:>10s} {thr:>10s}")

            print("─" * 75)
            if result.passed:
                print(f"RESULT: ALL GATES PASSED (ADX={result.adx_value})")
            else:
                print(f"RESULT: REJECTED — {result.reject_code.value}: "
                      f"{result.reject_code.description}")
    else:
        print(f"Price fetch failed: {r3.status_code}")

    # Logout
    try:
        ig_headers["VERSION"] = "1"
        requests.delete(f"{base}/session", headers=ig_headers)
    except Exception:
        pass


def cmd_status(args):
    """Show account status and trades summary."""
    settings = get_settings()
    setup_logging("WARNING")

    ig_headers = ig_login(settings)
    base = settings.ig_base_url

    # Account balance
    ig_headers["VERSION"] = "1"
    r = requests.get(f"{base}/accounts", headers=ig_headers)
    if r.status_code == 200:
        for acc in r.json().get("accounts", []):
            if acc.get("accountId") == settings.ig_account_id:
                bal = acc.get("balance", {})
                print(f"\n{'═' * 50}")
                print(f"  Account: {acc['accountId']} ({acc.get('accountName', '?')})")
                print(f"  Balance:   £{bal.get('balance', 0):>10,.2f}")
                print(f"  Available: £{bal.get('available', 0):>10,.2f}")
                print(f"  P&L:       £{bal.get('profitLoss', 0):>10,.2f}")
                print(f"{'═' * 50}")

    # Open positions
    ig_headers["VERSION"] = "2"
    r2 = requests.get(f"{base}/positions", headers=ig_headers)
    if r2.status_code == 200:
        positions = r2.json().get("positions", [])
        print(f"\n  Open positions: {len(positions)}")
        for p in positions:
            mkt = p.get("market", {})
            pos = p.get("position", {})
            print(f"    {mkt.get('instrumentName', '?'):30s} "
                  f"{pos.get('direction', '?'):5s} "
                  f"size={pos.get('size', '?')} "
                  f"@ {pos.get('level', '?')}")

    # Trades file summary
    trades_path = Path("data/trades.json")
    if trades_path.exists():
        with open(trades_path) as f:
            trades = json.load(f).get("trades", [])
        by_status = {}
        for t in trades:
            s = t.get("status", "?")
            by_status[s] = by_status.get(s, 0) + 1
        print(f"\n  Trades file: {len(trades)} trade(s)")
        for status, count in sorted(by_status.items()):
            print(f"    {status}: {count}")

    print()

    # Logout
    try:
        ig_headers["VERSION"] = "1"
        requests.delete(f"{base}/session", headers=ig_headers)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Money Program — Entry Refinement Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  python run.py run                 Unified daemon (entry + management in ONE loop, cap 6)
  python run.py run --live          Unified daemon, live orders + live stop management
  python run.py run --max-positions 6   Explicit concurrent-fill cap (default 6)

  python run.py                     [Legacy] Stalk entry zones only (dry run)
  python run.py --live              [Legacy] Stalk and execute for real
  python run.py monitor             [Legacy] Monitor FILLED positions only
  python run.py monitor --live      [Legacy] Monitor with live stop management

  python run.py check AVGO          Gate check on Broadcom
  python run.py check VOD --uk      Gate check on Vodafone (UK)
  python run.py status              Account balance + positions
        """,
    )
    subparsers = parser.add_subparsers(dest="command")

    # Default: entry monitoring
    parser.add_argument("--live", action="store_true",
                        help="Place real orders (default: dry run)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Seconds between checks (default: 60)")
    parser.add_argument("--window-start", type=str, default=None,
                        help="Entry window start in EST, HH:MM (default: 14:45)")
    parser.add_argument("--window-end", type=str, default=None,
                        help="Entry window end in EST, HH:MM (default: 15:15)")
    parser.add_argument("--now", action="store_true",
                        help="Ignore time window — run checks immediately (for testing)")

    # Run subcommand: unified daemon (entry + position management in one loop)
    run_p = subparsers.add_parser(
        "run",
        help="Unified daemon — stalks entries AND manages positions in one loop",
    )
    run_p.add_argument("--live", action="store_true", help="Place real orders & manage stops")
    run_p.add_argument("--interval", type=int, default=60, help="Seconds between ticks (default: 60)")
    run_p.add_argument("--window-start", type=str, default=None,
                       help="Entry window start EST HH:MM (default: from settings)")
    run_p.add_argument("--window-end", type=str, default=None,
                       help="Entry window end EST HH:MM (default: from settings)")
    run_p.add_argument("--max-positions", type=int, default=6,
                       help="Cap on concurrent filled positions (default: 6)")

    # Monitor subcommand: position management
    mon_p = subparsers.add_parser("monitor", help="Monitor filled positions")
    mon_p.add_argument("--live", action="store_true", help="Enable live stop management")
    mon_p.add_argument("--interval", type=int, default=60, help="Check interval in seconds")

    # Check subcommand: gate analysis
    check_p = subparsers.add_parser("check", help="Quick gate check on a ticker")
    check_p.add_argument("ticker", help="Stock ticker (e.g. AVGO, VOD)")
    check_p.add_argument("--direction", "-d", default="LONG", help="LONG or SHORT")
    check_p.add_argument("--uk", action="store_true", help="UK market (default: US)")

    # Status subcommand
    subparsers.add_parser("status", help="Account status and trade summary")

    args = parser.parse_args()

    if args.command == "check":
        cmd_check(args)
    elif args.command == "monitor":
        cmd_monitor(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        cmd_enter(args)


if __name__ == "__main__":
    main()
