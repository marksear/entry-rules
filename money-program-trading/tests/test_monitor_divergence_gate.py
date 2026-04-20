"""
Regression tests for the S-4 interim price-divergence gate
(ADD_DIVERGENCE_GATE_SPEC.md).

Gate invariant: the monitor must REFUSE to run ``trail_manager.evaluate_exit``
when its cached ``last_traded`` disagrees with the broker's live deal
price by more than ``PRICE_DIVERGENCE_SKIP_BPS`` (default 30bps), or
when the broker can't produce a live price at all. In both cases a
PRICE_DIVERGENCE_SKIP event is written to ``candidate_events`` with a
typed payload (monitor_price, deal_price, delta_bps, threshold_bps,
reason) and ``state.consecutive_divergence_skips`` is incremented.

The five scenarios enumerated in the spec:

  1. matched prices proceed (delta < threshold, evaluate_exit runs)
  2. divergent prices skip exit evaluation (JNJ-magnitude ~93bps)
  3. None deal_price fails safe (NO_DEAL_PRICE reason)
  4. divergence clears on next tick (counter resets)
  5. consecutive-skip WARNING fires after threshold (3+ ticks)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.engine.broker import CloseResult, StopModifyResult
from src.engine.monitor import (
    CandidatePlan,
    CandidateRuntimeState,
    MonitorLoop,
)
from src.engine.trail_manager import (
    PRICE_DIVERGENCE_SKIP_BPS,
    PRICE_DIVERGENCE_SKIP_WARN_AFTER_TICKS,
    ExitConfig,
)
from src.logging_mod.db import Database
from src.logging_mod.session_writer import SessionWriter
from src.models.common import Direction, EntryType, Market
from src.models.log_enums import (
    BrokerMode,
    CandidateGrade,
    EventType,
    SessionLabel,
)

from tests.test_monitor_exit_paths import (  # type: ignore[import-not-found]
    _StubMarketData,
    _insert_prereq_rows,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Divergence-aware broker stub
# ---------------------------------------------------------------------------


@dataclass
class _DivergenceBroker:
    """Broker stub that exposes a tunable ``get_deal_price``.

    ``deal_price_queue`` is consumed one price per call. If the queue is
    exhausted, the last value is repeated. A queued value of ``None``
    returns None (simulating the NO_DEAL_PRICE branch). ``close_position``
    and ``modify_stop`` return canned successes so any evaluate_exit that
    slips through doesn't blow up the test.
    """

    deal_price_queue: list[float | None] = field(default_factory=list)
    deal_calls: list[tuple[str, Direction]] = field(default_factory=list)
    modify_calls: list[dict[str, Any]] = field(default_factory=list)
    close_calls: list[dict[str, Any]] = field(default_factory=list)

    def get_deal_price(self, epic: str, direction: Direction) -> float | None:
        self.deal_calls.append((epic, direction))
        if not self.deal_price_queue:
            return None
        if len(self.deal_price_queue) == 1:
            return self.deal_price_queue[0]
        return self.deal_price_queue.pop(0)

    def modify_stop(self, deal_id, new_stop_price, *, epic=None):
        self.modify_calls.append(
            {"deal_id": deal_id, "new_stop_price": new_stop_price, "epic": epic}
        )
        return StopModifyResult(
            success=True,
            deal_id=deal_id,
            new_stop_price=new_stop_price,
            reason_code="SUCCESS",
        )

    def close_position(self, *, deal_id, direction, epic, size):
        self.close_calls.append(
            {"deal_id": deal_id, "direction": direction, "epic": epic, "size": size}
        )
        return CloseResult(
            success=True,
            deal_id=deal_id,
            fill_price=None,
            closed_at_utc=datetime.utcnow(),
            reason_code="SUCCESS",
        )


class _StaticSnapshotMarket:
    """MarketData stub that serves a fixed snapshot on every fetch."""

    def __init__(self, last: float, status: str = "TRADEABLE") -> None:
        self._snap = {
            "bid": last,
            "ask": last,
            "last_traded": last,
            "market_status": status,
            "high": None,
            "low": None,
            "net_change": None,
            "pct_change": None,
            "update_time_utc": None,
        }

    def get_market_snapshot(self, epic: str) -> dict:
        return dict(self._snap)

    def resolve_epic(self, symbol: str, market: str = "US") -> str:
        return "IX.D.SPTRD.DAILY.IP"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def database(tmp_path: Path):
    db = Database(db_path=str(tmp_path / "divergence_gate.db"))
    db.initialize()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def writer(database: Database):
    with SessionWriter(
        database,
        broker_mode=BrokerMode.DEMO,
        session_label=SessionLabel.US_REGULAR,
        account_size_gbp=1000.0,
        rule_set_version="divergence-gate-test",
        session_date=date.today(),
    ) as w:
        yield w


def _short_plan(session_id: str) -> CandidatePlan:
    return CandidatePlan(
        candidate_id=str(uuid.uuid4()),
        scan_id=str(uuid.uuid4()),
        session_id=session_id,
        symbol="JNJ",
        market=Market.US,
        direction=Direction.SHORT,
        setup_type=EntryType.S_A,
        grade=CandidateGrade.B,
        trigger_low=233.50,
        trigger_high=234.50,
        stop_price=238.81,
        target_price=None,
        ig_epic="KA.D.JNJ.CASH.IP",
        broker_mode=BrokerMode.DEMO,
        planned_stake_gbp_per_pt=11.6,
        planned_risk_gbp=56.96,
    )


def _seed_open_state(
    loop: MonitorLoop,
    plan: CandidatePlan,
    *,
    now: datetime,
    fill_minutes_ago: int = 45,
    fill_price: float = 233.90,
) -> CandidateRuntimeState:
    state = CandidateRuntimeState(
        fired=True,
        deal_id="DIAAAAW9ELXVPAB",
        deal_reference="LNEA2AU68ELTYP5",
        fill_price=fill_price,
        fill_ts_utc=now - timedelta(minutes=fill_minutes_ago),
        stake_gbp_per_pt=11.6,
        initial_stop_price=238.81,
        current_stop_price=238.81,
        peak_pnl_gbp=0.0,
        trail_step_count=0,
        trail_mode_activated=False,
        sessions_held=1,
    )
    loop._runtime[plan.candidate_id] = state
    return state


def _divergence_events(database: Database, candidate_id: str) -> list[dict]:
    rows = database.conn.execute(
        "SELECT reason_code, payload_json FROM candidate_events "
        "WHERE candidate_id = ? AND event_type = ? ORDER BY rowid",
        (candidate_id, EventType.PRICE_DIVERGENCE_SKIP.value),
    ).fetchall()
    return [
        {
            "reason_code": r["reason_code"],
            "payload": json.loads(r["payload_json"]) if r["payload_json"] else {},
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 1. Matched prices proceed
# ---------------------------------------------------------------------------


def test_matched_prices_proceed_to_exit_eval(
    database: Database, writer: SessionWriter
):
    """delta ~1bps < 30 → evaluate_exit runs. No divergence event."""
    now = datetime(2026, 4, 20, 13, 32, 24)
    plan = _short_plan(writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    # Snapshot last_traded = 233.92, deal_price = 233.90 → delta ~0.86bps
    market = _StaticSnapshotMarket(last=233.92)
    broker = _DivergenceBroker(deal_price_queue=[233.90])

    loop = MonitorLoop(
        writer=writer,
        market_data=market,
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
        now_fn=lambda: now,
    )
    state = _seed_open_state(loop, plan, now=now)

    loop.run_one_tick(now)

    # Broker was asked for a deal price, counter stayed at 0.
    assert len(broker.deal_calls) == 1
    assert state.consecutive_divergence_skips == 0
    # No divergence event written.
    assert _divergence_events(database, plan.candidate_id) == []


# ---------------------------------------------------------------------------
# 2. Divergent prices skip exit evaluation
# ---------------------------------------------------------------------------


def test_divergent_prices_skip_exit_eval(
    database: Database, writer: SessionWriter
):
    """JNJ scenario: monitor=234.50, deal=232.33 → ~93bps > 30 → skip.

    Evaluate_exit MUST NOT run (no close_position / modify_stop calls).
    A PRICE_DIVERGENCE_SKIP event is written with reason
    DIVERGENCE_OVER_THRESHOLD, correct delta_bps/threshold_bps, and
    the counter increments to 1.
    """
    now = datetime(2026, 4, 20, 13, 32, 24)
    plan = _short_plan(writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    market = _StaticSnapshotMarket(last=234.50)
    broker = _DivergenceBroker(deal_price_queue=[232.33])

    loop = MonitorLoop(
        writer=writer,
        market_data=market,
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
        now_fn=lambda: now,
    )
    state = _seed_open_state(loop, plan, now=now)

    loop.run_one_tick(now)

    assert state.consecutive_divergence_skips == 1
    # evaluate_exit did not run — neither broker action happened.
    assert broker.close_calls == []
    assert broker.modify_calls == []

    events = _divergence_events(database, plan.candidate_id)
    assert len(events) == 1
    evt = events[0]
    assert evt["reason_code"] == "DIVERGENCE_OVER_THRESHOLD"
    payload = evt["payload"]
    assert payload["reason"] == "DIVERGENCE_OVER_THRESHOLD"
    assert payload["monitor_price"] == pytest.approx(234.50)
    assert payload["deal_price"] == pytest.approx(232.33)
    # (234.50 - 232.33) / 232.33 * 10000 ≈ 93.4 bps
    assert payload["delta_bps"] == pytest.approx(93.4, abs=0.5)
    assert payload["threshold_bps"] == pytest.approx(PRICE_DIVERGENCE_SKIP_BPS)
    assert payload["consecutive_skips"] == 1


# ---------------------------------------------------------------------------
# 3. None deal_price fails safe
# ---------------------------------------------------------------------------


def test_none_deal_price_fails_safe(database: Database, writer: SessionWriter):
    """broker.get_deal_price returns None → skip, NO_DEAL_PRICE reason."""
    now = datetime(2026, 4, 20, 13, 32, 24)
    plan = _short_plan(writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    market = _StaticSnapshotMarket(last=234.50)
    broker = _DivergenceBroker(deal_price_queue=[None])

    loop = MonitorLoop(
        writer=writer,
        market_data=market,
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
        now_fn=lambda: now,
    )
    state = _seed_open_state(loop, plan, now=now)

    loop.run_one_tick(now)

    assert state.consecutive_divergence_skips == 1
    assert broker.close_calls == []

    events = _divergence_events(database, plan.candidate_id)
    assert len(events) == 1
    assert events[0]["reason_code"] == "NO_DEAL_PRICE"
    payload = events[0]["payload"]
    assert payload["reason"] == "NO_DEAL_PRICE"
    assert payload["deal_price"] is None
    assert payload["delta_bps"] is None
    assert payload["threshold_bps"] == pytest.approx(PRICE_DIVERGENCE_SKIP_BPS)


# ---------------------------------------------------------------------------
# 4. Divergence clears on next tick
# ---------------------------------------------------------------------------


def test_divergence_clears_on_next_tick(
    database: Database, writer: SessionWriter
):
    """Tick 1 diverges, tick 2 within threshold. Counter 1 → 0 on tick 2."""
    t0 = datetime(2026, 4, 20, 13, 32, 24)
    plan = _short_plan(writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    # Snapshot is static — what changes between ticks is the deal_price.
    market = _StaticSnapshotMarket(last=234.00)
    # Tick 1: deal=230.00 (diverges). Tick 2: deal=233.95 (~2bps, within).
    broker = _DivergenceBroker(deal_price_queue=[230.00, 233.95])

    current = {"now": t0}
    loop = MonitorLoop(
        writer=writer,
        market_data=market,
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
        now_fn=lambda: current["now"],
    )
    state = _seed_open_state(loop, plan, now=t0)

    loop.run_one_tick(t0)
    assert state.consecutive_divergence_skips == 1

    current["now"] = t0 + timedelta(minutes=1)
    loop.run_one_tick(current["now"])

    # Counter reset.
    assert state.consecutive_divergence_skips == 0
    # Tick 2 ran evaluate_exit — confirm by checking the broker got
    # called for get_deal_price twice (once per tick).
    assert len(broker.deal_calls) == 2
    # Still exactly one PRICE_DIVERGENCE_SKIP event (from tick 1).
    events = _divergence_events(database, plan.candidate_id)
    assert len(events) == 1


# ---------------------------------------------------------------------------
# 5. Consecutive-skip WARNING fires after threshold
# ---------------------------------------------------------------------------


def test_consecutive_skip_warning_fires_after_threshold(
    database: Database, writer: SessionWriter, caplog
):
    """5 consecutive divergent ticks — WARNING on the 3rd, 4th, 5th,
    but NOT on the 1st or 2nd.
    """
    t0 = datetime(2026, 4, 20, 13, 32, 24)
    plan = _short_plan(writer.session_id)
    _insert_prereq_rows(database, writer, plan)

    market = _StaticSnapshotMarket(last=234.00)
    # 5 divergent deal prices.
    broker = _DivergenceBroker(
        deal_price_queue=[220.00, 220.00, 220.00, 220.00, 220.00]
    )

    current = {"now": t0}
    loop = MonitorLoop(
        writer=writer,
        market_data=market,
        plans=[plan],
        broker=broker,
        exit_config=ExitConfig(),
        tick_interval_seconds=1,
        now_fn=lambda: current["now"],
    )
    _seed_open_state(loop, plan, now=t0)

    warning_counts_per_tick: list[int] = []
    for i in range(5):
        current["now"] = t0 + timedelta(minutes=i)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            loop.run_one_tick(current["now"])
        warns = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and "consecutive ticks due to price divergence" in r.getMessage()
        ]
        warning_counts_per_tick.append(len(warns))

    # Ticks 1,2 → no warning. Ticks 3,4,5 → one warning each.
    assert warning_counts_per_tick[0] == 0
    assert warning_counts_per_tick[1] == 0
    assert warning_counts_per_tick[2] == 1
    assert warning_counts_per_tick[3] == 1
    assert warning_counts_per_tick[4] == 1
    assert PRICE_DIVERGENCE_SKIP_WARN_AFTER_TICKS == 3
