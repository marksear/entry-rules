"""
Moving average calculations — SMA and EMA.

All functions are pure: same input → same output.
All operate on pandas Series (close prices).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """
    Simple Moving Average.

    Args:
        series: Price series (typically close prices)
        period: Lookback period (e.g., 50, 150, 200)

    Returns:
        Series of SMA values. First (period-1) values will be NaN.
    """
    if len(series) < period:
        return pd.Series(np.nan, index=series.index)
    return series.rolling(window=period, min_periods=period).mean()


def sma_value(series: pd.Series, period: int) -> float:
    """Get the current (most recent) SMA value."""
    result = sma(series, period)
    if result.empty or pd.isna(result.iloc[-1]):
        return float("nan")
    return float(result.iloc[-1])


def ema(series: pd.Series, period: int) -> pd.Series:
    """
    Exponential Moving Average.

    Uses the standard EMA formula:
        multiplier = 2 / (period + 1)
        EMA = (close - EMA_prev) × multiplier + EMA_prev

    Args:
        series: Price series
        period: Lookback period (e.g., 10, 20)

    Returns:
        Series of EMA values.
    """
    if len(series) < period:
        return pd.Series(np.nan, index=series.index)
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def ema_value(series: pd.Series, period: int) -> float:
    """Get the current (most recent) EMA value."""
    result = ema(series, period)
    if result.empty or pd.isna(result.iloc[-1]):
        return float("nan")
    return float(result.iloc[-1])
