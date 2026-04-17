"""
Spread bet sizing utilities.

In spread betting, position size is expressed as £ per point (GBP per point
of price movement), not number of shares.

For UK equities: 1 point = 1 penny, so £1/point = 100 shares equivalent.
For US equities: 1 point = 1 cent (USD), so £1/point ≈ 1 share at $1 move.

The risk calculation is:
    risk_per_point = entry_price - stop_price  (in points)
    stake_per_point = risk_budget / risk_per_point

Example:
    Entry: 150.00, Stop: 140.00 → risk = 10.00 points (1000 pence)
    Portfolio: £10,000, 1% risk = £100
    Stake: £100 / 1000 = £0.10 per point

    If price moves from 150 to 160 (1000 points up):
    Profit = £0.10 × 1000 = £100

For IG spread bets:
    - Size is specified in the 'size' field of the order
    - For shares: size = £ per point (1 point = 1 penny for UK, 1 cent for US)
    - Currency of the spread bet is always GBP (your account currency)
    - P&L is calculated in GBP regardless of underlying market
"""

from __future__ import annotations

import math


def calculate_spread_bet_size(
    entry_price: float,
    stop_price: float,
    risk_budget_gbp: float,
    market: str = "UK",
) -> float:
    """
    Calculate spread bet stake (£ per point).

    Args:
        entry_price: Entry price level
        stop_price: Stop-loss price level
        risk_budget_gbp: Maximum risk in GBP (e.g., 1% of portfolio)
        market: "UK" or "US"

    Returns:
        Stake in £ per point. For IG, this goes in the 'size' field.

    Note on point values:
        UK equities: prices quoted in pence, 1 point = 1p
        US equities: prices quoted in cents, 1 point = 1c
        The risk_per_point is already in the right units because
        IG quotes spread bet prices in the same units as the market.
    """
    risk_per_point = abs(entry_price - stop_price)

    if risk_per_point <= 0:
        return 0.0

    # For US equities on IG spread bet, P&L is auto-converted to GBP
    # but the point value is in USD. We need the GBP/USD rate for
    # precise sizing. For now, use a conservative approximation.
    if market == "US":
        # Approximate GBP/USD conversion (updated at runtime)
        # £1/point on a US stock ≈ $1.25/point at current rates
        # So our GBP risk budget buys slightly more exposure
        # For safety, we don't adjust — IG handles the conversion
        pass

    stake = risk_budget_gbp / risk_per_point

    # IG has minimum stake sizes — typically £0.10/point for shares
    min_stake = 0.10
    if stake < min_stake:
        return 0.0  # Can't meet minimum — signal the position is too small

    # Round down to nearest £0.01 per point
    return math.floor(stake * 100) / 100


def calculate_spread_bet_risk(
    stake_per_point: float,
    entry_price: float,
    stop_price: float,
) -> float:
    """
    Calculate actual risk in GBP for a spread bet position.

    Args:
        stake_per_point: £ per point
        entry_price: Entry level
        stop_price: Stop level

    Returns:
        Risk in GBP
    """
    risk_per_point = abs(entry_price - stop_price)
    return stake_per_point * risk_per_point


def calculate_overnight_gap_stake(
    entry_price: float,
    gap_pct: float,
    max_loss_gbp: float,
) -> float:
    """
    Maximum stake that keeps a gap loss within budget.

    Rule 11: 10% overnight gap = max 1% portfolio loss.

    Args:
        entry_price: Entry price
        gap_pct: Assumed gap size (0.10 = 10%)
        max_loss_gbp: Maximum acceptable loss in GBP

    Returns:
        Maximum stake in £/point
    """
    gap_points = entry_price * gap_pct
    if gap_points <= 0:
        return 0.0
    return math.floor((max_loss_gbp / gap_points) * 100) / 100
