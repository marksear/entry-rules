"""
The Pipeline — main orchestrator for the Entry Refinement Engine.

Signal → Gates → Classify → Risk → Order → Log

This is the single entry point. Feed it a TradeSignal,
and it will either place an order or reject it with a full audit trail.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..auth.ig_auth import IGSession
from ..broker.ig_orders import IGOrders
from ..config.settings import Settings, get_settings
from ..data.market_data import MarketData
from ..data.supplementary import (
    StubEarningsProvider,
    StubShortInterestProvider,
    StubCatalystProvider,
)
from ..indicators.relative_strength import price_performance
from ..indicators.volume import avg_volume
from ..logging_mod.audit_log import AuditLog
from ..logging_mod.db import Database
from ..models.audit_entry import (
    AuditEntry, AuditGates, AuditLevels, AuditVolume,
    AuditTranche, AuditShortSpecific, AuditUKSpecific,
)
from ..models.common import Decision, Direction, Market, TradeSignal
from ..models.position import Position

from .entry_classifier import EntryClassifier
from .gates import evaluate_long_gates, evaluate_short_gates
from .risk_manager import RiskManager

logger = logging.getLogger(__name__)


class Pipeline:
    """
    The complete signal processing pipeline.

    Usage:
        pipeline = Pipeline()
        pipeline.initialize()
        result = pipeline.process_signal(signal)
        pipeline.shutdown()
    """

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._session: IGSession | None = None
        self._market_data: MarketData | None = None
        self._orders: IGOrders | None = None
        self._risk_mgr = RiskManager(self._settings)
        self._classifier = EntryClassifier(self._settings)
        self._db: Database | None = None
        self._audit: AuditLog | None = None

        # Supplementary data (stubbed)
        self._earnings = StubEarningsProvider()
        self._short_interest = StubShortInterestProvider()
        self._catalysts = StubCatalystProvider()

        # State
        self._open_positions: list[Position] = []
        self._portfolio_value: float = 0.0
        self._universe_rs: dict[str, float] = {}

    def initialize(self) -> None:
        """Set up all connections and state."""
        logger.info("Initializing Entry Refinement Engine...")

        # Database
        self._db = Database(self._settings.db_path)
        self._db.initialize()
        self._audit = AuditLog(self._db)

        # IG Connection
        self._session = IGSession(self._settings)
        self._session.connect()
        self._market_data = MarketData(self._session)
        self._orders = IGOrders(self._session)

        # Portfolio state
        balance = self._session.get_account_balance()
        self._portfolio_value = float(balance.get("balance", 0))
        logger.info("Portfolio value: %.2f", self._portfolio_value)

        # Load open positions from IG
        self._sync_positions()

        logger.info("Engine initialized. Open positions: %d", len(self._open_positions))

    def shutdown(self) -> None:
        """Clean shutdown."""
        if self._session:
            self._session.disconnect()
        if self._db:
            self._db.close()
        logger.info("Engine shut down.")

    def process_signal(self, signal: TradeSignal) -> Decision:
        """
        Process a single trade signal through the full pipeline.

        This is the core method. Everything flows through here.

        Returns:
            Decision — ENTER, SKIP, REJECT, or MONITOR
        """
        logger.info(
            "═══ Processing signal: %s %s %s %s (pivot=%.2f) ═══",
            signal.ticker,
            signal.direction.value,
            signal.entry_type.value,
            signal.market.value,
            signal.pivot_level,
        )

        # ── Step 1: Resolve IG epic if needed ─────────────────
        if not signal.ig_epic:
            signal.ig_epic = self._market_data.resolve_epic(
                signal.ticker, signal.market.value
            )
            if not signal.ig_epic:
                logger.error("Could not resolve IG epic for %s", signal.ticker)
                self._log_reject(signal, Decision.REJECT, None, "Epic resolution failed")
                return Decision.REJECT

        # ── Step 2: Fetch bars ────────────────────────────────
        bars = self._market_data.get_daily_bars(signal.ig_epic, 260)
        if bars is None or len(bars) < 50:
            logger.error("Insufficient bar data for %s (%d bars)", signal.ticker, len(bars) if bars is not None else 0)
            self._log_reject(signal, Decision.REJECT, None, "Insufficient price data")
            return Decision.REJECT

        # ── Step 3: Get RS percentile ─────────────────────────
        rs = self._universe_rs.get(signal.ticker, 50.0)  # Default to median

        # ── Step 4: Evaluate gates ────────────────────────────
        if signal.direction == Direction.LONG:
            gate_result = evaluate_long_gates(bars, rs, self._settings)
        else:
            si_pct = self._short_interest.get_short_interest(signal.ticker)
            dtc = self._short_interest.get_days_to_cover(signal.ticker)
            borrow = self._short_interest.get_borrow_fee(signal.ticker)
            gate_result = evaluate_short_gates(
                bars, rs, signal.entry_type.value,
                si_pct, dtc, borrow, self._settings,
            )

        if not gate_result.passed:
            logger.info(
                "REJECTED at gates: %s | %s",
                signal.ticker,
                gate_result.reject_code.value if gate_result.reject_code else "?",
            )
            self._log_gate_reject(signal, gate_result, bars)
            return gate_result.decision

        # ── Step 5: Risk assessment ───────────────────────────
        spread_pct = None
        if signal.market == Market.UK:
            spread_pct = self._market_data.get_spread_pct(signal.ig_epic)

        risk_result = self._risk_mgr.calculate_position(
            entry_price=signal.pivot_level,
            stop_price=self._estimate_stop(signal, bars),
            direction=signal.direction,
            market=signal.market,
            portfolio_value=self._portfolio_value,
            open_positions=self._open_positions,
            spread_pct=spread_pct,
        )

        if not risk_result.approved:
            logger.info(
                "SKIPPED by risk manager: %s | %s | %s",
                signal.ticker,
                risk_result.reject_code.value if risk_result.reject_code else "?",
                risk_result.reason,
            )
            self._log_risk_reject(signal, gate_result, risk_result, bars)
            return Decision.SKIP

        # ── Step 6: Classify and construct order ──────────────
        classify_result = self._classifier.classify(
            signal=signal,
            bars=bars,
            gate_result=gate_result,
            risk_shares=risk_result.shares,
            risk_tranche_1=risk_result.tranche_1,
            risk_tranche_2=risk_result.tranche_2,
            risk_amount=risk_result.risk_amount,
            risk_pct=risk_result.portfolio_risk_pct,
            spread_pct=spread_pct,
            spread_adjusted=risk_result.spread_adjusted,
            use_cfd=risk_result.use_cfd,
            stamp_duty=risk_result.stamp_duty_applies,
        )

        if classify_result.decision != Decision.ENTER:
            logger.info(
                "%s by classifier: %s | %s | %s",
                classify_result.decision.value,
                signal.ticker,
                classify_result.reject_code.value if classify_result.reject_code else "?",
                classify_result.reason,
            )
            self._log_classify_result(signal, gate_result, risk_result, classify_result, bars)
            return classify_result.decision

        # ── Step 7: Place the order ───────────────────────────
        instruction = classify_result.instruction
        order_result = self._orders.place_tranche_1(instruction)

        if order_result.success:
            logger.info(
                "ORDER PLACED: %s %s %d shares @ %s | deal=%s",
                signal.direction.value,
                signal.ticker,
                instruction.tranche_1.size,
                order_result.fill_price or "pending",
                order_result.deal_id,
            )
        else:
            logger.error(
                "ORDER FAILED: %s | %s",
                signal.ticker,
                order_result.reason,
            )

        # ── Step 8: Log everything ────────────────────────────
        self._log_entry(signal, gate_result, risk_result, instruction, order_result, bars)

        return Decision.ENTER

    # ── Continuous Monitoring ─────────────────────────────────

    def run_continuous_checks(self) -> None:
        """
        Background process: earnings auto-exit, emergency covers,
        overnight gap compliance, UK spread monitoring.
        """
        for pos in self._open_positions:
            price_data = self._market_data.get_current_price(pos.ig_epic)
            current_price = price_data.get("mid", pos.entry_price)

            # Emergency cover (shorts only)
            if self._risk_mgr.check_emergency_cover(pos, current_price):
                self._orders.close_position(
                    pos.deal_id, pos.direction, pos.shares
                )
                logger.warning("EMERGENCY COVER executed: %s", pos.ticker)

            # Earnings check
            if self._earnings.has_earnings_within(pos.ticker, 1):
                self._orders.close_position(
                    pos.deal_id, pos.direction, pos.shares
                )
                logger.warning("PRE-EARNINGS EXIT: %s", pos.ticker)

            # Overnight gap compliance
            trim_to = self._risk_mgr.check_overnight_gap_compliance(
                pos, self._portfolio_value
            )
            if trim_to is not None and trim_to < pos.shares:
                trim_amount = pos.shares - trim_to
                self._orders.close_position(
                    pos.deal_id, pos.direction, trim_amount
                )
                logger.info("GAP RISK TRIM: %s reduced by %d shares", pos.ticker, trim_amount)

    # ── Internal Helpers ──────────────────────────────────────

    def _estimate_stop(self, signal: TradeSignal, bars) -> float:
        """Quick stop estimate for risk calculation (before full classification)."""
        pivot = signal.pivot_level
        if signal.direction == Direction.LONG:
            return pivot * 0.93  # ~7% below
        return pivot * 1.07  # ~7% above

    def _sync_positions(self) -> None:
        """Sync open positions from IG."""
        # TODO: map IG positions to our Position model
        self._open_positions = []

    def _log_reject(self, signal, decision, reject_code, reason):
        if self._audit:
            entry = AuditEntry(
                signal_id=signal.signal_id,
                ticker=signal.ticker,
                market=signal.market,
                direction=signal.direction,
                entry_type=signal.entry_type,
                decision=decision,
                reason_code=reject_code,
                reason_detail=reason,
            )
            self._audit.log(entry)

    def _log_gate_reject(self, signal, gate_result, bars):
        if self._audit:
            avg_vol = avg_volume(bars["volume"], 50) if "volume" in bars.columns else 0
            entry = AuditEntry(
                signal_id=signal.signal_id,
                ticker=signal.ticker,
                market=signal.market,
                direction=signal.direction,
                entry_type=signal.entry_type,
                decision=gate_result.decision,
                reason_code=gate_result.reject_code,
                gates=AuditGates(
                    trend_template=all(g.passed for g in gate_result.gates if "Template" in g.name or "MA" in g.name),
                    adx=gate_result.adx_value,
                    volume_gate=gate_result.volume_dry_count or gate_result.distribution_count,
                ),
                volume=AuditVolume(avg_50d_volume=int(avg_vol) if avg_vol else None),
            )
            self._audit.log(entry)

    def _log_risk_reject(self, signal, gate_result, risk_result, bars):
        if self._audit:
            entry = AuditEntry(
                signal_id=signal.signal_id,
                ticker=signal.ticker,
                market=signal.market,
                direction=signal.direction,
                entry_type=signal.entry_type,
                decision=Decision.SKIP,
                reason_code=risk_result.reject_code,
                reason_detail=risk_result.reason,
                gates=AuditGates(
                    adx=gate_result.adx_value,
                    risk_budget_ok=False,
                ),
            )
            self._audit.log(entry)

    def _log_classify_result(self, signal, gate_result, risk_result, classify_result, bars):
        if self._audit:
            entry = AuditEntry(
                signal_id=signal.signal_id,
                ticker=signal.ticker,
                market=signal.market,
                direction=signal.direction,
                entry_type=signal.entry_type,
                decision=classify_result.decision,
                reason_code=classify_result.reject_code,
                reason_detail=classify_result.reason,
            )
            self._audit.log(entry)

    def _log_entry(self, signal, gate_result, risk_result, instruction, order_result, bars):
        if self._audit:
            avg_vol = avg_volume(bars["volume"], 50) if "volume" in bars.columns else 0
            current_vol = int(bars["volume"].iloc[-1]) if "volume" in bars.columns else 0

            entry = AuditEntry(
                signal_id=signal.signal_id,
                ticker=signal.ticker,
                market=signal.market,
                direction=signal.direction,
                entry_type=signal.entry_type,
                decision=Decision.ENTER,
                gates=AuditGates(
                    adx=gate_result.adx_value,
                    risk_budget_ok=True,
                    gap_classified=False,  # TODO: get from classifier
                ),
                levels=AuditLevels(
                    pivot_or_breakdown=signal.pivot_level,
                    entry_price=instruction.entry_price,
                    stop_price=instruction.stop_price,
                    risk_per_share=instruction.risk_per_share,
                    position_size=instruction.total_shares,
                    risk_amount=instruction.risk_amount,
                    portfolio_risk_pct=instruction.portfolio_risk_pct,
                ),
                volume=AuditVolume(
                    trigger_volume=current_vol,
                    avg_50d_volume=int(avg_vol),
                    volume_ratio=current_vol / avg_vol if avg_vol > 0 else 0,
                ),
                tranche=AuditTranche(
                    tranche_1_size=instruction.tranche_1.size,
                    tranche_2_eligible=instruction.tranche_2.size > 0,
                    tranche_2_size=instruction.tranche_2.size,
                ),
                uk_specific=AuditUKSpecific(
                    spread_pct=instruction.spread_pct,
                    stamp_duty_applied=instruction.stamp_duty_applies,
                    cfd_used=instruction.use_cfd,
                ) if instruction.is_uk else None,
                ig_deal_reference=order_result.deal_reference,
                ig_deal_id=order_result.deal_id,
            )
            self._audit.log(entry)

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False
