"""
Order instruction — the output of the entry classifier,
ready for the broker module to translate into API calls.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .common import Direction, EntryType, Market


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class Tranche(BaseModel):
    """A single tranche of a position."""

    number: int  # 1 or 2
    size: int  # Number of shares/contracts
    order_type: OrderType
    trigger_price: float | None = None  # For stop orders
    limit_price: float | None = None  # For limit/stop-limit orders
    placed: bool = False
    fill_price: float | None = None
    ig_deal_id: str | None = None


class OrderInstruction(BaseModel):
    """
    Complete instruction for entering a trade.
    The broker module translates this into IG API calls.
    """

    signal_id: str
    ticker: str
    ig_epic: str
    market: Market
    direction: Direction
    entry_type: EntryType

    # Levels
    entry_price: float
    stop_price: float
    risk_per_share: float = Field(description="abs(entry - stop)")

    # Position sizing
    total_shares: int
    tranche_1: Tranche
    tranche_2: Tranche  # May be blocked if T1 goes underwater

    # Risk
    risk_amount: float = Field(description="risk_per_share × total_shares")
    portfolio_risk_pct: float

    # UK adjustments
    spread_pct: float | None = None
    spread_adjusted: bool = False
    stamp_duty_applies: bool = False
    use_cfd: bool = False

    # Guaranteed stop
    use_guaranteed_stop: bool = False

    @property
    def is_uk(self) -> bool:
        return self.market == Market.UK
