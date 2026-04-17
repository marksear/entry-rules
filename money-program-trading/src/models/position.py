"""
Position model — represents an open position being managed by the engine.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from .common import Direction, EntryType, Market


class Position(BaseModel):
    """An open position tracked by the engine."""

    deal_id: str
    signal_id: str
    ticker: str
    ig_epic: str
    market: Market
    direction: Direction
    entry_type: EntryType

    # Entry details
    entry_price: float
    entry_date: datetime
    shares: int

    # Stop management
    initial_stop: float
    current_stop: float
    trailing_stop_active: bool = False

    # Tranche tracking
    tranche_1_shares: int
    tranche_1_fill: float
    tranche_2_shares: int = 0
    tranche_2_fill: float | None = None
    tranche_2_placed: bool = False
    tranche_2_blocked: bool = False

    # Status
    is_pilot: bool = False  # Reduced due to volume uncertainty
    sector: str = ""

    @property
    def cost_basis(self) -> float:
        """Total cost at entry."""
        return self.entry_price * self.shares

    @property
    def risk_amount(self) -> float:
        """Current risk on this position."""
        return abs(self.entry_price - self.current_stop) * self.shares

    def unrealised_pnl(self, current_price: float) -> float:
        """P&L based on current market price."""
        if self.direction == Direction.LONG:
            return (current_price - self.entry_price) * self.shares
        else:
            return (self.entry_price - current_price) * self.shares

    def unrealised_pnl_pct(self, current_price: float) -> float:
        """P&L as percentage of entry."""
        if self.direction == Direction.LONG:
            return (current_price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - current_price) / self.entry_price
