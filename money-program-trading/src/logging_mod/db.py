"""
SQLite database layer — lightweight, Pi-friendly, zero config.

Tables (v1 — execution audit):
- audit_log: Every decision (enter/skip/reject) with full context
- positions: Currently open positions and their state
- quarantine: Tickers in quarantine after failed reclaims
- daily_state: Daily snapshot of portfolio risk, open positions, etc.

Tables (v2 — observability log streams; see Observability_Design_v1.md):
- sessions:             One row per trading session (SessionRecord)
- scans:                One row per morning scan (ScanRecord scalar fields)
- scan_universe:        One row per scored-universe entry (UniverseScoreEntry)
- shortlist_entries:    A+/A/B candidates shortlisted from the scan
- candidate_snapshots:  Per-minute CandidateSnapshot rows (the "state log")
- candidate_events:     State-transition events (CandidateEvent — the "decision log")

v1 tables are preserved verbatim; v2 additions are idempotent
``CREATE TABLE IF NOT EXISTS`` so upgrades are a no-op for existing DBs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from ..config.settings import get_settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    market TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason_code TEXT,
    reason_detail TEXT,
    gates_json TEXT,
    levels_json TEXT,
    volume_json TEXT,
    tranche_json TEXT,
    short_specific_json TEXT,
    uk_specific_json TEXT,
    ig_deal_reference TEXT,
    ig_deal_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_ticker ON audit_log(ticker);
CREATE INDEX IF NOT EXISTS idx_audit_decision ON audit_log(decision);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_signal ON audit_log(signal_id);

CREATE TABLE IF NOT EXISTS positions (
    deal_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    ig_epic TEXT NOT NULL,
    market TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_date TEXT NOT NULL,
    shares INTEGER NOT NULL,
    initial_stop REAL NOT NULL,
    current_stop REAL NOT NULL,
    trailing_stop_active INTEGER DEFAULT 0,
    tranche_1_shares INTEGER NOT NULL,
    tranche_1_fill REAL NOT NULL,
    tranche_2_shares INTEGER DEFAULT 0,
    tranche_2_fill REAL,
    tranche_2_placed INTEGER DEFAULT 0,
    tranche_2_blocked INTEGER DEFAULT 0,
    is_pilot INTEGER DEFAULT 0,
    sector TEXT DEFAULT '',
    closed_at TEXT,
    close_price REAL,
    close_reason TEXT,
    pnl REAL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pos_ticker ON positions(ticker);
CREATE INDEX IF NOT EXISTS idx_pos_open ON positions(closed_at);

CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    quarantine_start TEXT NOT NULL,
    quarantine_end TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_quar_ticker ON quarantine(ticker);
CREATE INDEX IF NOT EXISTS idx_quar_end ON quarantine(quarantine_end);

CREATE TABLE IF NOT EXISTS daily_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    portfolio_value REAL,
    total_open_risk REAL,
    total_long_exposure REAL,
    total_short_exposure REAL,
    open_position_count INTEGER,
    signals_processed INTEGER DEFAULT 0,
    entries_placed INTEGER DEFAULT 0,
    rejections INTEGER DEFAULT 0,
    skips INTEGER DEFAULT 0,
    state_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- =============================================================================
-- v2 — Observability log streams
-- =============================================================================

-- sessions: one row per trading session.
-- Closed by SessionWriter.__exit__ (closed_at_utc is NULL while session is open).
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    session_date     TEXT NOT NULL,
    session_label    TEXT NOT NULL,
    broker_mode      TEXT NOT NULL,
    account_size_gbp REAL NOT NULL,
    opened_at_utc    TEXT NOT NULL,
    closed_at_utc    TEXT,
    scan_id          TEXT,
    rule_set_version TEXT NOT NULL,
    schema_version   INTEGER NOT NULL,
    notes            TEXT DEFAULT '',
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sess_date ON sessions(session_date);
CREATE INDEX IF NOT EXISTS idx_sess_broker ON sessions(broker_mode);

-- scans: one row per morning scan. scored_universe is normalised into
-- scan_universe (below) so queries don't need JSON-path traversal.
CREATE TABLE IF NOT EXISTS scans (
    scan_id          TEXT PRIMARY KEY,
    session_id       TEXT,
    scanned_at_utc   TEXT NOT NULL,
    universe_size    INTEGER NOT NULL,
    broker_mode      TEXT NOT NULL,

    regime           TEXT NOT NULL,
    regime_score     REAL,
    vix_level        REAL,
    breadth_us       REAL,
    breadth_uk       REAL,
    regime_notes     TEXT DEFAULT '',

    scanner_version  TEXT DEFAULT '',
    rule_set_version TEXT DEFAULT '',
    schema_version   INTEGER NOT NULL,
    extras_json      TEXT,
    created_at       TEXT DEFAULT (datetime('now')),

    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_scans_session ON scans(session_id);
CREATE INDEX IF NOT EXISTS idx_scans_scanned_at ON scans(scanned_at_utc);

-- scan_universe: one row per scored ticker in a scan. Big table — ~175 rows
-- per scan per day. FK to scans so we can prune old scans cleanly.
CREATE TABLE IF NOT EXISTS scan_universe (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id             TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    market              TEXT NOT NULL,
    price               REAL,
    currency            TEXT,
    pillar_pass_count   INTEGER NOT NULL,
    pillar_bitmap       INTEGER NOT NULL,
    day1_score          REAL,
    day1_tier           TEXT,
    grade               TEXT,
    shortlisted         INTEGER NOT NULL DEFAULT 0,
    rejection_reason    TEXT,
    rejection_code      TEXT,
    created_at          TEXT DEFAULT (datetime('now')),

    FOREIGN KEY (scan_id) REFERENCES scans(scan_id)
);

CREATE INDEX IF NOT EXISTS idx_univ_scan ON scan_universe(scan_id);
CREATE INDEX IF NOT EXISTS idx_univ_symbol ON scan_universe(symbol);
CREATE INDEX IF NOT EXISTS idx_univ_shortlisted ON scan_universe(shortlisted);

-- shortlist_entries: A+/A/B candidates selected for execution.
-- pillar_votes is lifted to six bool columns so SQL can filter by pillar
-- without JSON-path traversal.
CREATE TABLE IF NOT EXISTS shortlist_entries (
    candidate_id              TEXT PRIMARY KEY,
    scan_id                   TEXT NOT NULL,
    session_id                TEXT,

    symbol                    TEXT NOT NULL,
    market                    TEXT NOT NULL,
    direction                 TEXT NOT NULL,
    setup_type                TEXT NOT NULL,
    grade                     TEXT NOT NULL,

    trigger_low               REAL NOT NULL,
    trigger_high              REAL NOT NULL,
    stop_price                REAL NOT NULL,
    target_price              REAL,

    planned_stake_gbp_per_pt  REAL NOT NULL,
    planned_risk_gbp          REAL NOT NULL,
    planned_risk_pct_account  REAL NOT NULL,

    pv_livermore              INTEGER NOT NULL DEFAULT 0,
    pv_oneil                  INTEGER NOT NULL DEFAULT 0,
    pv_minervini              INTEGER NOT NULL DEFAULT 0,
    pv_darvas                 INTEGER NOT NULL DEFAULT 0,
    pv_raschke                INTEGER NOT NULL DEFAULT 0,
    pv_weinstein              INTEGER NOT NULL DEFAULT 0,

    committee_stance          TEXT DEFAULT '',
    day1_score                REAL,
    day1_tier                 TEXT,

    broker_mode               TEXT NOT NULL,
    created_at_utc            TEXT NOT NULL,
    schema_version            INTEGER NOT NULL,
    rule_set_version          TEXT DEFAULT '',
    extras_json               TEXT,
    notes                     TEXT DEFAULT '',
    created_at                TEXT DEFAULT (datetime('now')),

    FOREIGN KEY (scan_id)    REFERENCES scans(scan_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_short_session ON shortlist_entries(session_id);
CREATE INDEX IF NOT EXISTS idx_short_scan ON shortlist_entries(scan_id);
CREATE INDEX IF NOT EXISTS idx_short_symbol ON shortlist_entries(symbol);
CREATE INDEX IF NOT EXISTS idx_short_grade ON shortlist_entries(grade);

-- candidate_snapshots: per-minute CandidateSnapshot rows. This is the biggest
-- table by row count — one row per candidate per minute per session.
-- Flat schema (no JSON payload) so column scans stay fast.
CREATE TABLE IF NOT EXISTS candidate_snapshots (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id                   TEXT NOT NULL,
    candidate_id                 TEXT NOT NULL,
    ts_utc                       TEXT NOT NULL,
    symbol                       TEXT NOT NULL,
    status                       TEXT NOT NULL,

    last_price                   REAL,
    bid                          REAL,
    ask                          REAL,

    gate_hard_mask               INTEGER,
    gate_soft_mask               INTEGER,
    trigger_armed                INTEGER NOT NULL DEFAULT 0,

    entry_price                  REAL,
    fill_ts_utc                  TEXT,
    stake_gbp_per_pt             REAL,
    initial_stop                 REAL,
    current_stop                 REAL,

    unrealised_pnl_gbp           REAL,
    peak_unrealised_pnl_gbp      REAL,
    current_locked_profit_gbp    REAL,
    trail_step_count             INTEGER,
    trail_mode_active            INTEGER NOT NULL DEFAULT 0,

    invalidation_window_active   INTEGER NOT NULL DEFAULT 0,
    mins_since_fill              INTEGER,
    mins_to_timestop             INTEGER,

    regime                       TEXT,
    broker_mode                  TEXT NOT NULL,
    schema_version               INTEGER NOT NULL,
    rule_set_version             TEXT DEFAULT '',
    created_at                   TEXT DEFAULT (datetime('now')),

    FOREIGN KEY (session_id)   REFERENCES sessions(session_id),
    FOREIGN KEY (candidate_id) REFERENCES shortlist_entries(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_snap_session ON candidate_snapshots(session_id);
CREATE INDEX IF NOT EXISTS idx_snap_candidate ON candidate_snapshots(candidate_id);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON candidate_snapshots(ts_utc);
CREATE INDEX IF NOT EXISTS idx_snap_status ON candidate_snapshots(status);
CREATE INDEX IF NOT EXISTS idx_snap_cand_ts ON candidate_snapshots(candidate_id, ts_utc);

-- candidate_events: state-transition + decision events. The discriminated
-- payload union is stored as payload_json; the event_type + reason_code
-- scalar columns give cheap filtering, and DuckDB can extract typed fields
-- from payload_json for heavier analysis.
CREATE TABLE IF NOT EXISTS candidate_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL UNIQUE,
    session_id        TEXT NOT NULL,
    candidate_id      TEXT,
    ts_utc            TEXT NOT NULL,

    event_type        TEXT NOT NULL,
    actor             TEXT NOT NULL,
    reason_code       TEXT,

    payload_json      TEXT NOT NULL,
    terminal_reason   TEXT,

    broker_mode       TEXT NOT NULL,
    schema_version    INTEGER NOT NULL,
    rule_set_version  TEXT DEFAULT '',
    created_at        TEXT DEFAULT (datetime('now')),

    FOREIGN KEY (session_id)   REFERENCES sessions(session_id),
    FOREIGN KEY (candidate_id) REFERENCES shortlist_entries(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_evt_session ON candidate_events(session_id);
CREATE INDEX IF NOT EXISTS idx_evt_candidate ON candidate_events(candidate_id);
CREATE INDEX IF NOT EXISTS idx_evt_type ON candidate_events(event_type);
CREATE INDEX IF NOT EXISTS idx_evt_ts ON candidate_events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_evt_terminal ON candidate_events(terminal_reason);
"""


class Database:
    """SQLite database connection and schema management."""

    def __init__(self, db_path: str | None = None):
        self._path = db_path or get_settings().db_path
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent access
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def initialize(self) -> None:
        """Create tables if they don't exist."""
        self.conn.executescript(_SCHEMA)
        # Check/set schema version
        cursor = self.conn.execute("SELECT COUNT(*) FROM schema_version")
        if cursor.fetchone()[0] == 0:
            self.conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
        self.conn.commit()
        logger.info("Database initialized at %s (schema v%d)", self._path, SCHEMA_VERSION)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
