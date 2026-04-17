"""Hand-curated ticker lists for the intraday scan universe.

These are the tickers we feed into ``MarketData.resolve_epic`` to build
the scannable universe. Kept as module-level constants so the lists
are inspectable without loading the scanner runtime.

Sources (snapshot 2026-04-17, refresh quarterly):
* S&P 100 — OEX index constituents
* Nasdaq 100 — QQQ holdings
* FTSE 100 top-15-by-ADV — LSE hand-picked for intraday liquidity

Index proxies are hard-coded epics (see
``reference_ig_index_proxies.md``): IG UK retail doesn't offer SPY/QQQ/
IWM/DIA/XLK as spread bets, so we trade IG's own index products.
XLK has no equivalent and is intentionally omitted — tech mega-caps
(AAPL/MSFT/NVDA/GOOGL/META) are already in SP100/NDX.
"""
from __future__ import annotations


# ─── S&P 100 (OEX) ──────────────────────────────────────────────────────
# Source: S&P Global public methodology doc, April 2026 constituents.
SP100_TICKERS: tuple[str, ...] = (
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AIG", "ALL", "AMD", "AMGN", "AMT",
    "AMZN", "AVGO", "AXP", "BA", "BAC", "BK", "BKNG", "BLK", "BMY", "BRK.B",
    "C", "CAT", "CHTR", "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO",
    "CVS", "CVX", "DE", "DHR", "DIS", "DUK", "EMR", "F", "FDX", "GD",
    "GE", "GILD", "GM", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTC",
    "JNJ", "JPM", "KHC", "KO", "LIN", "LLY", "LMT", "LOW", "MA", "MCD",
    "MDLZ", "MDT", "MET", "META", "MMM", "MO", "MRK", "MS", "MSFT", "NEE",
    "NFLX", "NKE", "NVDA", "ORCL", "PEP", "PFE", "PG", "PM", "PYPL", "QCOM",
    "RTX", "SBUX", "SCHW", "SO", "SPG", "T", "TGT", "TMO", "TMUS", "TSLA",
    "TXN", "UNH", "UNP", "UPS", "USB", "V", "VZ", "WBA", "WFC", "WMT",
    "XOM",
)


# ─── Nasdaq 100 ─────────────────────────────────────────────────────────
# Source: Nasdaq listings site, April 2026. Many names overlap with
# SP100; the resolver dedupes on ticker.
NDX_TICKERS: tuple[str, ...] = (
    "AAPL", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN", "AMZN",
    "ANSS", "ASML", "AVGO", "AZN", "BIIB", "BKNG", "BKR", "CDNS", "CDW", "CEG",
    "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO", "CSX", "CTAS", "CTSH",
    "DASH", "DDOG", "DLTR", "DXCM", "EA", "EBAY", "ENPH", "EXC", "FANG",
    "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX",
    "ILMN", "INTC", "INTU", "ISRG", "KDP", "KHC", "KLAC", "LRCX", "LULU",
    "MAR", "MCHP", "MDLZ", "MELI", "META", "MNST", "MRNA", "MRVL", "MSFT",
    "MU", "NFLX", "NVDA", "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX",
    "PCAR", "PDD", "PEP", "PYPL", "QCOM", "REGN", "ROP", "ROST", "SBUX",
    "SIRI", "SNPS", "TEAM", "TMUS", "TSLA", "TTD", "TTWO", "TXN", "VRSK",
    "VRSN", "WBA", "WBD", "WDAY", "XEL", "ZM", "ZS",
)


# ─── FTSE 100 — top 15 by ADV (hand-picked) ─────────────────────────────
# Keeping this narrow on purpose: spread costs on lower-volume FTSE names
# are punitive for a small account, and our intraday edge needs liquidity.
FTSE_TICKERS: tuple[str, ...] = (
    "SHEL",   # Shell
    "AZN",    # AstraZeneca
    "HSBA",   # HSBC
    "ULVR",   # Unilever
    "BATS",   # BAT
    "BP",     # BP
    "RIO",    # Rio Tinto
    "GSK",    # GSK
    "REL",    # RELX
    "DGE",    # Diageo
    "LSEG",   # LSE Group
    "VOD",    # Vodafone
    "LLOY",   # Lloyds
    "NWG",    # NatWest
    "BARC",   # Barclays
)


# ─── IG Index Proxies (hard-coded — see reference memory) ───────────────
# These bypass ticker-search resolution entirely. Epic verified via
# tools/verify_etf_spreadbets.py on 2026-04-17 (DEMO).
INDEX_PROXIES: dict[str, dict[str, str]] = {
    "US500": {
        "epic": "IX.D.SPTRD.DAILY.IP",
        "instrument_name": "US 500",
        "market": "US",
        "replaces": "SPY",
    },
    "USTECH100": {
        "epic": "IX.D.NASDAQ.CASH.IP",
        "instrument_name": "US Tech 100",
        "market": "US",
        "replaces": "QQQ",
    },
    "WALLSTREET": {
        "epic": "IX.D.DOW.DAILY.IP",
        "instrument_name": "Wall Street",
        "market": "US",
        "replaces": "DIA",
    },
    "RUSSELL2000": {
        "epic": "IX.D.RUSSELL.DAILY.IP",
        "instrument_name": "US Russell 2000",
        "market": "US",
        "replaces": "IWM",
    },
    "FTSE100": {
        "epic": "IX.D.FTSE.DAILY.IP",
        "instrument_name": "FTSE 100",
        "market": "UK",
        "replaces": "—",
    },
    "GER40": {
        "epic": "IX.D.DAX.DAILY.IP",
        "instrument_name": "Germany 40",
        "market": "DE",
        "replaces": "—",
    },
}


# ─── Manual epic overrides ──────────────────────────────────────────────
# Some IG epics don't match their ticker by whole-token (e.g. AbbVie is
# listed as ``SA.D.ABBVWUS.DAILY.IP`` — ticker shows up as ``ABBVWUS``,
# not ``ABBV``, inside the epic). The whole-token matcher will correctly
# reject these, so we seed the epic cache with hand-verified overrides.
#
# Each entry maps ``f"{ticker}:{market}" → epic``. These are written to
# ``data/cache/epic_map.json`` at the start of ``build_universe()`` so
# the resolver short-circuits on them.
#
# Add entries here only after confirming the epic via IG search + a
# ``fetch_market_by_epic`` that returns the expected instrumentName.
MANUAL_EPIC_OVERRIDES: dict[str, str] = {
    # Confirmed via tools/verify_etf_spreadbets.py + search dump 2026-04-17.
    "ABBV:US": "SA.D.ABBVWUS.DAILY.IP",   # AbbVie Inc (24 Hours)
}

# Tickers IG does not offer under any recognisable epic on a UK retail
# spread-bet account. We drop these from the universe rather than keep
# searching for a match that will never come.
UNAVAILABLE_ON_IG: frozenset[str] = frozenset({
    # Searched 2026-04-17 — top search results were all unrelated names
    # (Amte Power, AMTD Digital, Amtech Systems). American Tower REIT is
    # not in IG's UK spread-bet catalog.
    "AMT",
})


def combined_us_tickers() -> list[str]:
    """Deduped union of S&P 100 and Nasdaq 100."""
    seen: set[str] = set()
    out: list[str] = []
    for t in SP100_TICKERS + NDX_TICKERS:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def universe_size_estimate() -> dict[str, int]:
    """Quick counts for sanity-checking the scan universe."""
    us_unique = len(combined_us_tickers())
    return {
        "sp100": len(SP100_TICKERS),
        "ndx": len(NDX_TICKERS),
        "us_deduped": us_unique,
        "ftse": len(FTSE_TICKERS),
        "index_proxies": len(INDEX_PROXIES),
        "total": us_unique + len(FTSE_TICKERS) + len(INDEX_PROXIES),
    }
