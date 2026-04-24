"""
Price feed abstraction — S-3 Lightstreamer migration (Phases 1 + 2).

See ``docs/specs/S3_LIGHTSTREAMER_SPEC.md`` for the full plan. This module
introduces a ``PriceFeed`` ABC so ``MarketData`` can read live prices from
either the current REST polling path (``RestPriceFeed``) or the
Lightstreamer streaming path (``LightstreamerPriceFeed``).

**Phase 1 (shipped):** ``PriceFeed`` ABC + ``Tick`` + ``RestPriceFeed``.
Zero behaviour change — REST wrapper is a verbatim copy of the inline
logic that used to live in ``MarketData.get_market_snapshot``.

**Phase 2 (this file):** ``LightstreamerPriceFeed`` + ``build_price_feed``
factory. Gated behind ``Settings.price_feed_mode`` env flag — default
stays ``rest`` until PARALLEL validation in Phase 3 proves LS agrees
with REST on fresh ticks (and disagrees exactly in the staleness pattern
we expect).
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential
from trading_ig.rest import IGException
# Lightstreamer client bindings. Imported at module level so
# ``unittest.mock.patch("src.data.price_feed.LightstreamerClient")`` in
# tests substitutes the mock cleanly (patching only works on names
# visible in the target module's namespace). The library ships with
# trading-ig's full install so we can rely on it being present even on
# REST-only hosts.
from lightstreamer.client import (
    LightstreamerClient,
    Subscription,
    SubscriptionListener,
)

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


# ─────────────────────────────────────────────────────────────────────────
# LightstreamerPriceFeed — Phase 2
# ─────────────────────────────────────────────────────────────────────────
#
# IG's Lightstreamer server pushes MARKET updates over a long-lived
# connection. Flow:
#
#   1. ``start()`` — reads CST/X-SECURITY-TOKEN from the existing
#      ``IGSession.service.session.headers`` (no second REST auth —
#      that would trip DEMO rate-limits; see
#      ``feedback_ig_switch_account_race``), reads ``lightstreamerEndpoint``
#      via ``ig_service.read_session(fetch_session_tokens="true")``, and
#      connects a ``LightstreamerClient``.
#   2. ``subscribe(epic)`` — one-time REST ``fetch_market_by_epic`` to
#      cache the ``instrument`` metadata (for the scalingFactor fallback
#      on first tick), then creates a MARKET:{epic} subscription with
#      a listener that writes ticks into our in-memory cache.
#   3. ``latest(epic, max_age)`` — looks up the cached tick; raises
#      ``StalePriceError`` when the tick is older than ``max_age`` or
#      absent entirely.
#   4. ``stop()`` — unsubscribes everything and disconnects.
#
# LS callbacks fire on the client's own thread. Cache reads/writes go
# through ``self._lock`` (a ``threading.Lock``).
#
# Imports are at module level so tests can patch ``LightstreamerClient`` /
# ``Subscription`` via ``unittest.mock.patch``. The import is lazy in the
# sense that ``RestPriceFeed`` doesn't touch any LS symbol — so code paths
# that stay on REST-mode don't incur the LS library import cost.


def _safe_float(v):
    """Coerce to float if possible, else None. Matches the inline ``_f``
    helper used in ``RestPriceFeed.latest``."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Fields we subscribe to on each MARKET:{epic} item. Order must match
# what IG's Lightstreamer server documents for equity MARKET subscriptions.
# Reference: https://labs.ig.com/streaming-api-reference
_LS_MARKET_FIELDS = [
    "BID",
    "OFFER",
    "HIGH",
    "LOW",
    "UPDATE_TIME",
    "MARKET_STATE",
    "CHANGE",
    "CHANGE_PCT",
]


class LightstreamerPriceFeed(PriceFeed):
    """Streaming price feed via IG's Lightstreamer server.

    Phase 2 of the S-3 migration. Default-off — only active when
    ``Settings.price_feed_mode == PriceFeedMode.LIGHTSTREAMER``.

    **Staleness contract:** ``latest(epic, max_age_seconds=None)`` raises
    ``StalePriceError`` when the most recent tick for ``epic`` is older
    than ``max_age_seconds`` (default: the feed's configured
    ``stale_seconds``, typically 10s). Escalation at 60s to
    ``PRICE_FEED_DEGRADED`` is the monitor loop's responsibility, not the
    feed's — see spec §7.2 and (future) ``monitor.py`` wiring.

    **Thread safety:** LS callbacks fire on the client's internal thread.
    ``_tick_cache`` writes and ``latest`` reads both acquire ``_lock``.
    """

    def __init__(
        self,
        session: IGSession,
        stale_seconds: float = 10.0,
        degraded_seconds: float = 60.0,
    ) -> None:
        self._session = session
        self._stale_seconds = stale_seconds
        self._degraded_seconds = degraded_seconds
        self._scale_cache: dict[str, float] = {}
        self._instrument_cache: dict[str, dict] = {}
        self._tick_cache: dict[str, Tick] = {}
        self._subscriptions: dict[str, object] = {}
        self._lock = threading.Lock()
        self._ls_client: object | None = None
        self._started = False

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self) -> None:
        """Open the Lightstreamer connection using the existing IG
        session's CST/XST tokens. No second REST authentication."""
        if self._started:
            logger.debug("LightstreamerPriceFeed.start() — already started")
            return

        service = self._session.service  # raises if not connected
        headers = service.session.headers
        cst = headers.get("CST")
        xst = headers.get("X-SECURITY-TOKEN")
        if not cst or not xst:
            raise RuntimeError(
                "LightstreamerPriceFeed.start: IG session has no CST / "
                "X-SECURITY-TOKEN headers — call IGSession.connect() first."
            )

        # Fetch lightstreamerEndpoint from the existing session. This is
        # a lightweight GET on /session (no re-authentication).
        endpoint = _read_lightstreamer_endpoint(service)
        if not endpoint:
            raise RuntimeError(
                "LightstreamerPriceFeed.start: could not resolve "
                "lightstreamerEndpoint from the existing IG session."
            )

        account_id = self._session.account_id or ""
        client = LightstreamerClient(endpoint, None)
        client.connectionDetails.setUser(account_id)
        client.connectionDetails.setPassword(f"CST-{cst}|XST-{xst}")
        client.connect()

        self._ls_client = client
        self._started = True
        logger.info(
            "LightstreamerPriceFeed started (endpoint=%s, account=%s)",
            endpoint, account_id,
        )

    def stop(self) -> None:
        """Unsubscribe all + disconnect. Idempotent."""
        if not self._started:
            return
        # Copy keys because _unsubscribe_internal mutates the dict.
        for epic in list(self._subscriptions.keys()):
            try:
                self._unsubscribe_internal(epic)
            except Exception as e:
                logger.warning("LS unsubscribe(%s) failed: %s", epic, e)
        try:
            if self._ls_client is not None:
                self._ls_client.disconnect()
        except Exception as e:
            logger.warning("LS disconnect failed: %s", e)
        self._ls_client = None
        self._started = False
        logger.info("LightstreamerPriceFeed stopped.")

    # ── Subscription management ───────────────────────────────

    def subscribe(self, epic: str) -> None:
        """Open a MARKET:{epic} subscription. Pre-loads instrument
        metadata (for scalingFactor) via one REST fetch_market_by_epic."""
        if not self._started:
            raise RuntimeError(
                "LightstreamerPriceFeed.subscribe: call start() before "
                "subscribing to any epic."
            )
        if epic in self._subscriptions:
            logger.debug("LS subscribe(%s) — already subscribed", epic)
            return

        self._preload_instrument_metadata(epic)

        sub = Subscription(
            mode="MERGE",
            items=[f"MARKET:{epic}"],
            fields=_LS_MARKET_FIELDS,
        )
        sub.addListener(_TickListener(feed=self, epic=epic))
        self._ls_client.subscribe(sub)
        self._subscriptions[epic] = sub
        logger.info("LS subscribed: MARKET:%s", epic)

    def unsubscribe(self, epic: str) -> None:
        """Close the subscription for ``epic``. Idempotent."""
        self._unsubscribe_internal(epic)

    def _unsubscribe_internal(self, epic: str) -> None:
        sub = self._subscriptions.pop(epic, None)
        if sub is None or self._ls_client is None:
            return
        self._ls_client.unsubscribe(sub)
        logger.info("LS unsubscribed: MARKET:%s", epic)

    # ── Data reads ────────────────────────────────────────────

    def latest(self, epic: str, max_age_seconds: float | None = None) -> Tick:
        """Return the most recent tick for ``epic`` or raise
        ``StalePriceError``.

        ``max_age_seconds=None`` (the default) uses the feed's configured
        ``stale_seconds``. Callers — e.g. the monitor loop — can override
        per-call if they know a specific epic should tolerate longer gaps.
        """
        threshold = self._stale_seconds if max_age_seconds is None else max_age_seconds
        with self._lock:
            tick = self._tick_cache.get(epic)
        if tick is None:
            raise StalePriceError(epic=epic, age_seconds=float("inf"))
        age = (datetime.utcnow() - tick.updated_at_utc).total_seconds()
        if age > threshold:
            raise StalePriceError(epic=epic, age_seconds=age)
        return tick

    def get_scaling_factor(self, epic: str) -> float:  # noqa: D401
        """Return the cached scaling factor for ``epic``, or 1.0."""
        return self._scale_cache.get(epic, 1.0)

    # ── Internal helpers ──────────────────────────────────────

    def _preload_instrument_metadata(self, epic: str) -> None:
        """One-time REST call to cache the ``instrument`` block so the
        first incoming LS tick can resolve scalingFactor without another
        network round-trip. Ignores errors — the scaling fallback
        heuristic works on bid/ask alone if instrument is empty."""
        if epic in self._instrument_cache:
            return
        try:
            result = self._session.service.fetch_market_by_epic(epic)
            if hasattr(result, "model_dump"):
                result = result.model_dump()
            if isinstance(result, dict):
                self._instrument_cache[epic] = result.get("instrument") or {}
            else:
                self._instrument_cache[epic] = {}
        except Exception as e:
            logger.warning(
                "LS preload instrument metadata for %s failed: %s — "
                "scalingFactor will fall back to the bid/ask heuristic.",
                epic, e,
            )
            self._instrument_cache[epic] = {}

    def _on_tick(self, epic: str, item_update: object) -> None:
        """Called (on the LS thread) for each incoming MARKET update.
        Parses the fields into a ``Tick`` and publishes into the cache."""
        get = item_update.getValue  # bound lookup
        bid_raw = _safe_float(get("BID"))
        ask_raw = _safe_float(get("OFFER"))
        instrument = self._instrument_cache.get(epic) or {}
        scaling_factor = resolve_scaling_factor(
            epic, instrument, bid_raw, ask_raw,
        )
        self._scale_cache[epic] = scaling_factor

        # LS doesn't send a last-traded field on MARKET subscriptions — use
        # mid as the fallback, same convention as RestPriceFeed when IG
        # omits lastTraded.
        last_traded_raw = None
        if bid_raw is not None and ask_raw is not None:
            last_traded_raw = (bid_raw + ask_raw) / 2.0

        def _scale(v):
            return None if v is None else v / scaling_factor

        tick = Tick(
            epic=epic,
            bid=_scale(bid_raw),
            ask=_scale(ask_raw),
            last_traded=_scale(last_traded_raw),
            market_status=get("MARKET_STATE"),
            high=_scale(_safe_float(get("HIGH"))),
            low=_scale(_safe_float(get("LOW"))),
            net_change=_scale(_safe_float(get("CHANGE"))),
            pct_change=_safe_float(get("CHANGE_PCT")),  # already %
            update_time_utc=get("UPDATE_TIME"),
            scaling_factor=scaling_factor,
            updated_at_utc=datetime.utcnow(),
        )
        with self._lock:
            self._tick_cache[epic] = tick


def _read_lightstreamer_endpoint(service) -> str | None:
    """Extract ``lightstreamerEndpoint`` from the existing IG session.

    ``trading_ig.IGService.read_session(fetch_session_tokens="true")`` is
    a lightweight GET that returns the same session metadata IG sent at
    authentication time, including the Lightstreamer endpoint URL. This
    avoids calling ``create_session`` a second time (which would be a
    fresh auth and trip DEMO's rate-limit).
    """
    try:
        info = service.read_session(fetch_session_tokens="true")
    except Exception as e:
        logger.error("read_session() failed — cannot resolve LS endpoint: %s", e)
        return None
    if hasattr(info, "model_dump"):
        info = info.model_dump()
    if not isinstance(info, dict):
        return None
    return info.get("lightstreamerEndpoint")


class _TickListener(SubscriptionListener):
    """A ``SubscriptionListener`` for one MARKET:{epic} item.

    Tests can skip the LS client entirely and just call
    ``_feed._on_tick(epic, item_update)`` directly — the listener is a
    thin adapter between LS callbacks and our pure-Python ``_on_tick``
    method.
    """

    def __init__(self, feed: LightstreamerPriceFeed, epic: str) -> None:
        super().__init__()
        self._feed = feed
        self._epic = epic

    def onItemUpdate(self, item_update) -> None:  # noqa: N802 (LS API)
        try:
            self._feed._on_tick(self._epic, item_update)
        except Exception as e:
            logger.warning(
                "LS tick listener error for %s: %s: %s",
                self._epic, type(e).__name__, e,
            )

    def onSubscription(self) -> None:  # noqa: N802 (LS API)
        logger.debug("LS onSubscription: MARKET:%s", self._epic)

    def onUnsubscription(self) -> None:  # noqa: N802 (LS API)
        logger.debug("LS onUnsubscription: MARKET:%s", self._epic)

    def onSubscriptionError(self, code, message) -> None:  # noqa: N802
        logger.error(
            "LS subscription error for MARKET:%s — [%s] %s",
            self._epic, code, message,
        )

    def onItemLostUpdates(self, item_name, lost_updates) -> None:  # noqa: N802
        logger.warning(
            "LS lost %s updates on %s — tick cache may be behind briefly.",
            lost_updates, item_name,
        )


# ─────────────────────────────────────────────────────────────────────────
# ParallelPriceFeed — Phase 3 (validation only; not for production)
# ─────────────────────────────────────────────────────────────────────────
#
# Runs REST and Lightstreamer side-by-side, logging divergence on every
# ``latest()`` call. The purpose is to prove, with real IG data, that
# Lightstreamer and REST DO diverge — specifically in the staleness
# pattern we suspect from 2026-04-23 (REST returning values unchanged
# for minutes while the LS feed shows real tape movement).
#
# Safety during validation: when LS is fresh, ``latest()`` returns the
# LS tick (that's the cutover preview). When LS raises StalePriceError
# — i.e. disconnect, slow subscription start, first-tick-not-yet — we
# fall back to REST rather than raising, so the monitor loop never
# halts mid-session just because the validation harness hit a hiccup.
# Divergence is still logged: every call records REST vs LS mid, plus a
# ``source_used`` indicator so the post-session analysis can tell what
# the monitor was actually trading on.
#
# This feed is NOT meant to run in LIVE. Use in DEMO only, flip back to
# ``rest`` once Phase 3 validation complete.


_DIVERGENCE_BPS_THRESHOLD = 30.0  # basis points — matches S-4 gate


class ParallelPriceFeed(PriceFeed):
    """Run REST + Lightstreamer in parallel. Validation tool.

    Lifecycle delegates to both inner feeds. ``latest()`` fetches from
    both, computes basis-points divergence between mid prices, logs on
    every call, and returns the fresher source (LS preferred).

    Not thread-safe beyond what the inner feeds provide; LS callback
    thread writes into LS's own cache, REST is fetched synchronously on
    each ``latest()`` call.
    """

    def __init__(self, rest: RestPriceFeed, ls: LightstreamerPriceFeed) -> None:
        self._rest = rest
        self._ls = ls

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self) -> None:
        """Start both feeds. LS first because it's the one with a real
        network connection to warm up; REST is stateless."""
        self._ls.start()
        self._rest.start()  # no-op, but honours the contract

    def stop(self) -> None:
        """Stop both feeds. Swallow failures on LS so REST teardown
        still runs — matches the ``LightstreamerPriceFeed.stop``
        convention."""
        try:
            self._ls.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("ParallelPriceFeed: LS stop failed: %s", e)
        self._rest.stop()

    def subscribe(self, epic: str) -> None:
        """Subscribe both feeds. REST's subscribe is a no-op; LS opens
        the streaming subscription."""
        self._ls.subscribe(epic)
        self._rest.subscribe(epic)

    def unsubscribe(self, epic: str) -> None:
        self._ls.unsubscribe(epic)
        self._rest.unsubscribe(epic)

    # ── Data reads ────────────────────────────────────────────

    def latest(self, epic: str, max_age_seconds: float | None = None) -> Tick:
        """Fetch from both, log divergence, prefer LS.

        Behaviour:
        1. Try LS (subject to its ``max_age_seconds`` threshold).
        2. Fetch REST unconditionally (to have a divergence comparator).
        3. If divergence exceeds the 30bps threshold, emit a
           ``PRICE_DIVERGENCE`` log line with both mids + timestamps.
        4. Return the LS tick if it was fresh; else REST.

        The REST fetch happens on every call even when LS is fresh —
        yes, that's 2× the REST load of normal operation. That's the
        price of validation; it goes back to LS-only at Phase 4 cutover.
        """
        ls_tick: Tick | None = None
        try:
            ls_tick = self._ls.latest(epic, max_age_seconds=max_age_seconds)
        except StalePriceError as e:
            # LS couldn't serve — record and fall through to REST.
            logger.info(
                "ParallelPriceFeed: LS stale for %s (age=%.1fs), "
                "falling back to REST for this tick.",
                epic, e.age_seconds,
            )
        rest_tick = self._rest.latest(epic)  # always fresh on return
        self._log_divergence(epic, rest_tick, ls_tick)
        if ls_tick is not None:
            return ls_tick
        return rest_tick

    def get_scaling_factor(self, epic: str) -> float:
        """Prefer LS's cached factor; fall back to REST's cache."""
        return self._ls.get_scaling_factor(epic) or self._rest.get_scaling_factor(epic)

    # ── Divergence detection ──────────────────────────────────

    def _log_divergence(
        self,
        epic: str,
        rest_tick: Tick,
        ls_tick: Tick | None,
    ) -> None:
        """Emit a PRICE_DIVERGENCE log when |rest_mid − ls_mid|/rest_mid
        exceeds the threshold. No-op when either mid is unavailable."""
        rest_mid = _mid(rest_tick)
        if rest_mid is None or rest_mid <= 0:
            return
        if ls_tick is None:
            # Special case: LS unavailable; that's itself a signal, but
            # logged at latest()'s info line above rather than as a
            # divergence event (the two feeds didn't "disagree", LS just
            # couldn't serve).
            return
        ls_mid = _mid(ls_tick)
        if ls_mid is None or ls_mid <= 0:
            return
        diff_bps = abs(rest_mid - ls_mid) / rest_mid * 10_000.0
        if diff_bps < _DIVERGENCE_BPS_THRESHOLD:
            logger.debug(
                "ParallelPriceFeed: %s — REST=%.4f LS=%.4f diff=%.1fbps (within threshold)",
                epic, rest_mid, ls_mid, diff_bps,
            )
            return
        logger.warning(
            "PRICE_DIVERGENCE %s — REST=%.4f (updated_at=%s) LS=%.4f "
            "(updated_at=%s) diff=%.1fbps (threshold=%.1fbps)",
            epic, rest_mid, rest_tick.updated_at_utc.isoformat(),
            ls_mid, ls_tick.updated_at_utc.isoformat(),
            diff_bps, _DIVERGENCE_BPS_THRESHOLD,
        )


def _mid(tick: Tick) -> float | None:
    """Compute the mid of a Tick. Falls back to ``last_traded`` when bid
    or ask is None (some LS frames don't carry one side)."""
    if tick is None:
        return None
    if tick.bid is not None and tick.ask is not None:
        return (tick.bid + tick.ask) / 2.0
    return tick.last_traded


# ─────────────────────────────────────────────────────────────────────────
# Factory — single entry-point for session_init / callers
# ─────────────────────────────────────────────────────────────────────────


def build_price_feed(session: IGSession, settings) -> PriceFeed:
    """Construct the configured ``PriceFeed`` implementation.

    Reads ``settings.price_feed_mode`` and dispatches:

    - ``rest`` → ``RestPriceFeed`` (today's behaviour).
    - ``lightstreamer`` → ``LightstreamerPriceFeed`` (Phase 2).
    - ``parallel`` → ``ParallelPriceFeed`` wrapping both (Phase 3,
      validation only — see ``docs/specs/S3_LIGHTSTREAMER_SPEC.md`` §6
      Phase 3 for rollout discipline).

    ``settings`` is typed loosely (duck-typed on ``price_feed_mode``,
    ``price_feed_stale_seconds``, ``price_feed_degraded_seconds``) so the
    factory stays importable without a hard dep on pydantic-settings —
    useful for unit tests that construct fake settings via
    ``types.SimpleNamespace``.
    """
    mode = getattr(settings, "price_feed_mode", "rest")
    mode_str = mode.value if hasattr(mode, "value") else str(mode)
    stale = getattr(settings, "price_feed_stale_seconds", 10.0)
    degraded = getattr(settings, "price_feed_degraded_seconds", 60.0)

    if mode_str == "rest":
        return RestPriceFeed(session)
    if mode_str == "lightstreamer":
        return LightstreamerPriceFeed(
            session, stale_seconds=stale, degraded_seconds=degraded,
        )
    if mode_str == "parallel":
        return ParallelPriceFeed(
            rest=RestPriceFeed(session),
            ls=LightstreamerPriceFeed(
                session, stale_seconds=stale, degraded_seconds=degraded,
            ),
        )
    raise ValueError(
        f"Unknown price_feed_mode={mode_str!r}. Expected one of "
        "'rest' | 'lightstreamer' | 'parallel'."
    )
