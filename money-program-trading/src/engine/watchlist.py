"""
Watchlist processor — reads trade ideas from JSON, validates against
the Masterclass rules, and either executes or rejects each one.

You specify:  ticker, direction, entry type, pivot, stop, stake (£/point)
Engine checks: gates, risk limits, spread, gap rules — and tells you why if it says no.

Workflow:
    1. Add trade ideas to data/watchlist.json with status "NEW"
    2. Run the processor
    3. Engine validates each NEW trade against all gates
    4. Approved trades → order placed, status → ACTIVE
    5. Rejected trades → reason logged, status stays NEW with rejection noted
    6. Review the output and adjust
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..auth.ig_auth import IGSession
from ..config.rejection_codes import RejectCode
from ..config.settings import Settings, get_settings
from ..data.market_data import MarketData
from ..engine.gates import evaluate_long_gates, evaluate_short_gates
from ..engine.risk_manager import RiskManager
from ..indicators.volume import avg_volume
from ..logging_mod.audit_log import AuditLog
from ..logging_mod.db import Database
from ..models.audit_entry import (
    AuditEntry, AuditGates, AuditLevels, AuditVolume, AuditTranche,
)
from ..models.common import Decision, Direction, EntryType, Market
from ..utils.spread_bet import (
    calculate_spread_bet_size,
    calculate_spread_bet_risk,
    calculate_overnight_gap_stake,
)

logger = logging.getLogger(__name__)

WATCHLIST_PATH = Path("data/watchlist.json")


class TradeIdea:
    """A single trade idea from the watchlist."""

    def __init__(self, data: dict):
        self.ticker: str = data["ticker"]
        self.market: Market = Market(data["market"])
        self.direction: Direction = Direction(data["direction"])
        self.entry_type: str = data["entry_type"]
        self.pivot: float = float(data["pivot"])
        self.stop: float = float(data["stop"])
        self.stake: float = float(data["stake"])  # £ per point
        self.base_stage: int = int(data.get("base_stage", 1))
        self.notes: str = data.get("notes", "")
        self.status: str = data.get("status", "NEW")
        self.ig_epic: str = data.get("ig_epic", "")
        self.rejection: str = data.get("rejection", "")
        self._raw = data

    @property
    def risk_per_point(self) -> float:
        return abs(self.pivot - self.stop)

    @property
    def risk_gbp(self) -> float:
        return self.stake * self.risk_per_point

    @property
    def reward_at_3r(self) -> float:
        return self.risk_per_point * 3

    def to_dict(self) -> dict:
        d = dict(self._raw)
        d["status"] = self.status
        d["ig_epic"] = self.ig_epic
        d["rejection"] = self.rejection
        return d


class WatchlistProcessor:
    """
    Reads the watchlist, validates each NEW trade, and produces
    a report of what passed and what didn't.
    """

    def __init__(
        self,
        session: IGSession,
        settings: Settings | None = None,
        watchlist_path: Path | None = None,
        dry_run: bool = True,
    ):
        self._session = session
        self._settings = settings or get_settings()
        self._market_data = MarketData(session)
        self._risk_mgr = RiskManager(self._settings)
        self._watchlist_path = watchlist_path or WATCHLIST_PATH
        self._dry_run = dry_run  # True = validate only, don't place orders

    def process(self) -> list[dict]:
        """
        Process all NEW trades in the watchlist.

        Returns a list of results for each trade processed.
        """
        watchlist = self._load_watchlist()
        trades = watchlist.get("trades", [])
        new_trades = [TradeIdea(t) for t in trades if t.get("status") == "NEW"]

        if not new_trades:
            logger.info("No NEW trades in watchlist.")
            return []

        logger.info("Processing %d NEW trade(s)...", len(new_trades))
        results = []

        for idea in new_trades:
            result = self._process_trade(idea)
            results.append(result)

        # Update the watchlist file with results
        self._save_watchlist(watchlist, new_trades)

        # Print summary
        self._print_summary(results)

        return results

    def _process_trade(self, idea: TradeIdea) -> dict:
        """Validate a single trade idea against all gates and risk rules."""
        result = {
            "ticker": idea.ticker,
            "direction": idea.direction.value,
            "entry_type": idea.entry_type,
            "pivot": idea.pivot,
            "stop": idea.stop,
            "stake": idea.stake,
            "risk_gbp": idea.risk_gbp,
            "decision": "",
            "reason": "",
            "gate_details": [],
        }

        logger.info(
            "── %s %s %s | pivot=%.1f stop=%.1f stake=£%.2f/pt | risk=£%.2f ──",
            idea.ticker, idea.direction.value, idea.entry_type,
            idea.pivot, idea.stop, idea.stake, idea.risk_gbp,
        )

        # ── Step 1: Resolve epic ─────────────────────────────
        if not idea.ig_epic:
            idea.ig_epic = self._market_data.resolve_epic(
                idea.ticker, idea.market.value
            )
        if not idea.ig_epic:
            result["decision"] = "REJECT"
            result["reason"] = "Could not resolve IG epic"
            idea.rejection = result["reason"]
            logger.warning("  REJECT: %s", result["reason"])
            return result

        # ── Step 2: Fetch bars ────────────────────────────────
        bars = self._market_data.get_daily_bars(idea.ig_epic, 260)
        if bars is None or len(bars) < 50:
            result["decision"] = "REJECT"
            result["reason"] = f"Insufficient price data ({len(bars) if bars is not None else 0} bars)"
            idea.rejection = result["reason"]
            logger.warning("  REJECT: %s", result["reason"])
            return result

        result["bars_available"] = len(bars)

        # ── Step 3: Run gates ─────────────────────────────────
        # RS percentile defaults to 50 (middle) until universe scan is built
        rs_pct = 50.0

        if idea.direction == Direction.LONG:
            gate_result = evaluate_long_gates(bars, rs_pct, self._settings)
        else:
            gate_result = evaluate_short_gates(
                bars, rs_pct, idea.entry_type,
                short_interest_pct=None,
                days_to_cover=None,
                borrow_fee=None,
                settings=self._settings,
            )

        result["gate_details"] = [
            {"name": g.name, "passed": g.passed, "value": g.value, "threshold": g.threshold}
            for g in gate_result.gates
        ]

        if not gate_result.passed:
            result["decision"] = "REJECT"
            result["reason"] = (
                f"{gate_result.reject_code.value}: "
                f"{gate_result.reject_code.description}"
            )
            idea.rejection = result["reason"]
            idea.status = "NEW"  # Keep as NEW so you can review and re-evaluate

            # Show which specific conditions failed
            failed = [g for g in gate_result.gates if not g.passed]
            for f in failed:
                logger.info("  FAILED: %s (value=%s, need=%s)", f.name, f.value, f.threshold)

            logger.info("  REJECT: %s", result["reason"])
            return result

        # ── Step 4: Validate your sizing ──────────────────────
        portfolio_balance = self._get_portfolio_value()
        max_risk = portfolio_balance * self._settings.max_risk_per_trade

        warnings = []

        # Check if your stake risks more than 1% of portfolio
        if idea.risk_gbp > max_risk:
            warnings.append(
                f"Risk £{idea.risk_gbp:.2f} exceeds 1% limit (£{max_risk:.2f}). "
                f"Suggested stake: £{max_risk / idea.risk_per_point:.2f}/pt"
            )

        # Check overnight gap sizing
        gap_max_stake = calculate_overnight_gap_stake(
            idea.pivot, self._settings.overnight_gap_pct, max_risk
        )
        if idea.stake > gap_max_stake and gap_max_stake > 0:
            warnings.append(
                f"Stake £{idea.stake:.2f}/pt exceeds overnight gap limit. "
                f"Max for 10% gap = £{gap_max_stake:.2f}/pt"
            )

        # Check stop distance
        stop_distance_pct = idea.risk_per_point / idea.pivot
        if stop_distance_pct > self._settings.max_stop_distance:
            result["decision"] = "REJECT"
            result["reason"] = (
                f"R05: Stop distance {stop_distance_pct:.1%} exceeds "
                f"{self._settings.max_stop_distance:.0%} maximum"
            )
            idea.rejection = result["reason"]
            logger.info("  REJECT: %s", result["reason"])
            return result

        # Check reward:risk
        # (We don't know the target, but we can flag if stop is unreasonably tight)

        # Check UK spread
        if idea.market == Market.UK:
            spread_pct = self._market_data.get_spread_pct(idea.ig_epic)
            if spread_pct:
                result["spread_pct"] = round(spread_pct, 4)
                if spread_pct > self._settings.uk_spread_skip_threshold:
                    result["decision"] = "REJECT"
                    result["reason"] = f"R06: UK spread {spread_pct:.2%} too wide"
                    idea.rejection = result["reason"]
                    return result
                elif spread_pct > self._settings.uk_spread_reduce_threshold:
                    warnings.append(
                        f"UK spread {spread_pct:.2%} > 0.3% — "
                        f"consider reducing stake by 25%"
                    )

        # ── Step 5: Decision ──────────────────────────────────
        result["decision"] = "APPROVED"
        result["warnings"] = warnings
        result["portfolio_value"] = portfolio_balance
        result["max_risk_1pct"] = max_risk
        result["suggested_stake"] = round(max_risk / idea.risk_per_point, 2)
        result["adx"] = gate_result.adx_value
        result["volume_gate"] = gate_result.volume_dry_count or gate_result.distribution_count

        idea.rejection = ""

        if warnings:
            for w in warnings:
                logger.warning("  WARNING: %s", w)

        logger.info(
            "  APPROVED | ADX=%.1f | risk=£%.2f | %s",
            gate_result.adx_value or 0,
            idea.risk_gbp,
            "DRY RUN (no order placed)" if self._dry_run else "ORDER WILL BE PLACED",
        )

        if not self._dry_run:
            # TODO: Place the actual order via IG
            idea.status = "ACTIVE"
        else:
            idea.status = "NEW"  # Keep as NEW in dry-run mode

        return result

    def _get_portfolio_value(self) -> float:
        """Get current account balance."""
        try:
            balance = self._session.get_account_balance()
            return float(balance.get("balance", 10000))
        except Exception:
            return 10000.0  # Default for demo

    def _load_watchlist(self) -> dict:
        if not self._watchlist_path.exists():
            return {"trades": []}
        with open(self._watchlist_path) as f:
            return json.load(f)

    def _save_watchlist(self, watchlist: dict, processed: list[TradeIdea]) -> None:
        """Update the watchlist file with processing results."""
        # Update the trades in the watchlist
        trades = watchlist.get("trades", [])
        for idea in processed:
            for t in trades:
                if (t["ticker"] == idea.ticker
                    and t["direction"] == idea.direction.value
                    and t.get("status") == "NEW"):
                    t["status"] = idea.status
                    t["ig_epic"] = idea.ig_epic
                    t["rejection"] = idea.rejection
                    t["last_checked"] = datetime.utcnow().isoformat()
                    break

        with open(self._watchlist_path, "w") as f:
            json.dump(watchlist, f, indent=2)

    def _print_summary(self, results: list[dict]) -> None:
        """Print a clean summary to the console."""
        approved = [r for r in results if r["decision"] == "APPROVED"]
        rejected = [r for r in results if r["decision"] == "REJECT"]

        print()
        print("═" * 60)
        print(f"  WATCHLIST PROCESSED: {len(results)} trade(s)")
        print(f"  Approved: {len(approved)}  |  Rejected: {len(rejected)}")
        print("═" * 60)

        for r in approved:
            warnings = r.get("warnings", [])
            flag = " ⚠" if warnings else ""
            print(
                f"  ✓ {r['ticker']:8s} {r['direction']:5s} {r['entry_type']:4s} "
                f"| pivot={r['pivot']:>8.1f} stop={r['stop']:>8.1f} "
                f"| stake=£{r['stake']:.2f}/pt risk=£{r['risk_gbp']:.2f}"
                f"| suggested=£{r.get('suggested_stake', 0):.2f}/pt{flag}"
            )
            for w in warnings:
                print(f"    ⚠ {w}")

        for r in rejected:
            print(
                f"  ✗ {r['ticker']:8s} {r['direction']:5s} {r['entry_type']:4s} "
                f"| {r['reason']}"
            )

        if self._dry_run:
            print()
            print("  Mode: DRY RUN — no orders placed")
            print("  To go live: set dry_run=False")

        print("═" * 60)
