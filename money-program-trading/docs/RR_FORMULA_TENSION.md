# Reward:Risk formula tension — pivot-based 3R vs fill-based 3:1

**Status:** 2026-04-29 · open strategy question · no action without Mark

## The tension in one sentence

The §4.6 derivation formula constructs `target = trigger_low + 3R` (a 3R
multiple from the **pivot**), but the desk reference policy reads "R:R
≥ 3:1 measured from the **fill price**". Under Rule 22 strict-breakout
enforcement, fills happen at `trigger_high` or above — never at the
pivot — so the two measures disagree by ~33% on every entry.

## Concrete example (NVDA-shape with 0.5×ATR zone)

```
lastClose       = 209.03
atr14           = 3.42
trigger_low     = 209.03
trigger_high    = 209.03 + 3.42 × 0.5 = 210.74
stop            = 209.03 - 3.42 × 1.5 = 203.90
R (pivot-based) = trigger_low - stop  = 5.13
target          = trigger_low + 3R    = 224.42
```

| Measure | Value |
|---------|-------|
| §4.6 R:R (pivot-based) | `(224.42 − 209.03) / 5.13` = **3.00** |
| R:R at Rule 22 strict-break fill (trigger_high) | `(224.42 − 210.74) / (210.74 − 203.90)` = **2.00** |
| R:R at limit-at-pivot fill (Rule 9B, future) | `(224.42 − 209.03) / (209.03 − 203.90)` = **3.00** |

## Why Rule 22 fills above the pivot

Rule 22 (the strict-breakout discipline added 2026-04-24) requires
`last > trigger_high` for a LONG to fire — Livermore / Minervini
pivot-point rule. The first tick that strictly breaks resistance is
the entry signal. By definition that tick is at or above
`trigger_high`, never at `trigger_low`. The fixed-3% zone formula
made the gap small enough to ignore (3% of $200 = $6); the new
0.5×ATR formula keeps it at ~half-an-ATR which is real money.

## Three resolution paths

### Path A — Stretch the target

Change the §4.6 formula to anchor target on `trigger_high`:

```
target = trigger_high + 3 × (trigger_high - stop)
```

For NVDA: `target = 210.74 + 3 × 6.84 = 231.26` (vs current 224.42).

**Pros:** restores literal "3:1 at fill" against the desk reference.
**Cons:** target moves 33% further. On the 09:45–15:55 ET window with
the 5-day timestop, more candidates time out before reaching target.
Likely degrades win-rate.

### Path B — Tighten the stop

Keep target at 224.42 (3R from pivot), but move stop closer so that
risk-at-fill shrinks to match the unchanged reward-at-fill:

```
stop = trigger_high - (target - trigger_high) / 3
```

For NVDA: `stop = 210.74 - (224.42 - 210.74) / 3 = 206.18` (vs current
203.90). Stop distance shrinks from 4.85% to 2.16%.

**Pros:** "3:1 at fill" satisfied without moving target.
**Cons:** stop within typical intraday noise — way more stop-outs.
Likely degrades expectancy from the other side.

### Path C — Update policy to match reality (recommended)

Accept that under Rule 22 strict-breakout, the spec-compliant R:R is
3R-from-pivot, which equals **2R-at-fill**. Re-frame the desk
reference: instead of "R:R ≥ 3:1", use "target ≥ pivot + 3R" (same
constraint, different framing).

**Pros:** zero code change. The §4.6 formula is mathematically
self-consistent. The fill-based 3:1 was always aspirational under
intraday-managed conditions where you don't get pivot fills.
**Cons:** the desk-reference number changes from a clean 3:1 to a
2:1-with-asterisk. Visually less impressive in the operator's
mental model.

**Phase 1 today (Task #53 surfacing):** `extras.computed_reward_risk`
is currently fill-based (uses `trigger_mid` ≈ `(trigger_low + trigger_high) / 2`).
That's halfway between the two extremes — neither pivot-based nor
strict-break-fill-based. If we adopt Path C, recompute against
`trigger_low` (LONG) / `trigger_high` (SHORT) so the surfacing matches
the policy.

## Recommendation

**Path C** unless Mark wants to flip a dial. Reasons:

1. **The §4.6 formula is good.** It's mathematically clean (3R from
   pivot), it's deterministic, and it encodes a real edge — the pivot
   is the legitimate breakout point.
2. **Rule 22 + 0.5×ATR are the right shape.** The strict-break
   discipline catches real momentum; the ATR-scaled zone fits each
   stock's natural volatility. Neither should be relaxed for the
   sake of a 3:1 fill number.
3. **The "3:1 at fill" target is from a different era.** Pre-Rule-22
   the system fired on any tick in zone — fills were closer to the
   pivot, and 3:1 at fill was achievable. Post-Rule-22 it isn't.

If Mark prefers a true 3:1 at fill, Path A is cleaner than Path B —
moving target is less destructive than tightening stop in an
intraday-managed model. Path B's tighter stop interacts badly with
the 09:45 noise window even with the 09:45 entries-open rule.

## Related code

- `swing-committee/lib/triggerDerivation.js` — §4.6 derivation
- `swing-committee/docs/lean_scan_spec.md §4.6`
- `entry-rules/money-program-trading/src/engine/monitor.classify_tick` — Rule 22 strict-break
- `entry-rules/money-program-trading/docs/specs/CANONICAL_ENTRY_RULES.md §R2`
- `swing-committee/lib/scanEmission.js` — Task #53 surfacing of
  `extras.computed_reward_risk` and `extras.r2_below_threshold`

## Decision needed

Mark to choose A / B / C and confirm. Then a focused commit
implements the chosen path across both repos:
- Path A: update §4.6 formula in `triggerDerivation.js` + Python
  backtest mirror, update lean_scan_spec.md §4.6 starting formula,
  update CANONICAL_ENTRY_RULES.md Rule 4.
- Path B: same files, different formula.
- Path C: update CANONICAL_ENTRY_RULES.md §R2 to define R:R as
  pivot-based; update Task #53's r2_below_threshold logic to compare
  against pivot-based R:R; no §4.6 formula change.
