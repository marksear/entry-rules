"""
Core enums and the TradeSignal model — the input to the entry refinement engine.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field
from ..utils.time_utils import utc_now


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Market(str, Enum):
    US = "US"
    UK = "UK"


class EntryType(str, Enum):
    """Masterclass v2 entry type taxonomy."""

    # Long
    L_A = "L-A"  # VCP Breakout
    L_B = "L-B"  # First Pullback to EMA
    L_C = "L-C"  # Buyable Gap Up (BGU)
    L_D = "L-D"  # Pocket Pivot
    L_E = "L-E"  # Secondary Reaction Re-Entry

    # Short
    S_A = "S-A"  # Head & Shoulders Breakdown
    S_B = "S-B"  # Rally into Resistance (First Bounce to EMA)
    S_C = "S-C"  # Shortable Gap Down (SGD)
    S_D = "S-D"  # Climax Top Reversal
    S_E = "S-E"  # Failed Breakout Short

    @property
    def direction(self) -> Direction:
        return Direction.LONG if self.value.startswith("L") else Direction.SHORT

    @property
    def display_name(self) -> str:
        return _ENTRY_NAMES[self]


_ENTRY_NAMES: dict[EntryType, str] = {
    EntryType.L_A: "VCP Breakout",
    EntryType.L_B: "First Pullback to EMA",
    EntryType.L_C: "Buyable Gap Up",
    EntryType.L_D: "Pocket Pivot",
    EntryType.L_E: "Secondary Reaction",
    EntryType.S_A: "H&S Breakdown",
    EntryType.S_B: "Rally into Resistance",
    EntryType.S_C: "Shortable Gap Down",
    EntryType.S_D: "Climax Top Reversal",
    EntryType.S_E: "Failed Breakout Short",
}


class Decision(str, Enum):
    ENTER = "ENTER"
    SKIP = "SKIP"
    REJECT = "REJECT"
    MONITOR = "MONITOR"  # Gap reclaim window


class TradeSignal(BaseModel):
    """
    Input from the signal engine. This is what arrives before
    the entry refinement engine takes over.
    """

    signal_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    ticker: str
    market: Market
    direction: Direction
    entry_type: EntryType
    pivot_level: float = Field(description="Pivot buy point or breakdown level")
    base_stage: int = Field(default=1, ge=1, description="Which base/top (1st, 2nd, 3rd+)")
    ig_epic: str = Field(default="", description="IG market epic, resolved at runtime if empty")
    notes: str = ""
