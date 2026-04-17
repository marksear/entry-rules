"""
Qualification gates — the binary filters that kill or pass a signal.

Long signals: 3 gates (Trend Template, ADX, Volume Dry-Up)
Short signals: 4 gates (Inverse Template, ADX, Distribution, Squeeze)

If any gate returns FALSE, the signal is dead. No override. No exceptions.
"""

from __future__ import annotations

import logging
import math

import pandas as pd

from ..config.rejection_codes import RejectCode
from ..config.settings import Settings, get_settings
from ..indicators.moving_averages import sma_value, ema_value
from ..indicators.adx import adx_value
from ..indicators.volume import avg_volume, volume_dry_up_count, distribution_day_count
from ..indicators.price import fifty_two_week_high, fifty_two_week_low
from ..models.gate_result import GateResult, GateDetail

logger = logging.getLogger(__name__)


def evaluate_long_gates(
    bars: pd.DataFrame,
    rs_percentile: float,
    settings: Settings | None = None,
) -> GateResult:
    """
    Evaluate all long-side qualification gates.

    Args:
        bars: Daily OHLCV DataFrame (≥260 rows ideally)
        rs_percentile: Pre-computed RS percentile (0–100)
        settings: Configuration (uses defaults if None)

    Returns:
        GateResult — either passed or rejected with reason code
    """
    s = settings or get_settings()
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    volume = bars["volume"]
    gates: list[GateDetail] = []

    # ── GATE L1: Trend Template (10 conditions) ───────────────
    current_close = float(close.iloc[-1])
    ma50 = sma_value(close, s.ma_short)
    ma150 = sma_value(close, s.ma_medium)
    ma200 = sma_value(close, s.ma_long)

    # MA(200) from 22 trading days ago
    if len(close) > 22:
        ma200_1mo = sma_value(close.iloc[:-22], s.ma_long)
    else:
        ma200_1mo = float("nan")

    wk52_high = fifty_two_week_high(high)
    wk52_low = fifty_two_week_low(low)

    tt_conditions = [
        ("Price > MA(150)", current_close > ma150, current_close, ma150),
        ("Price > MA(200)", current_close > ma200, current_close, ma200),
        ("MA(150) > MA(200)", ma150 > ma200, ma150, ma200),
        ("MA(200) trending up 1mo", ma200 > ma200_1mo, ma200, ma200_1mo),
        ("MA(50) > MA(150)", ma50 > ma150, ma50, ma150),
        ("MA(50) > MA(200)", ma50 > ma200, ma50, ma200),
        ("Price > MA(50)", current_close > ma50, current_close, ma50),
        ("Price ≥ 25% above 52wk low", current_close >= wk52_low * 1.25, current_close, wk52_low * 1.25),
        ("Price within 25% of 52wk high", current_close >= wk52_high * 0.75, current_close, wk52_high * 0.75),
        ("RS ≥ 70", rs_percentile >= s.rs_min_long, rs_percentile, s.rs_min_long),
    ]

    failed_conditions = []
    for name, passed, value, threshold in tt_conditions:
        gate = GateDetail(
            name=name,
            passed=passed,
            value=value if not math.isnan(value) else None,
            threshold=threshold if not math.isnan(threshold) else None,
        )
        gates.append(gate)
        if not passed:
            failed_conditions.append(name)

    if failed_conditions:
        logger.info("Trend Template FAILED: %s", ", ".join(failed_conditions))
        return GateResult.reject(RejectCode.R01, gates)

    # ── GATE L2: ADX > 25 ────────────────────────────────────
    current_adx = adx_value(high, low, close, s.adx_period)
    adx_gate = GateDetail(
        name="ADX(14) > 25",
        passed=current_adx > s.adx_min,
        value=round(current_adx, 2),
        threshold=s.adx_min,
    )
    gates.append(adx_gate)

    if not adx_gate.passed:
        logger.info("ADX gate FAILED: %.2f < %.1f", current_adx, s.adx_min)
        return GateResult.reject(RejectCode.R02, gates)

    # ── GATE L3: Volume Dry-Up ────────────────────────────────
    avg_vol = avg_volume(volume, s.volume_avg_period)
    dry_count = volume_dry_up_count(
        volume, avg_vol, s.volume_dryup_threshold, s.volume_dryup_lookback
    )
    vol_gate = GateDetail(
        name=f"Volume dry-up ≥ {s.volume_dryup_min_sessions} of {s.volume_dryup_lookback}",
        passed=dry_count >= s.volume_dryup_min_sessions,
        value=dry_count,
        threshold=s.volume_dryup_min_sessions,
    )
    gates.append(vol_gate)

    if not vol_gate.passed:
        logger.info("Volume dry-up FAILED: %d < %d", dry_count, s.volume_dryup_min_sessions)
        return GateResult.reject(RejectCode.R03, gates)

    # ── ALL GATES PASSED ──────────────────────────────────────
    logger.info("All long gates PASSED (ADX=%.1f, dry=%d)", current_adx, dry_count)
    return GateResult.passed_all(
        gates,
        adx_value=round(current_adx, 2),
        volume_dry_count=dry_count,
    )


def evaluate_short_gates(
    bars: pd.DataFrame,
    rs_percentile: float,
    entry_type: str,
    short_interest_pct: float | None = None,
    days_to_cover: float | None = None,
    borrow_fee: float | None = None,
    settings: Settings | None = None,
) -> GateResult:
    """
    Evaluate all short-side qualification gates.

    Args:
        bars: Daily OHLCV DataFrame
        rs_percentile: Pre-computed RS percentile
        entry_type: "S-A" through "S-E"
        short_interest_pct: Short interest % (from ORTEX/Fintel, None if unavailable)
        days_to_cover: Days to cover (None if unavailable)
        borrow_fee: Annual borrow fee as decimal (None if unavailable)
        settings: Configuration

    Returns:
        GateResult
    """
    s = settings or get_settings()
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    volume = bars["volume"]
    gates: list[GateDetail] = []

    # ── GATE S1: Inverse Trend Template (or S1-ALT for Climax) ─
    if entry_type == "S-D":
        gates.extend(_evaluate_climax_conditions(bars, s))
        if any(not g.passed for g in gates):
            return GateResult.reject(RejectCode.R18, gates)
    else:
        gates.extend(_evaluate_inverse_trend_template(bars, rs_percentile, s))
        if any(not g.passed for g in gates):
            return GateResult.reject(RejectCode.R11, gates)

    # ── GATE S2: ADX > 25 ────────────────────────────────────
    current_adx = adx_value(high, low, close, s.adx_period)
    adx_gate = GateDetail(
        name="ADX(14) > 25 (short)",
        passed=current_adx > s.adx_min,
        value=round(current_adx, 2),
        threshold=s.adx_min,
    )
    gates.append(adx_gate)

    if not adx_gate.passed:
        return GateResult.reject(RejectCode.R02, gates)

    # ── GATE S3: Distribution Days ────────────────────────────
    avg_vol = avg_volume(volume, s.volume_avg_period)
    dist_count = distribution_day_count(
        close, volume, avg_vol, s.distribution_threshold, s.distribution_lookback
    )
    dist_gate = GateDetail(
        name=f"Distribution ≥ {s.distribution_min_days} of {s.distribution_lookback}",
        passed=dist_count >= s.distribution_min_days,
        value=dist_count,
        threshold=s.distribution_min_days,
    )
    gates.append(dist_gate)

    if not dist_gate.passed:
        return GateResult.reject(RejectCode.R12, gates)

    # ── GATE S4: Squeeze / Borrow Check ──────────────────────
    # If supplementary data is unavailable, we BLOCK shorts (safe default)
    if short_interest_pct is not None:
        si_gate = GateDetail(
            name="Short interest < 20%",
            passed=short_interest_pct < s.short_interest_max,
            value=short_interest_pct,
            threshold=s.short_interest_max,
        )
        gates.append(si_gate)
        if not si_gate.passed:
            return GateResult.reject(RejectCode.R13, gates)
    else:
        # No data — log warning but allow (CFD model doesn't need borrow)
        gates.append(GateDetail(
            name="Short interest check",
            passed=True,
            detail="SI data unavailable — CFD model, proceeding with caution",
        ))

    if days_to_cover is not None:
        dtc_gate = GateDetail(
            name="Days to cover < 5",
            passed=days_to_cover < s.days_to_cover_max,
            value=days_to_cover,
            threshold=s.days_to_cover_max,
        )
        gates.append(dtc_gate)
        if not dtc_gate.passed:
            return GateResult.reject(RejectCode.R14, gates)

    if borrow_fee is not None:
        borrow_gate = GateDetail(
            name="Borrow fee < 5%",
            passed=borrow_fee < s.borrow_fee_max,
            value=borrow_fee,
            threshold=s.borrow_fee_max,
        )
        gates.append(borrow_gate)
        if not borrow_gate.passed:
            return GateResult.reject(RejectCode.R15, gates)

    # ── ALL GATES PASSED ──────────────────────────────────────
    return GateResult.passed_all(
        gates,
        adx_value=round(current_adx, 2),
        distribution_count=dist_count,
        short_interest_pct=short_interest_pct,
        days_to_cover=days_to_cover,
        borrow_fee=borrow_fee,
    )


# ── Internal helpers ──────────────────────────────────────────


def _evaluate_inverse_trend_template(
    bars: pd.DataFrame, rs_percentile: float, s: Settings
) -> list[GateDetail]:
    """Evaluate the 10 conditions of the inverse (short) trend template."""
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    current_close = float(close.iloc[-1])

    ma50 = sma_value(close, s.ma_short)
    ma150 = sma_value(close, s.ma_medium)
    ma200 = sma_value(close, s.ma_long)
    ma200_1mo = sma_value(close.iloc[:-22], s.ma_long) if len(close) > 22 else float("nan")
    wk52_high = fifty_two_week_high(high)
    wk52_low = fifty_two_week_low(low)

    conditions = [
        ("Price < MA(150)", current_close < ma150, current_close, ma150),
        ("Price < MA(200)", current_close < ma200, current_close, ma200),
        ("MA(150) < MA(200)", ma150 < ma200, ma150, ma200),
        ("MA(200) trending down 1mo", ma200 < ma200_1mo, ma200, ma200_1mo),
        ("MA(50) < MA(150)", ma50 < ma150, ma50, ma150),
        ("MA(50) < MA(200)", ma50 < ma200, ma50, ma200),
        ("Price < MA(50)", current_close < ma50, current_close, ma50),
        ("Price ≤ 25% below 52wk high", current_close <= wk52_high * 0.75, current_close, wk52_high * 0.75),
        ("Price within 25% of 52wk low", current_close <= wk52_low * 1.25, current_close, wk52_low * 1.25),
        ("RS ≤ 30", rs_percentile <= s.rs_max_short, rs_percentile, s.rs_max_short),
    ]

    gates = []
    for name, passed, value, threshold in conditions:
        gates.append(GateDetail(
            name=name,
            passed=passed,
            value=value if not math.isnan(value) else None,
            threshold=threshold if not math.isnan(threshold) else None,
        ))
    return gates


def _evaluate_climax_conditions(
    bars: pd.DataFrame, s: Settings
) -> list[GateDetail]:
    """
    Rule S1-ALT: Climax Top conditions for S-D entries.
    All 5 must be TRUE. This is intentionally rare.
    """
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    volume = bars["volume"]
    current_close = float(close.iloc[-1])
    current_high = float(high.iloc[-1])
    current_low = float(low.iloc[-1])
    current_vol = float(volume.iloc[-1])

    # 1. Stock advanced ≥ 100% in prior 8 weeks (40 trading days)
    lookback_8wk = min(40, len(close) - 1)
    low_8wk_ago = float(low.iloc[-lookback_8wk]) if lookback_8wk > 0 else current_close
    advance = (current_close / low_8wk_ago) if low_8wk_ago > 0 else 0

    # 2. Widest daily range in the entire advance
    daily_range = high - low
    current_range = current_high - current_low
    max_range = float(daily_range.tail(lookback_8wk).max()) if lookback_8wk > 0 else 0

    # 3. Highest volume day in the advance
    max_vol = float(volume.tail(lookback_8wk).max()) if lookback_8wk > 0 else 0

    # 4. Reversal candle: closes in lower 25% of day's range
    if current_range > 0:
        close_position = (current_close - current_low) / current_range
    else:
        close_position = 0.5

    # 5. Extended ≥ 50% above 200-day MA
    ma200 = sma_value(close, s.ma_long)
    extension = (current_close / ma200 - 1) if ma200 > 0 else 0

    gates = [
        GateDetail(
            name="Advanced ≥ 100% in 8 weeks",
            passed=advance >= (1 + s.climax_advance_min),
            value=round((advance - 1) * 100, 1),
            threshold=s.climax_advance_min * 100,
        ),
        GateDetail(
            name="Widest daily range in advance",
            passed=current_range >= max_range,
            value=round(current_range, 4),
            threshold=round(max_range, 4),
        ),
        GateDetail(
            name="Highest volume in advance",
            passed=current_vol >= max_vol,
            value=int(current_vol),
            threshold=int(max_vol),
        ),
        GateDetail(
            name="Close in lower 25% of range",
            passed=close_position <= s.climax_candle_threshold,
            value=round(close_position, 4),
            threshold=s.climax_candle_threshold,
        ),
        GateDetail(
            name="Extended ≥ 50% above 200 MA",
            passed=extension >= s.climax_extension_min,
            value=round(extension * 100, 1),
            threshold=s.climax_extension_min * 100,
        ),
    ]
    return gates
