"""
Relative Strength percentile ranking.

This is the one indicator ProRealTime can't compute natively —
it requires cross-universe comparison, so it stays in Python.

RS = 6-month price performance ranked against the full universe.
Gate L1 condition 10: RS ≥ 70 for longs.
Gate S1 condition 10: RS ≤ 30 for shorts.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats


def price_performance(close: pd.Series, period: int = 126) -> float:
    """
    Calculate price performance over `period` trading days.
    126 trading days ≈ 6 months.

    Returns:
        Percentage change as a decimal (e.g., 0.25 = 25% gain).
        NaN if insufficient data.
    """
    if len(close) < period:
        return float("nan")
    current = close.iloc[-1]
    past = close.iloc[-period]
    if past <= 0:
        return float("nan")
    return (current - past) / past


def relative_strength_percentile(
    ticker_performance: float,
    universe_performances: list[float] | np.ndarray,
) -> float:
    """
    Rank a ticker's performance against the universe.

    Args:
        ticker_performance: 6-month price change for this stock
        universe_performances: 6-month price changes for all stocks in universe

    Returns:
        Percentile rank (0–100). Higher = stronger relative performance.
    """
    if np.isnan(ticker_performance):
        return float("nan")

    valid = [p for p in universe_performances if not np.isnan(p)]
    if len(valid) < 10:
        return float("nan")

    # scipy.stats.percentileofscore gives the percentile rank
    return float(stats.percentileofscore(valid, ticker_performance, kind="rank"))


def compute_universe_rs(
    universe_closes: dict[str, pd.Series],
    period: int = 126,
) -> dict[str, float]:
    """
    Compute RS percentile for every ticker in the universe.

    Args:
        universe_closes: Dict of ticker → close price Series
        period: Performance lookback (126 = 6 months)

    Returns:
        Dict of ticker → RS percentile (0–100)
    """
    # Step 1: compute raw performance for each ticker
    performances: dict[str, float] = {}
    for ticker, closes in universe_closes.items():
        perf = price_performance(closes, period)
        performances[ticker] = perf

    # Step 2: rank against the universe
    all_perfs = list(performances.values())
    rs_ranks: dict[str, float] = {}
    for ticker, perf in performances.items():
        rs_ranks[ticker] = relative_strength_percentile(perf, all_perfs)

    return rs_ranks
