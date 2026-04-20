"""
broker — thin wrapper around the trading_ig IGService for order placement,
stop modification, and position close.

Scope: one method per verb, structured return types, error normalisation. No
rule logic lives here — the MonitorLoop + trail_manager decide *what* to do;
this module just talks to IG.

Design notes
------------
- UK spread betting is always the account flavour (see Settings.account_mode).
  Every order ships with ``currency_code="GBP"``, ``expiry`` resolved from the
  epic, and ``force_open=True`` (so a BUY on an existing SHORT opens a new
  position rather than closing the old one — matters if we ever have both
  directions running).
- We poll ``fetch_deal_by_deal_reference`` for a fixed number of attempts
  before giving up. IG confirmations usually land within 500ms but
  occasionally take 2–3 seconds; ``DEAL_POLL_ATTEMPTS × DEAL_POLL_DELAY`` caps
  the worst case at ~10s.
- Non-success confirmations (dealStatus != OPENED/ACCEPTED) return
  ``success=False`` with the IG reason string in ``reason_code``. The caller
  decides whether to emit ORDER_PLACED + REJECTED_RISK_BUDGET, skip the
  candidate, etc.
- Exceptions from trading_ig are caught and normalised into a non-successful
  result so the monitor loop never dies on a single bad order. Retries at the
  HTTP layer are not done here — trading_ig's session handling already covers
  transient auth refreshes, and re-placing a MARKET order on a retry can
  double-fill.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from trading_ig.rest import IGException

from ..auth.ig_auth import IGSession
from ..models.common import Direction

if TYPE_CHECKING:
    from ..data.market_data import MarketData

logger = logging.getLogger(__name__)

# Poll cadence for deal confirmations. IG usually confirms in under a second
# but can take longer on DEMO.
DEAL_POLL_ATTEMPTS = 10
DEAL_POLL_DELAY_SECONDS = 1.0


def _ig_direction(direction: Direction) -> str:
    return "BUY" if direction == Direction.LONG else "SELL"


def _close_direction(direction: Direction) -> str:
    """The direction needed to flatten an existing position."""
    return "SELL" if direction == Direction.LONG else "BUY"


def _expiry_for_epic(epic: str) -> str:
    """DFB for daily funded bets; '-' (none) for cash markets.

    IG epics ending in ``.DAILY.IP`` are DFBs. Anything else (including
    ``.CASH.IP`` equities) uses ``-``.
    """
    return "DFB" if ".DAILY." in epic.upper() else "-"


@dataclass
class OrderResult:
    """Outcome of :meth:`Broker.place_open_position`."""

    success: bool
    deal_reference: str = ""
    deal_id: str = ""
    fill_price: float | None = None
    stake_gbp_per_pt: float = 0.0
    stop_price: float | None = None
    deal_status: str = ""
    reason_code: str = ""
    raw: dict | None = None


@dataclass
class StopModifyResult:
    """Outcome of :meth:`Broker.modify_stop`."""

    success: bool
    deal_id: str
    new_stop_price: float
    reason_code: str = ""
    raw: dict | None = None


@dataclass
class CloseResult:
    """Outcome of :meth:`Broker.close_position`."""

    success: bool
    deal_id: str
    fill_price: float | None = None
    closed_at_utc: datetime | None = None
    reason_code: str = ""
    raw: dict | None = None


class Broker:
    """Narrow IG REST surface used by the MonitorLoop fill/exit paths.

    When ``market_data`` is supplied, all price-unit arguments passed to
    IG (``stop_level``, ``stop_distance``, ``limit_level``) are rescaled
    from scan units (e.g., USD dollars) into IG's quoted units (e.g.,
    cents for ×100 epics) using
    :meth:`MarketData.to_ig_units`. Without a ``market_data`` the broker
    behaves as before — suitable for unit tests that mock at the
    trading_ig layer directly.
    """

    def __init__(
        self,
        session: IGSession,
        market_data: "MarketData | None" = None,
    ):
        self._session = session
        self._market_data = market_data

    @property
    def ig(self):
        return self._session.service

    def _to_ig(self, epic: str, value: float | None) -> float | None:
        """Scale a scan-unit price into IG quoted units for ``epic``.

        Returns ``value`` unchanged when no MarketData is attached —
        preserves pre-2026-04-17 behaviour for unit tests."""
        if self._market_data is None or value is None:
            return value
        return self._market_data.to_ig_units(epic, value)

    def _fetch_min_deal_size(self, epic: str) -> float:
        """Return IG's per-epic ``dealingRules.minDealSize.value`` in £/pt.

        Falls back to ``0.10`` on any fetch or parse failure. Used by
        :meth:`place_open_position` to clamp the descaled ``size`` so IG
        doesn't reject the order with a sub-minimum stake. An equivalent
        lives in legacy ``executor.py`` — kept independent here because
        the broker owns its own REST surface and executor.py isn't on
        the active path.
        """
        try:
            result = self.ig.fetch_market_by_epic(epic)
            if hasattr(result, "model_dump"):
                result = result.model_dump()
            rules = (result or {}).get("dealingRules") or {}
            min_size = (rules.get("minDealSize") or {}).get("value")
            if min_size is not None:
                return float(min_size)
            logger.warning(
                "Broker: fetch_market_by_epic for %s returned no "
                "dealingRules.minDealSize — using 0.10 fallback.",
                epic,
            )
        except Exception as e:  # noqa: BLE001 — defensive at the broker edge
            logger.warning(
                "Broker: fetch_market_by_epic failed for %s: %s — "
                "using 0.10 minDealSize fallback.",
                epic,
                e,
            )
        return 0.10

    # ------------------------------------------------------------------
    # Open a new spread-bet position at market
    # ------------------------------------------------------------------

    def place_open_position(
        self,
        *,
        epic: str,
        direction: Direction,
        size: float,
        stop_price: float | None = None,
        stop_distance: float | None = None,
        limit_level: float | None = None,
    ) -> OrderResult:
        """Open a spread bet at MARKET with an attached stop.

        Exactly one of ``stop_price`` or ``stop_distance`` should be provided.
        Prefer ``stop_price`` — the monitor already computed the pre-fill stop.

        Returns an OrderResult. A ``success=False`` result always carries a
        ``reason_code`` so the caller can log and move on.
        """
        ig_direction = _ig_direction(direction)
        expiry = _expiry_for_epic(epic)

        # Scale every price-unit argument from scan units into IG's
        # quoted units for this epic. stop_distance is a delta in the
        # same units as price so it scales identically to stop_level.
        ig_stop_level = self._to_ig(epic, stop_price)
        ig_stop_distance = self._to_ig(epic, stop_distance)
        ig_limit_level = self._to_ig(epic, limit_level)

        # Descale ``size`` symmetrically with the price levels above.
        # IG interprets ``size`` in the same minor-unit points as
        # ``level``, so a scan stake of £7.12/pt on a ×100 epic lands
        # as £712 per $1 display-move if we don't divide — notional
        # blows through the account and IG rejects with
        # INSUFFICIENT_FUNDS (DEMO 2026-04-20, ARM/JNJ).
        ig_size = size
        if self._market_data is not None:
            scale = self._market_data.get_scaling_factor(epic)
            if scale and scale != 1.0:
                ig_size = size / scale

            # Clamp at IG's per-epic minimum deal size. Below this IG
            # will refuse the order. When clamping triggers, effective
            # risk exceeds plan target — log WARNING so the divergence
            # shows up in the session journal.
            min_deal_size = self._fetch_min_deal_size(epic)
            if ig_size < min_deal_size:
                logger.warning(
                    "Broker: %s descaled size %.4f < minDealSize %.2f — "
                    "clamping to minimum. Effective risk will exceed "
                    "plan target.",
                    epic,
                    ig_size,
                    min_deal_size,
                )
                ig_size = min_deal_size

            if scale and scale != 1.0:
                logger.info(
                    "Broker: scaling %s ×%.4g → stop_level=%s "
                    "stop_distance=%s limit_level=%s; size %.4g → %.4g",
                    epic,
                    scale,
                    ig_stop_level,
                    ig_stop_distance,
                    ig_limit_level,
                    size,
                    ig_size,
                )

        kwargs = dict(
            currency_code="GBP",
            direction=ig_direction,
            epic=epic,
            expiry=expiry,
            force_open=True,
            order_type="MARKET",
            size=ig_size,
            guaranteed_stop=False,
            trailing_stop=False,
            level=None,
            limit_distance=None,
            limit_level=ig_limit_level,
            quote_id=None,
            stop_distance=ig_stop_distance,
            stop_level=ig_stop_level,
            time_in_force="EXECUTE_AND_ELIMINATE",
            trailing_stop_increment=None,
        )

        try:
            open_resp = self.ig.create_open_position(**kwargs)
        except IGException as e:
            logger.error("IG create_open_position failed (%s %s): %s", ig_direction, epic, e)
            return OrderResult(
                success=False,
                reason_code=str(e),
                stake_gbp_per_pt=size,
                stop_price=stop_price,
            )
        except Exception as e:  # noqa: BLE001 — defensive at the broker edge
            logger.exception("Unexpected error placing order (%s %s): %s", ig_direction, epic, e)
            return OrderResult(
                success=False,
                reason_code=f"UNHANDLED: {e}",
                stake_gbp_per_pt=size,
                stop_price=stop_price,
            )

        if hasattr(open_resp, "model_dump"):
            open_resp = open_resp.model_dump()
        deal_reference = (open_resp or {}).get("dealReference", "")
        if not deal_reference:
            logger.warning("No dealReference returned for %s %s: %r", ig_direction, epic, open_resp)
            return OrderResult(
                success=False,
                reason_code="NO_DEAL_REFERENCE",
                stake_gbp_per_pt=size,
                stop_price=stop_price,
                raw=open_resp,
            )

        confirm = self._poll_confirmation(deal_reference)
        if confirm is None:
            return OrderResult(
                success=False,
                deal_reference=deal_reference,
                reason_code="CONFIRM_TIMEOUT",
                stake_gbp_per_pt=size,
                stop_price=stop_price,
            )

        deal_status = (confirm.get("dealStatus") or "").upper()
        reason = (confirm.get("reason") or "").upper()
        ok = deal_status in ("ACCEPTED", "OPENED") or reason == "SUCCESS"
        # IG returns ``level`` in its quoted units; convert back to scan
        # units so the monitor stores a fill_price directly comparable
        # with plan.stop_price / trigger levels.
        raw_fill = confirm.get("level")
        fill_price: float | None = None
        if raw_fill is not None:
            fill_price = float(raw_fill)
            if self._market_data is not None:
                scale = self._market_data.get_scaling_factor(epic)
                if scale and scale != 1.0:
                    fill_price = fill_price / scale
        return OrderResult(
            success=ok,
            deal_reference=deal_reference,
            deal_id=confirm.get("dealId", ""),
            fill_price=fill_price,
            stake_gbp_per_pt=size,
            stop_price=stop_price,
            deal_status=deal_status,
            reason_code=reason if not ok else "SUCCESS",
            raw=confirm,
        )

    # ------------------------------------------------------------------
    # Move the stop on an open position
    # ------------------------------------------------------------------

    def modify_stop(
        self,
        deal_id: str,
        new_stop_price: float,
        *,
        epic: str | None = None,
    ) -> StopModifyResult:
        """Update the stop-level on an open IG position.

        IG's REST update endpoint takes stop-level in price (not distance) so
        the ``new_stop_price`` we compute from the trail ladder is passed
        through after scaling into IG quoted units for ``epic``.

        ``epic`` is keyword-only and optional to keep existing MockBroker /
        unit-test callers working without churn. In production the
        MonitorLoop passes ``epic=plan.ig_epic`` so the scaling factor is
        applied; omitted ``epic`` = no scaling, same as pre-2026-04-17.
        """
        ig_stop_level: float = (
            self._to_ig(epic, new_stop_price) if epic else new_stop_price
        )
        try:
            resp = self.ig.update_open_position(
                limit_level=None,
                stop_level=ig_stop_level,
                deal_id=deal_id,
            )
        except IGException as e:
            logger.error("IG update_open_position failed (deal_id=%s): %s", deal_id, e)
            return StopModifyResult(
                success=False,
                deal_id=deal_id,
                new_stop_price=new_stop_price,
                reason_code=str(e),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected error modifying stop (deal_id=%s)", deal_id)
            return StopModifyResult(
                success=False,
                deal_id=deal_id,
                new_stop_price=new_stop_price,
                reason_code=f"UNHANDLED: {e}",
            )

        if hasattr(resp, "model_dump"):
            resp = resp.model_dump()
        deal_reference = (resp or {}).get("dealReference", "")
        confirm = (
            self._poll_confirmation(deal_reference) if deal_reference else (resp or {})
        )
        if confirm is None:
            return StopModifyResult(
                success=False,
                deal_id=deal_id,
                new_stop_price=new_stop_price,
                reason_code="CONFIRM_TIMEOUT",
            )
        reason = (confirm.get("reason") or "").upper()
        deal_status = (confirm.get("dealStatus") or "").upper()
        ok = deal_status in ("ACCEPTED", "OPENED", "AMENDED") or reason == "SUCCESS"
        return StopModifyResult(
            success=ok,
            deal_id=deal_id,
            new_stop_price=new_stop_price,
            reason_code=reason if not ok else "SUCCESS",
            raw=confirm,
        )

    # ------------------------------------------------------------------
    # Close a position at market
    # ------------------------------------------------------------------

    def close_position(
        self,
        *,
        deal_id: str,
        direction: Direction,
        epic: str,
        size: float,
    ) -> CloseResult:
        """Flatten an open position at market.

        ``direction`` is the *original* trade direction — we flip it here so
        a LONG gets a SELL to close.
        """
        # Descale ``size`` and clamp at minDealSize for symmetry with
        # place_open_position — the close crosses the same REST
        # boundary and IG will reject on the same grounds. In practice
        # ``size`` on close mirrors the (already-clamped) open, so the
        # clamp here is defence-in-depth against future divergence.
        ig_size = size
        if self._market_data is not None:
            scale = self._market_data.get_scaling_factor(epic)
            if scale and scale != 1.0:
                ig_size = size / scale

            min_deal_size = self._fetch_min_deal_size(epic)
            if ig_size < min_deal_size:
                logger.warning(
                    "Broker: %s close descaled size %.4f < minDealSize "
                    "%.2f — clamping to minimum.",
                    epic,
                    ig_size,
                    min_deal_size,
                )
                ig_size = min_deal_size

            if scale and scale != 1.0:
                logger.info(
                    "Broker: close %s size %.4g → %.4g (÷%.4g)",
                    epic,
                    size,
                    ig_size,
                    scale,
                )

        try:
            resp = self.ig.close_open_position(
                deal_id=deal_id,
                direction=_close_direction(direction),
                epic=epic,
                expiry=_expiry_for_epic(epic),
                level=None,
                order_type="MARKET",
                quote_id=None,
                size=ig_size,
                time_in_force="EXECUTE_AND_ELIMINATE",
            )
        except IGException as e:
            logger.error("IG close_open_position failed (deal_id=%s): %s", deal_id, e)
            return CloseResult(success=False, deal_id=deal_id, reason_code=str(e))
        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected error closing position (deal_id=%s)", deal_id)
            return CloseResult(
                success=False,
                deal_id=deal_id,
                reason_code=f"UNHANDLED: {e}",
            )

        if hasattr(resp, "model_dump"):
            resp = resp.model_dump()
        deal_reference = (resp or {}).get("dealReference", "")
        confirm = (
            self._poll_confirmation(deal_reference) if deal_reference else (resp or {})
        )
        if confirm is None:
            return CloseResult(success=False, deal_id=deal_id, reason_code="CONFIRM_TIMEOUT")

        deal_status = (confirm.get("dealStatus") or "").upper()
        reason = (confirm.get("reason") or "").upper()
        ok = deal_status in ("ACCEPTED", "OPENED", "CLOSED") or reason == "SUCCESS"
        # Descale close fill price back to scan units so downstream
        # P&L / journal entries use the same unit as plan.stop_price.
        raw_fill = confirm.get("level")
        fill_price: float | None = None
        if raw_fill is not None:
            fill_price = float(raw_fill)
            if self._market_data is not None:
                scale = self._market_data.get_scaling_factor(epic)
                if scale and scale != 1.0:
                    fill_price = fill_price / scale
        return CloseResult(
            success=ok,
            deal_id=deal_id,
            fill_price=fill_price,
            closed_at_utc=datetime.utcnow(),
            reason_code=reason if not ok else "SUCCESS",
            raw=confirm,
        )

    # ------------------------------------------------------------------
    # Internal — confirmation polling
    # ------------------------------------------------------------------

    def _poll_confirmation(self, deal_reference: str) -> dict | None:
        """Poll IG confirms until dealStatus is populated or we time out."""
        for attempt in range(DEAL_POLL_ATTEMPTS):
            try:
                result = self.ig.fetch_deal_by_deal_reference(deal_reference)
            except IGException as e:
                logger.debug(
                    "confirm poll %d/%d for %s: %s",
                    attempt + 1,
                    DEAL_POLL_ATTEMPTS,
                    deal_reference,
                    e,
                )
                time.sleep(DEAL_POLL_DELAY_SECONDS)
                continue
            if hasattr(result, "model_dump"):
                result = result.model_dump()
            if result and (result.get("dealStatus") or result.get("reason")):
                return result
            time.sleep(DEAL_POLL_DELAY_SECONDS)
        logger.warning(
            "Confirmation timeout for dealReference=%s after %d attempts",
            deal_reference,
            DEAL_POLL_ATTEMPTS,
        )
        return None


__all__ = ["Broker", "CloseResult", "OrderResult", "StopModifyResult"]
