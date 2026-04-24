# Rule 9 — Buyable Gap Up (BGU) Protocol

**Status:** draft (2026-04-24).
**Source of truth:** Entry_Refinement_Masterclass_v1.docx §3.3 Rule 9 (Raschke / Morales / Gil).
**Code target:** `src/engine/monitor.py::classify_tick` + `CandidateRuntimeState`.
**Motivating failure:** TMUS 2026-04-24 would have fired LONG at 09:30:04 ET at the opening top under current
`classify_tick`, losing ~£96 at clamped IG stake. BA on 2026-04-23 similarly suffered from the lack of
opening-range discipline.

---

## 1. What the Masterclass says (verbatim paraphrase)

> A Buyable Gap Up occurs when a stock gaps above a proper base on a fundamental catalyst with volume ≥ 2x the 50-day average.
>
> 1. Classify the gap as early-stage or late-stage. **Only buy early-stage gaps.**
> 2. **Wait for the first 15 minutes** of trading to establish an opening range (OR).
> 3. **Entry option A:** buy-stop above the OR high.
> 4. **Entry option B:** limit order on first intraday pullback to max(gap-up-day's open, VWAP).
> 5. Stop-loss: 3–4% below the low of the gap-up day.
> 6. **Late-stage gaps: Do not buy.**

---

## 2. Non-goals (this PR)

To keep scope tight and shippable in one session:

- **Option B (VWAP pullback):** requires a running per-epic VWAP calculation fed by every tick. Stateful + needs decision on whether to include pre-market. Out of scope — deferred.
- **Late-stage gap classification (§2.6 of Masterclass):** requires base-stage data from the scanner that isn't reliably surfaced today. Out of scope. The existing `entry_classifier.py` has partial base-stage logic that may route late-stage gaps to R04, but verifying/extending that is a separate PR.
- **Stop-loss adjustment (3–4% below gap-up day low):** keeps using the scan-emitted `stop_price`. Future refinement.
- **SHORT-side Rule 10 handling:** mirror logic for gap-downs. Out of scope; deferred.
- **Volume ≥ 2x 50d average check:** a gap without 2x volume is not technically a BGU per Masterclass — it's just a gap. We currently detect any gap into the trigger zone. Volume confirmation belongs to Rule 5 (separate missing feature).

What ships in this PR: **Rule 9A for LONGs only**, and only at the `classify_tick` level. No scanner changes, no
additional REST calls.

---

## 3. What triggers BGU detection

A LONG candidate is flagged as a gap-up when **the first tick we observe during the session satisfies
`last_traded >= plan.trigger_low`**. The scan's trigger zone encodes the pivot region — if price is already at
or above the pivot at session open, the stock has gapped into the zone. Equivalent to the Masterclass "gapped
above a proper base" definition.

Detection happens once per session per candidate, on the first non-stale snapshot. Stored on
`CandidateRuntimeState.gap_up_detected`. No retroactive re-detection — once set, it stays set for the session.

This implicitly excludes candidates whose first tick is BELOW the trigger zone (normal setup day). Those flow
through the existing `classify_tick` path unchanged.

---

## 4. The 15-minute opening-range window

- Starts: the tick at which `gap_up_detected` flips True (effectively the session's first usable tick for this
  epic).
- Duration: `Settings.bgu_opening_range_minutes` (default 15).
- During the window: `classify_tick` returns `REJECT(R20)` on any tick that would otherwise FIRE. Also: track
  running `opening_range_high` and `opening_range_low` as max/min of last-traded across every tick in the
  window.
- After the window: OR values freeze. No further updates to `opening_range_high`.

State fields added to `CandidateRuntimeState`:

- `session_open_price: float | None = None`
- `session_open_ts_utc: datetime | None = None`
- `gap_up_detected: bool = False`
- `opening_range_high: float | None = None`
- `opening_range_low: float | None = None`

---

## 5. Entry gate post-window (Rule 9A)

Once the 15-minute window elapses, a LONG candidate with `gap_up_detected=True` fires **only when
`last_traded > opening_range_high`** (strict break above the OR high). Any tick inside or below the OR returns
`REJECT(R21)` — we're not yet confirmed as continuation.

A LONG candidate whose price never breaks the OR high in the session never fires. That's the intended
outcome — it filters fading gap-ups (today's TMUS) from genuine continuation breakouts.

Once price breaks the OR high, subsequent ticks inside the zone fire as usual (no re-check). The break is a
one-time state transition.

---

## 6. Rejection codes

Add to `src/config/rejection_codes.py::RejectCode`:

- **R20** — "BGU: opening range not yet formed (first 15 min after gap-up open)"
- **R21** — "BGU: price has not broken above opening-range high"

Both apply to LONG only. Both follow the existing numeric R-code convention.

---

## 7. Settings additions

To `src/config/settings.py::Settings`:

```python
bgu_opening_range_minutes: int = Field(
    default=15,
    description="Masterclass Rule 9: first N minutes after gap-up open during "
                "which no LONG entry may fire; opening range is tracked in this "
                "window and becomes the break-above trigger afterwards.",
)
```

No env override for the BGU gate itself — it's unconditionally on. This is a Masterclass rule, not an
optional behaviour. Bypass mode still honours it (per `feedback_bypass_semantics` memory).

---

## 8. Contract invariants — do not break these

1. **Non-gap days are unaffected.** A LONG candidate whose first tick is BELOW `trigger_low` takes the
   existing pre-BGU path. `gap_up_detected` stays False. `classify_tick` behaves identically to before.
2. **SHORTs are unaffected** in this PR. Rule 10 handling is a future PR.
3. **`classify_tick` remains a pure function.** All new state lives on `CandidateRuntimeState`; the function
   reads and writes it but has no other side effects.
4. **Session-cutoff check still runs.** If the 15-min window elapsed AND price broke OR high AND the entries
   cutoff has passed, `R_SESSION_CUTOFF` wins — no entry. Same ordering as today.
5. **All 464/5 existing tests continue to pass.** BGU is additive; nothing existing changes shape.

---

## 9. Acceptance criteria

1. `pytest -q` passes: existing 464 green, new ~6 BGU tests green (total ~470).
2. New test file `tests/test_monitor_bgu.py` covers:
   - Non-gap day: BGU path bypassed, existing FIRE still works.
   - Gap-up day inside window: first-tick-in-zone returns `REJECT(R20)`.
   - Gap-up day after window, price not yet above OR high: `REJECT(R21)`.
   - Gap-up day after window, price breaks OR high: `FIRE`.
   - **TMUS 2026-04-24 regression fixture:** synthesise the observed tape (open $193, spike to $193.90, decline
     to $189). Assert `classify_tick` never returns FIRE across that sequence.
   - Session-cutoff precedence: if OR-break happens but cutoff also hit, returns `R_SESSION_CUTOFF`.
3. No changes to files outside `src/engine/monitor.py`, `src/config/settings.py`,
   `src/config/rejection_codes.py`, and the new test file.

---

## 10. What this does NOT fix (yet)

Per `feedback_masterclass_is_the_law`, the Masterclass implementation status after this PR:

- Rule 9A ✅
- Rule 9B (VWAP pullback) ❌ — deferred
- Rule 9 late-stage gap reject ❌ — deferred
- Rule 10 (gap-down) ⚠️ partial — deferred
- Rule 5 (volume confirmation) ❌ — missing, separate PR
- Rule 6 (EMA pullback / Holy Grail) ❌ — missing, separate PR
- Rule 4 (3% chase-reject) ⚠️ partial — may need hardening

Each of these should get its own small, scoped PR with a Masterclass rule citation in the commit message.

---

## 11. Commit convention for Masterclass-related PRs

From this PR onwards, any change touching entry/exit discipline uses the prefix:

```
feat(masterclass): <rule-id> <short description>
```

e.g.: `feat(masterclass): RULE-9A BGU 15-min opening-range gate`.

This makes `git log --grep masterclass` a reliable audit of rule-implementation progress.
