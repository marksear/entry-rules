# IG Ticker Collisions — Cross-Market Reference

**Status:** 2026-04-29 · operator reference for disambiguating tickers
that resolve to different companies on different exchanges.

## Why this matters

IG's `search_markets(ticker)` is **not market-segregated**. Searching
for `"RKT"` returns Rocket Companies (US) AND Reckitt Benckiser (UK)
in a single result set. The current `_search_market` resolver picks
the highest-priority spread-bet flavour with a whole-token match —
so without an explicit `market` filter it can pick the wrong company.

The 2026-04-29 RKT bug exposed this: bypass JSON shipped `RKT` with
`market: "US"` (a separate scanEmission bug, fixed under Task #66).
The resolver returned `SG.D.RKTUS.DAILY.IP` (Rocket Companies, US),
which the manual monitor then quoted with bad pricing. Two layers of
defence now apply:

1. **Whole-token match** (in `_search_market`) — `RKT` is not a whole
   token in `SG.D.RKTUS.DAILY.IP` (the segment is `RKTUS`), so it's
   correctly rejected on the bare-ticker path.
2. **UK-share gate** (when `market="UK"`) — accepts only rows whose
   instrumentName contains `LSE`, ticker has `.L`, or ticker ends in
   `LN`. Rocket Companies has none of those — rejected.
3. **LN-suffix retry** (Task #67, in `resolve_epic`) — when bare-
   ticker search returns empty for a UK candidate, retry with
   `<TICKER>LN`. That's IG's internal LSE convention.

## Confirmed collisions

| Search | US (DAILY.IP) | UK | Notes |
|--------|---------------|----|----|
| `RKT` | `SG.D.RKTUS.DAILY.IP` Rocket Companies | `KA.D.RB.DAILY.IP` Reckitt Benckiser | Reckitt kept legacy `RB` epic from pre-2021 ticker change. Search for `Reckitt` or `RB.` to confirm. **`RKTLN` does NOT match anything** — Reckitt is the rare UK exception that doesn't follow the LN convention. |
| `MNG` | (no significant US match) | `KA.D.MNGLN.DAILY.IP` M&G PLC | LN-suffix retry handles this. |
| `SBRY` | (no significant US match) | `KA.D.SBRY.DAILY.IP` Sainsbury (J) PLC | Bare ticker resolves cleanly via whole-token match. |

## Likely-collision tickers (not yet verified)

These are tickers where the US and LSE listings carry the same or
similar codes. Worth a `debug_uk_search.py` check before trading
either side:

| Ticker | Possible US | Possible UK | Verification command |
|--------|-------------|-------------|----------------------|
| `BP` | BP plc ADR (NYSE) | BP plc (LSE) | `.venv/bin/python tools/debug_uk_search.py BP BPLN` |
| `SHEL` | Shell plc ADR (NYSE) | Shell plc (LSE) | `.venv/bin/python tools/debug_uk_search.py SHEL SHELLN` |
| `AZN` | AstraZeneca ADR (NASDAQ) | AstraZeneca PLC (LSE) | `.venv/bin/python tools/debug_uk_search.py AZN AZNLN` |
| `GSK` | GSK plc ADR (NYSE) | GSK plc (LSE) | `.venv/bin/python tools/debug_uk_search.py GSK GSKLN` |
| `VOD` | Vodafone ADR (NASDAQ) | Vodafone Group PLC (LSE) | `.venv/bin/python tools/debug_uk_search.py VOD VODLN` |
| `BCS` | Barclays ADR (NYSE) | `BARC` (LSE) | UK ticker is BARC, not BCS — verify |
| `HSBC` | HSBC ADR (NYSE) | `HSBA` (LSE) | UK ticker is HSBA, not HSBC — verify |
| `AAL` | American Airlines (NASDAQ) | Anglo American PLC (LSE, ticker `AAL`) | **Active collision.** Distinct companies. |
| `RIO` | Rio Tinto ADR (NYSE) | Rio Tinto plc (LSE) | `.venv/bin/python tools/debug_uk_search.py RIO RIOLN` |
| `BHP` | BHP Group ADR (NYSE) | BHP Group plc (LSE) | `.venv/bin/python tools/debug_uk_search.py BHP BHPLN` |
| `UL` | Unilever ADR (NYSE) | Unilever plc (LSE, ticker `ULVR`) | UK ticker is ULVR, not UL — verify |
| `NWG` | NatWest Group ADR (NYSE) | NatWest Group plc (LSE) | `.venv/bin/python tools/debug_uk_search.py NWG NWGLN` |

## How to disambiguate

**Operator side (scanner / scanEmission):** ensure `market` is set
correctly on the shortlist entry. Task #66 fixed the bypass JSON to
attach `market="UK"` for `.L`-suffixed scanner inputs before
stripping the suffix.

**Resolver side (entry-rules):** the `_search_market` UK gate +
`resolve_epic` LN-suffix retry handle the standard UK shares. The
**Reckitt exception** (KA.D.RB.DAILY.IP) requires manual cache
injection until/unless we add a special-case lookup table.

**To pre-cache a known UK epic** for today's session:

```bash
cd ~/CoWork/entry-rules/money-program-trading
python3 -c "
import json
p = 'data/cache/epic_map.json'
m = json.load(open(p))
m['RKT:UK'] = 'KA.D.RB.DAILY.IP'   # Reckitt — won't auto-resolve
json.dump(m, open(p, 'w'), indent=2, sort_keys=True)
"
```

(Cache key uses the market the resolver will be called with — UK if
scanEmission is now correctly labelling, US if running against an
older bypass JSON.)

## Adding new entries

When a new collision is discovered:

1. Run `tools/debug_uk_search.py <TICKER>` to capture what IG returns.
2. Add a row to the **Confirmed collisions** table above.
3. If neither LN-suffix retry nor bare-ticker resolution finds the
   intended company (RKT/Reckitt class), add to a known-exceptions
   special-case map — currently kept as cache injections only.
4. Update `feedback_*` memory if the collision affected a real trade.

## Reference: spread-bet flavour preference

The resolver picks the highest-priority match across:

| Flavour | Notes | Priority |
|---------|-------|----------|
| `CASH` | Undated, regular hours. Cleanest. | 1 (best) |
| `DFB` | Daily Funded Bet — overnight funding. | 2 |
| `DAILY` | 24-hour spread bet. Slightly wider spread. | 3 |
| Dated (`JUN`, `SEP`, `DEC`, `MAR`) | Quarterly futures. Rejected. | — |

UK shares typically come back as `DAILY` flavour. Most bypass JSONs
today will resolve to `KA.D.<TICKER>LN.DAILY.IP` style epics.
