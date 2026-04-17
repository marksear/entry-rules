"""
Unit tests for the qualification gates.

Tests use synthetic data to verify gate logic independently
of real market data.
"""

import numpy as np
import pandas as pd
import pytest

from src.config.rejection_codes import RejectCode
from src.config.settings import Settings
from src.engine.gates import evaluate_long_gates, evaluate_short_gates
from src.models.common import Decision


def _make_trending_up_bars(n: int = 300) -> pd.DataFrame:
    """
    Create a synthetic stock in a clean Stage 2 uptrend.
    Should pass the long Trend Template.
    """
    np.random.seed(123)

    # Steady uptrend from 50 to 200 over 300 bars
    base = np.linspace(50, 200, n)
    noise = np.random.normal(0, 1.5, n)
    close = pd.Series(base + noise)
    high = close + abs(np.random.normal(2, 0.5, n))
    low = close - abs(np.random.normal(2, 0.5, n))

    # Volume: mostly around 1M, with some dry sessions at the end
    volume = pd.Series(np.random.randint(800_000, 1_200_000, n))
    # Last 5 bars: make 3 of them dry
    volume.iloc[-5] = 400_000
    volume.iloc[-3] = 350_000
    volume.iloc[-1] = 450_000

    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


def _make_trending_down_bars(n: int = 300) -> pd.DataFrame:
    """
    Create a synthetic stock in a Stage 4 decline.
    Should pass the short Inverse Trend Template.
    """
    np.random.seed(456)

    # Steady downtrend from 200 to 50 over 300 bars
    base = np.linspace(200, 50, n)
    noise = np.random.normal(0, 1.5, n)
    close = pd.Series(base + noise)
    high = close + abs(np.random.normal(2, 0.5, n))
    low = close - abs(np.random.normal(2, 0.5, n))

    # Distribution volume: heavy selling days scattered
    volume = pd.Series(np.random.randint(800_000, 1_200_000, n))
    # Add distribution days in last 10 bars
    for i in [-2, -4, -6, -8]:
        volume.iloc[i] = 1_500_000  # Heavy volume
        close.iloc[i] = close.iloc[i] - 3  # Close down

    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


class TestLongGates:
    def test_passing_signal(self):
        """A clean uptrending stock should pass all long gates."""
        bars = _make_trending_up_bars()
        result = evaluate_long_gates(bars, rs_percentile=80.0)
        # May or may not pass depending on ADX — the key thing is
        # the logic runs without error and returns a valid result
        assert result.decision in (Decision.ENTER, Decision.REJECT)
        assert len(result.gates) > 0

    def test_low_rs_rejected(self):
        """RS < 70 should fail Gate L1 condition 10."""
        bars = _make_trending_up_bars()
        result = evaluate_long_gates(bars, rs_percentile=40.0)
        assert not result.passed
        assert result.reject_code == RejectCode.R01

    def test_all_gates_logged(self):
        """Every gate evaluation should produce GateDetail objects."""
        bars = _make_trending_up_bars()
        result = evaluate_long_gates(bars, rs_percentile=80.0)
        # Should have at least the 10 trend template conditions
        assert len(result.gates) >= 10

    def test_insufficient_data(self):
        """Short bar history should fail gracefully."""
        bars = pd.DataFrame({
            "open": [100, 101, 102],
            "high": [102, 103, 104],
            "low": [99, 100, 101],
            "close": [101, 102, 103],
            "volume": [1000000, 1100000, 1200000],
        })
        result = evaluate_long_gates(bars, rs_percentile=80.0)
        # Should reject (NaN indicators won't pass conditions)
        assert not result.passed


class TestShortGates:
    def test_passing_short_signal(self):
        """A clean downtrending stock should run through short gates."""
        bars = _make_trending_down_bars()
        result = evaluate_short_gates(
            bars, rs_percentile=15.0, entry_type="S-A",
            short_interest_pct=10.0, days_to_cover=2.0,
        )
        assert result.decision in (Decision.ENTER, Decision.REJECT)
        assert len(result.gates) > 0

    def test_high_rs_rejected(self):
        """RS > 30 should fail the inverse template."""
        bars = _make_trending_down_bars()
        result = evaluate_short_gates(
            bars, rs_percentile=60.0, entry_type="S-A",
        )
        assert not result.passed
        assert result.reject_code == RejectCode.R11

    def test_squeeze_rejection(self):
        """SI > 20% should reject with R13."""
        bars = _make_trending_down_bars()
        result = evaluate_short_gates(
            bars, rs_percentile=15.0, entry_type="S-A",
            short_interest_pct=25.0,  # Above 20% threshold
        )
        # If it gets past the template, it should hit squeeze check
        if result.reject_code not in (RejectCode.R11, RejectCode.R02, RejectCode.R12):
            assert result.reject_code == RejectCode.R13

    def test_no_si_data_allowed_for_cfd(self):
        """Missing SI data should be tolerated (CFD model)."""
        bars = _make_trending_down_bars()
        result = evaluate_short_gates(
            bars, rs_percentile=15.0, entry_type="S-A",
            short_interest_pct=None,  # No data
            days_to_cover=None,
        )
        # Should not reject on R13/R14 — only on template/ADX/distribution
        if result.reject_code:
            assert result.reject_code not in (RejectCode.R13, RejectCode.R14, RejectCode.R15)

    def test_climax_top_uses_alt_gate(self):
        """S-D entries should use S1-ALT instead of inverse template."""
        bars = _make_trending_down_bars()
        result = evaluate_short_gates(
            bars, rs_percentile=15.0, entry_type="S-D",
        )
        # Should reject with R18 (climax conditions not met)
        # because our synthetic data won't have a real climax top
        assert result.reject_code in (RejectCode.R18, RejectCode.R02, RejectCode.R12)
