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

from trading_ig.rest import IGException

from ..auth.ig_auth import IGSession
from ..models.common import Direction

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
    """Narrow IG REST surface used by the MonitorLoop fill/exit paths."""

    def __init__(self, session: IGSession):
        self._session = session

    @property
    def ig(self):
        return self._session.service

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

        kwargs = dict(
            currency_code="GBP",
            direction=ig_direction,
            epic=epic,
            expiry=expiry,
            force_open=True,
            order_type="MARKET",
            size=size,
            guaranteed_stop=False,
            trailing_stop=False,
            level=None,
            limit_distance=None,
            limit_level=limit_level,
            quote_id=None,
            stop_distance=stop_distance,
            stop_level=stop_price,
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
        fill_price = confirm.get("level")
        return OrderResult(
            success=ok,
            deal_reference=deal_reference,
            deal_id=confirm.get("dealId", ""),
            fill_price=float(fill_price) if fill_price is not None else None,
            stake_gbp_per_pt=size,
            stop_price=stop_price,
            deal_status=deal_status,
            reason_code=reason if not ok else "SUCCESS",
            raw=confirm,
        )

    # ------------------------------------------------------------------
    # Move the stop on an open position
    # ------------------------------------------------------------------

    def modify_stop(self, deal_id: str, new_stop_price: float) -> StopModifyResult:
        """Update the stop-level on an open IG position.

        IG's REST update endpoint takes stop-level in price (not distance) so
        the ``new_stop_price`` we compute from the trail ladder is passed
        through directly.
        """
        try:
            resp = self.ig.update_open_position(
                limit_level=None,
                stop_level=new_stop_price,
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
        try:
            resp = self.ig.close_open_position(
                deal_id=deal_id,
                direction=_close_direction(direction),
                epic=epic,
                expiry=_expiry_for_epic(epic),
                level=None,
                order_type="MARKET",
                quote_id=None,
                size=size,
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
        fill_price = confirm.get("level")
        return CloseResult(
            success=ok,
            deal_id=deal_id,
            fill_price=float(fill_price) if fill_price is not None else None,
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
