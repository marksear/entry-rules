"""
journal — end-of-session report writer.

Reads from the observability DB (``sessions`` + ``shortlist_entries`` +
``candidate_events`` + ``candidate_snapshots``) and emits a single
markdown file summarising the session. Goal: Mark can open the file and
judge rule adherence vs outcome in under 30 seconds.

Output layout
-------------
1. Session header — id, date, label, account size, broker mode, duration.
2. Top-line summary — candidates monitored, fired / not fired, realised
   P&L, open at close.
3. Filled positions — one row each: symbol, direction, fill, exit reason,
   realised P&L.
4. Open-at-close — positions carried into the next session.
5. Exit-reason histogram — count by ``terminal_reason``.
6. No-enter reject-code histogram — why candidates skipped.
7. Resumed — any positions carried in from a prior session (their initial
   state at session open).

No narrative, no emojis, no opinion — just rule-based counts and numbers.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..logging_mod.db import Database

logger = logging.getLogger(__name__)


def write_session_journal(
    database: Database,
    session_id: str,
    output_dir: str | Path,
) -> Path:
    """Write a markdown journal for ``session_id`` to ``output_dir``.

    Returns the path to the written file. Raises if the session row is
    missing (a closed session should always have its row).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    session = _load_session(database, session_id)
    counts = _load_candidate_counts(database, session_id)
    fills = _load_fills(database, session_id)
    exits = _load_exits(database, session_id)
    rejects = _load_reject_histogram(database, session_id)
    resumed = _load_resumed_positions(database, session_id)

    realised_pnl = sum(e["realised_pnl_gbp"] or 0.0 for e in exits)
    open_at_close = [
        f for f in fills if f["candidate_id"] not in {e["candidate_id"] for e in exits}
    ]

    filename = (
        f"journal_{session['session_date']}_{session['session_label']}"
        f"_{session_id[:8]}.md"
    )
    path = output_dir / filename

    lines = _render(
        session=session,
        counts=counts,
        fills=fills,
        exits=exits,
        rejects=rejects,
        resumed=resumed,
        realised_pnl=realised_pnl,
        open_at_close=open_at_close,
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote session journal: %s", path)
    return path


# ---------------------------------------------------------------------------
# DB loaders
# ---------------------------------------------------------------------------


def _load_session(database: Database, session_id: str) -> dict:
    row = database.conn.execute(
        """
        SELECT session_id, session_date, session_label, broker_mode,
               account_size_gbp, opened_at_utc, closed_at_utc, scan_id,
               rule_set_version, notes
        FROM sessions WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No sessions row for session_id={session_id}")
    return dict(row)


def _load_candidate_counts(database: Database, session_id: str) -> dict[str, int]:
    shortlisted = database.conn.execute(
        """
        SELECT COUNT(*) AS n FROM shortlist_entries
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()["n"]

    fired = database.conn.execute(
        """
        SELECT COUNT(DISTINCT candidate_id) AS n FROM candidate_events
        WHERE session_id = ? AND event_type = 'TRIGGER_FIRED'
        """,
        (session_id,),
    ).fetchone()["n"]

    filled = database.conn.execute(
        """
        SELECT COUNT(DISTINCT candidate_id) AS n FROM candidate_events
        WHERE session_id = ? AND event_type = 'FILLED'
        """,
        (session_id,),
    ).fetchone()["n"]

    no_enter = database.conn.execute(
        """
        SELECT COUNT(DISTINCT candidate_id) AS n FROM candidate_events
        WHERE session_id = ? AND event_type IN (
            'SESSION_ENDED_NO_TRIGGER', 'ENTRY_EVALUATED_NO_ENTER'
        )
        """,
        (session_id,),
    ).fetchone()["n"]

    return {
        "shortlisted": shortlisted,
        "fired": fired,
        "filled": filled,
        "no_enter": no_enter,
    }


def _load_fills(database: Database, session_id: str) -> list[dict]:
    rows = database.conn.execute(
        """
        SELECT e.candidate_id, e.ts_utc, e.payload_json,
               s.symbol, s.direction, s.grade
        FROM candidate_events e
        JOIN shortlist_entries s ON s.candidate_id = e.candidate_id
        WHERE e.session_id = ? AND e.event_type = 'FILLED'
        ORDER BY e.ts_utc
        """,
        (session_id,),
    ).fetchall()
    fills = []
    for r in rows:
        payload = json.loads(r["payload_json"])
        fills.append(
            {
                "candidate_id": r["candidate_id"],
                "symbol": r["symbol"],
                "direction": r["direction"],
                "grade": r["grade"],
                "ts_utc": r["ts_utc"],
                "fill_price": payload.get("fill_price"),
                "stake_gbp_per_pt": payload.get("stake_gbp_per_pt"),
                "initial_stop_price": payload.get("initial_stop_price"),
                "initial_risk_gbp": payload.get("initial_risk_gbp"),
                "ig_deal_id": payload.get("ig_deal_id"),
            }
        )
    return fills


def _load_exits(database: Database, session_id: str) -> list[dict]:
    """Return all terminal events written this session (one per exit)."""
    rows = database.conn.execute(
        """
        SELECT candidate_id, event_type, terminal_reason, ts_utc,
               payload_json
        FROM candidate_events
        WHERE session_id = ? AND terminal_reason IS NOT NULL
          AND event_type NOT IN (
            'SESSION_ENDED_NO_TRIGGER', 'INVALIDATED_PRE_TRIGGER'
          )
        ORDER BY ts_utc
        """,
        (session_id,),
    ).fetchall()
    exits = []
    for r in rows:
        payload = json.loads(r["payload_json"])
        exits.append(
            {
                "candidate_id": r["candidate_id"],
                "event_type": r["event_type"],
                "terminal_reason": r["terminal_reason"],
                "ts_utc": r["ts_utc"],
                "realised_pnl_gbp": payload.get("realised_pnl_gbp"),
            }
        )
    return exits


def _load_reject_histogram(database: Database, session_id: str) -> Counter:
    rows = database.conn.execute(
        """
        SELECT reason_code, COUNT(*) AS n FROM candidate_events
        WHERE session_id = ?
          AND event_type = 'ENTRY_EVALUATED_NO_ENTER'
          AND reason_code IS NOT NULL
        GROUP BY reason_code
        ORDER BY n DESC
        """,
        (session_id,),
    ).fetchall()
    return Counter({r["reason_code"]: r["n"] for r in rows})


def _load_resumed_positions(database: Database, session_id: str) -> list[dict]:
    """Resumed = a FILLED from a prior session_id that has open snapshots
    in THIS session. Heuristic, but good enough for a summary line."""
    rows = database.conn.execute(
        """
        SELECT DISTINCT s.candidate_id, se.symbol, se.direction,
               s.entry_price, s.current_stop, s.stake_gbp_per_pt,
               s.peak_unrealised_pnl_gbp, s.trail_step_count
        FROM candidate_snapshots s
        JOIN shortlist_entries se ON se.candidate_id = s.candidate_id
        WHERE s.session_id = ?
          AND s.status = 'TRIGGERED_OPEN'
          AND NOT EXISTS (
            SELECT 1 FROM candidate_events e
            WHERE e.candidate_id = s.candidate_id
              AND e.event_type = 'FILLED'
              AND e.session_id = ?
          )
        ORDER BY s.candidate_id
        """,
        (session_id, session_id),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(
    *,
    session: dict,
    counts: dict[str, int],
    fills: list[dict],
    exits: list[dict],
    rejects: Counter,
    resumed: list[dict],
    realised_pnl: float,
    open_at_close: list[dict],
) -> list[str]:
    lines: list[str] = []

    opened = session["opened_at_utc"]
    closed = session["closed_at_utc"] or "(still open)"
    duration = _duration_str(opened, session["closed_at_utc"])

    lines.append(f"# Session journal — {session['session_date']} {session['session_label']}")
    lines.append("")
    lines.append(f"- session_id: `{session['session_id']}`")
    lines.append(f"- broker_mode: `{session['broker_mode']}`")
    lines.append(f"- account_size_gbp: £{session['account_size_gbp']:.2f}")
    lines.append(f"- opened_at_utc: `{opened}`")
    lines.append(f"- closed_at_utc: `{closed}`")
    lines.append(f"- duration: {duration}")
    lines.append(f"- rule_set_version: `{session['rule_set_version']}`")
    if session.get("notes"):
        lines.append(f"- notes: {session['notes']}")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- shortlisted: {counts['shortlisted']}")
    lines.append(f"- fired:       {counts['fired']}")
    lines.append(f"- filled:      {counts['filled']}")
    lines.append(f"- no-enter:    {counts['no_enter']}")
    lines.append(f"- realised_pnl_gbp: £{realised_pnl:+.2f}")
    lines.append(f"- open at close:    {len(open_at_close)}")
    lines.append(f"- resumed (from prior session): {len(resumed)}")
    lines.append("")

    lines.append("## Fills")
    lines.append("")
    if not fills:
        lines.append("_No fills this session._")
    else:
        lines.append(
            "| ts_utc | symbol | dir | grade | fill | stake £/pt | "
            "init stop | init risk £ | exit | realised £ |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        exit_by_cand = {e["candidate_id"]: e for e in exits}
        for f in fills:
            e = exit_by_cand.get(f["candidate_id"])
            exit_reason = e["terminal_reason"] if e else "OPEN"
            if e and e.get("realised_pnl_gbp") is not None:
                realised = f"£{e['realised_pnl_gbp']:+.2f}"
            else:
                realised = "—"
            lines.append(
                f"| {f['ts_utc']} | {f['symbol']} | {f['direction']} | {f['grade']} | "
                f"{_fmt_num(f['fill_price'])} | {_fmt_num(f['stake_gbp_per_pt'])} | "
                f"{_fmt_num(f['initial_stop_price'])} | £{_fmt_num(f['initial_risk_gbp'])} | "
                f"{exit_reason} | {realised} |"
            )
    lines.append("")

    lines.append("## Open at close")
    lines.append("")
    if not open_at_close:
        lines.append("_None — all fills exited during this session._")
    else:
        lines.append("| symbol | dir | fill | stake £/pt | ig_deal_id |")
        lines.append("|---|---|---|---|---|")
        for f in open_at_close:
            lines.append(
                f"| {f['symbol']} | {f['direction']} | {_fmt_num(f['fill_price'])} | "
                f"{_fmt_num(f['stake_gbp_per_pt'])} | `{f['ig_deal_id']}` |"
            )
    lines.append("")

    lines.append("## Exit-reason breakdown")
    lines.append("")
    if not exits:
        lines.append("_No exits this session._")
    else:
        exit_counts = Counter(e["terminal_reason"] for e in exits)
        for reason, n in exit_counts.most_common():
            lines.append(f"- {reason}: {n}")
    lines.append("")

    lines.append("## No-enter reasons")
    lines.append("")
    if not rejects:
        lines.append("_No ENTRY_EVALUATED_NO_ENTER events._")
    else:
        for code, n in rejects.most_common():
            lines.append(f"- {code}: {n}")
    lines.append("")

    lines.append("## Resumed from prior session")
    lines.append("")
    if not resumed:
        lines.append("_None._")
    else:
        lines.append("| symbol | dir | fill | current stop | peak £ | trail step |")
        lines.append("|---|---|---|---|---|---|")
        for r in resumed:
            lines.append(
                f"| {r['symbol']} | {r['direction']} | {_fmt_num(r['entry_price'])} | "
                f"{_fmt_num(r['current_stop'])} | £{_fmt_num(r['peak_unrealised_pnl_gbp'])} | "
                f"{r['trail_step_count']} |"
            )
    lines.append("")

    return lines


def _fmt_num(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def _duration_str(opened: str, closed: str | None) -> str:
    if not closed:
        return "(still open)"
    try:
        o = datetime.fromisoformat(opened.replace("Z", ""))
        c = datetime.fromisoformat(closed.replace("Z", ""))
        mins = (c - o).total_seconds() / 60.0
        return f"{mins:.1f} min"
    except ValueError:
        return f"{opened} → {closed}"


__all__ = ["write_session_journal"]
