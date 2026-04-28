# Trading Rules — Desk Card

**One-page operator reference for the Money Program Trading rule
stack.** The full per-rule rationale lives in
`docs/specs/CANONICAL_ENTRY_RULES.md` (the law). This card is the
at-a-glance "what's gating my trade right now" lookup the operator
keeps next to the screen.

When in doubt, the **canonical doc wins**. When the canonical doc and
this card disagree, fix this card.

Last updated: 2026-04-28 (zone formula switched to 0.5×ATR per Raschke
/ Masterclass canonical).

---

## Layer 1 — Pre-screen (scanner side, before shortlist)

| ID  | Rule                | Applies | Status |
|-----|---------------------|---------|--------|
| P1  | Trend Template      | LONG    | ✓ ENFORCED |
| P2  | Inverse TT          | SHORT   | ✓ ENFORCED |
| P3  | ADX(14) > 25        | BOTH    | ✓ ENFORCED |
| P4  | Volume Dry-Up       | LONG    | ✓ ENFORCED |
| P5  | Distribution Days   | SHORT   | ✓ ENFORCED |
| P6  | Squeeze Check       | SHORT   | ⏳ PARTIAL — supplementary data gap |
| P7  | Climax Top (S-D)    | SHORT   | ⏳ PARTIAL — thresholds need verification |

By the time a candidate reaches the entry-rules side, all of these
have passed (or it would not be shortlisted).

---

## Layer 2 — Pre-flight (scanEmission, when shortlist entry built)

| ID  | Rule                  | Threshold              | Status |
|-----|-----------------------|------------------------|--------|
| R1  | Risk per trade        | ≤ 1% of portfolio      | ⏳ DRIFT — B-grade currently 0.5% (Task #52) |
| R2  | Reward:risk           | ≥ 3:1                  | ✗ NOT ENFORCED (Task #53) |
| R3  | Stop distance         | ≤ 8%                   | ✓ ENFORCED 2026-04-28 (#57) |
| R4  | Single-position cap   | LONG ≤ 10%, SHORT ≤ 8% | ✗ NOT ENFORCED |
| R5  | Total open risk       | ≤ 6%                   | DROPPED — top-3 × 1% never binds |
| R6  | Short exposure cap    | ≤ 50% notional         | DEFERRED — SHORT off |

---

## Layer 3 — Entry-time (`classify_tick`, every tick)

| ID  | Rule                       | Meaning                                                     | Status |
|-----|----------------------------|-------------------------------------------------------------|--------|
| S1  | Market tradeable           | snapshot.market_status TRADEABLE → else NO_PRICE            | ✓ |
| S2  | Last price available       | snapshot.last_traded non-null → else NO_PRICE               | ✓ |
| S3  | Entries-open               | US ≥ 09:45 ET, UK ≥ 08:15 UK (R_SESSION_PREMATURE)          | ✓ 2026-04-24 |
| S4  | Entries-cutoff             | US default 14:30 ET / session_end − 60min (R_SESSION_CUTOFF) | ✓ |
| 4   | Pivot Buy / Pivot Short    | Strict breakout. LONG > trigger_high, SHORT < trigger_low.  | ✓ via Rule 22 |
| 4-C | Open > pivot ± 3% (no BGU) | Skip if open chases > 3% past pivot without gap-up qualifier | ✓ ENFORCED 2026-04-28 (#58) |
| 5   | Volume confirm 1.4×        | Breakout candle volume ≥ 1.4× 50-day avg                    | DROPPED — intraday-only |
| 6   | EMA pullback (L-B / S-B)   | Buy-stop above 10/20 EMA touch candle                       | DROPPED — L-A only universe |
| 9A  | BGU 15-min OR (LONG)       | Gap-up day → 15-min opening range → strict break OR_high     | ✓ 2026-04-24 |
| 9B  | BGU VWAP fallback (LONG)   | After OR window, limit at session VWAP if no OR break        | ✗ NOT ENFORCED (#60) |
| 9-L | Late-stage gap = exhaustion | Late-stage BGU → reject even if other gates pass             | ✗ NOT ENFORCED (#59) |
| 10  | SGD (SHORT mirror of 9)    | S-C entry. Late-stage gap-down → 15-min OR or VWAP limit    | DEFERRED — SHORT off |
| 22  | Strict breakout            | LONG > trigger_high, SHORT < trigger_low. Inside-zone REJECT | ✓ 2026-04-24 |

**Trigger zone width:** `0.5 × ATR14` above pivot for LONG (mirror for
SHORT). Adapts to each stock's natural volatility (Raschke /
Masterclass canonical). Was fixed 3% — changed 2026-04-28.

---

## Layer 4 — Pre-fire (executor, after FIRE)

| ID  | Rule                       | Status |
|-----|----------------------------|--------|
| X1  | Risk-budget capacity       | ⏳ NEEDS VERIFICATION |
| X2  | Position-size cap          | ⏳ NEEDS VERIFICATION |
| X3  | UK spread filter           | ⏳ ACTIVE — display ✓, executor wiring needed (#62 doc partial) |
| X4  | Earnings blackout          | ✓ in code, ⏳ env-flag off in Vercel — flip `EVENT_FILTER_ENABLED=1` |
| X5  | Sector correlation cap     | DROPPED — rare bind at N=3 |

---

## Layer 5 — Post-fill (NOT entry; listed for completeness)

| ID  | Rule                          | Status |
|-----|-------------------------------|--------|
| 7   | Scaling T1/T2                 | OFF (design) — single-entry 100% on trigger |
| 8   | Order Types (stop-limit/limit) | ✓ |
| 11  | Overnight & catalyst protect  | ⏳ PARTIAL |
| 12  | UK-Specific (LSE auction etc) | ✓ at account-mode level |
| E1  | 15% emergency cover (SHORT)   | ⏳ NEEDS VERIFICATION |
| E2  | Failed gap reclaim window     | ⏳ wiring unverified |

---

## Rejection codes (most common)

| Code                  | Meaning                                                 |
|-----------------------|---------------------------------------------------------|
| `R_SESSION_PREMATURE` | Tick before 09:45 ET / 08:15 UK (S3)                    |
| `R_SESSION_CUTOFF`    | Tick after entries-cutoff (S4)                          |
| `R20`                 | Inside 15-min opening range (Rule 9A — wait)            |
| `R21`                 | After OR window but no break of OR_high (Rule 9A)       |
| `R22`                 | Inside trigger zone, no strict breakout (Rule 22)       |
| `R23`                 | Open chased > 3% past pivot without BGU (Rule 4-Chase)  |
| `R3`                  | Stop > 8% from entry — drop in scanEmission (Rule R3)   |

---

## Trading-day quick-flow

1. **Scan** (one per day, swing-committee Vercel) — picks top-3
   candidates by grade.
2. **Pre-flight** (scanEmission) — R1–R6 sized & validated; trigger /
   stop / target derived deterministically (§4.6). R3 drops anything
   with stop > 8%.
3. **Entries window opens** (09:45 ET / 08:15 UK) — manual_entry_monitor
   starts ticking. S1/S2/S3 active.
4. **Per-tick gating** — Rule 9A (gap-up days) or Rule 22 (non-gap)
   decides FIRE vs HOLD. Rule 4-Chase pre-screens.
5. **FIRE** — executor pre-fire X1–X4 (X5 dropped). Order placed with
   limit_level attached for the £50 hard cap.
6. **Hold to 15:55 ET / 16:25 UK** — hard-close. No overnights while
   Iran geopolitics unresolved.

---

## Status legend

- **✓ ENFORCED** — fully wired, has tests, passes the live engine.
- **⏳ PARTIAL / DRIFT** — implemented but doesn't match the spec, or
  gated by a flag that's off, or wiring incomplete.
- **✗ NOT ENFORCED** — not in code today.
- **DROPPED** — deliberately not enforced for the current profile
  (top-3 × 1%, intraday-managed, LONG-biased). Re-enable when profile
  changes.
- **DEFERRED** — paused until SHORT trading or other prerequisite
  returns.
- **OFF (design)** — by-design disabled (e.g. R7 scaling).

---

## Maintenance

When a rule's status flips (✗ → ✓, DROPPED → ⏳, etc.), update **both**
this card AND `docs/specs/CANONICAL_ENTRY_RULES.md` in the same
commit. Per `feedback_rule_doc_sync` 2026-04-28: code/doc drift is
expensive, two-doc updates are cheap.
