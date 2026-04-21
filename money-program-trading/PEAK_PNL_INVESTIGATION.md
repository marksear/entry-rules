# `peak_unrealised_pnl_gbp` Stuck at 0.0 — Phase 1 Investigation

**Incident:** DEMO Day-1 2026-04-20.

- **JNJ SHORT** (fill 233.9, 0.24/pt IG-side, 7.12 £/pt monitor-side): `peak_unrealised_pnl_gbp = 0.0` across all 37 post-fill snapshots. Live IG deal peaked at **+£37.68** (implied IG-side price ~232.33).
- **ARM LONG** (fill 167.1, 7.12 £/pt): manually closed at +£55.56. Per live-view recollection, held through a drawdown-and-recovery arc. Peak trajectory was unknown going in.

Upstream context: [`PRICE_FEED_DIVERGENCE_INVESTIGATION.md`](PRICE_FEED_DIVERGENCE_INVESTIGATION.md) §8 flagged the stuck-0 as a secondary concern ("possibly a midpoint-fallback masking favourable bid moves"). This memo picks up from there.

Phase 1 scope: investigation only. **No `src/` changes.**

---

## 1. Grep trail — peak read/write path

All call sites for `peak_unrealised_pnl_gbp` (DB column) and `peak_pnl_gbp` (runtime state):

**Runtime state (`CandidateRuntimeState.peak_pnl_gbp`)**
- [src/engine/monitor.py:154](src/engine/monitor.py:154) — declared `= 0.0`.
- [src/engine/monitor.py:602](src/engine/monitor.py:602) — **initialised to `0.0` at fill time** (post-`place_open_position` block).
- [src/engine/monitor.py:820-827](src/engine/monitor.py:820) — **the sole per-tick update**, inside `_handle_open_position_tick`:

```python
if snapshot and snapshot.get("last_traded") is not None and state.fill_price is not None:
    live_pnl = unrealised_pnl_gbp(
        plan.direction,
        snapshot["last_traded"],          # ← midpoint-ish, see §2
        state.fill_price,
        state.stake_gbp_per_pt or 0.0,
    )
    state.peak_pnl_gbp = max(state.peak_pnl_gbp, live_pnl)   # monotonic, floor=0.0
```

- [src/engine/monitor.py:851, 1068, 1269, 1308, 1329, 1352](src/engine/monitor.py:851) — read-only (event emission / snapshot write / STOP_MOVED payloads / session-end). No further writes.
- [src/engine/trail_manager.py:165, 226, 357, 370](src/engine/trail_manager.py:357) — `PositionState.peak_pnl_gbp` in the pure evaluator. `evaluate_exit` also does `max(position.peak_pnl_gbp, pnl)` on the snapshot's `last_traded`, but returns it to the caller; the *persisted* peak on `state` is the one written at [monitor.py:827](src/engine/monitor.py:827).
- [src/engine/resume.py](src/engine/resume.py) — session-resume restores `peak_pnl_gbp` from the latest `STOP_MOVED` event; not relevant for in-session behaviour.

**DB column (`candidate_snapshots.peak_unrealised_pnl_gbp`)**
- [src/logging_mod/db.py:285](src/logging_mod/db.py:285) — schema.
- [src/logging_mod/session_writer.py:527, 567](src/logging_mod/session_writer.py:527) — `INSERT` column list (positional write).
- [src/engine/monitor.py:1068](src/engine/monitor.py:1068) — where the runtime `state.peak_pnl_gbp` is copied into the snapshot row: `peak_unrealised_pnl_gbp=outcome.peak_pnl_gbp`.
- [src/models/candidate_snapshot.py:117](src/models/candidate_snapshot.py:117) — pydantic field.

**Conclusion:** exactly one write path. The compute reads `snapshot["last_traded"]`, applies `unrealised_pnl_gbp(direction, last_traded, fill_price, stake)`, then `max(state.peak, live_pnl)` with floor `0.0` from fill-time init.

---

## 2. Snapshot schema — bid/ask are present

```
CREATE TABLE candidate_snapshots (
    ...
    last_price   REAL,
    bid          REAL,
    ask          REAL,
    ...
    peak_unrealised_pnl_gbp  REAL,
    ...
);
```

**Hypothesis 2 (schema lacks bid/ask) is falsified.** Both quote sides are persisted on every snapshot (see §4 data). The pydantic model at [src/models/candidate_snapshot.py](src/models/candidate_snapshot.py) and the writer at [src/logging_mod/session_writer.py](src/logging_mod/session_writer.py) both carry `bid` and `ask` end-to-end.

What `last_traded` actually is, per [src/data/market_data.py:560-568](src/data/market_data.py:560):

```python
last_traded = None
for key in ("lastTraded", "lastTradedPrice"):
    if key in snap:
        last_traded = _f(snap.get(key))
        break
if last_traded is None and bid is not None and ask is not None:
    last_traded = (bid + ask) / 2.0      # ← midpoint fallback
```

Cross-checked against the DB: for JNJ at 13:14:54, `last_price=234.25`, `bid=233.90`, `ask=234.60` → `(bid+ask)/2 = 234.25` exactly. **IG is not populating `lastTraded` on `SD.D.JNJ.DAILY.IP` — the runtime is using the midpoint fallback throughout.** Same pattern on ARM (e.g. 13:20:54: last=167.32, (166.6+168.04)/2=167.32).

So "side-appropriate" vs "midpoint" is a live design question — but the schema is ready for either; the bottleneck is the *consumer* (`monitor.py:820-827`, `trail_manager.py:303`) which reads `last_traded` only.

---

## 3. DB forensics — ARM peak trajectory

307 ARM snapshots across 2026-04-20 after fill at 13:14:24 (stake 7.12 £/pt, fill 167.1). Selected rows showing peak ratcheting (peak = peak_unrealised_pnl_gbp, pnl = unrealised_pnl_gbp at that tick):

| ts_utc (UTC) | last_price | bid | ask | peak | pnl | mins |
|---|---|---|---|---|---|---|
| 13:14:54 | 167.05 | 166.49 | 167.61 | **0.0** | -0.36 | 0 |
| 13:15:54 | 167.105 | 166.60 | 167.61 | **0.036** | +0.036 | 1 |
| 13:20:54 | 167.32 | 166.60 | 168.04 | **1.566** | 1.566 | 6 |
| 13:21:54 | 167.355 | 166.60 | 168.11 | **1.816** | 1.816 | 7 |
| 13:24:24 | 167.495 | 166.89 | 168.10 | **2.812** | 2.812 | 10 |
| 13:30:24 | 169.13 | 168.67 | 169.59 | **14.454** | 14.454 | 16 |
| 13:44:24 | 165.70 | 165.46 | 165.94 | 14.454 | -9.97 | 30 (drawdown) |
| 15:22:25 | 169.24 | 169.02 | 169.46 | **15.237** | 15.237 | 128 |
| 15:25:55 | 170.47 | 170.30 | 170.64 | **23.994** | 23.994 | 131 |
| 15:26:25 | 170.785 | 170.62 | 170.95 | **26.237** | 26.237 | 132 (arm threshold crossed) |
| 15:26:55 | 171.08 | 170.89 | 171.27 | **28.338** | 28.338 | 132 |
| 15:39:55 | 171.375 | 171.18 | 171.57 | **30.438** | 30.438 | 145 |
| 15:40:55 | 171.62 | 171.41 | 171.83 | **32.182** | 32.182 | 146 |
| 15:43:25 | 171.85 | 171.64 | 172.06 | **33.820** | 33.820 | 149 |
| 15:47:25 | 171.645 | 171.42 | 171.87 | 33.820 | 32.360 | 153 |

Aggregates (all ARM rows): `MAX(peak) = 33.82`, `MAX(last_price) = 171.85`, `MAX(bid) = 171.64`, `MAX(ask) = 172.06`.

Observations on ARM:

1. **Peak ratchets monotonically.** Each new `last_traded` high is promptly promoted to peak. No missed updates.
2. **Peak matches midpoint arithmetic exactly.** At the 33.82 high: `(171.85 − 167.1) × 7.12 = 33.82`. ✓
3. **Peak survives drawdown without decay.** Between 13:30 and 15:22 the position went underwater to pnl=-20.08 (13:46:24). Peak held 14.454 across every one of those adverse snapshots — exactly as intended.
4. **Trail arm threshold (£25) was crossed** at 15:25:55 → 15:26:25 (peak jumped from 23.99 to 26.24). The trail ladder had material to work with for ~20 minutes before the position was manually closed.
5. **No monitor-side TERMINAL event for ARM** was written on 2026-04-20 — the `candidate_events` log ends at the fill/order events; the manual IG-side close at +£55.56 happened outside the monitor's view. This is an observability gap (see §8).

JNJ trajectory (summarised; full table in `PRICE_FEED_DIVERGENCE_INVESTIGATION.md` §3):

- 37 snapshots, 18 minutes.
- `MIN(last_price) = 233.94`, `MIN(bid) = 233.45`, `MIN(ask) = 234.43`.
- Fill 233.9 SHORT. Arithmetic via `last_traded`: best possible pnl = (233.9 − 233.94) × 7.12 = **−£0.28** (adverse). So the midpoint-based peak correctly stays at 0.0 given the data the monitor had.
- Via bid (generous side for SHORT): best possible pnl = (233.9 − 233.45) × 7.12 = **+£3.20**. Non-zero but nowhere near the £25 arm threshold.
- Via ask (the side-appropriate mark for a SHORT, which is what you actually pay to close): best possible pnl = (233.9 − 234.43) × 7.12 = **−£3.77** (more adverse than midpoint). Peak still 0.0.

---

## 4. IG response-shape evidence

Re-using the findings from `PRICE_FEED_DIVERGENCE_INVESTIGATION.md` §1 and §4 — the IG REST `/markets/{epic}` endpoint returned `bid`, `offer` (ask), and either `lastTraded` or nothing on each poll. `scalingFactor` was absent on `SD.D.*.DAILY.IP` equity-spread-bet epics and the equity-convention fallback (÷100) applied correctly. `bid` and `ask` both flowed through [src/data/market_data.py:580-582](src/data/market_data.py:580) into the persisted snapshot.

The monitor **receives** side-appropriate prices. It just doesn't **consume** them for P&L compute — it consumes `last_traded`, which is the midpoint whenever `lastTraded` is missing (JNJ and ARM on 2026-04-20 both).

---

## 5. Arithmetic reconstruction

**JNJ (SHORT @ 233.9, 7.12 £/pt):**

| Mark price source | Best value over window | Implied peak P&L |
|---|---|---|
| `last_traded` (midpoint) | 233.94 | **−£0.28** → clamped to £0.00 ✓ matches DB |
| `bid` | 233.45 | +£3.20 |
| `ask` (correct close side for SHORT) | 234.43 | −£3.77 → £0.00 |
| Live IG dealing engine (Lightstreamer) | ~232.33 | **+£37.68** (Mark's observation) |

**The monitor's persisted peak of 0.0 is arithmetically correct given the feed data it received.** The offset between monitor's feed (~233.9 floor) and IG's dealing-engine feed (~232.33 floor) is **the entire story** — the peak calc is doing its job.

**ARM (LONG @ 167.1, 7.12 £/pt):**

| Mark price source | Best value over window | Implied peak P&L |
|---|---|---|
| `last_traded` (midpoint) | 171.85 | **£33.82** ✓ matches DB |
| `bid` (correct close side for LONG) | 171.64 | £32.32 (smaller than midpoint) |
| `ask` | 172.06 | £35.31 (larger than midpoint) |

ARM shows the peak tracker works end-to-end: compute is right, write path is right, runs on the filled-position path, ratchets monotonically, survives drawdowns. The gap between persisted peak (£33.82) and realised-at-close (£55.56) is **not a peak-tracker bug** — it's that the monitor's last snapshot (15:47:25) caught £33.82, and the position subsequently rallied further; Mark closed manually on IG without a monitor-mediated TERMINAL event, so no further peak updates were logged.

---

## 6. Hypothesis match table

| # | Hypothesis | Match | Evidence |
|---|---|---|---|
| 1 | **Midpoint-fallback masks side-appropriate favourable moves** | **Partial / misdirected** | Monitor does read `last_traded` (midpoint fallback) at [monitor.py:823](src/engine/monitor.py:823). But for JNJ, neither midpoint nor bid nor ask crossed fill in favour — peak-0 was correct for every mark-source. For ARM, midpoint *overstated* peak vs the side-appropriate bid (£33.82 vs £32.32). Hypothesis 1 has a real but small effect on SHORTs where the bid briefly dips past fill while midpoint doesn't — none of Day-1's data actually shows that, so the hypothesis is *plausible in general* but not *the cause of what we saw*. |
| 2 | Snapshot schema has no `bid`/`ask` columns | **No** | Schema at [db.py:285](src/logging_mod/db.py:285) and DB forensics (§3) both confirm `bid` and `ask` are present and populated on every row. |
| 3 | `max(0, ...)` clamp bug in peak update | **No** | [monitor.py:827](src/engine/monitor.py:827) is `max(state.peak_pnl_gbp, live_pnl)` starting from `0.0` — a correct floor for "peak unrealised gain" (not "least-negative P&L"). ARM's peak moved off 0.0 the first tick `live_pnl` went positive (13:15:54 → £0.036). Clamp is fine. |
| 4 | Peak update writes to wrong column | **No** | Only one write path ([monitor.py:1068](src/engine/monitor.py:1068)) maps `state.peak_pnl_gbp → snapshot.peak_unrealised_pnl_gbp`. ARM's column updates correctly. |
| 5 | Peak update doesn't run on filled positions | **No** | The update at [monitor.py:820-827](src/engine/monitor.py:820) sits inside `_handle_open_position_tick`, which is the post-fill path. ARM confirms it runs on every post-fill tick (307 rows, all with peak populated). |

**Best match: no distinct bug. JNJ's stuck-0 is arithmetically correct given the (stale-REST) feed data the monitor received; ARM's peak tracker is working as designed.**

---

## 7. Recommended Phase 2 fix shape

**Recommended: P-5 (no separate bug — resolved by S-3 Lightstreamer migration, task #24).**

### Rationale

JNJ's stuck-0 is **completely accounted for** by feed staleness:

- Monitor's midpoint range over the 18-minute window: 233.94–234.96.
- Fill 233.9 SHORT. For peak to be non-zero, midpoint needed to drop below 233.9. It never did (nor did the bid drop below 233.45, which is still ≤£3.20 of peak P&L — well below the £25 arm threshold).
- Meanwhile the dealing engine was at ~232.33 (+£37.68). That is not a "midpoint vs bid" gap (which is bounded by the bid-ask spread, ~0.7 scan-units on JNJ); it is a **whole-feed staleness gap** (~1.6 scan-units of drift on the midpoint vs the streaming feed). Switching from midpoint to bid on the REST feed would still leave us ~1.0 scan-unit short of reality.

ARM's peak tracker works correctly against whatever price feed it's given — ratchets, holds through drawdowns, hits arithmetic exactness. It will start producing correct peaks at the £5-£55 scale the moment the feed underneath it is accurate.

### Closing task #25

Task #25 ("peak_unrealised_pnl_gbp stuck at 0.0") is **not a separate bug from task #24 (S-3 Lightstreamer migration).** When S-3 ships and `candidate_snapshots` carries streaming bid/ask/last-traded, peak will move off 0.0 on winning trades without any change to [monitor.py:820-827](src/engine/monitor.py:820) or [trail_manager.py](src/engine/trail_manager.py).

### Not P-1, with caveat

Fix shape P-1 (side-appropriate quote selection) is a **real, worthwhile improvement** — but it's small, orthogonal, and bundles naturally with S-3's schema extension for `feed_source` rather than being the primary fix. Specifically:

- For SHORT: using `ask` (the side you actually pay to close a SHORT) rather than midpoint gives a *more conservative* peak. Under-counts favourable moves. This is the opposite direction from what the spec's hypothesis-1 framing assumed ("bid for SHORT"); the conservative-mark convention and the leading-edge-of-favourable-moves convention point to different quote sides. Worth a separate design discussion before implementing.
- For LONG: using `bid` (conservative close side) *further reduces* peak below midpoint (ARM: £32.32 vs £33.82). Using `ask` (leading-edge) inflates it (£35.31 vs £33.82). Neither is unambiguously "the right answer"; it depends on whether we want peak to mean "what I could realise now" or "the best thing the market showed me".

Recommend **not** landing P-1 inside the Phase 2 peak-fix because there isn't one; revisit side-appropriateness as a sub-task of S-3 where the bundled schema/compute work can be tested against real streaming quotes.

---

## 8. Secondary concerns flagged

- **ARM closed outside the monitor** — no `TERMINAL` event for ARM in `candidate_events` on 2026-04-20; the IG-side manual close at +£55.56 left no monitor-mediated trail. The trail ladder did not fire the close. This is both: (a) why peak caps at £33.82 in the DB (snapshots stopped at 15:47:25, position closed manually later at a higher price); and (b) a real observability gap. Flag as follow-up — monitor should reconcile position state against IG at each tick and emit a synthetic `TERMINAL` event when it discovers a position was closed externally. Tie-in with the existing "monitor tick keeps ticking filled positions until close event fires" work ([3c2b62e](../3c2b62e)).

- **`lastTraded` is absent on all DAILY.IP equity-spread-bet epics.** The midpoint fallback at [market_data.py:567-568](src/data/market_data.py:567) fires on 100% of JNJ and ARM snapshots. Not a bug today, but means the monitor is implicitly midpoint-only for this epic class. When S-3 lands, verify the streaming payload populates a real `lastTraded` / sequence of trades so we can distinguish a true print from a midpoint.

- **Side-appropriate mark selection is a real design question, not a ruled-out hypothesis.** See §7 caveat. Worth a short design note before Phase 2 chooses bid vs ask vs midpoint for LONG and SHORT. Bundle with S-3.

- **Peak-update is a fragile one-liner at [monitor.py:820-827](src/engine/monitor.py:820).** Correct today, but there is no unit test that asserts peak ratchets monotonically under a midpoint-only feed, survives drawdown without decay, or obeys the 0.0 floor at fill. An adversarial fixture (LONG that fills at 100, drops to 95, rallies to 110, drops to 102, rallies to 108) with explicit peak assertions would close the regression loop before S-3 and catch any side-selection re-wiring from silently under/over-counting.

- **No test covers the JNJ-shaped degenerate case** (all-adverse window, peak remains 0.0 throughout, no spurious arm/trail-move events). Worth a dedicated test once S-3 gates the input feed.

---

## Phase 2 follow-up for Mark

- Confirm P-5 is the right call. If so, close task #25 with reasoning, and note in memory that peak-stuck-0 was a feed-staleness symptom, not a distinct monitor bug.
- Consider bundling side-appropriate mark selection into the S-3 Lightstreamer spec (task #24) as a sub-task, not a standalone Phase 2.
- Separately, the observability gap — ARM closed without a monitor-side TERMINAL — is worth its own spec; it's orthogonal to both task #24 and task #25 but was surfaced here.
