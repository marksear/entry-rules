"""
Average Directional Index (ADX) — Raschke's trend strength filter.

ADX > 25 = genuine trend (Gate 2 for both longs and shorts).
This is computed from scratch — no TA library dependency,
keeping the Pi deployment lightweight.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def directional_indicators(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute +DI, -DI, and ADX.

    Args:
        high: High prices
        low: Low prices
        close: Close prices
        period: Smoothing period (default 14)

    Returns:
        Tuple of (+DI, -DI, ADX) as pd.Series
    """
    n = len(high)
    if n < period + 1:
        empty = pd.Series(np.nan, index=high.index)
        return empty, empty, empty

    # True Range
    tr = _true_range(high, low, close)

    # Directional Movement
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(0.0, index=high.index)
    minus_dm = pd.Series(0.0, index=high.index)

    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

    # Wilder's smoothing (equivalent to EMA with alpha = 1/period)
    atr = _wilder_smooth(tr, period)
    plus_dm_smooth = _wilder_smooth(plus_dm, period)
    minus_dm_smooth = _wilder_smooth(minus_dm, period)

    # Directional Indicators
    plus_di = 100 * (plus_dm_smooth / atr)
    minus_di = 100 * (minus_dm_smooth / atr)

    # DX and ADX
    di_sum = plus_di + minus_di
    di_diff = (plus_di - minus_di).abs()

    # Avoid division by zero
    dx = pd.Series(0.0, index=high.index)
    mask = di_sum > 0
    dx[mask] = 100 * (di_diff[mask] / di_sum[mask])

    adx_series = _wilder_smooth(dx, period)

    return plus_di, minus_di, adx_series


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    Compute ADX only (most common use case).

    Returns:
        ADX series
    """
    _, _, adx_series = directional_indicators(high, low, close, period)
    return adx_series


def adx_value(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    """Get the current (most recent) ADX value."""
    result = adx(high, low, close, period)
    if result.empty or pd.isna(result.iloc[-1]):
        return float("nan")
    return float(result.iloc[-1])


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range = max(H-L, |H-Cp|, |L-Cp|)"""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """
    Wilder's smoothing method.
    Equivalent to EMA with alpha = 1/period.

    First value is the simple average of the first `period` values.
    Subsequent values: (prev_smooth × (period - 1) + current) / period
    """
    result = pd.Series(np.nan, index=series.index)

    # Find first valid window
    valid = series.dropna()
    if len(valid) < period:
        return result

    # Seed with simple average
    first_idx = valid.index[period - 1]
    seed = valid.iloc[:period].mean()
    result[first_idx] = seed

    # Smooth forward using Wilder's formula
    prev = seed
    for i in range(period, len(valid)):
        idx = valid.index[i]
        val = valid.iloc[i]
        smoothed = (prev * (period - 1) + val) / period
        result[idx] = smoothed
        prev = smoothed

    return result
