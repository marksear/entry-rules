"""
Entry Classifier — determines entry type and applies gap/execution rules.

Takes a signal that passed all gates and produces an OrderInstruction
(or a Skip/Reject if gap or chase rules apply).
"""

from __future__ import annotations

import logging
import math

import pandas as pd

from ..config.rejection_codes import RejectCode
from ..config.settings import Settings, get_settings
from ..indicators.price import opening_range, vwap_value
from ..indicators.volume import avg_volume
from ..models.common import (
    Decision, Direction, EntryType, Market, TradeSignal,
)
from ..models.gate_result import GateResult
from ..models.order_instruction import OrderInstruction, OrderType, Tranche
from ..models.position import Position

logger = logging.getLogger(__name__)


class ClassifyResult:
    """Output of the classifier — either an OrderInstruction or a reason not to trade."""

    def __init__(
        self,
        decision: Decision,
        instruction: OrderInstruction | None = None,
        reject_code: RejectCode | None = None,
        reason: str = "",
    ):
        self.decision = decision
        self.instruction = instruction
        self.reject_code = reject_code
        self.reason = reason


class EntryClassifier:
    """Classify the entry and construct the order instruction."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()

    def classify(
        self,
        signal: TradeSignal,
        bars: pd.DataFrame,
        gate_result: GateResult,
        risk_shares: int,
        risk_tranche_1: int,
        risk_tranche_2: int,
        risk_amount: float,
        risk_pct: float,
        spread_pct: float | None = None,
        spread_adjusted: bool = False,
        use_cfd: bool = False,
        stamp_duty: bool = False,
        intraday_bars: pd.DataFrame | None = None,
    ) -> ClassifyResult:
        """
        Full classification and order construction.

        Args:
            signal: The original trade signal
            bars: Daily OHLCV bars
            gate_result: Result of gate evaluation
            risk_*: Position sizing from RiskManager
            spread_pct: Current bid-ask spread (UK)
            intraday_bars: 5-min bars for gap protocols (optional)

        Returns:
            ClassifyResult with OrderInstruction if approved
        """
        s = self._settings
        close = bars["close"]
        current_close = float(close.iloc[-1])
        prior_close = float(close.iloc[-2]) if len(close) > 1 else current_close

        # ── Check for gap ─────────────────────────────────────
        gap_pct = (current_close - prior_close) / prior_close
        gap_classified = abs(gap_pct) > s.gap_threshold

        if gap_classified:
            result = self._handle_gap(
                signal, gap_pct, bars, intraday_bars,
                risk_shares, risk_tranche_1, risk_tranche_2,
                risk_amount, risk_pct,
                spread_pct, spread_adjusted, use_cfd, stamp_duty,
            )
            if result is not None:
                return result

        # ── Chase rule ────────────────────────────────────────
        # If the stock opened too far past the pivot without a gap classification
        current_open = float(bars["open"].iloc[-1]) if "open" in bars.columns else current_close

        if signal.direction == Direction.LONG:
            if current_open > signal.pivot_level * (1 + s.chase_limit) and not gap_classified:
                return ClassifyResult(
                    decision=Decision.SKIP,
                    reject_code=RejectCode.R08,
                    reason=f"Opened {((current_open/signal.pivot_level)-1):.1%} above pivot without gap",
                )
        else:
            if current_open < signal.pivot_level * (1 - s.chase_limit) and not gap_classified:
                return ClassifyResult(
                    decision=Decision.SKIP,
                    reject_code=RejectCode.R08,
                    reason=f"Opened {((signal.pivot_level/current_open)-1):.1%} below breakdown without gap",
                )

        # ── Construct order based on entry type ───────────────
        order_type, trigger, limit = self._determine_order_params(
            signal, current_close, bars
        )

        stop_price = self._calculate_stop(signal, bars)
        risk_per_share = abs(signal.pivot_level - stop_price)

        instruction = OrderInstruction(
            signal_id=signal.signal_id,
            ticker=signal.ticker,
            ig_epic=signal.ig_epic,
            market=signal.market,
            direction=signal.direction,
            entry_type=signal.entry_type,
            entry_price=trigger or signal.pivot_level,
            stop_price=stop_price,
            risk_per_share=risk_per_share,
            total_shares=risk_shares,
            tranche_1=Tranche(
                number=1,
                size=risk_tranche_1,
                order_type=order_type,
                trigger_price=trigger,
                limit_price=limit,
            ),
            tranche_2=Tranche(
                number=2,
                size=risk_tranche_2,
                order_type=OrderType.LIMIT,  # T2 is always a follow-through
            ),
            risk_amount=risk_amount,
            portfolio_risk_pct=risk_pct,
            spread_pct=spread_pct,
            spread_adjusted=spread_adjusted,
            stamp_duty_applies=stamp_duty,
            use_cfd=use_cfd,
        )

        return ClassifyResult(
            decision=Decision.ENTER,
            instruction=instruction,
        )

    def _handle_gap(
        self,
        signal: TradeSignal,
        gap_pct: float,
        bars: pd.DataFrame,
        intraday_bars: pd.DataFrame | None,
        risk_shares: int,
        risk_tranche_1: int,
        risk_tranche_2: int,
        risk_amount: float,
        risk_pct: float,
        spread_pct: float | None,
        spread_adjusted: bool,
        use_cfd: bool,
        stamp_duty: bool,
    ) -> ClassifyResult | None:
        """Handle gap protocols (Rules L9/L10/S9/S10)."""
        s = self._settings

        if signal.direction == Direction.LONG:
            if gap_pct > 0:
                # Gap up on a long signal
                if signal.base_stage >= 3:
                    return ClassifyResult(
                        decision=Decision.REJECT,
                        reject_code=RejectCode.R04,
                        reason=f"Late-stage ({signal.base_stage}) exhaustion gap",
                    )
                # Early-stage BGU — proceed with gap protocol
                # (handled normally with opening range entry)
                return None

            else:
                # Gap down on a long signal
                return ClassifyResult(
                    decision=Decision.MONITOR,
                    reject_code=RejectCode.R09,
                    reason="Gap-down — 3-day reclaim window active",
                )

        else:  # SHORT
            if gap_pct < 0:
                # Gap down on a short signal
                if signal.base_stage < 3:
                    return ClassifyResult(
                        decision=Decision.REJECT,
                        reject_code=RejectCode.R19,
                        reason="Early-stage gap down — possible shakeout",
                    )
                # Late-stage SGD — proceed
                return None

            else:
                # Gap up on a short signal
                return ClassifyResult(
                    decision=Decision.MONITOR,
                    reject_code=RejectCode.R09,
                    reason="Gap-up against short — 3-day reclaim window active",
                )

    def _determine_order_params(
        self, signal: TradeSignal, current_close: float, bars: pd.DataFrame
    ) -> tuple[OrderType, float | None, float | None]:
        """
        Determine order type and prices based on entry type.

        Returns (order_type, trigger_price, limit_price)
        """
        s = self._settings
        pivot = signal.pivot_level

        if signal.direction == Direction.LONG:
            match signal.entry_type:
                case EntryType.L_A | EntryType.L_D | EntryType.L_E:
                    # Breakout entries: buy-stop limit
                    trigger = pivot * 1.01  # 1% above pivot
                    limit = pivot * (1 + s.chase_limit + 0.02)  # 5% max chase
                    return OrderType.STOP_LIMIT, trigger, limit

                case EntryType.L_B:
                    # Pullback to EMA: limit order at EMA
                    from ..indicators.moving_averages import ema_value
                    ema_10 = ema_value(bars["close"], s.ema_fast)
                    ema_20 = ema_value(bars["close"], s.ema_slow)
                    # Use whichever EMA the price is closer to
                    entry_level = ema_10 if abs(current_close - ema_10) < abs(current_close - ema_20) else ema_20
                    return OrderType.LIMIT, None, entry_level

                case EntryType.L_C:
                    # BGU: buy-stop above opening range high
                    # Trigger/limit set at opening range level
                    return OrderType.STOP, pivot, None

        else:  # SHORT
            match signal.entry_type:
                case EntryType.S_A | EntryType.S_D | EntryType.S_E:
                    # Breakdown entries: sell-stop limit
                    trigger = pivot * 0.99  # 1% below breakdown
                    limit = pivot * (1 - s.chase_limit - 0.02)
                    return OrderType.STOP_LIMIT, trigger, limit

                case EntryType.S_B:
                    # Rally to EMA: limit order at declining EMA
                    from ..indicators.moving_averages import ema_value
                    ema_10 = ema_value(bars["close"], s.ema_fast)
                    ema_20 = ema_value(bars["close"], s.ema_slow)
                    entry_level = ema_10 if abs(current_close - ema_10) < abs(current_close - ema_20) else ema_20
                    return OrderType.LIMIT, None, entry_level

                case EntryType.S_C:
                    # SGD: sell-stop below opening range low
                    return OrderType.STOP, pivot, None

        # Fallback
        return OrderType.LIMIT, None, pivot

    def _calculate_stop(self, signal: TradeSignal, bars: pd.DataFrame) -> float:
        """
        Calculate the initial stop-loss price based on entry type.
        """
        pivot = signal.pivot_level
        low = bars["low"]
        high = bars["high"]

        if signal.direction == Direction.LONG:
            match signal.entry_type:
                case EntryType.L_A | EntryType.L_D | EntryType.L_E:
                    # Breakout: stop 7% below pivot (within 5-8% range)
                    return pivot * 0.93

                case EntryType.L_B:
                    # Pullback: stop below swing low
                    swing_low = float(low.tail(10).min())
                    return swing_low * 0.99  # 1% below swing low

                case EntryType.L_C:
                    # BGU: stop 3% below gap day low
                    gap_day_low = float(low.iloc[-1])
                    return gap_day_low * 0.97

        else:  # SHORT
            match signal.entry_type:
                case EntryType.S_A | EntryType.S_D | EntryType.S_E:
                    # Breakdown: stop 7% above breakdown
                    return pivot * 1.07

                case EntryType.S_B:
                    # Rally: stop above swing high
                    swing_high = float(high.tail(10).max())
                    return swing_high * 1.01

                case EntryType.S_C:
                    # SGD: stop 3% above gap day high
                    gap_day_high = float(high.iloc[-1])
                    return gap_day_high * 1.03

        # Fallback: 7% stop
        if signal.direction == Direction.LONG:
            return pivot * 0.93
        return pivot * 1.07
