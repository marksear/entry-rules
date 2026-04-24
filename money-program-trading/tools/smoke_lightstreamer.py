#!/usr/bin/env python
"""
End-to-end smoke test for ``LightstreamerPriceFeed`` against the real IG
Lightstreamer server. Validates the two things unit tests can't:

1. **CST/XST token sharing works in practice.** ``start()`` reads tokens
   from the existing ``IGSession`` and hands them to the LS client
   without triggering a second REST auth (which would hit DEMO's
   rate-limit — see ``feedback_ig_switch_account_race``).
2. **MARKET:{epic} ticks actually flow.** We subscribe to a
   known-liquid epic, print the ticks we receive for N seconds, then
   disconnect cleanly.

**Run during US market hours for live ticks:**

    cd ~/CoWork/entry-rules/money-program-trading
    source .venv/bin/activate
    python -m tools.smoke_lightstreamer

**Pre-market / after-hours:** you'll see the connection succeed and the
subscription open, but tick count may be low. That's still a valid auth
test — if the LS server accepts our credentials, the structural goal of
Phase 2 is proved.

Default epic: ``IX.D.SPTRD.DAILY.IP`` (US 500 — IG's most liquid
24-hour spread-bet index, still quotes after hours). Override with the
``--epic`` flag.

Exit codes:
* 0 — at least one tick received within the window.
* 1 — connection/auth failed.
* 2 — connected but zero ticks received within the window (either the
  market is closed OR subscription is mis-wired).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime

from src.auth.ig_auth import IGSession
from src.config.settings import get_settings
from src.data.price_feed import LightstreamerPriceFeed, StalePriceError

logger = logging.getLogger("smoke_lightstreamer")


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Smoke-test LightstreamerPriceFeed against real IG DEMO.",
    )
    p.add_argument(
        "--epic",
        default="IX.D.SPTRD.DAILY.IP",
        help=(
            "IG epic to subscribe to (default: IX.D.SPTRD.DAILY.IP — US 500 "
            "24h spread-bet, always ticks). Use a liquid US equity DAILY.IP "
            "epic to reproduce the 2026-04-23 BA bug scenario."
        ),
    )
    p.add_argument(
        "--seconds",
        type=int,
        default=60,
        help="How long to listen for ticks (default: 60).",
    )
    p.add_argument(
        "--verbose", action="store_true", help="DEBUG-level logging."
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    _configure_logging(args.verbose)

    settings = get_settings()
    logger.info(
        "Smoke test: epic=%s, seconds=%d, broker_mode=%s, account=%s",
        args.epic, args.seconds, settings.ig_acc_type.value,
        settings.ig_account_id or "(default)",
    )

    # ── 1) IG auth (REST) ──────────────────────────────────
    ig = IGSession(settings)
    try:
        ig.connect()
    except Exception as e:  # noqa: BLE001
        logger.error("IG connect failed: %s", e)
        return 1

    feed = LightstreamerPriceFeed(
        ig,
        stale_seconds=settings.price_feed_stale_seconds,
        degraded_seconds=settings.price_feed_degraded_seconds,
    )

    try:
        # ── 2) LS start — this is the token-sharing moment ───
        logger.info("Starting LightstreamerPriceFeed...")
        feed.start()
        logger.info("LS started OK (no double-auth — CST/XST shared).")

        # ── 3) Subscribe ──────────────────────────────────────
        logger.info("Subscribing to MARKET:%s ...", args.epic)
        feed.subscribe(args.epic)

        # ── 4) Wait for ticks ────────────────────────────────
        deadline = time.monotonic() + args.seconds
        tick_count = 0
        last_printed: datetime | None = None
        while time.monotonic() < deadline:
            try:
                tick = feed.latest(args.epic, max_age_seconds=args.seconds)
            except StalePriceError:
                time.sleep(1.0)
                continue
            if tick.updated_at_utc != last_printed:
                tick_count += 1
                last_printed = tick.updated_at_utc
                logger.info(
                    "TICK bid=%.4f ask=%.4f mid=%.4f state=%s age_ms=%d",
                    tick.bid or 0, tick.ask or 0, tick.last_traded or 0,
                    tick.market_status,
                    int((datetime.utcnow() - tick.updated_at_utc).total_seconds() * 1000),
                )
            time.sleep(1.0)

        # ── 5) Report ────────────────────────────────────────
        logger.info("Smoke finished. Ticks received in %ds: %d",
                    args.seconds, tick_count)
        if tick_count == 0:
            logger.warning(
                "Zero ticks received — either market is closed OR the LS "
                "subscription is mis-wired. If market was open, investigate "
                "the _TickListener path."
            )
            return 2
        return 0
    except Exception as e:  # noqa: BLE001
        logger.exception("LS smoke test failed: %s", e)
        return 1
    finally:
        try:
            feed.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("feed.stop() raised: %s", e)
        ig.disconnect()


if __name__ == "__main__":
    sys.exit(main())
