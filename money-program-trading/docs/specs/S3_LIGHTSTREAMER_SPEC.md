# S-3 — Lightstreamer Price Feed Migration

**Status:** draft (written 2026-04-23 after DEMO 2026-04-23 surfaced finding #1).
**Target repo:** `money-program-trading` (entry-rules).
**Owner:** Mark (solo dev).
**Related:** `project_monitor_price_feed_divergence` memory; S-4 divergence gate already shipped at `9b1a999`.

---

## 1. Context — why this exists

Every DEMO session where `MarketData.get_market_snapshot()` is called per tick has a non-trivial chance of serving **stale cached quotes** from IG's `/markets/{epic}` REST endpoint. On 2026-04-23 this cost us a real **£50 of realised P&L** when `KA.D.BA.DAILY.IP` returned `bid=2053.4` (a number ~10× off reality) for the entire 11-minute session — BA's true price at that moment was $230–$235 per the IG UI chart, well inside our 229–233 LONG trigger zone.

The S-4 divergence gate (`9b1a999`) was meant to catch this class of failure by comparing `monitor.last_traded` against `broker.get_deal_price`. It didn't fire because **both** reads go through the same REST cache — when the cache is stale, both agree, and S-4 reports `total_skips=0`.

The only structural fix is to stop polling REST for per-tick prices and instead consume IG's **Lightstreamer streaming feed** — a push-based real-time channel that bypasses the REST cache entirely.

---

## 2. Non-goals — what this spec does NOT touch

- **Historical bars** (`get_daily_bars`, `get_intraday_bars`, `_fetch_bars`, bar cache) — these fetch once or on session startup; staleness isn't a factor.
- **Epic resolution** (`resolve_epic`, `_search_market`, `_load_epic_cache`) — one-shot lookup, whole-token matcher already fixed at `4037257`.
- **Instrument metadata** (`get_scaling_factor`, `to_ig_units`) — fetched once per epic, cached per-session. Not a per-tick call.
- **Account / trade-confirm streams** — IG Lightstreamer also offers `ACCOUNT:{accid}` and `TRADE:{accid}` subscriptions. Out of scope; separate spec when needed.
- **Broker-side order placement** (`broker.place_open_position`, `modify_stop`, `close_position`) — still REST, stays REST.

Anything not listed above is in-scope only if the migration forces a change.

---

## 3. Surface area — what changes

Inside `src/data/market_data.py`:

- **Replaced with streaming:**
  - `get_market_snapshot(epic) -> dict` — currently line 438–593.
  - `get_current_price(epic) -> dict` — currently line 404–427.
  - `get_spread_pct(epic) -> float | None` — derived from `get_current_price`, moves with it.

- **Unchanged:** everything else (resolve_epic, bars, cache methods, scaling helpers).

Call sites for those three methods (grep confirms these are the only ones that matter for this refactor):

```
src/engine/monitor.py       — per-tick snapshot reads
src/engine/broker.py         — pre-order sanity price reads
src/engine/resume.py         — rehydrate path one-time reads
tools/build_shakedown_scan.py — one-shot builder
```

All four continue to call `MarketData.get_market_snapshot(epic)` unchanged at the boundary. **The method's return shape must be identical post-migration.** What changes is the internal source of the data.

---

## 4. Architecture — the PriceFeed abstraction

Introduce a new module `src/data/price_feed.py` with an abstract interface and two implementations:

```python
# src/data/price_feed.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class Tick:
    epic: str
    bid: float
    ask: float
    last_traded: float | None
    updated_at_utc: datetime
    market_state: str  # TRADEABLE / EDITS_ONLY / OFFLINE / etc.

class PriceFeed(ABC):
    @abstractmethod
    def subscribe(self, epic: str) -> None: ...
    @abstractmethod
    def unsubscribe(self, epic: str) -> None: ...
    @abstractmethod
    def latest(self, epic: str, max_age_seconds: float = 10.0) -> Tick:
        """Return the most recent tick for epic. Raise StalePriceError
        if the cached tick is older than max_age_seconds — forces
        the caller to handle staleness explicitly rather than trade
        on silent old data."""
    @abstractmethod
    def start(self) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...
```

Two implementations:

### 4.1 `RestPriceFeed` — today's behaviour, wrapped

Polls `/markets/{epic}` via the existing `IGService.fetch_market_by_epic`. This is what runs today inside `get_market_snapshot`; we're just packaging it behind the same interface so the LS variant is a drop-in. `latest()` calls REST synchronously, stamps `updated_at_utc = datetime.utcnow()` on return.

**Preserves current behaviour verbatim. Phase 1 ships this with zero behavioural change.**

### 4.2 `LightstreamerPriceFeed` — the replacement

Wraps `trading_ig.stream.IGStreamService`. On `start()`:

1. Reads `CST` and `X-SECURITY-TOKEN` from the existing `IGSession.service.session.headers` (do NOT call `create_session` again — that would trigger a second REST auth and trip DEMO rate-limits, exactly the bug at `feedback_ig_switch_account_race`). Construct `IGStreamService` with these tokens directly.
2. Reads `lightstreamerEndpoint` — one-time REST call `GET /session?fetchSessionTokens=true` returns it in the response body.
3. Establishes the LS client connection.
4. Registers a listener class that writes each tick into an in-memory `dict[epic, Tick]`.

On `subscribe(epic)`:

1. Build a `Subscription(mode="MERGE", items=[f"MARKET:{epic}"], fields=["BID", "OFFER", "HIGH", "LOW", "UPDATE_TIME", "MARKET_STATE", "CHANGE"])`.
2. Attach a `SubscriptionListener` that updates the in-memory tick on each `onItemUpdate` event.
3. Call `IGStreamService.subscribe(sub)`.

On `latest(epic, max_age_seconds)`:

1. Look up the last tick in the in-memory dict.
2. Compute `age = now() - tick.updated_at_utc`.
3. If `age > max_age_seconds`, **raise `StalePriceError(epic, age)`**. The monitor's caller decides how to handle — today's default is to skip the tick evaluation and log a `PRICE_STALE` event (new event type, see §7).
4. Otherwise return the `Tick`.

On `stop()`: unsubscribe all, disconnect LS client. Idempotent.

### 4.3 `MarketData.get_market_snapshot` — re-implementation

```python
def get_market_snapshot(self, epic: str) -> dict:
    tick = self._price_feed.latest(epic)  # may raise StalePriceError
    scaling = self.get_scaling_factor(epic)
    return {
        "bid": tick.bid / scaling,
        "ask": tick.ask / scaling,
        "last_traded": (tick.last_traded or (tick.bid + tick.ask) / 2) / scaling,
        "updated_at_utc": tick.updated_at_utc,
        "market_state": tick.market_state,
    }
```

Same return shape as today. Scaling factor logic unchanged (the `_looks_like_minor_units` heuristic + `feedback_ig_scaling_factor_daily_ip` fallback still apply, because LS raw values are in the same minor-unit convention as REST).

---

## 5. Feature flag — safe rollout

New env var `PRICE_FEED_MODE`, read in `MarketData.__init__`:

| Value | Behaviour |
|---|---|
| `rest` (default) | Use `RestPriceFeed`. Identical to today. Zero-risk fallback. |
| `lightstreamer` | Use `LightstreamerPriceFeed`. Full cutover. |
| `parallel` | Run both feeds. Every `latest(epic)` call fetches from both, compares bid/ask, logs divergence, returns the Lightstreamer value. Debugging mode — not for production. |

Default **stays on `rest`** through Phase 3. `lightstreamer` only becomes default in Phase 4 after PARALLEL validation.

Rollback is always one env-var flip + session restart. No schema migrations, no persisted state changes.

---

## 6. Phases

### Phase 1 — Refactor, no behavioural change (~1 day)

- Create `src/data/price_feed.py` with interface + `RestPriceFeed`.
- Refactor `MarketData` to take `PriceFeed` in constructor (default: `RestPriceFeed(session=self._session)`).
- Refactor `get_market_snapshot` / `get_current_price` / `get_spread_pct` to read from `self._price_feed.latest(epic)`.
- Move `_looks_like_equity_spreadbet` and `_looks_like_minor_units` helpers into `RestPriceFeed` (or a shared `scaling.py`).
- **All 410 existing tests must pass.** No new tests in this phase.
- `session_init.py` constructs `MarketData` exactly as today; no env-var read yet.

**Gate to Phase 2:** green tests, manual DEMO session run produces identical behaviour to pre-refactor baseline.

### Phase 2 — Lightstreamer implementation (~3 days)

- Implement `LightstreamerPriceFeed` per §4.2.
- Add `PRICE_FEED_MODE` env var to `Settings`.
- `session_init.py` constructs the selected `PriceFeed` and injects into `MarketData`.
- New test file `tests/test_lightstreamer_feed.py`:
  - Mock `LightstreamerClient` — feed synthetic `onItemUpdate` events, assert `latest()` returns them correctly.
  - Assert `StalePriceError` raises when no update for > max_age.
  - Assert `start` → `subscribe` → tick arrives → `unsubscribe` → `stop` lifecycle.
  - Assert `connect` failures propagate cleanly (no sys.exit, no silent swallow).
- New test file `tests/test_price_feed_abstraction.py`:
  - Assert `RestPriceFeed` and `LightstreamerPriceFeed` satisfy the same `PriceFeed` contract (parametrized tests).

**Gate to Phase 3:** green unit tests + one successful end-to-end DEMO dry-run (`--dry-run`) with `PRICE_FEED_MODE=lightstreamer` that subscribes, receives ticks, unsubscribes, disconnects cleanly — validated against an empty shortlist so no orders are placed.

### Phase 3 — PARALLEL validation (~3+ DEMO sessions)

- `PRICE_FEED_MODE=parallel` runs both feeds simultaneously.
- Each tick logs a `PRICE_DIVERGENCE` event if |rest.mid − ls.mid| / rest.mid > 30 bps (same threshold as today's S-4).
- Run at least 3 full DEMO sessions in parallel mode. The bar to advance:
  - Lightstreamer connect + subscribe + unsubscribe + disconnect succeed every time.
  - Tick arrival rate ≥ 1/sec per active epic during market hours.
  - **Zero tick gaps > 10 seconds** (the `max_age_seconds` default) during regular US or UK hours. Out-of-hours can obviously go quiet.
  - Divergence analysis: if REST shows stuck quotes while Lightstreamer shows motion, that's the bug; log it. If both agree, good.

**Gate to Phase 4:** 3 parallel-mode sessions with zero connection failures and documented divergence patterns (we'd expect to *see* REST staleness as the divergence pattern — confirming the theory).

### Phase 4 — Cutover (~1 session)

- Flip `PRICE_FEED_MODE=lightstreamer` in `.env`.
- Run one full DEMO session in LS mode.
- Acceptance:
  - `divergence-summary` line shows `total_skips=0` (because LS can't be stale by construction, and S-4's comparison becomes LS vs broker-REST — which should diverge only when REST itself is stale, which is fine and logged).
  - All triggers fire based on real prices. Specifically: if the same scan that ran on 2026-04-23 (AVGO / BA / BRK-B bypass) re-runs under LS, **BA fires and fills** (proving the bug is fixed).
- Keep `RestPriceFeed` code in-tree as fallback for one more month.

### Phase 5 (future, out of current spec) — Deprecate REST path

- After 10+ stable LS sessions, delete `RestPriceFeed` and the env var. LS becomes the only price source.

---

## 7. New observability events + staleness escalation

### 7.1 Event types

Three new entries in `EventType` (add to `log_enums.py`, extend the `candidate_event.py` discriminated union):

- **`PRICE_STALE`** — emitted when `PriceFeed.latest(epic)` raises `StalePriceError` at the default threshold (10s). Payload: `{epic, age_seconds, last_tick_utc}`. Monitor caller skips the tick evaluation (does not fire triggers on stale data). Short glitches are expected; 10–60s staleness is tolerated with no open-position action.
- **`PRICE_FEED_DEGRADED`** — emitted when staleness crosses the **hard threshold of 60 seconds**. See §7.2 for the defensive-close contract.
- **`PRICE_DIVERGENCE`** — emitted in PARALLEL mode when REST and LS disagree beyond 30 bps. Payload: `{epic, rest_mid, ls_mid, diff_bps, rest_updated_at, ls_updated_at}`. Non-fatal; logged for post-session analysis.

The existing S-4 divergence machinery (`monitor.py:_evaluate_with_divergence_check`) is **not** removed — it becomes redundant in LS-only mode but stays as belt-and-braces for the LS-vs-broker-REST comparison at the broker call boundary.

### 7.2 Staleness escalation ladder

Two thresholds, defined in `Settings`:

- `PRICE_FEED_STALE_SECONDS` (default `10`) — tick-level staleness; raises `StalePriceError`.
- `PRICE_FEED_DEGRADED_SECONDS` (default `60`) — feed-level degradation; triggers defensive action.

Behaviour by staleness age per epic:

| Age | Open position on epic? | Action |
|---|---|---|
| < 10s | any | normal; trigger/exit eval runs |
| 10–60s | no | emit `PRICE_STALE` each tick; skip trigger arming/firing; keep monitor running |
| 10–60s | yes | emit `PRICE_STALE`; freeze trail/stop-move decisions (do not move a stop based on stale data); keep position open; IG-side broker-enforced limit + stop still protect at the server side |
| ≥ 60s | no | emit `PRICE_FEED_DEGRADED`; halt trigger arming/firing on that epic until feed recovers; log warning in session summary |
| ≥ 60s | yes | emit `PRICE_FEED_DEGRADED`; **force-close the position via `broker.close_position()` (REST — independent of the stale LS feed); emit `POSITION_CLOSED_DEGRADED_FEED` terminal event; do not re-enter while feed remains degraded** |

The broker REST path is used for the defensive close because:
1. Order-placement REST calls are separate from the `/markets/{epic}` snapshot cache that went stale.
2. If broker REST is also broken, the IG-attached stop_level and limit_level at server side still fire independently.
3. Failing that, IG's own platform-level risk management is the final backstop.

This implements the standing principle: **"if we can't see prices, we don't hold positions."** One-minute threshold matches Mark's stated tolerance on 2026-04-23.

Reconnection: LS client auto-reconnects in the background. On first fresh tick after recovery, a `PRICE_FEED_RECOVERED` event fires and normal trading resumes on un-degraded epics. Degraded-then-closed positions stay closed for the session — no auto-re-entry.

---

## 8. Contracts that must not break

1. **`MarketData.get_market_snapshot(epic) -> dict` return shape is stable.** Keys: `bid`, `ask`, `last_traded`, `updated_at_utc`, `market_state`. Types unchanged. Scaling factor applied identically.
2. **`candidate_events` schema stable.** Only additions (new `EventType` values). No column changes.
3. **`Settings` additions are optional with safe defaults.** Missing `PRICE_FEED_MODE` → defaults to `rest` → identical to today.
4. **REST path remains fully functional through Phases 1–4.** Rollback = one env-var flip.

---

## 9. Acceptance criteria — complete when all of these hold

1. `pytest -q` passes with baseline ≥ 410 tests plus new LS + PriceFeed tests (expect 420+).
2. `PRICE_FEED_MODE=rest` behaves identically to pre-spec master (manual DEMO comparison).
3. `PRICE_FEED_MODE=parallel` produces divergence logs for at least one session showing REST-stale vs LS-fresh disagreement — direct evidence the migration actually fixes something.
4. `PRICE_FEED_MODE=lightstreamer` completes a full US session with:
   - zero `PRICE_FEED_DEGRADED` events during US regular hours (short <10s ticks can glitch; but no sustained 60s+ gaps),
   - at least one `TRIGGER_FIRED` event on a candidate that had the same trigger-zone geometry as BA on 2026-04-23 (a regression test for the specific bug).
   - a simulated 60s+ feed outage test (inject a mocked disconnect during a dry-run with an open dummy position) proves the defensive-close path fires correctly and emits `POSITION_CLOSED_DEGRADED_FEED`.
5. Rollback tested: after a successful LS session, flipping back to `rest` on the next session produces a clean REST-based session with no state carryover.

---

## 10. Known risks & mitigations

- **IG DEMO rate-limit on double-auth.** Mitigation: read CST/XST tokens from the existing `IGSession` instead of calling `IGStreamService.create_session` fresh. If that proves fiddly, fall back to a single startup auth sequence that pre-reads the `lightstreamerEndpoint` during `IGSession.connect()` and passes it through — costs one extra call at session start, not per reconnect.
- **LS client disconnects mid-session.** Lightstreamer clients auto-reconnect; we need to handle the reconnect window gracefully. During reconnect, `latest()` will raise `StalePriceError` by design — monitor skips ticks, no trigger fires, no fill. Acceptable degradation.
- **`lightstreamer-client` Python package version pinning.** `trading_ig` imports from `lightstreamer.client`. Pin the version explicitly in `pyproject.toml` to avoid silent breakage — mark `trading-ig>=X.Y` and `lightstreamer-client-lib>=Z.W` with verified compatible versions during Phase 2.
- **Event loop integration.** LS listener callbacks fire on LS's own thread. The monitor loop is synchronous. Thread-safe in-memory tick dict (using `threading.Lock` or a lock-free `dict` + `threading.Event`). Decide at Phase 2; default to a `Lock`.
- **DEMO-only first.** Do not flip LIVE to `lightstreamer` until 10+ clean DEMO sessions. LIVE cutover is a separate decision beyond the 20-day floor anyway.

---

## 11. Sequencing within the broader roadmap

- **Do NOT tangle this with sizing-ladder redesign.** Sizing is finding #2 — a smaller, independent PR. That can land in parallel (different files, no overlap).
- **DEMO attendance during LS development.** Sessions in Phases 1–3 (pre-cutover) are diagnostic-only — they exercise the pipeline but run on known-broken REST or debug PARALLEL mode. **They do not count toward the 20-day floor.**
- **The 20-day DEMO floor count resets on Phase 4 cutover.** Today's attendance count (2/20 as of 2026-04-23) is **invalidated** the day LS becomes the default price source. The floor requires 20 clean sessions on the final infrastructure — mixing pre-LS and post-LS days dilutes the signal.
- **What "clean session" means post-cutover:** zero `PRICE_FEED_DEGRADED` events; all triggers fired on live LS data; all exits logged cleanly (trail/target/stop/timestop/HARD_CLOSE); any manual IG close reconciled via the future `reconcile_with_ig` helper.

---

## 12. Open questions to resolve during Phase 1

1. **Does `IGStreamService` need a separate `IGService` instance, or can it share the one `IGSession` already holds?** The library source (`stream.py:23`) calls `self.ig_service.create_session(...)`. Need to test in Phase 1 whether the existing authenticated session's service is reusable.
2. **UPDATE_TIME field format from Lightstreamer.** IG docs specify it as a string like `"HH:MM:SS"` without date. Need to combine with session date for UTC stamping. Worth one experimental session to confirm.
3. **Can we receive consolidated quotes across a basket subscription rather than one subscription per epic?** Efficiency — three epics today becomes 30 on wider universe. One subscription per epic = 30 active subscriptions; one aggregated = cheaper. Research during Phase 2.
