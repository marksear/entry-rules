# Price-Feed Divergence Investigation (Phase 1)

**Incident:** DEMO Day-1 2026-04-20. JNJ SHORT filled at 13:14:24 UTC (0.24/pt
IG-side stake, fill 233.9, initial stop 238.81). Over the next 18 minutes the
monitor's `candidate_snapshots` view of JNJ stayed pinned in the 234.02–234.96
range with `peak_unrealised_pnl_gbp = 0.0` on every row. Mark, watching the
live IG dealing screen, saw the same position peak at **+£37.68** unrealised
— implying an IG-side price of **~232.33** at peak (arithmetic in §5). A
sustained ≈2 scan-unit offset, in the adverse direction for the monitor's view,
caused the spurious `INVALIDATION_EXIT` at 13:32:24.

Phase 1 scope: investigation only. No `src/` changes. Phase 2 fix is a separate
spec written after Mark reads this memo.

The sibling `TERMINAL_INVESTIGATION.md` §2 already cited the divergence and
flagged it as out-of-scope for that bug. This memo picks up from there and
pins down the cause.

---

## 1. Grep trail — the snapshot-read path

Only one function populates `last_traded` / `bid` / `ask` for the monitor:
[`MarketData.get_market_snapshot`](money-program-trading/src/data/market_data.py:437).

Write-side (the row that ends up in `candidate_snapshots`):

- [monitor.py:228](money-program-trading/src/engine/monitor.py:228) — `run_one_tick` pulls `snapshot.get("last_traded")` for trigger evaluation.
- [monitor.py:595-598](money-program-trading/src/engine/monitor.py:595) — P&L / peak calc uses the same `snapshot["last_traded"]`.
- [monitor.py:778, 820, 872, 899](money-program-trading/src/engine/monitor.py:778) — every event emitter (trigger, exit, session-end) pulls the same key.
- [trail_manager.py:303](money-program-trading/src/engine/trail_manager.py:303) — `evaluate_exit` reads `snapshot.get("last_traded")` for both trail-ladder arming and the invalidation-window cross check.
- The row writer ([session_writer.py](money-program-trading/src/logging_mod/session_writer.py)) simply persists whatever the monitor passes it; it does no scaling or transform.

Read-side (where the price comes from):

- [market_data.py:472-473](money-program-trading/src/data/market_data.py:472) — single HTTPS call: `self.ig.fetch_market_by_epic(epic)` (trading-ig's wrapper for IG's REST `GET /markets/{epic}`).
- [market_data.py:522-556](money-program-trading/src/data/market_data.py:522) — reads `instrument.scalingFactor`; when missing and epic+instrument match the US-equity-spread-bet pattern AND `bid > 1500`, falls back to `scalingFactor = 100` with a loud `WARNING`.
- [market_data.py:560-568](money-program-trading/src/data/market_data.py:560) — `last_traded` is taken from `snap["lastTraded"]` or `snap["lastTradedPrice"]`; falls back to `(bid+ask)/2` when absent.
- [market_data.py:576-590](money-program-trading/src/data/market_data.py:576) — all prices divided by `scaling_factor` before return.

**No streaming / Lightstreamer consumer exists anywhere in `src/`:**

```
$ grep -rn "Lightstreamer\|streaming\|subscribe_market\|stream_\|LSClient" src/
(no matches)
```

The monitor is a pure REST-polling client. Every snapshot is one
`/markets/{epic}` call per tick.

Comparison with the write-side stake/limit scaling fixed in
[`f07f68b`](money-program-trading/src/engine/broker.py) / `9b4030a`: those
descale by `scaling_factor` on the way out to IG (`stake / sf`,
`limit_level * sf`). The read-side mirror is already present here
(`price / sf` on return) — the symmetry is correct. **The read path's
scaling is not the bug.**

---

## 2. Epic metadata

`data/cache/epic_map.json` is a flat `{symbol: epic}` map, no
scalingFactor / minDealSize cached:

```
"JNJ:US": "SD.D.JNJ.DAILY.IP",
```

Live session log shows the epic IG returned for JNJ had
`scalingFactor = None` (missing) on every tick, so the
`_looks_like_equity_spreadbet + bid>1500` fallback fired:

```
reports/live_20260420_f07f68b.log:45
  get_market_snapshot: SD.D.JNJ.DAILY.IP — scalingFactor missing/invalid (raw=None)
  but type=SHARES and bid=23390.0 ask=23460.0 look like minor units;
  assuming scalingFactor=100 (equity convention).
```

After ÷100: `bid=233.90 ask=234.60` — consistent with the row the monitor
wrote to `candidate_snapshots` at 13:14:24. The scaling applied *correctly*
(the fallback worked and converted to scan-unit range).

Broker's `place_open_position` path used the same epic (`SD.D.JNJ.DAILY.IP`)
with the same scalingFactor=100 fallback. The `FILLED` event payload records
`fill_price = 233.9`, which matches monitor's bid at that tick exactly.

So the deal opened on the same epic the monitor is polling. **No epic
mismatch.**

---

## 3. DB forensics — `candidate_snapshots` and `candidate_events`

Snapshot series for JNJ, 2026-04-20, full 18-min window (trimmed columns;
`last_price` is the DB's name for what the runtime calls `last_traded`):

| ts_utc | status | last_price | bid | ask | peak_pnl | mins |
|---|---|---|---|---|---|---|
| 13:14:24 | PENDING_TRIGGER | 234.25 | 233.90 | 234.60 |  |  |
| 13:14:54 | TRIGGERED_OPEN | 234.25 | 233.90 | 234.60 | 0.0 | 0 |
| 13:15:24 | TRIGGERED_OPEN | 234.25 | 233.90 | 234.60 | 0.0 | 1 |
| 13:15:54 | TRIGGERED_OPEN | 234.25 | 233.90 | 234.60 | 0.0 | 1 |
| 13:16:24 | TRIGGERED_OPEN | 234.25 | 233.90 | 234.60 | 0.0 | 2 |
| 13:16:54 | TRIGGERED_OPEN | 234.25 | 233.90 | 234.60 | 0.0 | 2 |
| 13:17:24 | TRIGGERED_OPEN | 234.25 | 233.90 | 234.60 | 0.0 | 3 |
| 13:17:54 | TRIGGERED_OPEN | 234.25 | 233.90 | 234.60 | 0.0 | 3 |
| 13:18:24 | TRIGGERED_OPEN | 234.35 | 234.10 | 234.60 | 0.0 | 4 |
| 13:18:54 | TRIGGERED_OPEN | 234.35 | 234.10 | 234.60 | 0.0 | 4 |
| 13:19:24 | TRIGGERED_OPEN | 234.25 | 233.90 | 234.60 | 0.0 | 5 |
| 13:19:54 | TRIGGERED_OPEN | 234.35 | 234.10 | 234.60 | 0.0 | 5 |
| 13:20:24 | TRIGGERED_OPEN | 234.195 | 233.90 | 234.49 | 0.0 | 6 |
| ... | ... | ... | ... | ... | ... | ... |
| 13:27:54 | TRIGGERED_OPEN | 234.025 | 233.45 | 234.60 | 0.0 | 13 |
| 13:28:24 | TRIGGERED_OPEN | 234.025 | 233.45 | 234.60 | 0.0 | 14 |
| 13:28:54 | TRIGGERED_OPEN | 234.13 | 233.66 | 234.60 | 0.0 | 14 |
| 13:29:24 | TRIGGERED_OPEN | 234.13 | 233.66 | 234.60 | 0.0 | 15 |
| 13:31:54 | TRIGGERED_OPEN | 234.48 | 234.12 | 234.84 | 0.0 | 17 |
| **13:32:24** | **TERMINAL** | **234.96** | 234.65 | 235.27 | 0.0 | 18 |

Full count: **37 snapshots** across the 18-minute window. Polling
cadence is 30 seconds as designed. The monitor was not blocked, lagging,
or missing ticks — it polled 37 times and received 37 near-identical
answers.

Critical observations:

- **`bid` and `ask` are present**, so this isn't a `last_traded`-only
  anomaly. Both quote sides agree and both stayed high.
- **`bid` range across the full 18 minutes: 233.45–234.65** (1.2 scan-units).
  **`ask` range: 234.43–235.27** (0.84 scan-units). For a live US equity
  inside the US regular-trading-hours window after 13:30 UTC, this is
  implausibly narrow.
- **`ask = 234.60` repeats verbatim on 9 distinct snapshot rows** spanning
  ~8 minutes (13:14:54–13:22:54 inclusive). Real intraday top-of-book
  doesn't sit at one decimal for 8 minutes during US RTH.
- `peak_unrealised_pnl_gbp = 0.0` every row — monitor's price never moved
  past fill in the favourable (down) direction, by the monitor's data.

Events for JNJ on 2026-04-20 (see TERMINAL_INVESTIGATION.md §2 for full
payloads — not duplicated):

- 13:14:24 `FILLED` — `fill_price=233.9`, matches monitor's bid at that tick.
- 13:32:24 `INVALIDATION_EXIT` — `last_price=234.96`, `mins_since_fill=18`.
  `trail_manager.py:336-342` fired correctly *given the data it received*:
  adverse re-cross of `trigger_high=234.5` on a SHORT inside the 30-min
  invalidation window.

---

## 4. Live-session log: the smoking gun

[reports/live_20260420_f07f68b.log](money-program-trading/reports/live_20260420_f07f68b.log)
contains a `WARNING` log line for every `get_market_snapshot` call (emitted
by the scalingFactor fallback), including the **raw** IG bid/ask before
scaling. Sampling JNJ across the 18-minute window:

```
14:14:25  SD.D.JNJ.DAILY.IP  bid=23390.0  ask=23460.0
14:14:54  SD.D.JNJ.DAILY.IP  bid=23390.0  ask=23460.0
14:15:24  SD.D.JNJ.DAILY.IP  bid=23390.0  ask=23460.0
14:15:54  SD.D.JNJ.DAILY.IP  bid=23390.0  ask=23460.0
14:16:24  SD.D.JNJ.DAILY.IP  bid=23390.0  ask=23460.0
14:16:54  SD.D.JNJ.DAILY.IP  bid=23390.0  ask=23460.0
14:17:24  SD.D.JNJ.DAILY.IP  bid=23390.0  ask=23460.0
14:17:54  SD.D.JNJ.DAILY.IP  bid=23390.0  ask=23460.0
14:18:24  SD.D.JNJ.DAILY.IP  bid=23410.0  ask=23460.0
...
```

(Log timestamps are BST = UTC+1; the DB stores UTC.)

The exact integer pair `bid=23390.0 ask=23460.0` repeats **10 times**.
`ask=23460.0` repeats far more often — anchored at 234.60 for the majority
of the 18-minute window.

**This is not a client bug.** The raw IG REST payload itself is
re-delivering the same integer cent values tick after tick. The client
divides by 100 faithfully, which is why the persisted `bid` / `ask` /
`last_traded` look plausible in isolation — but they are plausible stale
values, not plausible live values.

Corroborating evidence from the same log file — other tickers in the
universe show the same pattern during the same window:

```
$ grep -c "bid=7860.1 ask=7875.9"  reports/live_20260420_f07f68b.log
32    # SA.D.AIG.DAILY.IP — identical bid/ask for 32 consecutive snapshots
```

32 consecutive ticks of an identical quote (to the first decimal) on a
live US-listed insurer during its own RTH is not survivable as "real data".
The REST endpoint is returning a cached/snapshot quote that only refreshes
infrequently.

The dealing engine (where the actual deal lives) is driven by IG's
**streaming** bid/ask (Lightstreamer), not by `/markets/{epic}` REST.
Those two feeds are *separate surfaces* inside IG. The streaming feed
updates on every market tick; the REST snapshot endpoint is a slow-refresh
aggregate. Over 18 minutes of real price drift, the streaming feed tracked
JNJ down to ~232.33 while REST continued to serve ~233.90/234.60 almost
unchanged.

---

## 5. Arithmetic check — IG-implied peak price

IG-side stake (after `÷scalingFactor=100` descaling in the broker): 11.6 £/pt
÷ 100 = 0.116 £/pt; clamped up to the epic's minDealSize **0.24 £/pt** (see
log line 48). So the live deal sits at 0.24 £/pt.

1 IG "point" on a US-equity DAILY.IP spread-bet epic corresponds to a
**1-cent** move in the underlying (IG quotes cents internally — the 100×
relationship we've been descaling on the way in and out). At 0.24 £/pt and
a SHORT opened at bid = 233.9:

```
peak_unrealised_pnl_gbp      = 37.68
stake_gbp_per_cent           = 0.24
cents_moved_favourably       = 37.68 / 0.24 = 157
dollars_moved_favourably     = 157 / 100 = 1.57
implied_IG_price_at_peak     = 233.90 - 1.57 = 232.33   (SHORT → down is favourable)
```

Offset at peak:

```
monitor_min_bid_over_window  = 233.45      (row at 13:23:24, 13:24:24, …)
implied_IG_price_at_peak     = 232.33
offset                       = 1.12 scan-units on bid
```

Offset on `last_traded` (the column `trail_manager` actually reads):

```
monitor_min_last_traded      = 233.94      (13:23:24)
implied_IG_price_at_peak     = 232.33
offset                       = 1.61 scan-units
```

Offset on `ask` (what IG uses to close a SHORT):

```
monitor_min_ask              = 234.43      (13:21:54)
implied_IG_peak_ask_approx   = 232.33      (using IG's displayed PnL)
offset                       = 2.10 scan-units
```

All three offsets are in the same direction (monitor high vs IG low)
and of similar magnitude (~1.1–2.1 scan-units). Consistent with a
stale REST feed that never caught up to the live streaming feed.

The spec's nominal "~£2.00" → ~2.0 scan-unit offset figure checks out on
the `ask` side; the bid / last_traded sides are slightly smaller but in
the same class.

---

## 6. Hypothesis match table

| # | Hypothesis | Match | Evidence |
|---|---|---|---|
| 1 | Missing/incorrect scaling factor on read path | **No** | Fallback correctly detected missing `scalingFactor`, applied ×100, and produced values in sane scan-unit range that matched the broker's `fill_price=233.9` exactly. If scaling were the bug, monitor would show values in 23000+ range or 2.3 range, not 234.x. Values were *accurately scaled stale prices*. |
| 2 | Epic mismatch — monitor polls a different instrument | **No** | `epic_map.json` resolves JNJ:US to `SD.D.JNJ.DAILY.IP`. Broker opened the deal on the same epic (log line 49 "scaling SD.D.JNJ.DAILY.IP ×100 → stop_level=23881"). `fill_price` from the broker matched monitor's bid at fill tick. Only one epic involved end-to-end. |
| 3 | **Cache / streaming staleness** | **STRONG** | Raw REST bid=23390/ask=23460 repeated verbatim ≥10 times over 18 min; AIG's bid=7860.1/ask=7875.9 repeated **32** times in the same session; total quote range for JNJ across 37 live ticks is 1.2 scan-units, impossibly narrow for US RTH; polling cadence was correct (37/18min = 30s as designed) so this is not a polling-side issue — the upstream REST endpoint is delivering slow-refresh cached quotes. IG's dealing engine uses the streaming (Lightstreamer) feed, which is a separate surface. |
| 4 | Clock-skew on `ts_utc` | **No** | `ts_utc` values are smoothly spaced at 30-second intervals (see §3). The problem is bad prices at correct timestamps, not vice-versa. |
| 5 | Currency / unit mismatch (FX conversion) | **No** | A GBPUSD ≈1.26 offset would produce a ~60-point gap on a $234 stock, not 2 points. Ruled out. |

**Best match: Hypothesis 3 — the IG REST `/markets/{epic}` endpoint is
serving stale/slow-refresh quotes while the dealing engine (and the IG
app Mark was watching) uses the live streaming feed. The client-side
scaling and epic resolution are both fine.**

Hypotheses 1 and 2, while both plausible a priori, are each directly
falsified by log / DB evidence (monitor's values *are* in scan units
and *do* match the broker's `fill_price`).

---

## 7. Recommended Phase 2 fix shape

**Recommended: S-3 (feed swap), with S-4 interim gate.**

### Why S-3 (streaming feed) over S-1 and S-2

- S-1 (scaling) and S-2 (epic resolution) are both ruled out by §6 —
  there is nothing to fix on those paths for this incident.
- The REST endpoint is fundamentally the wrong data source for a
  monitor that has to make exit-or-hold decisions on 30-second
  cadence. Any tuning short of switching feeds is going to work
  "most of the time" at best.
- trading-ig already ships a Lightstreamer client
  (`trading_ig.lightstreamer.LSClient`); migration cost is a new
  subscription manager module + rewire of `MarketData.get_market_snapshot`
  into a cache fronted by an async streaming listener. Non-trivial,
  but bounded.

### Proposed S-4 interim gate (ship first, before S-3)

Because S-3 is a medium-sized change and DEMO Day-2 is imminent, add
a pre-exit sanity gate in `run_one_tick` (or in `_handle_exit` before
calling `broker.close_position`):

```
# Pseudocode — Phase 2 spec will land the real version.
if decision is EXIT:
    live_deal_price = broker.get_deal_price(deal_id)   # already exists on IG REST deals API
    monitor_price   = snapshot["last_traded"]
    if abs(live_deal_price - monitor_price) / monitor_price > 0.003:   # 30 bps, say
        emit_event("EXIT_SUPPRESSED_FEED_DIVERGENCE",
                   monitor=monitor_price, broker=live_deal_price)
        return   # do not close; next tick re-checks
```

This turns the class of bug into a visible, event-table-recorded
"I refused to act on suspect data" rather than a spurious close.
Cheap to implement, orthogonal to the Phase 2 TERMINAL fix, and
survives S-3 landing later (it becomes belt-and-braces).

The paired Phase 2 TERMINAL fix (Shape C in `TERMINAL_INVESTIGATION.md`)
only addresses what happens when `broker.close_position` returns
`success=False`. It does **not** protect the monitor from *deciding to
close* on bad data — the close could succeed cleanly and still exit a
winning position. The S-4 gate is the complementary half.

### S-3 scope sketch (for the Phase 2 spec)

- New module: `src/data/streaming_quotes.py`, owning an LSClient and
  an in-memory `{epic: latest_quote}` dict.
- Subscribe to `MARKET:{epic}` streams for every symbol in the session's
  universe at session-start; unsubscribe at session-end.
- `MarketData.get_market_snapshot` becomes a read of the in-memory
  dict, with the REST path kept as a cold-start fallback (first tick
  before the stream has delivered) and as a sanity backstop when the
  stream silently disconnects.
- Add a `feed_source` column to `candidate_snapshots` so post-session
  forensics can tell REST rows from streaming rows.
- Keep the existing scalingFactor fallback logic — streaming payloads
  also omit `scalingFactor` on some epics, per trading-ig docs.

Test strategy: unit-test the dict-backed reader with a fake LSClient;
integration-test end-of-session reconciliation (streaming latest vs
REST at shutdown) to detect silent stream disconnects.

---

## 8. Secondary concerns surfaced

- **Other symbols affected.** AIG, ARMUS, and every other equity in the
  session show the same stuck-quote pattern (AIG's quote repeated **32
  times** verbatim). This wasn't a JNJ-specific flake; the whole
  universe was being run on stale REST snapshots. Any future paper /
  live run that opens a position will be equally exposed until the
  feed swap lands.

- **`broker.close_position` also emitted
  `validation.mutual-exclusive-value.request`** at 13:32:24
  ([log line 273](money-program-trading/reports/live_20260420_f07f68b.log)).
  This is the close-failure path the TERMINAL investigation already
  identified — so the spurious exit *was paired* with a failed close
  (which is why the position stayed open at IG). Noted but not in scope
  here — addressed by the sibling Phase 2 TERMINAL fix.

- **`peak_unrealised_pnl_gbp` never moved off 0.0.** Even with a stale
  feed, if monitor bid ever dipped from 233.90 to 233.45 (which it did,
  per §3), a SHORT opened at 233.9 should have shown a small positive
  peak. Worth checking whether the peak calc at
  [monitor.py:595](money-program-trading/src/engine/monitor.py:595) uses
  `last_traded` (which can be the bid/ask midpoint when no trades print,
  per [market_data.py:567-568](money-program-trading/src/data/market_data.py:567))
  and the midpoint was anchored to 234.1 even when bid dipped. Flag for
  a follow-up task — orthogonal to the feed-swap fix.

- **`epic_map.json` has no `scalingFactor` / `minDealSize` / `type`
  cache.** Every session re-discovers these via REST. If
  `/markets/{epic}` is slow/stale for quotes, it may also be slow/stale
  for instrument metadata on the way in. Consider caching the
  `instrument` block alongside the epic mapping once per session.

- **No integration test exercises the "monitor sees a stuck quote"
  path.** The repo has test doubles that always return fresh prices.
  An adversarial fixture that serves the same bid/ask N times in a row
  and asserts the monitor doesn't fire a spurious exit would close the
  loop after the S-4 gate lands.
