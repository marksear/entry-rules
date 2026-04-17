# Intraday Committee — Build Plan

**Status:** decisions captured 2026-04-17. Owner: Mark. Target start: 2026-04-20 (Mon).
**Plan version:** v1 — decisions locked; open items flagged inline.

---

## Decisions locked 2026-04-17

Mark's directional calls supersede any "proposal" wording later in this doc:

1. **Strategic reframe:** NOT "replace swing with intraday." Keep the swing committee signals (Livermore, O'Neil, Minervini, Darvas, Raschke, Weinstein). Manage trades to **exit same-day where possible**, hard-close before session end. Rationale: most swing moves happen day-1; day-1-only exits capture the bulk of the edge while dropping overnight gap risk. This reverses the v0 draft below that re-weighted to Raschke-central and dropped Minervini/Darvas — those are back in.
2. **Universe:** ~200 tradable epics — S&P 100 ∪ Nasdaq 100 (deduped) + 4 IG **index proxies** (US 500 / US Tech 100 / Wall Street / US Russell 2000) + FTSE 100 / Germany 40 indices + 15 FTSE 100 liquid single names. Locked.
3. **ETF spread-bet check (resolved 2026-04-17):** IG DEMO search confirms **SPY/QQQ/IWM/DIA/XLK are NOT offered as direct spread bets** on UK retail. IG serves that exposure via its own index products: US 500 (`IX.D.SPTRD.DAILY.IP`), US Tech 100 (`IX.D.NASDAQ.CASH.IP`), Wall Street / Dow (`IX.D.DOW.DAILY.IP`), US Russell 2000 (`IX.D.RUSSELL.DAILY.IP`), plus FTSE 100 (`IX.D.FTSE.DAILY.IP`) and Germany 40 (`IX.D.DAX.DAILY.IP`) for variety. **XLK has no IG equivalent** — the "Technology Select Sector" concept doesn't exist as a spread-bet product. Substitute by trading the tech mega-cap single names directly (AAPL/MSFT/NVDA/GOOGL/META) — cleaner breakouts anyway. Full evidence: `data/cache/etf_verification.json`.
4. **Risk ladder:** **keep swing ladder** A+/A/B = 1% / 0.75% / 0.5%. Do NOT halve. (v0 draft's 0.5%/0.35%/0.25% proposal rejected.)
5. **Max trades per day:** 3. Locked.
6. **Last-entry cutoff:** **19:30 UK (90 min before close) + hard rule: no new entries in final 60 min.** Locked as v1 starting point. Mark's gut says cut off earlier; flag to revisit once the backtest harness can quantify expectancy decay on late-session entries — then argue from data, not vibes.
7. **Hard-close buffer:** 10 min before session close, no exceptions. Locked.
8. **Event filter:** Finnhub free tier. Locked.
9. **Backtest data:** Yahoo Finance 5-min bars for now; switch to Polygon/databento after live validation. Locked.
10. **swing-committee repo:** **permanent.** Stays as the scan producer. Do not delete. The LLM-hallucinated-prices problem is a separate bug to solve inside swing-committee (ground scan prices in real IG quotes via a post-scan price-check step).

---

## Why we're rebuilding

Three problems compound in the current system:

1. **Scan prices are LLM-hallucinated.** The 2026-04-17 shakedown surfaced AMD at a fictional $278 and FDX at $380 — self-consistent but not the tape. Every trigger/stop/target downstream of that is a bet on the model having memorised good 2025 price data. That's not a foundation to risk real money on.
2. **Overnight carry is a small-account killer.** Gap risk, DFB funding, and post-close headline risk all eat edge. A £5k account can't afford a 2% gap against a 1% risk.
3. **The pipeline isn't backtestable.** LLM prompts aren't deterministic, so the current scanner can't be replayed against history. Without that, there's no honest gate between DEMO and LIVE.

The rebuild solves all three by collapsing the scan into the execution repo, expressing each trader's rules as code over IG bars, and committing to **single-session, intraday-only** holds.

---

## Strategic shift — from swing to intraday

Today's system is "swing trader who also day-trades" — 2–3 day hold cap, overnight stops, timestop at N sessions. We're reworking to **pure intraday**:

- Every position opens and closes within the same session.
- Hard close 10 minutes before the relevant market's close, no exceptions.
- No entries in the last 2 hours of the session.
- No rehydration of open positions on next-day launch — if IG shows an open position at startup, that's a bug or a manual trade and surfaces an alert.

This forces a re-weighting of the committee. Most of the six traders whose methodology we were going to encode operate on weekly or multi-week timeframes:

| Trader | Native timeframe | Intraday fit |
|---|---|---|
| Livermore | Daily + intraday | ✅ partial — pivotal points / reaction buying transfer |
| O'Neil (CANSLIM) | Weeks to months | ⚠️ only the RS + volume pieces; the fundamentals half (C, A) is out |
| Minervini (VCP) | Days to weeks | ❌ VCP spans days; out of scope for intraday |
| Darvas (boxes) | Days to weeks | ❌ boxes need days to form |
| Raschke | **Intraday** | ✅ core — ORB, turtle soup, momentum pinball, 3-day high/low are all intraday native |
| Weinstein (stages) | Weekly | ❌ weekly stage analysis; out of intraday runtime but useful as a **regime filter** |

Translation: **Raschke becomes the centre of gravity.** Livermore's reaction-buying and Weinstein's stage filter earn seats. The others either drop out or get re-scoped as slower filters (e.g., Weinstein's stage 2 as a screen, not a signal).

---

## What "optimal for making money" looks like for this account

Small UK spread-bet account. A few structural realities drive the design:

**Spread cost dominates at small stake.** A £5/pt stake on a 5-pt spread = £25 paid at entry, £25 at exit. That's a 10-pt hurdle before the first penny of profit. Every rule we write needs to price this in — trades have to target at least **3× the spread** before we'll take them.

**Liquidity is a hard filter, not a tiebreaker.** IG widens spreads aggressively on thin names and outside US regular hours. The scan universe should be hand-picked liquid names, not "every S&P 500 constituent". My starting suggestion: 30–50 US mega-caps + the big ETFs (SPY / QQQ / IWM / XLF / XLK), plus 10–20 FTSE 100 names for UK-session variety. Bigger universes just mean more noise for a solo trader.

**Earnings and scheduled news must force abstention.** No position opened within 2 trading days of the name's earnings release. Scheduled macro (FOMC, NFP, CPI) likewise halts new entries for that session. This is the single biggest edge protection for a small account — one earnings-gap loss can wipe a month of gains.

**Risk ladder probably halves.** Current memory says A+/A/B = 1% / 0.75% / 0.5%. For intraday with ~30min to a few hours holds, typical R is smaller and trade frequency is higher, so 1% compounds into drawdown faster. Starting proposal: **A+/A/B = 0.5% / 0.35% / 0.25%**, max 3 trades per session, daily drawdown circuit-breaker at −2R (if you lose 2R total in a day, you're done for the day). These are levers you can tune — bake them into `Settings`, not code.

**No trades in the "chop hour".** 14:30–15:00 UK (first 30 min of US open) is opening volatility. Scanner watches but doesn't signal. Entries from 15:00 UK onward. This also gives the opening range enough bars to be meaningful.

---

## The intraday committee — redefined

Each "pillar" is a pure function. Input: `Bars` (daily + 1-min / 5-min intraday) + `MarketContext` (regime, VIX level, SPY trend, sector RS). Output: `PillarResult(pass: bool, score: float, reason_code: str, payload: dict)`.

Grades come from a voting scheme: A+ = 4+ pillars pass with at least one being a "primary signal" (ORB or pivotal-point). A = 3 pillars, B = 2 pillars.

**Regime filter (Weinstein's ghost).** Daily SPY stage (1–4) plus 20-EMA slope. Only LONG when SPY is in stage 2 and EMA rising. Only SHORT when stage 4 and EMA falling. This is a hard gate, not a vote — if regime fails, scan emits zero candidates.

**Relative strength (O'Neil-lite).** Daily: ticker closing strength vs. SPY over last 20 days (percentile ≥ 80 for LONG). Intraday: ticker vs. SPY since open, positive for LONG.

**Opening range breakout (Raschke — primary signal).** 30-min opening range calculated 15:00 UK. Break above OR-high with volume ≥ 1.5× 20-day average at that bar triggers LONG. Symmetric for SHORT below OR-low.

**Pivotal-point reaction (Livermore — primary signal).** A pullback of 38–61% of the opening-range move, then rejection back to the OR boundary on increasing volume. Fires as a secondary entry when ORB missed but the reaction is clean.

**Accumulation / distribution (O'Neil-lite).** Today's volume-weighted move vs. last 10 sessions. Positive cumulative delta + rising price = accumulation = pass. Protects against "false breakouts on light volume".

**Turtle soup / failed-breakdown (Raschke).** Price breaks prior day low by ≥0.3× ATR then reverses back above within 15 min on rising volume = reversal LONG setup. Symmetric SHORT on failed-breakout of prior-day high.

**Event filter (meta, applies to all).** If ticker has earnings within 2 trading days OR a scheduled macro event intersects the session, all pillars short-circuit to FAIL with reason `EVENT_BLACKOUT`. Economic calendar sourced from a free API (FMP, Polygon free tier, or Finnhub) — cached daily.

Derived levels from the data, not the LLM:

- **Trigger**: OR-high (LONG) / OR-low (SHORT) +/- half-spread cushion.
- **Stop**: OR-opposite-side, or 1× ATR(5-min, 20 periods), whichever is tighter. Initial stop is maximum **0.4%** of ticker price — anything wider means too much risk for the account, so the trade is skipped (not sized down).
- **Target 1**: 1.5R. Target 2: 2.5R (trail remainder or hard exit). For intraday, I'd start with single target at 2R and a £-trail after the first R is banked — matches the trail manager already built.

---

## Architecture — destination state

```
entry-rules/money-program-trading/
├── src/
│   ├── scanner/                    ← NEW
│   │   ├── __init__.py
│   │   ├── run.py                  CLI: python -m src.scanner.run
│   │   ├── universe.py             config: which tickers, which session
│   │   ├── market_context.py       regime, VIX, SPY trend, sector RS
│   │   ├── pillars/
│   │   │   ├── __init__.py
│   │   │   ├── base.py             PillarResult dataclass + interface
│   │   │   ├── regime.py
│   │   │   ├── relative_strength.py
│   │   │   ├── opening_range.py
│   │   │   ├── pivotal_point.py
│   │   │   ├── accumulation.py
│   │   │   ├── turtle_soup.py
│   │   │   └── event_filter.py
│   │   ├── grader.py               pillar votes → A+/A/B grade
│   │   ├── level_derivation.py     trigger/stop/target from bars
│   │   └── writer.py               persists to shortlist_entries + SHORTLIST_ADDED events
│   ├── engine/                     (existing — unchanged)
│   ├── data/                       (existing — MarketData grows intraday helpers)
│   └── ...
└── docs/
    └── intraday_committee_build_plan.md   (this file)
```

**One command replaces the whole swing-committee flow:**

```bash
.venv/bin/python -m src.scanner.run \
  --universe us_mega_caps \
  --session US_REGULAR \
  --broker-mode DEMO \
  --account-size-gbp 5000
```

Runs pre-market, writes the day's shortlist into SQLite. The existing `session_init` then launches the monitor against those rows, unchanged.

**swing-committee's role:** deleted from the runtime path. Kept in the git history. If Mark wants a visualisation layer later, it comes back as a read-only dashboard querying the entry-rules DB over a thin HTTP API — never writing, never generating.

---

## Build sequence

Sessions are numbered continuing from the existing session 8. Each ends with green tests and a committable checkpoint.

### Session 9 — Foundation + regime + first primary signal (~1 day)

Goal: end with one runnable scan producing real candidates, scored on a minimal committee.

1. `src/scanner/pillars/base.py` — `PillarResult` dataclass + abstract interface. ~40 lines, mostly dataclass.
2. `src/data/market_data.py` — `get_intraday_bars` already exists; add `get_pre_market_bars` (extended-hours fetch) and `get_session_vwap` helper.
3. `src/scanner/market_context.py` — compute regime from SPY daily + 20-EMA slope. `MarketContext` carries `regime: Stage1|2|3|4`, `vix_level`, `spy_trend_dir`.
4. `src/scanner/pillars/regime.py` — hard-gate evaluator.
5. `src/scanner/pillars/opening_range.py` — primary signal; ORB 30-min with volume confirmation.
6. `src/scanner/level_derivation.py` — compute trigger/stop/target from OR structure.
7. `src/scanner/writer.py` — write `ShortlistEntry` + emit `SHORTLIST_ADDED`.
8. `src/scanner/run.py` — CLI orchestrator: regime gate → for each ticker → ORB → grade → write.
9. **Tests**: 15–20 unit tests on synthetic bars. No IG integration yet.
10. Run `scanner.run --universe test_list --dry-run` against yesterday's cached bars. Visual check the candidates make sense.

Exit criteria: scanner produces a handful of candidates on a test universe, all levels are numbers from bars (not LLM).

### Session 10 — Full pillar set + grading (~1 day)

1. `pivotal_point.py`, `accumulation.py`, `turtle_soup.py`, `relative_strength.py`, `event_filter.py`.
2. `grader.py` — vote-counting, grade assignment.
3. Event filter wiring: fetch economic calendar + per-ticker earnings dates into a cache table, integrate into the event pillar.
4. **Tests**: each pillar gets ≥3 unit tests (pass, fail, edge). Grader gets 5 tests.
5. Re-run the scanner end-to-end. A+/A/B distribution should look reasonable (probably 0–2 A+, 2–5 A, 5–10 B on a typical day across 30 names).

Exit criteria: full committee votes, all 7 pillars covered, no placeholder returns.

### Session 11 — Intraday-only engine adjustments (~0.5 day)

Rework the existing monitor + exit config for single-session guarantees.

1. `ExitConfig` gains `session_end_cutoff_utc` and `last_entry_utc`. Monitor enforces both.
2. `timestop_sessions` locked to 1 (intraday).
3. Remove (or deprecate behind a flag) the `rehydrate_open_positions` path — an open position at start-of-day is a red-flag alert, not routine flow.
4. Add `DAILY_DRAWDOWN_CIRCUIT_BREAKER` event + session-level state in writer; once −2R crossed, no further signals consumed.
5. `risk_ladder` gets new defaults via `Settings`: 0.5 / 0.35 / 0.25.
6. **Tests**: 8–10 tests on the new exit cutoff, last-entry cutoff, and circuit-breaker.

Exit criteria: monitor cannot hold past close. Cannot enter late. Cannot overtrade the day.

### Session 12 — Backtest harness (~1 day — the critical one)

Without this, LIVE cutover is gambling.

1. `src/backtest/replay.py` — feed historical intraday bars into the scanner + monitor using the same code paths, with a fake broker that fills at bar-close + spread.
2. Dataset: 6 months of 5-min bars for the universe. Source: Yahoo Finance (free, good enough for large-cap US), or IG historical if we can afford the rate-limit spend.
3. `src/backtest/report.py` — emit: win rate, average R, expectancy, max drawdown, Sharpe, trades/day, grade-stratified results.
4. Per-pillar attribution — which pillars correlate with winning trades, which don't.
5. **Success gate for LIVE cutover** (non-negotiable):
   - Expectancy ≥ +0.2R per trade after spread costs.
   - Max drawdown ≤ 15% of starting equity.
   - Win rate ≥ 40% (room for asymmetric R).
   - At least 20 backtest days with ≥1 trade.
   - If any of these fail, rules get adjusted and backtest re-runs. LIVE stays blocked.

Exit criteria: backtest passes the gate, report reviewed and signed off.

### Session 13 — DEMO replay + UI decision (~0.5 day)

1. Run the new scanner on live DEMO for ≥10 sessions. Compare actual fills vs. backtest expectations.
2. If drift exceeds 20% on expectancy, halt and investigate (usually a slippage/spread model issue).
3. **Decision point on swing-committee:**
   - (a) delete entirely — simplest,
   - (b) keep as read-only dashboard — nicer but 0.5–1 day more work.
   - Default to (a) unless Mark specifically misses the narrative output.

Exit criteria: 10 DEMO days with real fills, backtest-vs-live drift within tolerance.

---

## LIVE-cutover gate (updated for this rebuild)

Current memory says "20 DEMO days with real fills before LIVE". Intraday trades faster, so 20 sessions = 20 days = ~4 weeks. The gate gets two new conditions:

1. Backtest passes the Session 12 gate.
2. DEMO shows:
   - Win rate within 5pp of backtest
   - Expectancy within 0.1R of backtest
   - No single day worse than −3R
   - At least 10 days profitable (50% of 20)

Only after both conditions hold does the LIVE-cutover runbook get written.

---

## Decisions I need from Mark before we start

These change the build, not just the config, so worth nailing down before code is written.

1. **Universe scope.** My default: 30 US mega-caps + 5 ETFs + 15 FTSE 100. Override if you want tighter (10 names) or a specific list.
2. **Risk ladder for intraday.** Proposal: 0.5 / 0.35 / 0.25 — halved from current swing config. Yes / no / different.
3. **Max trades per day.** Proposal: 3. Higher gives more opportunity, lower enforces selectivity.
4. **Last-entry cutoff.** Proposal: 18:00 UK for US session. Tighter (17:00) or looser (19:00)?
5. **Session-end hard-close buffer.** Proposal: 10 min before official close. Longer is safer (slippage-wise).
6. **Event filter source.** Proposal: Finnhub free tier for earnings + calendar. Alternative: manual weekly cache from company IR pages (zero cost, some discipline needed).
7. **Backtest data source.** Proposal: Yahoo Finance 5-min bars (free, 60-day rolling window — need to cache incrementally). Alternative: pay Polygon $29/mo for 2 years of 1-min data. Worth the cost if you want a real edge proof.
8. **swing-committee fate.** Delete now or keep as viewer? No urgency, but deciding up front lets Session 13 be shorter.

---

## Risks and open questions

**Survivorship bias in the backtest universe.** If we backtest on "today's S&P 500 mega-caps", we've already selected for 6 years of winners. Mitigation: fix the universe as of a historical date and hold it constant across the backtest window. For the MVP this is acceptable noise — we're not betting the account on small edge claims.

**IG API rate limits during scan.** 30 names × 1 daily-bar fetch + 1 pre-market-bar fetch = 60 calls. IG is ~10k/week. Cache aggressively (daily bars change once per day; intraday cached per bar close). Should be well within budget.

**Fundamentals absence** (O'Neil's C + A). Decision: skip for MVP. The chart-based CANSLIM pieces carry most of the tradable edge. Revisit only if backtest expectancy is borderline and adding fundamentals materially improves it.

**Spread widening at session edges.** Session starts with wider spreads for 5–10 min. The 15:00 UK entry-window rule already handles this. But we should log spread at entry + exit and surface in the journal so we notice if any name has pathological spreads.

**Slippage modelling.** Backtest assumes fill at bar close + half-spread. Real fills on MARKET orders can be worse during fast tape. Session 13's DEMO-vs-backtest drift check is the honest answer to this.

**What happens when no pillars vote?** Probably the common case on a chop day. Scanner emits zero candidates. Journal logs "no signals today". This should be celebrated, not worked around — no signal = no trade = no loss.

---

## Open items tracked separately

- Task #14 (pre-existing): validate the scaling patch on DEMO. Still worth doing before Session 9 kicks off so we know the broker adapter is sound.
- Ruff debt across older modules (Session 8 leftover). Low priority; can interleave during any build session.
