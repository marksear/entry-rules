"""
Central configuration for the Entry Refinement Engine.

All thresholds from the Masterclass v2 are defined here as typed,
validated settings. Nothing is hard-coded in the engine modules.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class AccType(str, Enum):
    DEMO = "DEMO"
    LIVE = "LIVE"


class PriceFeedMode(str, Enum):
    """Price-feed source selector for the monitor loop.

    See ``docs/specs/S3_LIGHTSTREAMER_SPEC.md`` §5.

    - ``REST`` — today's behaviour (default). Polls IG's
      ``/markets/{epic}`` REST endpoint on every tick via ``RestPriceFeed``.
      Zero-risk fallback; the REST cache staleness bug that cost £50 on
      2026-04-23 lives here.
    - ``LIGHTSTREAMER`` — push-based feed via IG's Lightstreamer server
      (``LightstreamerPriceFeed``). The target post-migration state.
    - ``PARALLEL`` — runs both and logs divergence on every tick. Debug
      mode only; not for production. Used during spec §6 Phase 3 to prove
      LS disagrees with REST exactly in the staleness pattern we expect.
    """

    REST = "rest"
    LIGHTSTREAMER = "lightstreamer"
    PARALLEL = "parallel"


class Settings(BaseSettings):
    """All configuration flows through this single class."""

    # ── IG API ────────────────────────────────────────────────
    ig_api_key: str = ""
    ig_username: str = ""
    ig_password: str = ""
    ig_acc_type: AccType = AccType.DEMO
    ig_account_id: str = ""

    @property
    def ig_base_url(self) -> str:
        if self.ig_acc_type == AccType.DEMO:
            return "https://demo-api.ig.com/gateway/deal"
        return "https://api.ig.com/gateway/deal"

    # ── Supplementary APIs (stubbed until keys obtained) ──────
    fmp_api_key: str = ""
    alpha_vantage_key: str = ""
    ortex_api_key: str = ""

    # ── Database ──────────────────────────────────────────────
    db_path: str = "data/trading.db"

    # ── Logging ───────────────────────────────────────────────
    log_level: str = "INFO"
    audit_log_dir: str = "logs"

    # ── Risk Parameters (Masterclass v2 hard limits) ──────────
    max_risk_per_trade: float = Field(default=0.01, description="1% of portfolio")
    max_total_risk: float = Field(default=0.06, description="6% of portfolio")
    max_single_position_long: float = Field(default=0.10, description="10% at cost")
    max_single_position_short: float = Field(default=0.08, description="8% at cost")
    max_short_exposure: float = Field(default=0.50, description="50% of portfolio")
    max_stop_distance: float = Field(default=0.08, description="8% max stop")
    min_reward_risk: float = Field(default=3.0, description="3:1 minimum")
    emergency_cover_pct: float = Field(default=0.15, description="15% adverse → cover")
    overnight_gap_pct: float = Field(default=0.10, description="10% gap assumption")

    # ── Gate Thresholds (Masterclass v2) ──────────────────────
    # Long gates
    adx_min: float = Field(default=25.0, description="ADX(14) minimum for trend")
    adx_period: int = Field(default=14)
    volume_dryup_threshold: float = Field(default=0.60, description="60% of 50d avg")
    volume_dryup_min_sessions: int = Field(default=2, description="Min dry sessions out of 5")
    volume_dryup_lookback: int = Field(default=5)
    rs_min_long: int = Field(default=70, description="RS percentile minimum for longs")

    # Short gates
    rs_max_short: int = Field(default=30, description="RS percentile maximum for shorts")
    distribution_threshold: float = Field(default=1.25, description="1.25× avg volume")
    distribution_min_days: int = Field(default=3, description="Min distribution days out of 10")
    distribution_lookback: int = Field(default=10)
    short_interest_max: float = Field(default=20.0, description="SI% > 20 → skip")
    days_to_cover_max: float = Field(default=5.0, description="DTC > 5 → skip")
    borrow_fee_max: float = Field(default=0.05, description="5% annual max")

    # Climax top (S-D)
    climax_advance_min: float = Field(default=1.0, description="100% advance in 8 weeks")
    climax_extension_min: float = Field(default=0.50, description="50% above 200 MA")
    climax_candle_threshold: float = Field(default=0.25, description="Close in lower 25%")

    # ── Execution Thresholds ──────────────────────────────────
    breakout_volume_ratio: float = Field(default=1.40, description="1.4× 50d avg")
    chase_limit: float = Field(default=0.03, description="3% above pivot → skip")
    gap_threshold: float = Field(default=0.02, description="2% gap classification")
    tranche_1_pct: float = Field(default=0.60, description="60% first tranche")
    tranche_2_pct: float = Field(default=0.40, description="40% second tranche")

    # ── Account Type ──────────────────────────────────────────
    # Spread bet only — tax-free profits for UK residents.
    # No CFD, no stamp duty, no SDRT.
    account_mode: str = Field(default="SPREADBET", description="SPREADBET or CFD")

    # ── UK-Specific ───────────────────────────────────────────
    uk_spread_reduce_threshold: float = Field(default=0.003, description="0.3% spread → reduce 25%")
    uk_spread_skip_threshold: float = Field(default=0.005, description="0.5% spread → skip")
    uk_spread_reduction: float = Field(default=0.75, description="75% of normal size")

    # ── Session Timing ────────────────────────────────────────
    us_mid_session: str = Field(default="11:30", description="ET")
    uk_mid_session: str = Field(default="12:15", description="GMT")
    us_session_minutes: int = Field(default=390)
    uk_session_minutes: int = Field(default=510)

    # ── Entry Window ──────────────────────────────────────────
    # The engine stalks entry zones during this time window.
    # Default: 09:45–10:15 EST (14:45–15:15 GMT)
    # 15 minutes after market open to let the noise settle,
    # then 30 minutes of stalking for a clean entry.
    entry_window_start: str = Field(default="09:45", description="EST, 24h format")
    entry_window_end: str = Field(default="11:00", description="EST, 24h format")
    entry_check_interval: int = Field(default=60, description="Seconds between checks")

    # ── Moving Average Periods ────────────────────────────────
    ma_short: int = 50
    ma_medium: int = 150
    ma_long: int = 200
    ema_fast: int = 10
    ema_slow: int = 20
    volume_avg_period: int = 50
    weekly_high_low_bars: int = 260  # 52 weeks of trading days

    # ── Quarantine ────────────────────────────────────────────
    quarantine_days: int = Field(default=10, description="Failed reclaim quarantine")
    gap_reclaim_window: int = Field(default=3, description="Days to reclaim after gap")

    # ── Exit Management (Exit_Management_v1.md) ────────────────
    # Stepped £-ratchet trail on peak unrealised P&L. All values in GBP.
    # Scale with account size — these defaults are calibrated for the £1k
    # starting-account profile (Masterclass v2 small-account ladder).
    trail_activation_gbp: float = Field(
        default=25.0,
        description="Arm the trail when peak unrealised P&L first reaches this.",
    )
    trail_initial_lock_gbp: float = Field(
        default=1.0,
        description="On arm, move stop to breakeven + this amount (Step 1).",
    )
    trail_step_trigger_gbp: float = Field(
        default=5.0,
        description="Each additional £ of peak P&L required to advance the stop.",
    )
    trail_step_size_gbp: float = Field(
        default=5.0,
        description="How far (in £ locked) the stop advances per step.",
    )
    trail_hard_target_gbp: float = Field(
        default=50.0,
        description="Market-exit threshold — peak P&L ≥ this closes the position.",
    )
    invalidation_window_minutes: int = Field(
        default=30,
        description="Window post-fill in which an adverse trigger re-cross forces exit.",
    )
    timestop_sessions: int = Field(
        default=3,
        description="Max trading sessions a position may remain open before timestop.",
    )

    # ── Price Feed (S-3 Lightstreamer migration) ─────────────────
    # See docs/specs/S3_LIGHTSTREAMER_SPEC.md §5 + §7.2.
    # Default stays REST through Phases 1–3; only flip to LIGHTSTREAMER
    # after PARALLEL-mode validation confirms LS disagrees with REST
    # exactly in the staleness pattern we expect.
    price_feed_mode: PriceFeedMode = Field(
        default=PriceFeedMode.REST,
        description="rest|lightstreamer|parallel — see PriceFeedMode enum.",
    )
    price_feed_stale_seconds: float = Field(
        default=10.0,
        description=(
            "Per-epic tick staleness threshold (seconds). "
            "LightstreamerPriceFeed.latest() raises StalePriceError when the "
            "last tick is older than this. Short glitches 10-60s are "
            "tolerated; see degraded_seconds for the hard cut-off."
        ),
    )
    price_feed_degraded_seconds: float = Field(
        default=60.0,
        description=(
            "Feed-level degradation threshold (seconds). When staleness on an "
            "epic exceeds this AND the epic has an open position, the monitor "
            "force-closes via broker REST (independent of the stale LS feed). "
            "'If we can't see prices, we don't hold positions.'"
        ),
    )

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


@lru_cache
def get_settings() -> Settings:
    """Singleton settings instance. Reads from .env on first call."""
    return Settings()
