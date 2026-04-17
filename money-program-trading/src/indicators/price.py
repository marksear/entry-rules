"""
Price-based indicators: 52-week high/low, opening range, VWAP.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def fifty_two_week_high(high: pd.Series, period: int = 260) -> float:
    """Highest high over the last 260 trading days."""
    if len(high) < period:
        return float(high.max()) if len(high) > 0 else float("nan")
    return float(high.tail(period).max())


def fifty_two_week_low(low: pd.Series, period: int = 260) -> float:
    """Lowest low over the last 260 trading days."""
    if len(low) < period:
        return float(low.min()) if len(low) > 0 else float("nan")
    return float(low.tail(period).min())


def opening_range(
    intraday_bars: pd.DataFrame,
    num_bars: int = 3,
) -> dict[str, float]:
    """
    Opening range from the first N intraday bars.
    Default: first 3 × 5-min bars = 15 minutes.

    Used for BGU and SGD gap protocols (Rules L9, S9).

    Args:
        intraday_bars: DataFrame with 'high' and 'low' columns
        num_bars: Number of bars in the opening range (3 for 5-min bars)

    Returns:
        {"high": float, "low": float} or empty dict
    """
    if len(intraday_bars) < num_bars:
        return {}

    first_n = intraday_bars.head(num_bars)
    return {
        "high": float(first_n["high"].max()),
        "low": float(first_n["low"].min()),
    }


def vwap(
    intraday_bars: pd.DataFrame,
) -> pd.Series:
    """
    Volume-Weighted Average Price (intraday).

    Cumulative (price × volume) / cumulative volume.

    Args:
        intraday_bars: DataFrame with 'high', 'low', 'close', 'volume'

    Returns:
        Series of running VWAP values.
    """
    if intraday_bars.empty or "volume" not in intraday_bars.columns:
        return pd.Series(dtype=float)

    typical_price = (
        intraday_bars["high"] + intraday_bars["low"] + intraday_bars["close"]
    ) / 3

    cumulative_tp_vol = (typical_price * intraday_bars["volume"]).cumsum()
    cumulative_vol = intraday_bars["volume"].cumsum()

    # Avoid division by zero
    result = pd.Series(np.nan, index=intraday_bars.index)
    mask = cumulative_vol > 0
    result[mask] = cumulative_tp_vol[mask] / cumulative_vol[mask]

    return result


def vwap_value(intraday_bars: pd.DataFrame) -> float:
    """Get the current (most recent) VWAP value."""
    result = vwap(intraday_bars)
    if result.empty or pd.isna(result.iloc[-1]):
        return float("nan")
    return float(result.iloc[-1])
