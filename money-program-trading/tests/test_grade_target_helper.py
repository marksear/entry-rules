"""
Unit tests for the grade-scaled hard-target helper (2026-04-21).

``get_hard_target_gbp`` is the single source of truth for the £ hard
cap that both the trail ladder (peak P&L ≥ target → EXIT) and the
broker's limit-on-open path consume. Divergence between the two
would let a position sit open past its broker-enforced limit —
regression against the DEMO Day-1 failure mode.
"""

from __future__ import annotations

import pytest

from src.engine.trail_manager import (
    GRADE_TARGET_GBP,
    ExitConfig,
    get_hard_target_gbp,
)
from src.models.log_enums import CandidateGrade


pytestmark = pytest.mark.unit


CFG = ExitConfig()  # defaults; trail_hard_target_gbp = 50.0


def test_a_plus_uses_sixty_two_fifty():
    assert get_hard_target_gbp(CandidateGrade.A_PLUS, CFG) == pytest.approx(62.50)
    assert get_hard_target_gbp("A+", CFG) == pytest.approx(62.50)


def test_a_uses_fifty():
    assert get_hard_target_gbp(CandidateGrade.A, CFG) == pytest.approx(50.00)
    assert get_hard_target_gbp("A", CFG) == pytest.approx(50.00)


def test_b_uses_fifty():
    assert get_hard_target_gbp(CandidateGrade.B, CFG) == pytest.approx(50.00)


def test_c_uses_fifty_mechanics_test_bypass_matches_b():
    # Grade-C is DEMO bypass only — treated as B-equivalent for the cap so
    # the mechanics-test path exercises the same broker-enforced limit
    # flow as a real B trade would.
    assert get_hard_target_gbp(CandidateGrade.C, CFG) == pytest.approx(50.00)


def test_unknown_grade_falls_back_to_config_scalar():
    """When the mapping doesn't have the grade (future grade, typo,
    bad data), fall back to the ExitConfig scalar so the system stays
    up rather than raises."""
    assert get_hard_target_gbp("X", CFG) == pytest.approx(CFG.trail_hard_target_gbp)
    assert get_hard_target_gbp(None, CFG) == pytest.approx(CFG.trail_hard_target_gbp)


def test_mapping_exposes_all_production_grades():
    """Guard the mapping keyset — if someone removes A+ by accident,
    every A+ trade silently regresses to the £50 fallback."""
    assert {"A+", "A", "B", "C"} <= set(GRADE_TARGET_GBP.keys())


def test_custom_config_scalar_changes_fallback_not_known_grades():
    tuned = ExitConfig(trail_hard_target_gbp=100.0)
    # Known grade ignores the scalar:
    assert get_hard_target_gbp(CandidateGrade.A, tuned) == pytest.approx(50.00)
    # Unknown grade falls back to whatever the scalar is:
    assert get_hard_target_gbp("?", tuned) == pytest.approx(100.00)
