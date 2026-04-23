"""
Price feed abstraction — Phase 1 of the S-3 Lightstreamer migration.

See ``docs/specs/S3_LIGHTSTREAMER_SPEC.md`` for the full plan. This module
introduces a ``PriceFeed`` ABC so ``MarketData`` can read live prices from
either the current REST polling path (``RestPriceFeed``) or a future
Lightstreamer streaming path (``LightstreamerPriceFeed``, Phase 2) without
changing its own call sites.

**Phase 1 is zero-behaviour-change.** ``RestPriceFeed`` calls exactly the
same IG endpoint (``fetch_market_by_epic``) and applies exactly the same
scaling heuristic as the inline logic that previously lived in
``MarketData.get_market_snapshot``. ``StalePriceError`` is part of the
interface but ``RestPriceFeed`` cannot raise it (a fresh REST fetch is
by definition not stale at return time) — that semantic activates in
Phase 2 when ``LightstreamerPriceFeed`` reads cached ticks.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential
from trading_ig.rest import IGException

from ..auth.ig_auth import IGSession
from .scaling import resolve_scaling_factor

logger = logging.getLogger(__name__)


class StalePriceError(Exception):
    """Raised when the most recent tick for an epic is older than the
    caller's ``max_age_seconds`` tolerance.

    Only raised by ``LightstreamerPriceFeed`` (Phase 2). ``RestPriceFeed``
    fetches synchronously on each call and cannot raise this.

    Attributes:
        epic: The epic whose tick was stale.
        age_seconds: How old the most recent tick actually is.
    """

    def __init__(self, epic: str, age_seconds: float) -> None:
        self.epic = epic
        self.age_seconds = age_seconds
        super().__init__(
            f"StalePriceError: {epic} last tick was {age_seconds:.1f}s ago"
        )


@dataclass(frozen=True)
class Tick:
    """A single market-data snapshot for one epic.

    Fields mirror the keys in the dict that ``MarketData.get_market_snapshot``
    returned prior to this refactor — preserving that contract is the whole
    point of Phase 1. Values are **scaled** (divided by IG's scalingFactor)
    so downstream code receives prices in the same unit as scan triggers.

    ``pct_change`` is the one unscaled field — IG returns it as a
    percentage already.
    """

    epic: str
    bid: Optional[float]
    ask: Optional[float]
    last_traded: Optional[float]
    market_status: Optional[str]
    high: Optional[float]
    low: Optional[float]
    net_change: Optional[float]
    pct_change: Optional[float]
    update_time_utc: Optional[str]
    scaling_factor: float
    updated_at_utc: datetime = field(default_factory=datetime.utcnow)

    def as_snapshot_dict(self) -> dict:
        """Return the Tick as the legacy 10-field dict shape that
        ``MarketData.get_market_snapshot`` used to emit. Preserves the
        public contract of that method across Phase 1.

        Returns an empty dict when the tick carries no usable price
        data — matches the pre-refactor behaviour where the method
        returned ``{}`` on IG's empty-snapshot responses."""
        if self.is_empty():
            return {}
        return {
            "bid": self.bid,
            "ask": self.ask,
            "last_traded": self.last_traded,
            "market_status": self.market_status,
            "high": self.high,
            "low": self.low,
            "net_change": self.net_change,
            "pct_change": self.pct_change,
            "update_time_utc": self.update_time_utc,
            "scaling_factor": self.scaling_factor,
        }

    def is_empty(self) -> bool:
        """True when the tick has no usable price data. Used to preserve
        the pre-refactor ``{}`` return on IG empty-snapshot responses."""
        return (
            self.bid is None
            and self.ask is None
            and self.last_traded is None
            and self.market_status is None
        )


class PriceFeed(ABC):
    """Abstract price source. Implementations: ``RestPriceFeed`` (today's
    behaviour) and ``LightstreamerPriceFeed`` (Phase 2).

    The ``subscribe``/``unsubscribe``/``start``/``stop`` lifecycle is a
    no-op for ``RestPriceFeed`` but required for the streaming feed.
    """

    @abstractmethod
    def latest(self, epic: str, max_age_seconds: float = 10.0) -> Tick:
        """Return the most recent ``Tick`` for ``epic``.

        ``LightstreamerPriceFeed`` raises ``StalePriceError`` if the
        cached tick is older than ``max_age_seconds``. ``RestPriceFeed``
        fetches fresh on each call and cannot raise this.
        """

    def get_scaling_factor(self, epic: str) -> float:
        """Return the cached scaling factor for ``epic``, or 1.0 if
        we've never fetched a tick for it. Implementations may override
        with a more efficient lookup, but the default inspects the cache
        populated by previous ``latest()`` calls."""
        return self._scale_cache.get(epic, 1.0)  # type: ignore[attr-defined]

    # Lifecycle — no-ops for REST, implemented for Lightstreamer.
    def start(self) -> None:
        """Start the feed. No-op for REST; connects the LS client."""

    def stop(self) -> None:
        """Stop the feed. No-op for REST; disconnects the LS client."""

    def subscribe(self, epic: str) -> None:
        """Subscribe to updates for ``epic``. No-op for REST."""

    def unsubscribe(self, epic: str) -> None:
        """Unsubscribe from ``epic``. No-op for REST."""


class RestPriceFeed(PriceFeed):
    """Synchronous REST price feed — today's behaviour, wrapped.

    Each ``latest(epic)`` call hits IG's ``/markets/{epic}`` endpoint and
    builds a ``Tick``. The scaling factor is resolved using the same
    heuristic as the pre-refactor inline code and cached per-epic.

    Phase 1 preserves the ``@retry`` decorator from
    ``MarketData.get_market_snapshot`` so transient token-invalid errors
    get retried identically to before.
    """

    def __init__(self, session: IGSession) -> None:
        self._session = session
        self._scale_cache: dict[str, float] = {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def latest(self, epic: str, max_age_seconds: float = 10.0) -> Tick:
        """Fetch a fresh snapshot from IG and return a scaled ``Tick``.

        ``max_age_seconds`` is accepted for interface parity but ignored
        here — a REST fetch is by definition fresh at return time.
        """
        try:
            result = self._session.service.fetch_market_by_epic(epic)
        except IGException as e:
            logger.error("IG fetch_market_by_epic failed for %s: %s", epic, e)
            raise
        except Exception as e:
            logger.warning(
                "Market snapshot raised %s for %s: %s — letting @retry try again.",
                type(e).__name__, epic, e,
            )
            raise

        # trading_ig returns either a dict (JSON) or a pydantic-like object.
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        if not isinstance(result, dict):
            logger.warning("Unexpected snapshot shape for %s: %r", epic, type(result))
            return _empty_tick(epic)

        snap = result.get("snapshot") or {}
        if not snap:
            return _empty_tick(epic)

        def _f(v):
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        instrument = result.get("instrument") or {}
        bid_raw = _f(snap.get("bid"))
        offer_raw = _f(snap.get("offer"))
        scaling_factor = resolve_scaling_factor(epic, instrument, bid_raw, offer_raw)
        self._scale_cache[epic] = scaling_factor

        last_traded_raw = None
        for key in ("lastTraded", "lastTradedPrice"):
            if key in snap:
                last_traded_raw = _f(snap.get(key))
                break
        if last_traded_raw is None and bid_raw is not None and offer_raw is not None:
            last_traded_raw = (bid_raw + offer_raw) / 2.0

        high_raw = _f(snap.get("high"))
        low_raw = _f(snap.get("low"))
        net_change_raw = _f(snap.get("netChange"))

        def _scale(v):
            return None if v is None else v / scaling_factor

        return Tick(
            epic=epic,
            bid=_scale(bid_raw),
            ask=_scale(offer_raw),
            last_traded=_scale(last_traded_raw),
            market_status=snap.get("marketStatus"),
            high=_scale(high_raw),
            low=_scale(low_raw),
            net_change=_scale(net_change_raw),
            pct_change=_f(snap.get("percentageChange")),  # already %
            update_time_utc=snap.get("updateTime") or snap.get("updateTimeUTC"),
            scaling_factor=scaling_factor,
        )


def _empty_tick(epic: str) -> Tick:
    """A Tick representing "no data this snapshot" — every field None
    except epic + scaling_factor (which defaults to 1.0, matching the
    pre-refactor fallback in MarketData.get_scaling_factor)."""
    return Tick(
        epic=epic,
        bid=None,
        ask=None,
        last_traded=None,
        market_status=None,
        high=None,
        low=None,
        net_change=None,
        pct_change=None,
        update_time_utc=None,
        scaling_factor=1.0,
    )
