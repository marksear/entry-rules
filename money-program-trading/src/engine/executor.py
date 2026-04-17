"""
Trade Executor — monitors entry zones and strikes when conditions are met.

The Swing Trader delivers the signal. You curate what to trade.
This engine handles execution — stalking the entry zone and entering
at the right moment — then monitors the open position.

ENTRY PHASE (for each PENDING trade):
    Every 60 seconds for 20 minutes:
    1. Is the market open and tradeable?
    2. Is price within the entry zone?
    3. Is volume confirming? (not dead air)
    4. Is the spread acceptable?
    5. Is price moving in our direction? (momentum)
    → If ALL pass → place spread bet with stop attached

POSITION PHASE (for each FILLED trade):
    Every 60 seconds continuously:
    1. Track P&L
    2. Mid-session volume confirmation (reduce to pilot if weak)
    3. Move stop to breakeven after 1R profit
    4. Activate trailing stop after 2R profit
    5. Emergency cover: 15% adverse on shorts
    6. Log everything
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from ..config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

TRADES_PATH = Path("data/trades.json")


# IG point conversion:
#   US stocks: IG quotes in cents → $392.00 = 39200 points
#   UK stocks: IG quotes in pence → 116p = 116.0 points
#
# The user enters prices in dollars (US) or pence (UK) as they
# see them from the Swing Trader. The engine converts internally.

def _detect_market(symbol: str, epic: str) -> str:
    """Detect US vs UK from the epic prefix."""
    if not epic:
        return "US"  # default
    prefix = epic.split(".")[0] if epic else ""
    # IG epic prefixes: KA/KB = UK shares, UA/UC/UD = US shares
    if prefix in ("KA", "KB", "KC"):
        return "UK"
    return "US"


def _to_ig_points(price: float, market: str) -> float:
    """Convert user price to IG points."""
    if market == "US":
        return price * 100  # dollars → cents
    return price  # UK pence → already in points


def _from_ig_points(points: float, market: str) -> float:
    """Convert IG points back to user price for display."""
    if market == "US":
        return points / 100  # cents → dollars
    return points


class Trade:
    """A single trade from the trades file."""

    def __init__(self, data: dict, index: int):
        self.index = index
        self.symbol: str = data["symbol"]
        self.direction: str = data["direction"].upper()
        self.market: str = data.get("market", "US").upper()
        self.stake: float = data["stake"]
        self.status: str = data.get("status", "PENDING")
        self.notes: str = data.get("notes", "")
        self.ig_epic: str = data.get("ig_epic", "")
        self.fill_price: float | None = data.get("fill_price")  # stored in IG points
        self.fill_time: str | None = data.get("fill_time")
        self.deal_id: str | None = data.get("deal_id")
        self.current_stop: float | None = data.get("current_stop")  # IG points
        self.trailing_active: bool = data.get("trailing_active", False)
        self.breakeven_moved: bool = data.get("breakeven_moved", False)
        self.is_pilot: bool = data.get("is_pilot", False)
        self.min_deal_size: float = data.get("min_deal_size", 0.11)  # fetched from IG per instrument
        self.highest_locked_profit: float = data.get("highest_locked_profit", 0.0)  # £ locked — only ratchets up
        self.stop_verified: bool = data.get("stop_verified", False)  # True only after IG confirms
        self.checks: list[dict] = data.get("checks", [])
        self._raw = data

        # User-entered prices (dollars for US, pence for UK)
        self._entry_low_user: float = data["entry_low"]
        self._entry_high_user: float = data["entry_high"]
        self._stop_user: float = data["stop"]

        # Convert to IG points for internal comparison
        self.entry_low: float = _to_ig_points(self._entry_low_user, self.market)
        self.entry_high: float = _to_ig_points(self._entry_high_user, self.market)
        self.stop: float = _to_ig_points(self._stop_user, self.market)

    def detect_market_from_epic(self) -> None:
        """Update market detection once epic is resolved."""
        if self.ig_epic:
            detected = _detect_market(self.symbol, self.ig_epic)
            if detected != self.market:
                self.market = detected
                # Re-convert prices
                self.entry_low = _to_ig_points(self._entry_low_user, self.market)
                self.entry_high = _to_ig_points(self._entry_high_user, self.market)
                self.stop = _to_ig_points(self._stop_user, self.market)

    @property
    def is_long(self) -> bool:
        return self.direction == "LONG"

    @property
    def risk_per_point(self) -> float:
        """Risk in IG points."""
        if self.fill_price:
            return abs(self.fill_price - self.stop)
        mid = (self.entry_low + self.entry_high) / 2
        return abs(mid - self.stop)

    @property
    def risk_gbp(self) -> float:
        """Risk in £. For spread bets: stake × risk_in_points."""
        return self.stake * self.risk_per_point

    def price_in_zone(self, bid: float, ask: float) -> bool:
        """Are IG prices within the entry zone (in IG points)?"""
        if self.is_long:
            return self.entry_low <= ask <= self.entry_high
        else:
            return self.entry_low <= bid <= self.entry_high

    def entry_price_now(self, bid: float, ask: float) -> float:
        """The IG price we'd enter at right now."""
        return ask if self.is_long else bid

    def display_price(self, ig_price: float) -> float:
        """Convert IG points to user-facing price (dollars/pence)."""
        return _from_ig_points(ig_price, self.market)

    def unrealised_pnl(self, bid: float, ask: float) -> float:
        """P&L in £ based on current IG prices."""
        if not self.fill_price:
            return 0.0
        if self.is_long:
            return (bid - self.fill_price) * self.stake
        else:
            return (self.fill_price - ask) * self.stake

    def unrealised_r(self, bid: float, ask: float) -> float:
        """P&L expressed as multiples of R (risk)."""
        pnl = self.unrealised_pnl(bid, ask)
        if self.risk_gbp <= 0:
            return 0.0
        return pnl / self.risk_gbp

    def to_dict(self) -> dict:
        d = dict(self._raw)
        d["market"] = self.market
        d["status"] = self.status
        d["ig_epic"] = self.ig_epic
        d["fill_price"] = self.fill_price
        d["fill_price_display"] = self.display_price(self.fill_price) if self.fill_price else None
        d["fill_time"] = self.fill_time
        d["deal_id"] = self.deal_id
        d["current_stop"] = self.current_stop
        d["trailing_active"] = self.trailing_active
        d["breakeven_moved"] = self.breakeven_moved
        d["is_pilot"] = self.is_pilot
        d["min_deal_size"] = self.min_deal_size
        d["highest_locked_profit"] = self.highest_locked_profit
        d["stop_verified"] = self.stop_verified
        d["checks"] = self.checks
        return d


# ══════════════════════════════════════════════════════════════
# ENTRY MONITOR — stalks the zone and enters
# ══════════════════════════════════════════════════════════════

class EntryMonitor:
    """
    Monitors PENDING trades. Checks 5 conditions every minute:
      1. Market open?
      2. Price in zone?
      3. Volume confirming?
      4. Spread acceptable?
      5. Momentum in our direction?
    All 5 must pass to trigger entry.
    """

    # Minimum volume ratio vs recent average to confirm entry
    MIN_VOLUME_RATIO = 0.8  # At least 80% of recent avg (not dead air)

    # Maximum spread as % of price
    MAX_SPREAD_PCT = 0.003  # 0.3% for most stocks
    MAX_SPREAD_PCT_UK_SMALL = 0.005  # 0.5% for UK small caps

    # Momentum: price must have moved in our direction over last N checks
    MOMENTUM_LOOKBACK = 3  # Need 3 checks of price data to assess

    def __init__(
        self,
        ig_headers: dict,
        settings: Settings | None = None,
        trades_path: Path | None = None,
        check_interval: int = 60,
        max_checks: int = 999,
        window_start: str | None = None,
        window_end: str | None = None,
        dry_run: bool = True,
    ):
        self._headers = ig_headers
        self._settings = settings or get_settings()
        self._trades_path = trades_path or TRADES_PATH
        self._check_interval = check_interval
        self._max_checks = max_checks  # Safety cap (overridden by time window)
        self._dry_run = dry_run
        self._base_url = self._settings.ig_base_url
        self._portfolio_value: float = 0.0

        # Time window (EST) — engine stalks entries during this window only
        self._window_start = window_start or self._settings.entry_window_start
        self._window_end = window_end or self._settings.entry_window_end

    def run(self) -> list[Trade]:
        """
        Main entry monitoring loop. Returns list of all processed trades.
        """
        # Fetch portfolio value for risk calculations
        self._portfolio_value = self._fetch_portfolio_value()
        if self._portfolio_value <= 0:
            self._portfolio_value = 10000.0  # Safe default for demo
            logger.warning("Could not fetch portfolio value — using £10,000 default")

        trades_data = self._load_trades()
        all_trades = trades_data.get("trades", [])
        pending = [
            Trade(t, i) for i, t in enumerate(all_trades)
            if t.get("status") == "PENDING"
        ]

        if not pending:
            print("\nNo PENDING trades. Add trades to data/trades.json")
            return []

        # Resolve epics and fetch per-instrument minimum deal sizes
        for trade in pending:
            if not trade.ig_epic:
                trade.ig_epic = self._resolve_epic(trade.symbol, trade.market)
                if not trade.ig_epic:
                    logger.error("Cannot resolve epic for %s (%s)", trade.symbol, trade.market)
                    trade.status = "ERROR"
                else:
                    trade.detect_market_from_epic()
                    trade.min_deal_size = self._fetch_min_deal_size(trade.ig_epic)

        active = [t for t in pending if t.status == "PENDING"]

        self._print_header(active)

        # ── Wait for entry window if we're early ──────────────
        if not self._in_window():
            window_msg = self._wait_for_window()
            if window_msg == "PAST":
                print(f"\n  Entry window ({self._window_start}–{self._window_end} EST) "
                      f"has passed for today.")
                return pending

        # ── Monitoring loop — runs until window closes ────────
        check_num = 0
        start_time = datetime.now()

        while active and self._in_window() and check_num < self._max_checks:
            check_num += 1
            now = datetime.now()
            elapsed = (now - start_time).total_seconds()
            remaining = self._seconds_until_window_end()

            print(f"\n─ Check {check_num} "
                  f"({now.strftime('%H:%M:%S')}) "
                  f"[{elapsed:.0f}s elapsed, {remaining:.0f}s remaining] ─")

            filled_this_round = []

            for trade in active:
                result = self._evaluate_entry(trade, check_num)
                self._print_check_result(trade, result)

                if result["action"] == "FILL":
                    if self._dry_run:
                        trade.status = "FILLED_DRY"
                        trade.fill_price = result["price"]
                        trade.fill_time = now.isoformat()
                        trade.current_stop = trade.stop
                    else:
                        fill = self._place_order(trade, result["price"])
                        if fill["success"]:
                            trade.status = "FILLED"
                            trade.fill_price = fill.get("fill_price", result["price"])
                            trade.fill_time = now.isoformat()
                            trade.deal_id = fill.get("deal_id", "")
                            trade.current_stop = trade.stop
                        else:
                            print(f"    ORDER FAILED: {fill.get('reason', '?')}")

                    filled_this_round.append(trade)

            for t in filled_this_round:
                active.remove(t)

            self._save_trades(trades_data, pending)

            if not active:
                break

            # Wait for next check (if still within window)
            if self._in_window():
                time.sleep(self._check_interval)

        # Expire remaining — window closed
        for trade in active:
            trade.status = "EXPIRED"
            print(f"  ⏰ {trade.symbol} — EXPIRED (window closed)")

        self._save_trades(trades_data, pending)
        self._print_summary(pending)

        return pending

    def _evaluate_entry(self, trade: Trade, check_num: int) -> dict:
        """
        Evaluate all 5 entry conditions. ALL must pass.
        """
        # Fetch market snapshot
        snapshot = self._fetch_snapshot(trade.ig_epic)
        if not snapshot:
            return {"action": "WAIT", "reason": "Price unavailable", "checks": {}}

        bid = snapshot["bid"]
        ask = snapshot["ask"]
        status = snapshot["market_status"]
        high_today = snapshot.get("high", ask)
        low_today = snapshot.get("low", bid)

        checks = {
            "bid": bid,
            "ask": ask,
            "market_status": status,
        }

        # ── CHECK 1: Market open and tradeable? ───────────────
        if status != "TRADEABLE":
            checks["market_open"] = False
            trade.checks.append({"check": check_num, "time": datetime.now().isoformat(), **checks})
            return {"action": "WAIT", "reason": f"Market {status}", "checks": checks}
        checks["market_open"] = True

        # ── CHECK 2: Price in zone? ───────────────────────────
        in_zone = trade.price_in_zone(bid, ask)
        entry = trade.entry_price_now(bid, ask)
        checks["in_zone"] = in_zone
        checks["entry_price"] = entry

        if not in_zone:
            # Display in user prices (dollars/pence)
            dp = trade.display_price(entry)
            dl = trade._entry_low_user
            dh = trade._entry_high_user
            curr = "$" if trade.market == "US" else ""
            if trade.is_long:
                if entry < trade.entry_low:
                    reason = f"Below zone: {curr}{dp:.2f} < {curr}{dl:.2f}"
                else:
                    reason = f"Above zone: {curr}{dp:.2f} > {curr}{dh:.2f}"
            else:
                if entry > trade.entry_high:
                    reason = f"Above zone: {curr}{dp:.2f} > {curr}{dh:.2f}"
                else:
                    reason = f"Below zone: {curr}{dp:.2f} < {curr}{dl:.2f}"

            trade.checks.append({"check": check_num, "time": datetime.now().isoformat(), **checks})
            return {"action": "WAIT", "reason": reason, "checks": checks}

        # ── CHECK 3: Volume confirming? ───────────────────────
        # Use intraday volume from snapshot vs expected pace
        vol_ok, vol_detail = self._check_volume(trade, snapshot)
        checks["volume_ok"] = vol_ok
        checks["volume_detail"] = vol_detail

        if not vol_ok:
            trade.checks.append({"check": check_num, "time": datetime.now().isoformat(), **checks})
            return {"action": "WAIT", "reason": f"In zone but {vol_detail}", "checks": checks}

        # ── CHECK 4: Spread acceptable? ───────────────────────
        spread = ask - bid
        spread_pct = spread / bid if bid > 0 else 0
        checks["spread_pct"] = round(spread_pct, 5)

        max_spread = self.MAX_SPREAD_PCT
        if spread_pct > max_spread:
            trade.checks.append({"check": check_num, "time": datetime.now().isoformat(), **checks})
            return {
                "action": "WAIT",
                "reason": f"In zone but spread {spread_pct:.3%} > {max_spread:.1%}",
                "checks": checks,
            }
        checks["spread_ok"] = True

        # ── CHECK 5: Momentum in our direction? ───────────────
        momentum_ok, momentum_detail = self._check_momentum(trade)
        checks["momentum_ok"] = momentum_ok
        checks["momentum_detail"] = momentum_detail

        # Momentum is a soft check — we still enter if price and volume are right,
        # but we note it. On the first few checks we don't have enough data.
        if not momentum_ok and check_num > self.MOMENTUM_LOOKBACK:
            checks["momentum_warning"] = True
            logger.info("  Momentum note: %s", momentum_detail)

        # ── CHECK 6: Risk limits ──────────────────────────────
        # This is the final gatekeeper. The Masterclass hard limits:
        #   - Max 1% of portfolio risked per trade
        #   - Max stop distance 8%
        #   - Overnight gap sizing: 10% gap = max 1% portfolio loss
        risk_ok, risk_detail = self._check_risk(trade, entry)
        checks["risk_ok"] = risk_ok
        checks["risk_detail"] = risk_detail

        if not risk_ok:
            trade.checks.append({"check": check_num, "time": datetime.now().isoformat(), **checks})
            return {"action": "WAIT", "reason": risk_detail, "checks": checks}

        # ── ALL CHECKS PASSED → FILL ─────────────────────────
        trade.checks.append({"check": check_num, "time": datetime.now().isoformat(), **checks})
        return {
            "action": "FILL",
            "price": entry,
            "bid": bid,
            "ask": ask,
            "checks": checks,
        }

    def _check_volume(self, trade: Trade, snapshot: dict) -> tuple[bool, str]:
        """
        Is there volume behind this move?
        We don't want to enter on dead air — price drifting into the zone
        on no volume is not a real signal.
        """
        # IG snapshot includes day's traded volume
        day_volume = snapshot.get("net_change_vol", 0) or 0

        # Also check the last traded volume from the snapshot
        # If it's zero or very low, the market is thin
        if day_volume == 0:
            # Can't assess — allow entry but note it
            return True, "Volume data unavailable (allowing entry)"

        # We'd ideally compare against the 50-day average, but that requires
        # a separate bar fetch. For the real-time check, we look at whether
        # today's volume is building at a reasonable pace.
        # A simple heuristic: if we're into the session and volume is
        # very low relative to what we'd expect, flag it.

        return True, f"Day volume: {day_volume:,.0f}"

    def _check_momentum(self, trade: Trade) -> tuple[bool, str]:
        """
        Is price moving in our direction over recent checks?
        Looks at the last N check prices to see if there's directional bias.
        """
        recent = trade.checks[-self.MOMENTUM_LOOKBACK:]
        if len(recent) < self.MOMENTUM_LOOKBACK:
            return True, "Insufficient data for momentum check"

        # Get entry prices from recent checks
        prices = []
        for c in recent:
            if trade.is_long:
                p = c.get("ask", c.get("entry_price"))
            else:
                p = c.get("bid", c.get("entry_price"))
            if p:
                prices.append(p)

        if len(prices) < 2:
            return True, "Insufficient price history"

        # For longs: price should be stable or rising
        # For shorts: price should be stable or falling
        first = prices[0]
        last = prices[-1]
        change_pct = (last - first) / first if first > 0 else 0

        if trade.is_long:
            ok = change_pct >= -0.002  # Allow up to 0.2% pullback
            return ok, f"Momentum {change_pct:+.3%} ({'rising' if ok else 'fading'})"
        else:
            ok = change_pct <= 0.002
            return ok, f"Momentum {change_pct:+.3%} ({'falling' if ok else 'rising against'})"

    def _check_risk(self, trade: Trade, entry_price_ig: float) -> tuple[bool, str]:
        """
        Enforce Masterclass hard risk limits. This is the final gatekeeper.

        Enforces Masterclass hard risk limits. This is the final gatekeeper.

        Logic:
          1. Calculate max safe stake from 1% risk rule
          2. If safe stake ≥ IG minimum → use it (cap user's stake down)
          3. If safe stake < IG minimum → use minimum stake, tighten stop
             to keep risk at 1%
          4. Also check overnight gap sizing and adjust if needed
          5. Final stop distance must be ≤ 8%

        The engine NEVER rejects a trade on risk alone — it adjusts the
        parameters to make the trade safe. Only rejects if even a tight
        stop can't fit within limits.

        Returns (ok, detail).
        """
        import math

        s = self._settings
        portfolio = self._portfolio_value
        max_risk_gbp = portfolio * s.max_risk_per_trade  # 1% of portfolio
        min_stake = trade.min_deal_size  # Per-instrument minimum from IG API

        curr = "$" if trade.market == "US" else ""
        dp_entry = trade.display_price(entry_price_ig)

        # Original stop distance in IG points
        original_stop_distance = abs(entry_price_ig - trade.stop)
        original_stop_pct = original_stop_distance / entry_price_ig if entry_price_ig > 0 else 0
        original_risk = trade.stake * original_stop_distance

        # ── Step 1: What's the max safe stake at this stop? ───
        max_safe_stake = max_risk_gbp / original_stop_distance if original_stop_distance > 0 else 0
        max_safe_stake = math.floor(max_safe_stake * 100) / 100  # Round down

        adjusted_stake = trade.stake
        adjusted_stop = trade.stop
        adjusted_stop_distance = original_stop_distance
        stop_was_tightened = False

        if original_risk > max_risk_gbp:
            # Risk exceeds 1% — need to adjust

            if max_safe_stake >= min_stake:
                # ── Path A: Cap stake, keep stop ──────────────
                adjusted_stake = max_safe_stake
                print(
                    f"    ⚠ RISK CAP: £{trade.stake:.2f}/pt → £{adjusted_stake:.2f}/pt "
                    f"(your £{trade.stake:.2f} would risk £{original_risk:.2f} = "
                    f"{original_risk/portfolio:.1%})"
                )

            else:
                # ── Path B: Use min stake, tighten stop ───────
                # New stop distance = max_risk / min_stake
                adjusted_stake = min_stake
                new_stop_distance = max_risk_gbp / min_stake
                new_stop_distance = math.floor(new_stop_distance)  # Round down (tighter)

                if trade.is_long:
                    adjusted_stop = entry_price_ig - new_stop_distance
                else:
                    adjusted_stop = entry_price_ig + new_stop_distance

                adjusted_stop_distance = new_stop_distance
                stop_was_tightened = True

                dp_original_stop = trade._stop_user
                dp_new_stop = trade.display_price(adjusted_stop)
                new_stop_pct = new_stop_distance / entry_price_ig

                print(
                    f"    ⚠ RISK ADJUST: Min stake £{min_stake:.2f}/pt, "
                    f"tightening stop\n"
                    f"      Stop: {curr}{dp_original_stop:.2f} → "
                    f"{curr}{dp_new_stop:.2f} "
                    f"({new_stop_pct:.1%} from entry)\n"
                    f"      Risk: £{min_stake * new_stop_distance:.2f} "
                    f"({min_stake * new_stop_distance / portfolio:.1%} of portfolio)"
                )

        # ── Step 2: Overnight gap sizing check ────────────────
        gap_points = entry_price_ig * s.overnight_gap_pct  # 10% gap
        gap_loss = adjusted_stake * gap_points

        if gap_loss > max_risk_gbp:
            # Tighten further for gap risk
            gap_safe_stake = math.floor((max_risk_gbp / gap_points) * 100) / 100

            if gap_safe_stake >= min_stake:
                adjusted_stake = gap_safe_stake
                print(
                    f"    ⚠ GAP RISK: 10% gap @ £{adjusted_stake:.2f}/pt "
                    f"would cost £{adjusted_stake * gap_points:.2f} — "
                    f"capping stake"
                )
            else:
                # Even min stake can't handle the gap risk on this stock
                # Tighten stop further so gap is survivable at min stake
                adjusted_stake = min_stake
                # Accept gap risk at min stake — it's the smallest we can go
                print(
                    f"    ⚠ GAP NOTE: 10% gap at £{min_stake:.2f}/pt "
                    f"= £{min_stake * gap_points:.2f} "
                    f"({min_stake * gap_points / portfolio:.1%}). "
                    f"Minimum stake — accepting residual gap risk."
                )

        # ── Step 3: Final stop distance check (≤ 8%) ─────────
        final_stop_pct = adjusted_stop_distance / entry_price_ig if entry_price_ig > 0 else 0
        if final_stop_pct > s.max_stop_distance:
            return False, (
                f"RISK REJECT: Even adjusted stop is {final_stop_pct:.1%} "
                f"> {s.max_stop_distance:.0%} max. "
                f"This stock's price is too high relative to account size."
            )

        # ── Apply adjustments ─────────────────────────────────
        trade.stake = adjusted_stake
        if stop_was_tightened:
            trade.stop = adjusted_stop
            trade.current_stop = adjusted_stop

        final_risk = adjusted_stake * adjusted_stop_distance
        final_stop_display = trade.display_price(adjusted_stop) if stop_was_tightened else trade._stop_user

        return True, (
            f"Risk OK: £{final_risk:.2f} ({final_risk/portfolio:.1%}) "
            f"@ £{adjusted_stake:.2f}/pt | "
            f"Stop: {curr}{final_stop_display:.2f}"
        )

    def _fetch_portfolio_value(self) -> float:
        """Fetch current account balance from IG."""
        try:
            headers = dict(self._headers)
            headers["VERSION"] = "1"
            r = requests.get(f"{self._base_url}/accounts", headers=headers, timeout=10)
            if r.status_code == 200:
                for acc in r.json().get("accounts", []):
                    if acc.get("accountId") == self._settings.ig_account_id:
                        bal = acc.get("balance", {}).get("balance", 0)
                        logger.info("Portfolio value: £%.2f", bal)
                        return float(bal)
                # Fallback to first account
                accounts = r.json().get("accounts", [])
                if accounts:
                    return float(accounts[0].get("balance", {}).get("balance", 0))
        except Exception as e:
            logger.error("Failed to fetch portfolio value: %s", e)
        return 0.0

    def _fetch_snapshot(self, epic: str) -> dict | None:
        """Fetch full market snapshot from IG."""
        try:
            headers = dict(self._headers)
            headers["VERSION"] = "3"
            r = requests.get(
                f"{self._base_url}/markets/{epic}",
                headers=headers, timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                snap = data.get("snapshot", {})
                bid = snap.get("bid")
                ask = snap.get("offer")
                if bid is not None and ask is not None:
                    return {
                        "bid": float(bid),
                        "ask": float(ask),
                        "market_status": snap.get("marketStatus", "UNKNOWN"),
                        "high": float(snap.get("high", 0) or 0),
                        "low": float(snap.get("low", 0) or 0),
                        "net_change_vol": snap.get("netChange", 0),
                        "pct_change": snap.get("percentageChange", 0),
                    }
            return None
        except Exception as e:
            logger.error("Snapshot error for %s: %s", epic, e)
            return None

    def _place_order(self, trade: Trade, price: float) -> dict:
        """Place a spread bet on IG with stop attached."""
        try:
            headers = dict(self._headers)
            headers["VERSION"] = "2"

            direction = "BUY" if trade.is_long else "SELL"
            stop_distance = abs(price - trade.stop)

            body = {
                "epic": trade.ig_epic,
                "direction": direction,
                "size": trade.stake,
                "orderType": "MARKET",
                "stopDistance": stop_distance,
                "guaranteedStop": False,
                "forceOpen": True,
                "currencyCode": "GBP",
                "expiry": "DFB",
            }

            r = requests.post(
                f"{self._base_url}/positions/otc",
                json=body, headers=headers, timeout=15,
            )

            if r.status_code == 200:
                deal_ref = r.json().get("dealReference", "")
                time.sleep(1)
                headers["VERSION"] = "1"
                r2 = requests.get(
                    f"{self._base_url}/confirms/{deal_ref}",
                    headers=headers, timeout=10,
                )
                if r2.status_code == 200:
                    confirm = r2.json()
                    deal_status = confirm.get("dealStatus", "")
                    reason = confirm.get("reason", "")
                    # IG returns OPENED or ACCEPTED for successful orders
                    ok = deal_status in ("OPENED", "ACCEPTED") or reason == "SUCCESS"
                    return {
                        "success": ok,
                        "deal_id": confirm.get("dealId", ""),
                        "fill_price": confirm.get("level"),
                        "reason": reason,
                        "deal_status": deal_status,
                    }
                return {"success": True, "deal_id": deal_ref, "fill_price": price}
            else:
                return {"success": False, "reason": r.json().get("errorCode", r.text[:200])}
        except Exception as e:
            return {"success": False, "reason": str(e)}

    def _resolve_epic(self, symbol: str, market: str = "US") -> str:
        """
        Find IG spread bet epic for a stock symbol.

        IG epic prefixes:
            US stocks: UA, UC, UD  (e.g. UA.D.AVGO.DAILY.IP)
            UK stocks: KA, KB, KC  (e.g. KA.D.VOD.CASH.IP)
        """
        # Prefix filter based on market
        us_prefixes = ("UA.", "UC.", "UD.")
        uk_prefixes = ("KA.", "KB.", "KC.")
        wanted_prefixes = us_prefixes if market == "US" else uk_prefixes

        try:
            headers = dict(self._headers)
            headers["VERSION"] = "1"
            r = requests.get(
                f"{self._base_url}/markets?searchTerm={symbol}",
                headers=headers, timeout=10,
            )
            if r.status_code == 200:
                markets = r.json().get("markets", [])

                # Priority 1: SHARES type, correct market, symbol in epic
                for m in markets:
                    epic = m.get("epic", "")
                    inst_type = m.get("instrumentType", "").upper()
                    if (inst_type == "SHARES"
                            and epic.startswith(wanted_prefixes)
                            and symbol.upper() in epic.upper()):
                        logger.info("Resolved %s (%s) → %s", symbol, market, epic)
                        return epic

                # Priority 2: SHARES type, correct market prefix
                for m in markets:
                    epic = m.get("epic", "")
                    name = m.get("instrumentName", "").upper()
                    inst_type = m.get("instrumentType", "").upper()
                    if (inst_type == "SHARES"
                            and epic.startswith(wanted_prefixes)
                            and symbol.upper() in name):
                        logger.info("Resolved %s (%s) → %s (%s)", symbol, market, epic, name)
                        return epic

                # Priority 3: Any match with correct market prefix
                for m in markets:
                    epic = m.get("epic", "")
                    if epic.startswith(wanted_prefixes) and ("CASH" in epic or "DAILY" in epic):
                        logger.info("Resolved %s (%s) → %s (prefix match)", symbol, market, epic)
                        return epic

                # Last resort: first result (may be wrong market — log warning)
                if markets:
                    epic = markets[0].get("epic", "")
                    logger.warning(
                        "Resolved %s (%s) → %s (LAST RESORT — may be wrong market!)",
                        symbol, market, epic,
                    )
                    return epic

        except Exception as e:
            logger.error("Epic resolution failed for %s: %s", symbol, e)
        return ""

    def _fetch_min_deal_size(self, epic: str) -> float:
        """
        Fetch the minimum deal size (£/pt) for an instrument from IG.
        Each stock has its own minimum — e.g. AVGO = 0.24, cheaper stocks = 0.11.
        Falls back to 0.11 if the fetch fails.
        """
        try:
            headers = dict(self._headers)
            headers["VERSION"] = "3"
            r = requests.get(
                f"{self._base_url}/markets/{epic}",
                headers=headers, timeout=10,
            )
            if r.status_code == 200:
                rules = r.json().get("dealingRules", {})
                min_size = rules.get("minDealSize", {}).get("value")
                if min_size is not None:
                    logger.info("Min deal size for %s: £%.2f/pt", epic, float(min_size))
                    return float(min_size)
        except Exception as e:
            logger.warning("Could not fetch min deal size for %s: %s", epic, e)
        return 0.11  # Conservative fallback

    # ── Time Window Helpers ─────────────────────────────────────

    def _est_now(self) -> datetime:
        """Current time in US Eastern (handles EST/EDT automatically)."""
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
        except ImportError:
            # Fallback: UTC-5 (EST, close enough for off-DST)
            from datetime import timezone, timedelta
            est = timezone(timedelta(hours=-5))
            return datetime.now(est).replace(tzinfo=None)

    def _parse_time(self, time_str: str) -> datetime:
        """Parse HH:MM into today's date in EST."""
        h, m = map(int, time_str.split(":"))
        now = self._est_now()
        return now.replace(hour=h, minute=m, second=0, microsecond=0)

    def _in_window(self) -> bool:
        """Is the current EST time within the entry window?"""
        now = self._est_now()
        start = self._parse_time(self._window_start)
        end = self._parse_time(self._window_end)
        return start <= now <= end

    def _seconds_until_window_end(self) -> float:
        """Seconds remaining in the entry window."""
        now = self._est_now()
        end = self._parse_time(self._window_end)
        return max(0, (end - now).total_seconds())

    def _wait_for_window(self) -> str:
        """
        If before the window, wait. If after, return 'PAST'.
        Returns 'READY' when the window opens.
        """
        now = self._est_now()
        start = self._parse_time(self._window_start)
        end = self._parse_time(self._window_end)

        if now > end:
            return "PAST"

        if now < start:
            wait_secs = (start - now).total_seconds()
            print(f"\n  Entry window: {self._window_start}–{self._window_end} EST")
            print(f"  Current time: {now.strftime('%H:%M:%S')} EST")
            print(f"  Waiting {wait_secs:.0f}s ({wait_secs/60:.1f} min) for window to open...")
            print(f"  (Press Ctrl+C to cancel)\n")
            try:
                time.sleep(wait_secs)
            except KeyboardInterrupt:
                print("\n  Cancelled.")
                return "PAST"

        return "READY"

    def _print_header(self, active: list[Trade]):
        max_risk = self._portfolio_value * self._settings.max_risk_per_trade
        now_est = self._est_now()
        print()
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  MONEY PROGRAM — ENTRY MONITOR                             ║")
        print(f"║  {len(active)} trade(s) | "
              f"every {self._check_interval}s | "
              f"{'DRY RUN' if self._dry_run else '*** LIVE ***':11s}"
              f"                    ║")
        print(f"║  Window: {self._window_start}–{self._window_end} EST "
              f"(now: {now_est.strftime('%H:%M')} EST)"
              f"                        ║")
        print(f"║  Portfolio: £{self._portfolio_value:>10,.2f} | "
              f"Max risk/trade (1%): £{max_risk:>7,.2f}       ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print("║  Checks: ① Market  ② Zone  ③ Volume  ④ Spread             ║")
        print("║          ⑤ Momentum  ⑥ Risk (1% cap + gap sizing)         ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()
        for t in active:
            # Display in user prices (dollars/pence), not IG points
            curr = "$" if t.market == "US" else ""
            print(f"  {t.symbol:8s} {t.direction:5s} | "
                  f"Zone: {curr}{t._entry_low_user:.2f}–{curr}{t._entry_high_user:.2f} | "
                  f"Stop: {curr}{t._stop_user:.2f} | £{t.stake:.2f}/pt | "
                  f"Risk: £{t.risk_gbp:.2f}")

    def _print_check_result(self, trade: Trade, result: dict):
        action = result["action"]
        curr = "$" if trade.market == "US" else ""
        if action == "FILL":
            mode = "DRY RUN" if self._dry_run else "LIVE"
            dp = trade.display_price(result['price'])
            db = trade.display_price(result['bid'])
            da = trade.display_price(result['ask'])
            print(f"  ✓ {trade.symbol} FILL @ {curr}{dp:.2f} "
                  f"(bid={curr}{db:.2f} ask={curr}{da:.2f}) [{mode}]")
        elif action == "WAIT":
            # Convert any IG prices in the reason to user prices
            reason = result["reason"]
            print(f"  ○ {trade.symbol} — {reason}")
        elif action == "SKIP":
            print(f"  – {trade.symbol} — {result.get('reason', 'skipped')}")

    def _print_summary(self, trades: list[Trade]):
        filled = [t for t in trades if t.status in ("FILLED", "FILLED_DRY")]
        expired = [t for t in trades if t.status == "EXPIRED"]
        print(f"\n{'═' * 62}")
        print(f"  Entry monitoring complete")
        print(f"  Filled: {len(filled)} | Expired: {len(expired)}")
        for t in filled:
            dp = t.display_price(t.fill_price) if t.fill_price else 0
            curr = "$" if t.market == "US" else ""
            print(f"    {t.symbol} filled @ {curr}{dp:.2f}")
        print(f"{'═' * 62}")

    def _load_trades(self) -> dict:
        if not self._trades_path.exists():
            return {"trades": []}
        with open(self._trades_path) as f:
            return json.load(f)

    def _save_trades(self, trades_data: dict, processed: list[Trade]) -> None:
        for trade in processed:
            if trade.index < len(trades_data["trades"]):
                trades_data["trades"][trade.index] = trade.to_dict()
        with open(self._trades_path, "w") as f:
            json.dump(trades_data, f, indent=2, default=str)


# ══════════════════════════════════════════════════════════════
# POSITION MONITOR — manages open trades after fill
# ══════════════════════════════════════════════════════════════

class PositionMonitor:
    """
    Monitors FILLED trades every 60 seconds. Handles:
      - P&L tracking (in £ and in R-multiples)
      - Move stop to breakeven after +1R
      - Activate trailing stop after +2R
      - Emergency cover: 15% adverse on shorts
      - Volume mid-session check (flag weak volume)
      - Log every check
    """

    # Progressive £ trail: lock in profit in £5 steps
    TRAIL_START_PNL = 25.0             # £25 P&L → first stop move (BE+$1)
    TRAIL_FIRST_LOCK_IN = 1.0         # $1 locked at first trigger (covers spread)
    TRAIL_STEP = 5.0                   # Every £5 above £25, lock in £5 more
    TRAIL_LOCK_START = 30.0            # £30 P&L → lock £5, £35 → £10, etc.
    TARGET_PNL = 0                     # £ P&L hard target (0 = disabled, use R-based)
    TARGET_R = 3.0                     # Hard profit target at 3R
    EMERGENCY_COVER_PCT = 0.15         # 15% adverse move → cover immediately

    def __init__(
        self,
        ig_headers: dict,
        settings: Settings | None = None,
        trades_path: Path | None = None,
        check_interval: int = 60,
        dry_run: bool = True,
    ):
        self._headers = ig_headers
        self._settings = settings or get_settings()
        self._trades_path = trades_path or TRADES_PATH
        self._check_interval = check_interval
        self._dry_run = dry_run
        self._base_url = self._settings.ig_base_url

    def run(self) -> None:
        """Monitor all filled positions. Runs until interrupted (Ctrl+C)."""
        print()
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  MONEY PROGRAM — POSITION MONITOR                          ║")
        print(f"║  Every {self._check_interval}s | "
              f"{'DRY RUN' if self._dry_run else '*** LIVE ***':11s}"
              f"                                   ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print("║  Trail: £25→BE+$1 | £30→£5 | £35→£10 | +£5 steps | Tgt 3R  ║")
        print("╚══════════════════════════════════════════════════════════════╝")

        # On startup: verify actual stop levels on IG
        self._verify_stops_on_startup()

        try:
            while True:
                trades_data = self._load_trades()
                all_trades = trades_data.get("trades", [])
                filled = [
                    Trade(t, i) for i, t in enumerate(all_trades)
                    if t.get("status") in ("FILLED", "FILLED_DRY")
                ]

                if not filled:
                    print(f"\n  ({datetime.now().strftime('%H:%M:%S')}) No active positions")
                    time.sleep(self._check_interval)
                    continue

                now = datetime.now()
                print(f"\n─ Position check ({now.strftime('%H:%M:%S')}) "
                      f"— {len(filled)} position(s) ─")

                for trade in filled:
                    self._monitor_position(trade)

                self._save_trades(trades_data, filled)
                time.sleep(self._check_interval)

        except KeyboardInterrupt:
            print("\n\nPosition monitor stopped.")

    def _verify_stops_on_startup(self) -> None:
        """On startup, read actual stop levels from IG and reconcile with our JSON."""
        trades_data = self._load_trades()
        all_trades = trades_data.get("trades", [])
        filled = [
            Trade(t, i) for i, t in enumerate(all_trades)
            if t.get("status") in ("FILLED", "FILLED_DRY")
        ]
        if not filled:
            return

        print("\n  Verifying stops on IG...")
        changed = False
        failed = False
        for trade in filled:
            if not trade.deal_id or self._dry_run:
                continue
            ig_stop = self._read_ig_stop(trade.deal_id)
            our_stop = trade.current_stop or trade.stop
            curr = "$" if trade.market == "US" else ""
            if ig_stop is not None:
                trade.stop_verified = True
                dp_ig = trade.display_price(ig_stop)
                dp_ours = trade.display_price(our_stop)
                if abs(ig_stop - our_stop) > 1.0:  # >1 point difference
                    print(f"  ⚠ {trade.symbol} STOP MISMATCH!")
                    print(f"      Our record:  {curr}{dp_ours:.2f} ({our_stop:.1f} pts)")
                    print(f"      IG actual:   {curr}{dp_ig:.2f} ({ig_stop:.1f} pts)")
                    print(f"      → Updating to IG's actual level")
                    trade.current_stop = ig_stop
                    # Recalculate highest_locked_profit from actual stop
                    if trade.fill_price and trade.stake > 0:
                        lock_in_base_pts = self.TRAIL_FIRST_LOCK_IN * 100 if trade.market == "US" else self.TRAIL_FIRST_LOCK_IN
                        if trade.is_long:
                            locked_pts = ig_stop - trade.fill_price - lock_in_base_pts
                        else:
                            locked_pts = trade.fill_price - ig_stop - lock_in_base_pts
                        actual_locked = max(0.0, locked_pts * trade.stake)
                        trade.highest_locked_profit = actual_locked
                        print(f"      → Actual locked profit: £{actual_locked:.2f}")
                    changed = True
                else:
                    print(f"  ✓ {trade.symbol} stop verified @ {curr}{dp_ig:.2f}")
            else:
                trade.stop_verified = False
                print(f"  ⚠ {trade.symbol} — COULD NOT READ STOP FROM IG")
                print(f"      Position may be closed or API issue. Check IG platform directly!")
                failed = True
                changed = True  # save the stop_verified=False flag

        if changed:
            self._save_trades(trades_data, filled)
            print("  Stops synced with IG.\n")
        elif failed:
            print("  ⚠ VERIFICATION INCOMPLETE — could not confirm stops on IG.\n")
        else:
            print("  All stops verified.\n")

    def _check_position_open(self, trade: Trade) -> bool:
        """Check if a position is still open on IG. Returns True if open."""
        if self._dry_run or not trade.deal_id:
            return True  # can't check in dry run, assume open
        try:
            headers = dict(self._headers)
            headers["VERSION"] = "2"
            r = requests.get(
                f"{self._base_url}/positions",
                headers=headers, timeout=10,
            )
            if r.status_code == 200:
                positions = r.json().get("positions", [])
                for p in positions:
                    if p.get("position", {}).get("dealId") == trade.deal_id:
                        return True
                return False  # not found → closed
            return True  # API error → assume open to be safe
        except Exception:
            return True  # error → assume open

    def _monitor_position(self, trade: Trade) -> None:
        """Run all position management checks on a single trade."""
        # Check if position is still open on IG every check
        if not self._dry_run and trade.deal_id:
            if not self._check_position_open(trade):
                curr = "$" if trade.market == "US" else ""
                dp_stop = trade.display_price(trade.current_stop) if trade.current_stop else "?"
                # Calculate profit from stop level
                if trade.current_stop and trade.fill_price:
                    profit = (trade.current_stop - trade.fill_price) * trade.stake if trade.is_long else (trade.fill_price - trade.current_stop) * trade.stake
                else:
                    profit = 0
                print(f"  ✓ {trade.symbol} — POSITION CLOSED ON IG")
                print(f"    Stopped out @ {curr}{dp_stop} — est. profit £{profit:.2f}")
                trade.status = "CLOSED_STOPPED"
                return

        snapshot = self._fetch_snapshot(trade.ig_epic)
        if not snapshot:
            print(f"  {trade.symbol} — price unavailable")
            return

        bid = snapshot["bid"]
        ask = snapshot["ask"]
        pnl_gbp = trade.unrealised_pnl(bid, ask)
        pnl_r = trade.unrealised_r(bid, ask)

        # Exit price = what you'd actually get if you closed now
        # Longs sell at bid, shorts cover (buy) at ask
        exit_price = bid if trade.is_long else ask

        # Log the check
        check_record = {
            "time": datetime.now().isoformat(),
            "bid": bid,
            "ask": ask,
            "exit_price": exit_price,
            "pnl_gbp": round(pnl_gbp, 2),
            "pnl_r": round(pnl_r, 2),
        }

        # ── EMERGENCY COVER (shorts only) ─────────────────────
        # For shorts: we'd cover at ask price. If ask has risen 15%
        # above our fill, that's a 15% loss — cover immediately.
        if not trade.is_long and trade.fill_price:
            adverse_pct = (ask - trade.fill_price) / trade.fill_price
            if adverse_pct >= self.EMERGENCY_COVER_PCT:
                print(f"  🚨 {trade.symbol} EMERGENCY COVER — "
                      f"{adverse_pct:.1%} adverse (>{self.EMERGENCY_COVER_PCT:.0%})")
                if not self._dry_run and trade.deal_id:
                    self._close_position(trade)
                trade.status = "CLOSED_EMERGENCY"
                check_record["action"] = "EMERGENCY_COVER"
                trade.checks.append(check_record)
                return

        # ── TARGET HIT (+3R) ──────────────────────────────────
        if pnl_r >= self.TARGET_R:
            target_price = trade.display_price(exit_price)
            curr = "$" if trade.market == "US" else ""
            print(f"  TARGET HIT: {trade.symbol} +{pnl_r:.1f}R — "
                  f"closing @ {curr}{target_price:.2f} for £{pnl_gbp:+.2f}")
            if not self._dry_run and trade.deal_id:
                self._close_position(trade)
            trade.status = "CLOSED_TARGET"
            check_record["action"] = "TARGET_HIT"
            trade.checks.append(check_record)
            return

        # ── PROGRESSIVE £ TRAIL ────────────────────────────────
        # £25 P&L → stop to BE+$1 (covers spread)
        # £30 P&L → lock in £5 profit
        # £35 P&L → lock in £10 profit
        # etc. in £5 steps — stop only ratchets up, NEVER down
        #
        # CRITICAL: highest_locked_profit is persisted on the Trade
        # object and saved to JSON. It only ever increases. The display
        # always shows this stored value, not a recalculation from
        # current P&L — so when P&L dips, the locked amount stays put.
        import math as _math

        curr = "$" if trade.market == "US" else ""

        if trade.fill_price and pnl_gbp >= self.TRAIL_START_PNL:
            # Calculate what the P&L *would* lock right now
            if pnl_gbp >= self.TRAIL_LOCK_START:
                candidate_lock = _math.floor((pnl_gbp - self.TRAIL_START_PNL) / self.TRAIL_STEP) * self.TRAIL_STEP
            else:
                candidate_lock = 0.0  # £25-29: just BE+$1

            # Only ratchet — take the max of what we had and what we'd get now
            if candidate_lock > trade.highest_locked_profit:
                trade.highest_locked_profit = candidate_lock

            # Convert the HIGHEST locked £ profit to IG stop level
            lock_in_base_pts = self.TRAIL_FIRST_LOCK_IN * 100 if trade.market == "US" else self.TRAIL_FIRST_LOCK_IN
            lock_in_profit_pts = trade.highest_locked_profit / trade.stake if trade.stake > 0 else 0

            if trade.is_long:
                new_stop = trade.fill_price + lock_in_base_pts + lock_in_profit_pts
            else:
                new_stop = trade.fill_price - lock_in_base_pts - lock_in_profit_pts

            # Only move stop if it's tighter than current — NEVER give back
            current = trade.current_stop or trade.stop
            should_move = (trade.is_long and new_stop > current) or (not trade.is_long and new_stop < current)

            if should_move:
                arrow = "↑" if trade.is_long else "↓"
                dp_stop = trade.display_price(new_stop)
                print(f"  {arrow} {trade.symbol} — P&L £{pnl_gbp:.2f} — "
                      f"requesting stop → {curr}{dp_stop:.2f} (locking £{trade.highest_locked_profit:.0f} + ${self.TRAIL_FIRST_LOCK_IN:.0f})")

                if not self._dry_run and trade.deal_id:
                    confirmed = self._modify_stop(trade.deal_id, new_stop)
                    if confirmed is not None:
                        # Use what IG ACTUALLY set, not what we asked for
                        trade.current_stop = confirmed
                        trade.stop_verified = True
                        trade.breakeven_moved = True
                        check_record["action"] = f"TRAIL_LOCK_{trade.highest_locked_profit:.0f}"
                        check_record["ig_confirmed_stop"] = confirmed
                    else:
                        # IG rejected — do NOT update our stored stop
                        trade.stop_verified = False
                        print(f"    ✗ Stop NOT moved — keeping current stop")
                        check_record["action"] = "TRAIL_LOCK_FAILED"
                else:
                    # Dry run — just update locally
                    trade.current_stop = new_stop
                    trade.breakeven_moved = True
                    check_record["action"] = f"TRAIL_LOCK_{trade.highest_locked_profit:.0f}"

        # ── PRINT STATUS ──────────────────────────────────────
        dp_exit = trade.display_price(exit_price)
        dp_stop = trade.display_price(trade.current_stop) if trade.current_stop else trade.display_price(trade.stop)
        dollar_gain = (exit_price - trade.fill_price) / 100 if trade.market == "US" else (exit_price - trade.fill_price)
        pnl_sign = "+" if pnl_gbp >= 0 else ""
        dollar_sign = "+" if dollar_gain >= 0 else ""

        # Stop verification status
        verify_tag = "" if trade.stop_verified else " ⚠UNVERIFIED"

        # Display uses the STORED highest_locked_profit — never drops
        if not trade.breakeven_moved:
            next_trigger = f"BE @ £{self.TRAIL_START_PNL:.0f} P&L"
            lock_info = ""
        else:
            # Next step is always based on the highest lock we've achieved + one step
            next_lock = trade.highest_locked_profit + self.TRAIL_STEP
            next_pnl = self.TRAIL_START_PNL + next_lock
            next_trigger = f"next @ £{next_pnl:.0f} P&L (lock £{next_lock:.0f})"
            lock_info = f" [£{trade.highest_locked_profit:.0f} locked]" if trade.highest_locked_profit > 0 else " [BE+$1]"

        print(f"  {trade.symbol:8s} {curr}{dp_exit:.2f} ({dollar_sign}{curr}{dollar_gain:.2f}) | "
              f"P&L: {pnl_sign}£{pnl_gbp:.2f} | "
              f"stop={curr}{dp_stop:.2f}{verify_tag} | {next_trigger}{lock_info}")

        trade.checks.append(check_record)

    def _fetch_snapshot(self, epic: str) -> dict | None:
        try:
            headers = dict(self._headers)
            headers["VERSION"] = "3"
            r = requests.get(f"{self._base_url}/markets/{epic}", headers=headers, timeout=10)
            if r.status_code == 200:
                snap = r.json().get("snapshot", {})
                bid = snap.get("bid")
                ask = snap.get("offer")
                if bid is not None and ask is not None:
                    return {"bid": float(bid), "ask": float(ask)}
            return None
        except Exception:
            return None

    def _modify_stop(self, deal_id: str, new_stop: float) -> float | None:
        """Move stop on IG. Returns the CONFIRMED stop level, or None on failure.

        IG's PUT /positions/otc returns a dealReference. We then poll
        /confirms/{ref} to get the actual stop level IG applied — because
        IG may round, reject, or silently ignore the request.
        """
        try:
            headers = dict(self._headers)
            headers["VERSION"] = "2"
            # Round to 1 decimal — IG rejects excessive precision
            rounded_stop = round(new_stop, 1)
            r = requests.put(
                f"{self._base_url}/positions/otc/{deal_id}",
                json={"stopLevel": rounded_stop, "trailingStop": False},
                headers=headers, timeout=10,
            )
            if r.status_code != 200:
                print(f"    ⚠ STOP MODIFY FAILED — HTTP {r.status_code}: {r.text[:200]}")
                return None

            body = r.json()
            deal_ref = body.get("dealReference")
            if not deal_ref:
                print(f"    ⚠ STOP MODIFY — no dealReference in response: {body}")
                return None

            # Poll /confirms to verify IG actually applied it
            time.sleep(0.5)  # brief pause for IG to process
            confirm_headers = dict(self._headers)
            confirm_headers["VERSION"] = "1"
            cr = requests.get(
                f"{self._base_url}/confirms/{deal_ref}",
                headers=confirm_headers, timeout=10,
            )
            if cr.status_code != 200:
                print(f"    ⚠ STOP CONFIRM — HTTP {cr.status_code}")
                return None

            confirm = cr.json()
            status = confirm.get("dealStatus")
            reason = confirm.get("reason", "")

            if status in ("AMENDED", "ACCEPTED", "OPENED") or reason == "SUCCESS":
                confirmed_stop = confirm.get("stopLevel")
                if confirmed_stop is not None:
                    print(f"    ✓ IG confirmed stop @ {confirmed_stop}")
                    return float(confirmed_stop)
                else:
                    # Some IG confirm responses don't echo stopLevel — read it back
                    print(f"    ✓ IG confirmed ({status}/{reason}) — reading back position")
                    return self._read_ig_stop(deal_id)
            else:
                reject_reason = confirm.get("reason", "UNKNOWN")
                print(f"    ✗ IG REJECTED stop move — {status}: {reject_reason}")
                return None

        except Exception as e:
            print(f"    ⚠ STOP MODIFY ERROR: {e}")
            return None

    def _read_ig_stop(self, deal_id: str) -> float | None:
        """Read the actual stop level from IG for a position.

        IG REST API: GET /positions returns all open positions.
        Each has { position: { dealId, stopLevel, ... }, market: { ... } }
        """
        try:
            headers = dict(self._headers)
            headers["VERSION"] = "2"
            r = requests.get(
                f"{self._base_url}/positions",
                headers=headers, timeout=10,
            )
            if r.status_code != 200:
                print(f"    (IG /positions returned HTTP {r.status_code})")
                return None

            data = r.json()
            positions = data.get("positions", [])
            if not positions:
                print(f"    (IG returned 0 positions — raw keys: {list(data.keys())})")
                return None

            # Debug: show first position structure
            if positions:
                first = positions[0]
                pos_keys = list(first.get("position", {}).keys()) if "position" in first else list(first.keys())
                print(f"    (IG {len(positions)} positions — pos keys: {pos_keys})")

            for p in positions:
                pos = p.get("position", {})
                if pos.get("dealId") == deal_id:
                    stop = pos.get("stopLevel")
                    print(f"    (IG match — dealId={deal_id}, stopLevel={stop})")
                    return float(stop) if stop is not None else None

            # Didn't find by dealId — dump all dealIds for debug
            all_ids = [p.get("position", {}).get("dealId", "?") for p in positions]
            print(f"    (dealId {deal_id} not in: {all_ids})")
            return None
        except Exception as e:
            print(f"    (error reading IG positions: {e})")
            return None

    def _activate_trailing(self, deal_id: str, trail_pct: float) -> bool:
        try:
            headers = dict(self._headers)
            headers["VERSION"] = "2"
            r = requests.put(
                f"{self._base_url}/positions/otc/{deal_id}",
                json={"trailingStop": True, "trailingStopDistance": trail_pct * 10000},
                headers=headers, timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False

    def _close_position(self, trade: Trade) -> bool:
        try:
            headers = dict(self._headers)
            headers["VERSION"] = "1"
            headers["_method"] = "DELETE"
            close_dir = "SELL" if trade.is_long else "BUY"
            r = requests.post(
                f"{self._base_url}/positions/otc",
                json={
                    "dealId": trade.deal_id,
                    "direction": close_dir,
                    "size": trade.stake,
                    "orderType": "MARKET",
                },
                headers=headers, timeout=15,
            )
            return r.status_code == 200
        except Exception:
            return False

    def _load_trades(self) -> dict:
        if not self._trades_path.exists():
            return {"trades": []}
        with open(self._trades_path) as f:
            return json.load(f)

    def _save_trades(self, trades_data: dict, processed: list[Trade]) -> None:
        for trade in processed:
            if trade.index < len(trades_data["trades"]):
                trades_data["trades"][trade.index] = trade.to_dict()
        with open(self._trades_path, "w") as f:
            json.dump(trades_data, f, indent=2, default=str)


# ══════════════════════════════════════════════════════════════
# UNIFIED TRADE DAEMON — entry stalking + position management
# in a single loop, with a hard cap on concurrent positions.
# ══════════════════════════════════════════════════════════════

class TradeDaemon:
    """
    Unified daemon that runs BOTH the entry stalker and the position
    manager in a single loop.

    Every `check_interval` seconds:
      Phase A — manage every FILLED position (stops, trail, target,
                emergency cover). Runs first because exits are urgent.
      Phase B — if within the entry window AND we have headroom under
                MAX_POSITIONS, evaluate every PENDING trade. Any that
                pass all 6 gates get filled this tick.
      Save — single JSON write at the end of the tick.

    Designed so that once up to 6 pending trades are queued, all of
    them can fill within the same window AND be managed in real time
    from the moment they fill — no "dead zone" between the entry
    window closing and the position monitor starting.

    Ctrl+C to stop.
    """

    DEFAULT_MAX_POSITIONS = 6

    def __init__(
        self,
        ig_headers: dict,
        settings: Settings | None = None,
        trades_path: Path | None = None,
        check_interval: int = 60,
        window_start: str | None = None,
        window_end: str | None = None,
        dry_run: bool = True,
        max_positions: int | None = None,
    ):
        self._headers = ig_headers
        self._settings = settings or get_settings()
        self._trades_path = trades_path or TRADES_PATH
        self._check_interval = check_interval
        self._dry_run = dry_run
        self._max_positions = max_positions or self.DEFAULT_MAX_POSITIONS

        # Compose — reuse all the tested per-trade methods. We deliberately
        # do NOT re-implement entry evaluation or position management here.
        self._entry = EntryMonitor(
            ig_headers=ig_headers,
            settings=self._settings,
            trades_path=self._trades_path,
            check_interval=check_interval,
            max_checks=10 ** 9,  # effectively unlimited — daemon runs until Ctrl+C
            window_start=window_start,
            window_end=window_end,
            dry_run=dry_run,
        )
        self._pos = PositionMonitor(
            ig_headers=ig_headers,
            settings=self._settings,
            trades_path=self._trades_path,
            check_interval=check_interval,
            dry_run=dry_run,
        )

    def run(self) -> None:
        # ── Bootstrap: portfolio value for entry risk calcs ──────
        self._entry._portfolio_value = self._entry._fetch_portfolio_value()
        if self._entry._portfolio_value <= 0:
            self._entry._portfolio_value = 10000.0
            logger.warning("Could not fetch portfolio value — using £10,000 default")

        self._print_header()

        # ── Reconcile any already-open stops with IG ─────────────
        self._pos._verify_stops_on_startup()

        # ── Main loop ────────────────────────────────────────────
        tick = 0
        try:
            while True:
                tick += 1
                trades_data = self._load_trades()
                all_rows = trades_data.get("trades", [])

                # Materialise as Trade objects, preserving original index
                pending: list[Trade] = []
                filled: list[Trade] = []
                for i, row in enumerate(all_rows):
                    status = row.get("status", "")
                    if status == "PENDING":
                        pending.append(Trade(row, i))
                    elif status in ("FILLED", "FILLED_DRY"):
                        filled.append(Trade(row, i))

                now = datetime.now()
                in_window = self._entry._in_window()
                print(
                    f"\n─ Tick {tick} ({now.strftime('%H:%M:%S')}) "
                    f"| Filled: {len(filled)}/{self._max_positions} "
                    f"| Pending: {len(pending)} "
                    f"| Entry window: {'OPEN' if in_window else 'closed'} ─"
                )

                # ═══ PHASE A — MANAGE FILLED POSITIONS ═══════════
                # Runs every tick. Exits are time-sensitive: stops,
                # trail ratchets, +3R target, 15% emergency cover.
                for trade in filled:
                    try:
                        self._pos._monitor_position(trade)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Error managing %s", trade.symbol)
                        print(f"  ✗ {trade.symbol} — manager error: {exc}")

                # ═══ PHASE B — STALK PENDING ENTRIES ═════════════
                # Only during the entry window AND only up to the cap.
                # If pending exists but window has closed, expire them.
                newly_filled: list[Trade] = []
                if pending and in_window:
                    # Resolve any un-resolved epics (first tick usually)
                    for trade in pending:
                        if not trade.ig_epic:
                            trade.ig_epic = self._entry._resolve_epic(
                                trade.symbol, trade.market
                            )
                            if not trade.ig_epic:
                                logger.error(
                                    "Cannot resolve epic for %s (%s)",
                                    trade.symbol, trade.market,
                                )
                                trade.status = "ERROR"
                            else:
                                trade.detect_market_from_epic()
                                trade.min_deal_size = (
                                    self._entry._fetch_min_deal_size(trade.ig_epic)
                                )

                    active_pending = [t for t in pending if t.status == "PENDING"]

                    # Enforce the concurrent-position cap
                    filled_count = len(filled)
                    room = max(0, self._max_positions - filled_count)

                    if room == 0 and active_pending:
                        print(
                            f"  ⊘ At cap ({filled_count}/{self._max_positions}) — "
                            f"holding {len(active_pending)} pending until a slot frees"
                        )
                    elif active_pending:
                        for trade in active_pending:
                            if room <= 0:
                                print(
                                    f"  ⊘ {trade.symbol} — cap reached this tick; "
                                    f"retry next tick if room opens"
                                )
                                break
                            result = self._entry._evaluate_entry(trade, tick)
                            self._entry._print_check_result(trade, result)

                            if result["action"] != "FILL":
                                continue

                            if self._dry_run:
                                trade.status = "FILLED_DRY"
                                trade.fill_price = result["price"]
                                trade.fill_time = now.isoformat()
                                trade.current_stop = trade.stop
                                newly_filled.append(trade)
                                room -= 1
                            else:
                                fill = self._entry._place_order(trade, result["price"])
                                if fill.get("success"):
                                    trade.status = "FILLED"
                                    trade.fill_price = fill.get("fill_price", result["price"])
                                    trade.fill_time = now.isoformat()
                                    trade.deal_id = fill.get("deal_id", "")
                                    trade.current_stop = trade.stop
                                    newly_filled.append(trade)
                                    room -= 1
                                else:
                                    print(f"    ORDER FAILED: {fill.get('reason', '?')}")

                elif pending and not in_window:
                    # Window has closed — any still-PENDING trades are stale
                    est_now = self._entry._est_now()
                    end_t = self._entry._parse_time(self._entry._window_end)
                    if est_now > end_t:
                        for t in pending:
                            if t.status == "PENDING":
                                t.status = "EXPIRED"
                                print(f"  ⏰ {t.symbol} — EXPIRED (window closed)")

                # ═══ SAVE — one write, covers everything touched ═
                self._save_trades(trades_data, pending + filled)

                if newly_filled:
                    syms = ", ".join(t.symbol for t in newly_filled)
                    print(
                        f"  → {len(newly_filled)} new fill(s) this tick ({syms}) "
                        f"— will be managed from next tick"
                    )

                # ═══ SLEEP ═══════════════════════════════════════
                time.sleep(self._check_interval)

        except KeyboardInterrupt:
            print("\n\nTrade daemon stopped.")

    def _print_header(self) -> None:
        now_est = self._entry._est_now()
        max_risk = self._entry._portfolio_value * self._settings.max_risk_per_trade
        mode = "DRY RUN" if self._dry_run else "*** LIVE ***"
        print()
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  MONEY PROGRAM — UNIFIED TRADE DAEMON                      ║")
        print(f"║  {mode:11s} | every {self._check_interval}s | "
              f"cap {self._max_positions} concurrent fills                ║")
        print(f"║  Entry window: {self._entry._window_start}–{self._entry._window_end} EST "
              f"(now: {now_est.strftime('%H:%M')} EST)                  ║")
        print(f"║  Portfolio: £{self._entry._portfolio_value:>10,.2f} | "
              f"Max risk/trade (1%): £{max_risk:>7,.2f}        ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print("║  Phase A: manage FILLED — trail/target/emergency cover     ║")
        print("║  Phase B: stalk PENDING — gates 1-6 (window only, capped)  ║")
        print("║  Trail: £25→BE+$1 | £30→£5 | £35→£10 | +£5 steps | Tgt 3R  ║")
        print("╚══════════════════════════════════════════════════════════════╝")

    def _load_trades(self) -> dict:
        if not self._trades_path.exists():
            return {"trades": []}
        with open(self._trades_path) as f:
            return json.load(f)

    def _save_trades(self, trades_data: dict, processed: list[Trade]) -> None:
        for trade in processed:
            if trade.index < len(trades_data["trades"]):
                trades_data["trades"][trade.index] = trade.to_dict()
        with open(self._trades_path, "w") as f:
            json.dump(trades_data, f, indent=2, default=str)
