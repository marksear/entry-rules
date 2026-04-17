"""
Risk Manager — position sizing, risk budget enforcement, continuous monitoring.

Hard limits from Masterclass v2 §6. The system cannot override them.
"""

from __future__ import annotations

import logging
import math

from ..config.rejection_codes import RejectCode
from ..config.settings import Settings, get_settings
from ..models.common import Direction, Market
from ..models.position import Position

logger = logging.getLogger(__name__)


class RiskResult:
    """Result of a risk assessment."""

    def __init__(
        self,
        approved: bool,
        shares: int = 0,
        tranche_1: int = 0,
        tranche_2: int = 0,
        risk_amount: float = 0.0,
        portfolio_risk_pct: float = 0.0,
        reject_code: RejectCode | None = None,
        reason: str = "",
        spread_adjusted: bool = False,
        use_cfd: bool = False,
        stamp_duty_applies: bool = False,
    ):
        self.approved = approved
        self.shares = shares
        self.tranche_1 = tranche_1
        self.tranche_2 = tranche_2
        self.risk_amount = risk_amount
        self.portfolio_risk_pct = portfolio_risk_pct
        self.reject_code = reject_code
        self.reason = reason
        self.spread_adjusted = spread_adjusted
        self.use_cfd = use_cfd
        self.stamp_duty_applies = stamp_duty_applies


class RiskManager:
    """
    Enforces all risk limits. No trade enters without passing through here.
    """

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()

    def calculate_position(
        self,
        entry_price: float,
        stop_price: float,
        direction: Direction,
        market: Market,
        portfolio_value: float,
        open_positions: list[Position],
        spread_pct: float | None = None,
        expected_hold_days: int | None = None,
    ) -> RiskResult:
        """
        Calculate position size and check all risk limits.

        This is the final gatekeeper. If it says no, the trade doesn't happen.
        """
        s = self._settings

        # ── Stop distance check ───────────────────────────────
        risk_per_share = abs(entry_price - stop_price)
        stop_distance_pct = risk_per_share / entry_price

        if stop_distance_pct > s.max_stop_distance:
            return RiskResult(
                approved=False,
                reject_code=RejectCode.R05,
                reason=f"Stop distance {stop_distance_pct:.1%} > {s.max_stop_distance:.0%}",
            )

        # ── Base position size (1% risk) ──────────────────────
        risk_budget = portfolio_value * s.max_risk_per_trade
        if risk_per_share <= 0:
            return RiskResult(
                approved=False,
                reject_code=RejectCode.R05,
                reason="Invalid stop distance (zero or negative)",
            )

        shares = math.floor(risk_budget / risk_per_share)

        # ── Maximum single position ───────────────────────────
        max_pct = (
            s.max_single_position_long
            if direction == Direction.LONG
            else s.max_single_position_short
        )
        max_position_value = portfolio_value * max_pct
        if shares * entry_price > max_position_value:
            shares = math.floor(max_position_value / entry_price)

        # ── Overnight gap sizing ──────────────────────────────
        # 10% overnight gap should cost max 1% of portfolio
        gap_risk_shares = math.floor(
            (portfolio_value * s.max_risk_per_trade)
            / (entry_price * s.overnight_gap_pct)
        )
        shares = min(shares, gap_risk_shares)

        # ── Total open risk check ─────────────────────────────
        total_open_risk = sum(pos.risk_amount for pos in open_positions)
        new_trade_risk = risk_per_share * shares

        if total_open_risk + new_trade_risk > portfolio_value * s.max_total_risk:
            return RiskResult(
                approved=False,
                reject_code=RejectCode.R10,
                reason=(
                    f"Portfolio risk {(total_open_risk + new_trade_risk)/portfolio_value:.1%} "
                    f"would exceed {s.max_total_risk:.0%} limit"
                ),
            )

        # ── Short exposure cap ────────────────────────────────
        if direction == Direction.SHORT:
            total_short_exposure = sum(
                pos.cost_basis for pos in open_positions
                if pos.direction == Direction.SHORT
            )
            new_exposure = shares * entry_price
            if total_short_exposure + new_exposure > portfolio_value * s.max_short_exposure:
                return RiskResult(
                    approved=False,
                    reject_code=RejectCode.R16,
                    reason=f"Short exposure would exceed {s.max_short_exposure:.0%} cap",
                )

        # ── UK spread adjustments ─────────────────────────────
        spread_adjusted = False
        use_cfd = False
        stamp_duty_applies = False

        if market == Market.UK and spread_pct is not None:
            if spread_pct > s.uk_spread_skip_threshold:
                return RiskResult(
                    approved=False,
                    reject_code=RejectCode.R06,
                    reason=f"UK spread {spread_pct:.2%} > {s.uk_spread_skip_threshold:.1%}",
                )

            if spread_pct > s.uk_spread_reduce_threshold:
                shares = math.floor(shares * s.uk_spread_reduction)
                spread_adjusted = True
                logger.info("UK spread adjustment: reduced to %d shares", shares)

        # Spread bet mode: no CFD, no stamp duty.
        # Spread bet handles both long and short on all markets.
        # Tax-free profits for UK residents.

        # ── Sector correlation check ──────────────────────────
        # If ≥ 3 positions in same sector, reduce to 75%
        # (caller must provide sector info on positions)
        # This is handled at orchestrator level

        # ── Minimum viable position ───────────────────────────
        if shares < 1:
            return RiskResult(
                approved=False,
                reject_code=RejectCode.R05,
                reason="Position size too small after all adjustments",
            )

        # ── Tranche split ─────────────────────────────────────
        tranche_1 = math.floor(shares * s.tranche_1_pct)
        tranche_2 = shares - tranche_1

        if tranche_1 < 1:
            tranche_1 = shares
            tranche_2 = 0

        actual_risk = risk_per_share * shares
        risk_pct = actual_risk / portfolio_value

        return RiskResult(
            approved=True,
            shares=shares,
            tranche_1=tranche_1,
            tranche_2=tranche_2,
            risk_amount=actual_risk,
            portfolio_risk_pct=risk_pct,
            spread_adjusted=spread_adjusted,
            use_cfd=use_cfd,
            stamp_duty_applies=stamp_duty_applies,
        )

    def check_emergency_cover(
        self, position: Position, current_price: float
    ) -> bool:
        """
        Rule S11: Auto-cover shorts at 15% adverse move.

        Returns True if position should be covered immediately.
        """
        if position.direction != Direction.SHORT:
            return False

        adverse_pct = position.unrealised_pnl_pct(current_price)
        if adverse_pct < -self._settings.emergency_cover_pct:
            logger.warning(
                "EMERGENCY COVER: %s is %.1f%% adverse (threshold: %.0f%%)",
                position.ticker,
                adverse_pct * 100,
                self._settings.emergency_cover_pct * 100,
            )
            return True
        return False

    def check_overnight_gap_compliance(
        self, position: Position, portfolio_value: float
    ) -> int | None:
        """
        Rule 11: Check if position size is compliant with overnight gap rule.

        Returns the number of shares to trim to, or None if compliant.
        """
        gap_risk = position.entry_price * self._settings.overnight_gap_pct * position.shares
        max_gap_risk = portfolio_value * self._settings.max_risk_per_trade

        if gap_risk > max_gap_risk:
            compliant_shares = math.floor(
                max_gap_risk / (position.entry_price * self._settings.overnight_gap_pct)
            )
            return max(compliant_shares, 0)
        return None
