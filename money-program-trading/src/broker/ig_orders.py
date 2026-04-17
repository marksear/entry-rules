"""
IG Index order management — translates OrderInstructions into IG API calls.

Maps our entry types to IG's working order and position endpoints.
See IG_vs_CityIndex_Comparison.md §9 for the full mapping.
"""

from __future__ import annotations

import logging
import time

from trading_ig.rest import IGException

from ..auth.ig_auth import IGSession
from ..models.common import Direction, Market
from ..models.order_instruction import OrderInstruction, OrderType, Tranche
from ..models.position import Position

logger = logging.getLogger(__name__)


class OrderResult:
    """Result of an order placement attempt."""

    def __init__(
        self,
        success: bool,
        deal_reference: str = "",
        deal_id: str = "",
        fill_price: float | None = None,
        status: str = "",
        reason: str = "",
    ):
        self.success = success
        self.deal_reference = deal_reference
        self.deal_id = deal_id
        self.fill_price = fill_price
        self.status = status
        self.reason = reason


class IGOrders:
    """
    Translate OrderInstructions to IG API calls.
    """

    def __init__(self, session: IGSession):
        self._session = session

    @property
    def ig(self):
        return self._session.service

    # ── Place Entry Orders ────────────────────────────────────

    def place_tranche_1(self, instruction: OrderInstruction) -> OrderResult:
        """
        Place the first tranche (60%) of a trade.

        Uses working orders (pending) for stop/stop-limit entries,
        or direct position creation for market/limit orders.
        """
        t1 = instruction.tranche_1
        ig_direction = "BUY" if instruction.direction == Direction.LONG else "SELL"

        logger.info(
            "Placing T1: %s %s %d @ %s (stop=%s) | %s",
            ig_direction,
            instruction.ticker,
            t1.size,
            t1.trigger_price or t1.limit_price or "MARKET",
            instruction.stop_price,
            instruction.entry_type.value,
        )

        try:
            if t1.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
                return self._place_working_order(instruction, t1)
            else:
                return self._place_position(instruction, t1)

        except IGException as e:
            logger.error("Order placement failed: %s", e)
            return OrderResult(success=False, reason=str(e))

    def place_tranche_2(
        self, instruction: OrderInstruction, current_price: float
    ) -> OrderResult:
        """
        Place the second tranche (40%) — only if T1 is in profit.
        This is called by the monitoring loop, not at initial entry.
        """
        t2 = instruction.tranche_2
        ig_direction = "BUY" if instruction.direction == Direction.LONG else "SELL"

        logger.info(
            "Placing T2: %s %s %d @ MARKET | follow-through",
            ig_direction,
            instruction.ticker,
            t2.size,
        )

        try:
            return self._place_position(instruction, t2, use_market=True)
        except IGException as e:
            logger.error("T2 placement failed: %s", e)
            return OrderResult(success=False, reason=str(e))

    # ── Position Management ───────────────────────────────────

    def modify_stop(self, deal_id: str, new_stop: float) -> bool:
        """Update stop-loss on an open position."""
        try:
            self.ig.update_open_position(
                new_stop, None, deal_id  # stop_level, limit_level, deal_id
            )
            logger.info("Stop updated on %s → %.2f", deal_id, new_stop)
            return True
        except IGException as e:
            logger.error("Failed to modify stop on %s: %s", deal_id, e)
            return False

    def close_position(
        self, deal_id: str, direction: Direction, size: int
    ) -> OrderResult:
        """Close (or partially close) a position."""
        close_direction = "SELL" if direction == Direction.LONG else "BUY"

        logger.info("Closing position %s: %s %d", deal_id, close_direction, size)

        try:
            result = self.ig.close_open_position(
                deal_id=deal_id,
                direction=close_direction,
                size=size,
                order_type="MARKET",
            )

            deal_ref = result.get("dealReference", "")
            confirm = self._get_confirmation(deal_ref) if deal_ref else {}

            return OrderResult(
                success=True,
                deal_reference=deal_ref,
                deal_id=confirm.get("dealId", deal_id),
                fill_price=confirm.get("level"),
                status=confirm.get("dealStatus", ""),
            )

        except IGException as e:
            logger.error("Failed to close %s: %s", deal_id, e)
            return OrderResult(success=False, reason=str(e))

    def get_open_positions(self) -> list[dict]:
        """Fetch all currently open positions from IG."""
        try:
            positions = self.ig.fetch_open_positions()
            if positions is None:
                return []
            # trading-ig returns a DataFrame
            if hasattr(positions, "to_dict"):
                return positions.to_dict("records")
            return []
        except IGException as e:
            logger.error("Failed to fetch positions: %s", e)
            return []

    def cancel_working_order(self, deal_id: str) -> bool:
        """Cancel a pending working order."""
        try:
            self.ig.delete_working_order(deal_id)
            logger.info("Cancelled working order %s", deal_id)
            return True
        except IGException as e:
            logger.error("Failed to cancel order %s: %s", deal_id, e)
            return False

    # ── Internal ──────────────────────────────────────────────

    def _place_working_order(
        self, instruction: OrderInstruction, tranche: Tranche
    ) -> OrderResult:
        """Place a working (pending) order with attached stop."""
        ig_direction = "BUY" if instruction.direction == Direction.LONG else "SELL"
        ig_type = "STOP" if tranche.order_type == OrderType.STOP else "LIMIT"

        # For stop-limit, IG uses a stop working order
        # The "limit" part is handled by the limit_distance on the working order
        level = tranche.trigger_price or tranche.limit_price or instruction.entry_price

        # Calculate stop distance in points from entry level
        stop_distance = abs(level - instruction.stop_price)

        result = self.ig.create_working_order(
            epic=instruction.ig_epic,
            direction=ig_direction,
            size=tranche.size,
            level=level,
            type=ig_type,
            currency_code="GBP" if instruction.market == Market.UK else "USD",
            stop_distance=stop_distance,
            force_open=True,
            guaranteed_stop=instruction.use_guaranteed_stop,
            time_in_force="GOOD_TILL_CANCELLED",
        )

        deal_ref = result.get("dealReference", "")
        confirm = self._get_confirmation(deal_ref) if deal_ref else {}

        return OrderResult(
            success=confirm.get("dealStatus") == "OPENED" if confirm else bool(deal_ref),
            deal_reference=deal_ref,
            deal_id=confirm.get("dealId", ""),
            status=confirm.get("dealStatus", ""),
        )

    def _place_position(
        self,
        instruction: OrderInstruction,
        tranche: Tranche,
        use_market: bool = False,
    ) -> OrderResult:
        """Place a direct position (market or limit order)."""
        ig_direction = "BUY" if instruction.direction == Direction.LONG else "SELL"

        order_type = "MARKET" if use_market else "LIMIT"
        level = None if use_market else (tranche.limit_price or instruction.entry_price)

        stop_distance = abs(instruction.entry_price - instruction.stop_price)

        result = self.ig.create_open_position(
            epic=instruction.ig_epic,
            direction=ig_direction,
            size=tranche.size,
            order_type=order_type,
            level=level,
            currency_code="GBP" if instruction.market == Market.UK else "USD",
            stop_distance=stop_distance,
            force_open=True,
            guaranteed_stop=instruction.use_guaranteed_stop,
        )

        deal_ref = result.get("dealReference", "")
        confirm = self._get_confirmation(deal_ref) if deal_ref else {}

        return OrderResult(
            success=confirm.get("dealStatus") == "OPENED" if confirm else bool(deal_ref),
            deal_reference=deal_ref,
            deal_id=confirm.get("dealId", ""),
            fill_price=confirm.get("level"),
            status=confirm.get("dealStatus", ""),
        )

    def _get_confirmation(self, deal_reference: str) -> dict:
        """Poll for deal confirmation."""
        try:
            # Brief pause to allow IG to process
            time.sleep(0.5)
            confirm = self.ig.fetch_deal_by_deal_reference(deal_reference)
            return confirm if confirm else {}
        except Exception as e:
            logger.warning("Could not get confirmation for %s: %s", deal_reference, e)
            return {}
