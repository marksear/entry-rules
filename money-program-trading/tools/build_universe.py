"""Build / warm the IG-resolved intraday scan universe.

Usage
-----
    python -m tools.build_universe
    python -m tools.build_universe --max-tickers 30       # chunked run
    python -m tools.build_universe --out data/cache/universe.json

The resolver is resumable: epic_map.json is written incrementally, so
subsequent runs only search the tickers that didn't resolve last time.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow `python -m tools.build_universe` from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.auth.ig_auth import IGSession  # noqa: E402
from src.config.settings import get_settings  # noqa: E402
from src.data.market_data import MarketData  # noqa: E402
from src.scanner.universe import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    build_universe,
    summarise_report,
    write_universe,
)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Output JSON path (default: data/cache/universe.json)",
    )
    parser.add_argument(
        "--max-tickers",
        type=int,
        default=None,
        help="Cap number of uncached tickers to search this run (for chunking)",
    )
    parser.add_argument(
        "--rpm",
        type=int,
        default=25,
        help="Requests per minute cap (default 25, IG limit is 30)",
    )
    parser.add_argument(
        "--no-ftse",
        action="store_true",
        help="Skip FTSE 15 block",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore epic_map.json cache and re-search every ticker",
    )
    args = parser.parse_args()

    settings = get_settings()
    session = IGSession(settings)
    session.connect()
    market_data = MarketData(session)

    report = build_universe(
        market_data,
        include_ftse=not args.no_ftse,
        requests_per_minute=args.rpm,
        max_tickers=args.max_tickers,
        skip_resolved=not args.force_refresh,
    )

    out_path = _REPO_ROOT / args.out
    write_universe(report, out_path)

    print()
    print(summarise_report(report))
    print()
    print(f"Full universe: {out_path}")
    print(f"Epic cache:    {_REPO_ROOT / 'data/cache/epic_map.json'}")

    return 0 if report["errored"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
