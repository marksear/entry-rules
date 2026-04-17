# DEMO Shakedown Runbook

Reusable procedure for running a full-vertical-slice DEMO session on IG: scan ingest → trigger → fill → trail → exit → journal. Use this any time you need to validate production logic against a live DEMO account before promoting a change, or as a standing pre-flight before LIVE cutover.

**Account**: IG DEMO, spread-bet. Confirm stamp (starting balance) before each run.
**Stake**: whatever you're currently shaking down at (see *Trail maths* below — £ thresholds shift with stake).
**Rule set**: `main` of `entry-rules/money-program-trading` unless deliberately testing a branch.
**Session cadence**: one active session at a time. Don't overlap US and UK in the same DB.

---

## Epic reference (verify before each run)

| Symbol      | IG epic                 | Unit  | Min deal |
|-------------|-------------------------|-------|----------|
| US 500      | `IX.D.SPTRD.DAILY.IP`   | £/pt  | 1.0 pts  |
| US Tech 100 | `IX.D.NASDAQ.CASH.IP`   | £/pt  | 0.2 pts  |
| FTSE 100    | `IX.D.FTSE.DAILY.IP`    | £/pt  | 0.5 pts  |

Min deal sizes are **not uniform** — NASDAQ is 0.2pts, the others aren't. Don't assume. `data/cache/epic_map.json` is the canonical map.

---

## T-60 — background prep

1. Plug in, stable connection.
2. Terminal at `/Users/mark.sear/CoWork/entry-rules/money-program-trading`.
3. Check env:

   ```bash
   grep IG_ACC_TYPE .env        # must read: IG_ACC_TYPE=DEMO
   grep IG_ACCOUNT_ID .env      # DEMO spread-bet account id
   ```

4. Clean up dry-run DB if present — keep `trading.db` (real observability log), delete `test_shakedown.db`:

   ```bash
   ls -la data/trading.db data/test_shakedown.db 2>&1
   rm -f data/test_shakedown.db
   ```

5. Pull latest, note SHA:

   ```bash
   git pull --rebase && git status
   git rev-parse --short HEAD   # paste as --rule-set-version at launch
   ```

## T-30 — pre-flight

1. Smoke-test IG auth (also warms the session so scan builder doesn't race):

   ```bash
   python - <<'PY'
   from src.auth.ig_auth import IGSession
   s = IGSession(); s.connect()
   bal = s.get_account_balance()
   print("balance:", bal)
   # Fallback sanity check — raw IG response. Useful when balance is empty.
   if not bal:
       import json
       print("raw accounts:", json.dumps(s.service.fetch_accounts(), default=str, indent=2)[:1200])
   s.disconnect()
   PY
   ```

   **Gotcha 1 — rate limit**: IG DEMO rate-limits rapid logins. A 401 or `invalid-client-security-token` → wait **2 min**, retry once. Still 401? Wait **5 min** before anything else. Do not spam auth.

   **Gotcha 2 — silent empty balance**: `get_account_balance()` returns `{}` on three different failure modes (no accounts, account without `balance` key, swallowed `fetch_accounts` exception). If you see `balance: {}`, do NOT proceed to launch — dump the raw `fetch_accounts()` response (fallback above) to diagnose. Launching `session_init` against a half-initialised session will silently place orders against IG's default account, not your configured DEMO spread-bet account.

2. Test suite:

   ```bash
   pytest -q
   ```

   Baseline should be green — note any skips. A regression here aborts the shakedown.

3. Dry-run the ingest path (uses isolated DB — doesn't touch `trading.db`):

   ```bash
   python tools/build_shakedown_scan.py --sample --account-size <SIZE> \
       --broker-mode DEMO \
       --candidate <SYM>:<MKT>:<DIR>:<STAKE> [--candidate ...]

   DB_PATH=data/test_shakedown.db python -m src.session_init \
       --scan data/scans/scan_YYYYMMDD.json \
       --account-size <SIZE> --label <LABEL> --dry-run \
       --notes "pre-open dry-run"
   ```

   Expect: session row written, shortlist ingested, `SHORTLIST_ADDED` events emitted, clean exit. Delete `data/test_shakedown.db` afterwards.

## T-5 — read the tape

1. IG web/mobile open on the candidate charts, 5-minute candles.
2. Wait for the first one or two 5-min candles to form. For each epic:
   - **LONG setup**: price gapping + holding above pre-market high. Trigger = `first_5min_high + N pts` (symbol-specific). Stop = `trigger - M pts`.
   - **SHORT setup**: gapping + fading below pre-market low. Mirror the maths.
   - **Chop inside the first candle**: **skip the epic**. Trading nothing beats forcing a shakedown.

   Symbol-specific offsets (current working values):
   - SPTRD: trigger +2pts / stop -5pts
   - NASDAQ: trigger +5pts / stop -15pts
   - FTSE: TBD — read the tape, use your own volatility read

3. Jot per survivor: `SYMBOL direction trigger stop`.

## T-0+ — craft and launch

1. Build scan (or edit generated JSON directly to pin specific prices):

   ```bash
   python tools/build_shakedown_scan.py \
       --broker-mode DEMO --account-size <SIZE> \
       --candidate <SYM>:<MKT>:<DIR>:<STAKE> [--candidate ...]
   ```

   Verify the printed trigger/stop/risk summary against your tape read before launching.

2. Launch monitor loop:

   ```bash
   SHA=$(git rev-parse --short HEAD)
   python -m src.session_init \
       --scan data/scans/scan_YYYYMMDD.json \
       --account-size <SIZE> --label <LABEL> \
       --tick-seconds 30 \
       --rule-set-version "$SHA" \
       --notes "<session description>"
   ```

   - `--tick-seconds 30` is shakedown-appropriate (default is 60). Faster fill detection, more events to watch.
   - `--rule-set-version "$SHA"` is **mandatory**. Without it the journal can't link trades back to code. Don't launch without it.

## During the session — expected event stream

- **Trigger fires**: `SHORTLIST_TRIGGERED` → `ORDER_PLACED` → `FILLED` in logs and `candidate_events`.
- **Trail arm**: at unrealised P&L = **50pts × stake** (£25 @ £0.50/pt). Emits `TRAIL_ARMED`. Stop jumps to break-even + 1pt lock.
- **Trail bands**: every additional **10pts × stake** of peak P&L (£5 band @ £0.50/pt). Repeat `STOP_MOVED` with `trail_step_count` rising 0→1→2→…
- **Hard target**: **100pts × stake** gross. `TARGET_HIT` → position closes.
- **Trail exit**: retrace to trailed stop → `STOP_HIT` → P&L locked at last trail pin.
- **Timestop**: session-end, open trades close via `TIMESTOP_HIT` if nothing else triggered.

**Trail maths are in points, not pounds.** The £ values scale with stake; the pt thresholds (50/10/100) don't. If you change stake, don't mentally translate from the £ numbers above — stay in points.

**Sanity check**: live IG P&L should match in-memory P&L within a tick. If it drifts, log the discrepancy and investigate post-session — do **not** intervene manually unless something is clearly broken (e.g. stop orphaned on the IG side).

## Post-session — verification

1. Journal auto-writes on loop exit to `reports/journal_YYYYMMDD_<LABEL>.md`:

   ```bash
   cat reports/journal_YYYYMMDD_<LABEL>.md
   ```

   Check: session row stamped; each candidate shows trigger/fill/exit; trail step count makes sense (1 per 10pts of peak P&L); exit reason recorded (target / trail / timestop / stop).

2. Eyeball the event stream:

   ```bash
   sqlite3 data/trading.db "SELECT ts_utc, event_type, candidate_id FROM candidate_events WHERE session_id=(SELECT id FROM sessions ORDER BY id DESC LIMIT 1) ORDER BY ts_utc;"
   ```

3. Archive the journal outside the repo:

   ```bash
   cp reports/journal_YYYYMMDD_<LABEL>.md /Users/mark.sear/Documents/swing-committee/journals/
   ```

4. Any anomaly → log in memory as "Shakedown anomaly — `<thing>`" in `project_build_checkpoint.md`. Fix before next shakedown.

## Abort criteria

- **IG auth 401s twice after cooldown** → abort, file support ticket, do not trade. Market moves faster than the rate-limit clock.
- **Dry-run at T-30 crashes** with a schema error or fails to write `SHORTLIST_ADDED` → abort, fix, re-run tests, defer shakedown.
- **Pre-existing IG position not picked up by `rehydrate_open_positions`** → abort. The resume path is belt-and-braces; a miss there means double-entry risk.

## Multi-session days

Run sessions serially, not concurrently. US regular (14:30 UTC) → review journal → FTSE follow-up (next morning 08:00 UTC) as a separate `--label UK_REGULAR` session. One active session at a time keeps `trading.db` clean and the observability unambiguous.

## Before LIVE cutover

Use this runbook to exercise every code path that will run against real money. When the DEMO journal looks clean across multiple sessions — triggers fire correctly, trail maths match, exits log the right reason, no rehydrate misses — spawn a `live_cutover.md` sibling runbook. LIVE shifts the emphasis: less "did the event emit" (you've proved that), more "is position size right, is kill-switch reachable, what's the rollback plan".
