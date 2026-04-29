"""
Price scaling helpers — IG /markets/{epic} scalingFactor resolution.

Moved here out of ``market_data.py`` during the S-3 Lightstreamer migration
(Phase 1) so both ``RestPriceFeed`` and the future ``LightstreamerPriceFeed``
can share the same heuristic. See ``docs/specs/S3_LIGHTSTREAMER_SPEC.md``.

Behaviour is a verbatim copy of the logic that lived inline in
``MarketData.get_market_snapshot`` — no change of rules, only a change of
location. The only departure is the extraction of ``resolve_scaling_factor``
as a single entry-point so the feed layer has a clean call site.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _looks_like_equity_spreadbet(epic: str, instrument: Optional[dict]) -> bool:
    """True iff this looks like an IG single-name equity spread-bet epic.

    We require BOTH the epic flavour (CASH/DAILY/DFB under ``.D.``) AND the
    instrument ``type`` to be ``SHARES`` (IG's classification for
    individually-named equities). This explicitly excludes indices
    (``type='INDICES'`` for FTSE/SPX), commodities, and FX — all of which
    legitimately have no scalingFactor and trade in native units.

    If ``type`` is missing, we're conservative and return False — better to
    pass raw prices through (old behaviour) than to accidentally divide a
    correctly-quoted value by 100.
    """
    if not epic:
        return False
    e = epic.upper()
    if ".D." not in e:
        return False
    if not (e.endswith(".CASH.IP") or e.endswith(".DAILY.IP") or e.endswith(".DFB.IP")):
        return False
    inst_type = ""
    if isinstance(instrument, dict):
        inst_type = str(instrument.get("type") or "").upper()
    return inst_type == "SHARES"


def _looks_like_minor_units(bid: Optional[float], ask: Optional[float]) -> bool:
    """Heuristic: do the raw IG bid/ask look like minor units (×100)?

    True when EITHER raw price is above 1500. No US equity trades above
    $1500 per share except BRK.A (which isn't spread-bet anyway), so a
    raw bid/ask > 1500 is a near-certain tell that IG is quoting in cents
    rather than dollars. Returning False when we can't tell (both None)
    keeps us on the safe side — we only override the default to 100 when
    the evidence is positive.

    NOTE: this heuristic is US-equity-specific. For UK shares the same
    >1500 threshold gives a false positive — UK shares trade in pence
    natively and any £15+ stock will exceed 1500p. Callers must guard
    with ``_is_gbp_share`` before applying the ×100 fallback (see
    ``resolve_scaling_factor``).
    """
    for v in (bid, ask):
        if v is not None and v > 1500:
            return True
    return False


def _is_gbp_share(instrument: Optional[dict]) -> bool:
    """True iff IG's instrument metadata reports the primary currency as
    GBP/GBX (UK pence). Used to suppress the US-equity ×100 fallback
    heuristic for high-priced UK shares (RKT, AZN, ULVR, BLT, etc.).

    The 2026-04-29 RKT bug: bid 4682.3 (correct pence for Reckitt at
    £46.82) tripped ``_looks_like_minor_units`` and was wrongly divided
    by 100. The native scaling=1 was hidden in ``snapshot.scalingFactor``
    which the resolver wasn't consulting. After this guard lands, even
    if the snapshot path is bypassed the fallback won't fire on GBP
    shares.

    Returns False when the instrument dict is empty or currency is
    missing — the conservative default is "don't apply fallback unless
    we're confident it's a US equity". Old behaviour preserved for the
    AMD/FDX cases that originally motivated the heuristic.
    """
    if not isinstance(instrument, dict):
        return False
    currs = instrument.get("currencies") or []
    if not currs:
        return False
    first = currs[0]
    if not isinstance(first, dict):
        return False
    code = str(first.get("code") or "").upper()
    return code in ("GBP", "GBX")


def resolve_scaling_factor(
    epic: str,
    instrument: Optional[dict],
    bid_raw: Optional[float],
    ask_raw: Optional[float],
    snapshot: Optional[dict] = None,
) -> float:
    """
    Resolve the scaling factor for ``epic`` using IG's instrument metadata
    plus the US-equity-DAILY.IP fallback heuristic.

    Precedence (highest first):
    1. ``instrument['scalingFactor']`` if present and > 0 — IG's
       authoritative per-instrument answer for US equity DAILY.IP
       epics (typically ``100``, meaning prices come back in minor
       units / cents).
    2. ``100.0`` if (a) the epic looks like a single-name equity spread-bet
       AND (b) raw bid/ask look like minor units (one > 1500) AND
       (c) the instrument is NOT a GBP/GBX share. The GBP guard prevents
       the false-positive on high-priced UK shares (Reckitt, AstraZeneca,
       Unilever, etc. all trade above 1500p in native pence).
    3. ``1.0`` default — logs an info line with the instrument type for
       diagnosis.

    The ``snapshot`` parameter is accepted for forward-compatibility
    but **deliberately not consulted** — IG's
    ``snapshot.scalingFactor`` is misleading for US equities (returns
    ``1`` even when prices are quoted in cents and need a 100x divide).
    The 2026-04-29 RKT fix originally tried to read snapshot first;
    that caused a regression on AMGN/LMT (US cent-quoted shares with
    snapshot.scalingFactor=1 short-circuiting the correct heuristic).
    The GBP-share guard added in step 2 is sufficient to handle the
    UK pence case (RB / MNGLN / SBRY) without consulting snapshot.

    This function is intentionally pure: no caching, no IG calls. The
    caller owns the cache. See ``RestPriceFeed._scale_cache`` /
    ``LightstreamerPriceFeed._scale_cache``.
    """
    del snapshot  # accepted for back-compat; see docstring for rationale

    def _f(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # 1. Instrument-level scalingFactor — authoritative when present.
    #    US equity DAILY.IP typically reports 100 here. UK pence shares
    #    typically report None and rely on step 2's GBP guard.
    raw_sf = None
    if isinstance(instrument, dict):
        raw_sf = instrument.get("scalingFactor")
    scaling_factor = _f(raw_sf)

    if scaling_factor is not None and scaling_factor > 0:
        logger.debug(
            "resolve_scaling_factor: %s — scalingFactor=%s from instrument",
            epic, scaling_factor,
        )
        return scaling_factor

    # 3. Equity minor-units fallback — but ONLY for non-GBP shares. The
    #    >1500 threshold was written for US equities quoted in cents,
    #    where "raw bid > 1500" is a near-certain "this is cents not
    #    dollars" signal. UK shares trade in pence natively and any
    #    £15+ stock breaks the heuristic. The GBP guard below is the
    #    direct fix for the 2026-04-29 RKT bug.
    if (
        _looks_like_equity_spreadbet(epic, instrument)
        and _looks_like_minor_units(bid_raw, ask_raw)
        and not _is_gbp_share(instrument)
    ):
        logger.warning(
            "get_market_snapshot: %s — scalingFactor missing/invalid "
            "(raw=%r) but type=SHARES and bid=%s ask=%s look like "
            "minor units; assuming scalingFactor=100 (equity convention).",
            epic, raw_sf, bid_raw, ask_raw,
        )
        return 100.0

    inst_type = (instrument or {}).get("type") if isinstance(instrument, dict) else None
    logger.info(
        "get_market_snapshot: %s — scalingFactor missing/invalid "
        "(raw=%r); defaulting to 1.0. instrument.type=%r",
        epic, raw_sf, inst_type,
    )
    return 1.0
