"""
Market data retrieval and caching via IG REST API.

Handles:
- Historical daily OHLCV bars (for indicators)
- Intraday 5-min bars (for opening range, projected volume)
- Market search / epic resolution
- Local caching to stay within IG's 10k/week data allowance
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential
from trading_ig import IGService
from trading_ig.rest import IGException

from ..auth.ig_auth import IGSession

logger = logging.getLogger(__name__)

# Cache directory relative to project root
CACHE_DIR = Path("data/cache")


class MarketData:
    """
    Retrieve and cache price data from IG.

    Strategy (per the architecture doc):
    - Use IG for intraday bars on active signals only (low volume)
    - Daily bars fetched incrementally (only new bars since last fetch)
    - Cache aggressively — after initial load, daily cost is ~1 bar/stock
    - ProRealTime handles the bulk scanning (no Python cost there)
    """

    def __init__(self, session: IGSession):
        self._session = session
        self._epic_cache: dict[str, str] = {}
        self._bar_cache: dict[str, pd.DataFrame] = {}
        self._cache_dir = CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._load_epic_cache()

    @property
    def ig(self) -> IGService:
        return self._session.service

    # ── Epic Resolution ───────────────────────────────────────

    def resolve_epic(self, ticker: str, market: str = "US") -> str:
        """
        Resolve a ticker symbol to an IG epic identifier.
        Caches results — market IDs rarely change.

        Args:
            ticker: Stock symbol (e.g., "AAPL", "VOD")
            market: "US" or "UK"

        Returns:
            IG epic string (e.g., "KA.D.AAPL.CASH.IP")
        """
        cache_key = f"{ticker}:{market}"
        if cache_key in self._epic_cache:
            return self._epic_cache[cache_key]

        epic = self._search_market(ticker, market)
        if epic:
            self._epic_cache[cache_key] = epic
            self._save_epic_cache()
        return epic

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def _search_market(self, ticker: str, market: str) -> str:
        """Search IG for a market matching the ticker."""
        try:
            results = self.ig.search_markets(ticker)
            if results is None or results.empty:
                logger.warning("No IG markets found for %s", ticker)
                return ""

            # Filter for the right market type
            # IG returns CFD, spread bet, and other variants
            for _, row in results.iterrows():
                epic = row.get("epic", "")
                name = str(row.get("instrumentName", "")).upper()

                # Prefer cash/DFB instruments for equities
                if "CASH" in epic or "DFB" in epic:
                    if market == "UK" and ("LSE" in name or ".L" in ticker):
                        logger.info("Resolved %s → %s", ticker, epic)
                        return epic
                    elif market == "US":
                        logger.info("Resolved %s → %s", ticker, epic)
                        return epic

            # Fallback: return first result with CASH in epic
            for _, row in results.iterrows():
                epic = row.get("epic", "")
                if "CASH" in epic:
                    logger.info("Resolved %s → %s (fallback)", ticker, epic)
                    return epic

            # Last resort: first result
            epic = results.iloc[0].get("epic", "")
            logger.warning("Resolved %s → %s (last resort)", ticker, epic)
            return epic

        except IGException as e:
            logger.error("IG market search failed for %s: %s", ticker, e)
            raise

    # ── Daily Bars ────────────────────────────────────────────

    def get_daily_bars(self, epic: str, num_bars: int = 260) -> pd.DataFrame:
        """
        Fetch daily OHLCV bars. Uses local cache with incremental updates.

        Returns DataFrame with columns:
            open, high, low, close, volume
        Indexed by datetime.
        """
        cached = self._load_bar_cache(epic, "DAY")
        if cached is not None and len(cached) >= num_bars - 5:
            # Incremental update: only fetch bars since last cached date
            last_date = cached.index[-1]
            days_missing = (datetime.utcnow() - last_date.to_pydatetime()).days
            if days_missing <= 1:
                return cached.tail(num_bars)

            logger.info("Incremental update for %s: %d days missing", epic, days_missing)
            new_bars = self._fetch_bars(epic, "DAY", min(days_missing + 5, num_bars))
            if new_bars is not None and not new_bars.empty:
                combined = pd.concat([cached, new_bars])
                combined = combined[~combined.index.duplicated(keep="last")]
                combined = combined.sort_index()
                self._save_bar_cache(epic, "DAY", combined)
                return combined.tail(num_bars)

        # Full fetch
        logger.info("Full daily bar fetch for %s (%d bars)", epic, num_bars)
        bars = self._fetch_bars(epic, "DAY", num_bars)
        if bars is not None and not bars.empty:
            self._save_bar_cache(epic, "DAY", bars)
        return bars

    def get_intraday_bars(self, epic: str, resolution_mins: int = 5,
                          num_bars: int = 100) -> pd.DataFrame:
        """
        Fetch intraday bars. Not cached — always fresh for real-time decisions.

        Args:
            epic: IG epic
            resolution_mins: Bar size in minutes (5, 15, etc.)
            num_bars: Number of bars to fetch
        """
        resolution_map = {
            1: "MINUTE",
            5: "MINUTE_5",
            15: "MINUTE_15",
            30: "MINUTE_30",
            60: "HOUR",
        }
        resolution = resolution_map.get(resolution_mins, "MINUTE_5")
        return self._fetch_bars(epic, resolution, num_bars)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def _fetch_bars(self, epic: str, resolution: str, num_bars: int) -> pd.DataFrame:
        """Fetch historical bars from IG API."""
        try:
            response = self.ig.fetch_historical_prices_by_epic_and_num_points(
                epic=epic,
                resolution=resolution,
                numpoints=num_bars,
            )

            if response is None:
                logger.warning("No data returned for %s", epic)
                return pd.DataFrame()

            # trading-ig returns a dict with 'prices' key containing a DataFrame
            prices = response.get("prices", pd.DataFrame())
            if prices.empty:
                return pd.DataFrame()

            # Normalize the DataFrame
            df = self._normalize_bars(prices)
            logger.debug("Fetched %d %s bars for %s", len(df), resolution, epic)
            return df

        except IGException as e:
            logger.error("Failed to fetch bars for %s: %s", epic, e)
            raise

    def _normalize_bars(self, raw: pd.DataFrame) -> pd.DataFrame:
        """
        Normalize IG's bar format to our standard columns.
        IG returns multi-level columns: (bid/ask/mid, open/high/low/close)
        We use mid prices.
        """
        df = pd.DataFrame()

        # IG returns nested column structure — handle both formats
        if isinstance(raw.columns, pd.MultiIndex):
            # Multi-level: ('bid', 'Open'), ('ask', 'Open'), etc.
            if "mid" in raw.columns.get_level_values(0):
                df["open"] = raw[("mid", "Open")]
                df["high"] = raw[("mid", "High")]
                df["low"] = raw[("mid", "Low")]
                df["close"] = raw[("mid", "Close")]
            elif "bid" in raw.columns.get_level_values(0):
                df["open"] = raw[("bid", "Open")]
                df["high"] = raw[("bid", "High")]
                df["low"] = raw[("bid", "Low")]
                df["close"] = raw[("bid", "Close")]
        else:
            # Flat columns — map what we find
            col_map = {}
            for col in raw.columns:
                lower = col.lower()
                if "open" in lower:
                    col_map["open"] = col
                elif "high" in lower:
                    col_map["high"] = col
                elif "low" in lower:
                    col_map["low"] = col
                elif "close" in lower:
                    col_map["close"] = col

            for target, source in col_map.items():
                df[target] = raw[source]

        # Volume — IG returns lastTradedVolume
        vol_candidates = [c for c in raw.columns
                          if "volume" in str(c).lower() or "lasttraded" in str(c).lower()]
        if vol_candidates:
            vol_col = vol_candidates[0]
            if isinstance(vol_col, tuple):
                df["volume"] = raw[vol_col]
            else:
                df["volume"] = raw[vol_col]
        else:
            df["volume"] = 0
            logger.warning("No volume data found in bars — using 0")

        df.index = raw.index
        df = df.astype(float)
        return df

    # ── Streaming (Real-Time) ─────────────────────────────────

    def get_current_price(self, epic: str) -> dict:
        """
        Get current bid/ask/mid for an epic.
        Uses a single bar fetch (lightweight).
        """
        try:
            response = self.ig.fetch_historical_prices_by_epic_and_num_points(
                epic=epic, resolution="MINUTE", numpoints=1,
            )
            prices = response.get("prices", pd.DataFrame())
            if prices.empty:
                return {}

            last = prices.iloc[-1]
            result = {}
            if isinstance(prices.columns, pd.MultiIndex):
                for price_type in ["bid", "ask", "mid"]:
                    if price_type in prices.columns.get_level_values(0):
                        result[price_type] = float(last[(price_type, "Close")])
            return result
        except Exception as e:
            logger.error("Failed to get current price for %s: %s", epic, e)
            return {}

    def get_spread_pct(self, epic: str) -> float | None:
        """Calculate current bid-ask spread as a percentage."""
        price = self.get_current_price(epic)
        bid = price.get("bid")
        ask = price.get("ask")
        if bid and ask and bid > 0:
            return (ask - bid) / bid
        return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_market_snapshot(self, epic: str) -> dict:
        """
        Fetch a full real-time market snapshot from IG's /markets/{epic} endpoint.

        Unlike :meth:`get_current_price`, which infers price from a 1-minute bar
        close, this endpoint returns the live top-of-book quote plus the
        ``marketStatus`` field — which the per-minute monitor loop needs to
        distinguish "flat price because nothing traded" from "flat price because
        the market is CLOSED".

        Returns a dict with keys:
            bid, ask, last_traded, market_status, high, low,
            net_change, pct_change, update_time_utc

        Returns an empty dict on failure. Callers should treat an empty dict as
        "no data this tick" and write a CandidateSnapshot with nulls rather than
        retrying — the retry is already handled here.
        """
        try:
            result = self.ig.fetch_market_by_epic(epic)
        except IGException as e:
            logger.error("IG fetch_market_by_epic failed for %s: %s", epic, e)
            raise
        except Exception as e:
            logger.error("Market snapshot failed for %s: %s", epic, e)
            return {}

        # trading_ig returns either a dict (JSON) or an object; normalise.
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        if not isinstance(result, dict):
            logger.warning("Unexpected snapshot shape for %s: %r", epic, type(result))
            return {}

        snap = result.get("snapshot") or {}
        if not snap:
            return {}

        def _f(v):
            """Coerce to float if possible, else None."""
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        bid = _f(snap.get("bid"))
        ask = _f(snap.get("offer"))
        last_traded = None
        # Some IG instruments expose lastTraded; others only bid/offer. Mid fallback
        # is computed client-side so downstream code always has a usable price.
        for key in ("lastTraded", "lastTradedPrice"):
            if key in snap:
                last_traded = _f(snap.get(key))
                break
        if last_traded is None and bid is not None and ask is not None:
            last_traded = (bid + ask) / 2.0

        return {
            "bid": bid,
            "ask": ask,
            "last_traded": last_traded,
            "market_status": snap.get("marketStatus"),
            "high": _f(snap.get("high")),
            "low": _f(snap.get("low")),
            "net_change": _f(snap.get("netChange")),
            "pct_change": _f(snap.get("percentageChange")),
            "update_time_utc": snap.get("updateTime") or snap.get("updateTimeUTC"),
        }

    # ── Caching ───────────────────────────────────────────────

    def _cache_path(self, epic: str, resolution: str) -> Path:
        safe_epic = epic.replace(".", "_").replace(":", "_")
        return self._cache_dir / f"{safe_epic}_{resolution}.parquet"

    def _load_bar_cache(self, epic: str, resolution: str) -> pd.DataFrame | None:
        path = self._cache_path(epic, resolution)
        if path.exists():
            try:
                return pd.read_parquet(path)
            except Exception as e:
                logger.warning("Cache read failed for %s: %s", path, e)
        return None

    def _save_bar_cache(self, epic: str, resolution: str, df: pd.DataFrame) -> None:
        path = self._cache_path(epic, resolution)
        try:
            df.to_parquet(path)
        except Exception as e:
            logger.warning("Cache write failed for %s: %s", path, e)

    def _load_epic_cache(self) -> None:
        path = self._cache_dir / "epic_map.json"
        if path.exists():
            try:
                with open(path) as f:
                    self._epic_cache = json.load(f)
            except Exception:
                self._epic_cache = {}

    def _save_epic_cache(self) -> None:
        path = self._cache_dir / "epic_map.json"
        try:
            with open(path, "w") as f:
                json.dump(self._epic_cache, f, indent=2)
        except Exception as e:
            logger.warning("Epic cache write failed: %s", e)
