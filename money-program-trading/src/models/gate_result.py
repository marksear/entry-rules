"""
Gate evaluation results — what each gate returned and why.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..config.rejection_codes import RejectCode
from .common import Decision


class GateDetail(BaseModel):
    """Detail for a single gate evaluation."""

    name: str
    passed: bool
    value: float | int | bool | None = None
    threshold: float | int | None = None
    detail: str = ""


class GateResult(BaseModel):
    """Aggregate result of all gates for a signal."""

    passed: bool
    decision: Decision
    reject_code: RejectCode | None = None
    gates: list[GateDetail] = []

    # Raw values for audit logging
    adx_value: float | None = None
    volume_dry_count: int | None = None
    distribution_count: int | None = None
    short_interest_pct: float | None = None
    days_to_cover: float | None = None
    borrow_fee: float | None = None

    @classmethod
    def reject(cls, code: RejectCode, gates: list[GateDetail]) -> GateResult:
        return cls(passed=False, decision=Decision.REJECT, reject_code=code, gates=gates)

    @classmethod
    def passed_all(cls, gates: list[GateDetail], **kwargs) -> GateResult:
        return cls(passed=True, decision=Decision.ENTER, gates=gates, **kwargs)
