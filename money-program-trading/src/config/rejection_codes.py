"""
Rejection reason codes — R01 through R19.
Every decision (enter, skip, reject) is logged with a code from this enum.
"""

from __future__ import annotations

from enum import Enum


class RejectCode(str, Enum):
    """Masterclass v2 rejection codes."""

    # Long rejections
    R01 = "R01"  # Trend Template failed (long)
    R02 = "R02"  # ADX below threshold
    R03 = "R03"  # No volume dry-up
    R04 = "R04"  # Late-stage exhaustion gap (long)
    R05 = "R05"  # Risk budget exceeded / stop distance > 8%
    R06 = "R06"  # Spread too wide (UK)
    R07 = "R07"  # Volume confirmation failed
    R08 = "R08"  # Opened > 3% past entry without gap
    R09 = "R09"  # Gap — awaiting reclaim
    R10 = "R10"  # Total portfolio risk at limit

    # Short rejections
    R11 = "R11"  # Inverse Trend Template failed (short)
    R12 = "R12"  # No distribution evidence
    R13 = "R13"  # Short squeeze risk (SI > 20%)
    R14 = "R14"  # Days to cover > 5
    R15 = "R15"  # Borrow unavailable or fee > 5%
    R16 = "R16"  # Short exposure cap reached (50%)
    R17 = "R17"  # Emergency cover — 15% adverse move
    R18 = "R18"  # Climax top conditions not met
    R19 = "R19"  # Early-stage gap down — possible shakeout

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self]

    @property
    def applies_to(self) -> str:
        """Returns 'LONG', 'SHORT', or 'BOTH'."""
        return _APPLIES_TO[self]


_DESCRIPTIONS: dict[RejectCode, str] = {
    RejectCode.R01: "Trend Template failed (long)",
    RejectCode.R02: "ADX below threshold",
    RejectCode.R03: "No volume dry-up in base",
    RejectCode.R04: "Late-stage exhaustion gap",
    RejectCode.R05: "Risk budget exceeded or stop distance > 8%",
    RejectCode.R06: "Spread too wide (UK)",
    RejectCode.R07: "Volume confirmation failed",
    RejectCode.R08: "Opened too far past entry without qualifying gap",
    RejectCode.R09: "Gap — awaiting reclaim (monitoring window)",
    RejectCode.R10: "Total portfolio risk at limit",
    RejectCode.R11: "Inverse Trend Template failed (short)",
    RejectCode.R12: "No distribution evidence",
    RejectCode.R13: "Short squeeze risk — SI > 20%",
    RejectCode.R14: "Days to cover > 5",
    RejectCode.R15: "Borrow unavailable or fee > 5% annual",
    RejectCode.R16: "Short exposure cap reached (50%)",
    RejectCode.R17: "Emergency cover — 15% adverse move",
    RejectCode.R18: "Climax top conditions not met",
    RejectCode.R19: "Early-stage gap down — possible shakeout",
}

_APPLIES_TO: dict[RejectCode, str] = {
    RejectCode.R01: "LONG",
    RejectCode.R02: "BOTH",
    RejectCode.R03: "LONG",
    RejectCode.R04: "LONG",
    RejectCode.R05: "BOTH",
    RejectCode.R06: "BOTH",
    RejectCode.R07: "BOTH",
    RejectCode.R08: "BOTH",
    RejectCode.R09: "BOTH",
    RejectCode.R10: "BOTH",
    RejectCode.R11: "SHORT",
    RejectCode.R12: "SHORT",
    RejectCode.R13: "SHORT",
    RejectCode.R14: "SHORT",
    RejectCode.R15: "SHORT",
    RejectCode.R16: "SHORT",
    RejectCode.R17: "SHORT",
    RejectCode.R18: "SHORT",
    RejectCode.R19: "SHORT",
}
