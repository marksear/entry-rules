"""
Audit entry — the full JSON log schema from Masterclass v2 §8.5.
Every decision is logged with complete context.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ..config.rejection_codes import RejectCode
from .common import Decision, Direction, EntryType, Market
from ..utils.time_utils import utc_now


class AuditGates(BaseModel):
    trend_template: bool | None = None
    adx: float | None = None
    volume_gate: int | None = None  # dry-up count or distribution count
    squeeze_check: str | None = None  # PASS / FAIL / N/A
    gap_classified: bool = False
    risk_budget_ok: bool | None = None


class AuditLevels(BaseModel):
    pivot_or_breakdown: float | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    risk_per_share: float | None = None
    position_size: int | None = None
    risk_amount: float | None = None
    portfolio_risk_pct: float | None = None


class AuditVolume(BaseModel):
    trigger_volume: int | None = None
    avg_50d_volume: int | None = None
    volume_ratio: float | None = None


class AuditTranche(BaseModel):
    tranche_1_size: int | None = None
    tranche_2_eligible: bool = False
    tranche_2_size: int | None = None


class AuditShortSpecific(BaseModel):
    short_interest_pct: float | None = None
    days_to_cover: float | None = None
    borrow_fee_annual: float | None = None
    borrow_available: bool | None = None


class AuditUKSpecific(BaseModel):
    spread_pct: float | None = None
    stamp_duty_applied: bool = False
    cfd_used: bool = False


class AuditEntry(BaseModel):
    """Complete audit log entry — one per decision."""

    timestamp: datetime = Field(default_factory=utc_now)
    signal_id: str
    ticker: str
    market: Market
    direction: Direction
    entry_type: EntryType
    decision: Decision
    reason_code: RejectCode | None = None
    reason_detail: str = ""

    gates: AuditGates = Field(default_factory=AuditGates)
    levels: AuditLevels = Field(default_factory=AuditLevels)
    volume: AuditVolume = Field(default_factory=AuditVolume)
    tranche: AuditTranche = Field(default_factory=AuditTranche)
    short_specific: AuditShortSpecific | None = None
    uk_specific: AuditUKSpecific | None = None

    # IG-specific
    ig_deal_reference: str | None = None
    ig_deal_id: str | None = None
