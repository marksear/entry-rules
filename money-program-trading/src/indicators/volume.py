"""
Volume-based indicators — the lifeblood of the entry rules.

Volume dry-up (Gate 3 for longs): institutional holders sitting tight.
Distribution days (Gate 3 for shorts): heavy selling by institutions.
Projected volume: mid-session extrapolation for confirmation checks.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def avg_volume(volume: pd.Series, period: int = 50) -> float:
    """
    Average volume over the last `period` sessions.

    Args:
        volume: Volume series
        period: Lookback (default 50 days)

    Returns:
        Float average. NaN if insufficient data.
    """
    if len(volume) < period:
        return float("nan")
    return float(volume.tail(period).mean())


def volume_dry_up_count(
    volume: pd.Series,
    avg_vol_50d: float,
    threshold: float = 0.60,
    lookback: int = 5,
) -> int:
    """
    Count sessions with volume < threshold × 50-day average.
    Rule L3: ≥ 2 of last 5 sessions must be "dry" for longs to pass.

    Args:
        volume: Volume series (last N sessions)
        avg_vol_50d: 50-day average volume
        threshold: Dry-up threshold (0.60 = 60% of average)
        lookback: Number of sessions to check (5)

    Returns:
        Count of dry sessions.
    """
    if len(volume) < lookback or np.isnan(avg_vol_50d) or avg_vol_50d <= 0:
        return 0

    cutoff = avg_vol_50d * threshold
    recent = volume.tail(lookback)
    return int((recent < cutoff).sum())


def distribution_day_count(
    close: pd.Series,
    volume: pd.Series,
    avg_vol_50d: float,
    threshold: float = 1.25,
    lookback: int = 10,
) -> int:
    """
    Count distribution days: sessions closing down on heavy volume.
    Rule S3: ≥ 3 of last 10 sessions must show distribution for shorts.

    A distribution day = close < prior close AND volume ≥ threshold × avg.

    Args:
        close: Close price series
        volume: Volume series
        avg_vol_50d: 50-day average volume
        threshold: Heavy volume threshold (1.25 = 125% of average)
        lookback: Number of sessions to check (10)

    Returns:
        Count of distribution days.
    """
    if len(close) < lookback + 1 or np.isnan(avg_vol_50d) or avg_vol_50d <= 0:
        return 0

    vol_cutoff = avg_vol_50d * threshold

    # Get the last `lookback` sessions (need lookback+1 for prior close comparison)
    recent_close = close.tail(lookback + 1)
    recent_vol = volume.tail(lookback + 1)

    count = 0
    for i in range(1, len(recent_close)):
        closed_down = recent_close.iloc[i] < recent_close.iloc[i - 1]
        heavy_volume = recent_vol.iloc[i] >= vol_cutoff
        if closed_down and heavy_volume:
            count += 1

    return count


def projected_volume(
    current_volume: float,
    elapsed_minutes: float,
    session_minutes: float,
) -> float:
    """
    Project end-of-day volume from current intraday volume.
    Used for mid-session volume confirmation (Rule L5/S5).

    Args:
        current_volume: Volume traded so far today
        elapsed_minutes: Minutes since market open
        session_minutes: Total session minutes (US=390, UK=510)

    Returns:
        Projected volume for the full session.
    """
    if elapsed_minutes <= 0 or session_minutes <= 0:
        return 0.0
    return current_volume * (session_minutes / elapsed_minutes)


def volume_ratio(current_volume: float, avg_vol_50d: float) -> float:
    """Volume as a ratio of the 50-day average."""
    if avg_vol_50d <= 0:
        return 0.0
    return current_volume / avg_vol_50d


def volume_declining(volume: pd.Series, lookback: int = 3) -> bool:
    """
    Check if volume is declining over the lookback period.
    Used for pullback/rally entries (Rules L6, S6).

    Each session's volume must be lower than the prior session's.
    """
    if len(volume) < lookback:
        return False
    recent = volume.tail(lookback)
    for i in range(1, len(recent)):
        if recent.iloc[i] >= recent.iloc[i - 1]:
            return False
    return True
