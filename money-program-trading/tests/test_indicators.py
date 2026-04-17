"""
Unit tests for indicator calculations.

Every indicator must be deterministic: same input → same output.
Tests use known reference values to verify correctness.
"""

import numpy as np
import pandas as pd
import pytest

from src.indicators.moving_averages import sma, sma_value, ema, ema_value
from src.indicators.adx import adx, adx_value, _true_range
from src.indicators.volume import (
    avg_volume,
    volume_dry_up_count,
    distribution_day_count,
    projected_volume,
    volume_declining,
)
from src.indicators.price import (
    fifty_two_week_high,
    fifty_two_week_low,
    opening_range,
    vwap,
)


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def simple_prices():
    """20 bars of simple ascending prices."""
    return pd.Series([100 + i for i in range(20)])


@pytest.fixture
def trending_bars():
    """50 bars of a trending stock (up)."""
    np.random.seed(42)
    n = 50
    base = np.linspace(100, 150, n)
    noise = np.random.normal(0, 1, n)
    close = pd.Series(base + noise)
    high = close + abs(np.random.normal(1, 0.5, n))
    low = close - abs(np.random.normal(1, 0.5, n))
    volume = pd.Series(np.random.randint(500_000, 2_000_000, n))
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]),
                          "high": high, "low": low, "close": close, "volume": volume})


@pytest.fixture
def volume_series():
    """Volume series with some dry-up sessions."""
    avg = 1_000_000
    return pd.Series([
        1_200_000,  # normal
        900_000,    # normal
        500_000,    # dry (< 600k)
        400_000,    # dry
        1_100_000,  # normal
        550_000,    # dry
        1_500_000,  # heavy
        300_000,    # dry
        1_000_000,  # normal
        600_000,    # borderline (= 60% of avg, not < 60%)
    ])


# ── SMA Tests ─────────────────────────────────────────────────

class TestSMA:
    def test_basic_sma(self, simple_prices):
        result = sma(simple_prices, 5)
        # SMA(5) of [100,101,102,103,104] = 102.0
        assert result.iloc[4] == pytest.approx(102.0)

    def test_sma_length(self, simple_prices):
        result = sma(simple_prices, 5)
        assert len(result) == len(simple_prices)
        # First 4 should be NaN
        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[3])
        assert not pd.isna(result.iloc[4])

    def test_sma_value(self, simple_prices):
        val = sma_value(simple_prices, 5)
        # Last 5 values: 115,116,117,118,119 → mean = 117.0
        assert val == pytest.approx(117.0)

    def test_sma_insufficient_data(self):
        short = pd.Series([100, 101, 102])
        result = sma(short, 10)
        assert all(pd.isna(result))


# ── EMA Tests ─────────────────────────────────────────────────

class TestEMA:
    def test_ema_exists(self, simple_prices):
        result = ema(simple_prices, 10)
        assert len(result) == len(simple_prices)
        assert not pd.isna(result.iloc[-1])

    def test_ema_responds_to_recent(self):
        # EMA should weight recent prices more heavily
        stable = pd.Series([100.0] * 20)
        spike = stable.copy()
        spike.iloc[-1] = 200.0
        ema_stable = ema_value(stable, 10)
        ema_spike = ema_value(spike, 10)
        assert ema_spike > ema_stable

    def test_ema_value(self, simple_prices):
        val = ema_value(simple_prices, 10)
        assert not np.isnan(val)
        # EMA of ascending series should be near but below the latest value
        assert val < simple_prices.iloc[-1]
        assert val > simple_prices.iloc[-10]


# ── ADX Tests ─────────────────────────────────────────────────

class TestADX:
    def test_adx_returns_values(self, trending_bars):
        result = adx(trending_bars["high"], trending_bars["low"], trending_bars["close"])
        assert len(result) == len(trending_bars)
        # Should have some valid values after warm-up
        valid = result.dropna()
        assert len(valid) > 0

    def test_adx_value_trending(self, trending_bars):
        val = adx_value(trending_bars["high"], trending_bars["low"], trending_bars["close"])
        # A trending series should have ADX > 0
        assert val > 0

    def test_adx_range(self, trending_bars):
        result = adx(trending_bars["high"], trending_bars["low"], trending_bars["close"])
        valid = result.dropna()
        # ADX should be between 0 and 100
        assert all(valid >= 0)
        assert all(valid <= 100)


# ── Volume Tests ──────────────────────────────────────────────

class TestVolume:
    def test_avg_volume(self, volume_series):
        avg = avg_volume(volume_series, 10)
        expected = volume_series.mean()
        assert avg == pytest.approx(expected)

    def test_volume_dry_up_count(self, volume_series):
        avg_vol = 1_000_000.0
        # Last 5 sessions: [550000, 1500000, 300000, 1000000, 600000]
        # Threshold: 600000 (60% of 1M)
        # Dry sessions (< 600000): 550000, 300000 = 2
        # (600000 is NOT < 600000)
        count = volume_dry_up_count(volume_series, avg_vol, 0.60, 5)
        assert count == 2

    def test_distribution_day_count(self):
        # 11 bars: alternating up/down closes with varying volume
        close = pd.Series([100, 99, 101, 98, 102, 97, 103, 96, 104, 95, 105])
        volume = pd.Series([
            800_000,     # —
            1_500_000,   # down on heavy vol ✓
            900_000,     # up
            1_400_000,   # down on heavy vol ✓
            1_000_000,   # up
            1_600_000,   # down on heavy vol ✓
            800_000,     # up
            1_300_000,   # down on heavy vol ✓
            900_000,     # up
            1_500_000,   # down on heavy vol ✓
            700_000,     # up
        ])
        avg_vol = 1_000_000.0
        count = distribution_day_count(close, volume, avg_vol, 1.25, 10)
        assert count >= 3

    def test_projected_volume(self):
        # 100k volume in first 60 minutes of 390 minute session
        proj = projected_volume(100_000, 60, 390)
        assert proj == pytest.approx(650_000)

    def test_volume_declining(self):
        declining = pd.Series([1_000_000, 800_000, 600_000, 400_000])
        assert volume_declining(declining, 3) is True

        not_declining = pd.Series([1_000_000, 800_000, 900_000, 400_000])
        assert volume_declining(not_declining, 3) is False


# ── Price Tests ───────────────────────────────────────────────

class TestPrice:
    def test_52_week_high(self):
        high = pd.Series([100 + i for i in range(260)])
        assert fifty_two_week_high(high) == 359  # 100 + 259

    def test_52_week_low(self):
        low = pd.Series([100 + i for i in range(260)])
        assert fifty_two_week_low(low) == 100

    def test_opening_range(self):
        bars = pd.DataFrame({
            "high": [105, 107, 106, 108, 110],
            "low": [100, 102, 101, 103, 104],
            "close": [103, 105, 104, 106, 108],
        })
        result = opening_range(bars, 3)
        assert result["high"] == 107
        assert result["low"] == 100

    def test_vwap(self):
        bars = pd.DataFrame({
            "high": [102, 104, 103],
            "low": [98, 100, 99],
            "close": [100, 102, 101],
            "volume": [1000, 2000, 1500],
        })
        result = vwap(bars)
        assert len(result) == 3
        assert not np.isnan(result.iloc[-1])
        # VWAP should be between low and high
        assert result.iloc[-1] >= 98
        assert result.iloc[-1] <= 104
