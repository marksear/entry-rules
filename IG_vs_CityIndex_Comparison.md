# MONEY PROGRAM — BROKER API COMPARISON

## IG Index vs City Index — For Entry Refinement Engine

Version 1.0 | March 2026

---

## 1. Executive Summary

**Recommendation: IG Index is the stronger choice, but with one important caveat.**

IG has a more mature, better-documented API with richer features (streaming, client sentiment, guaranteed stops, working orders with attached stops). However, the initial assumption that IG provides server-side technical indicators is **incorrect** — IG confirmed on their labs forum that they do not compute indicators server-side because the permutations of parameters would require storing too much data. Both brokers require local indicator computation.

That said, IG still wins on several other fronts. Here's the full picture.

---

## 2. Head-to-Head Comparison

### 2.1 Authentication & Session Management

| Feature | IG Index | City Index |
|---------|----------|------------|
| Auth method | API key + credentials → OAuth-style tokens (CST + X-SECURITY-TOKEN) | Username + password → session token |
| Session duration | Configurable, typically 6 hours | ~20 minutes (must refresh frequently) |
| Token refresh | Long-lived tokens, less overhead | Frequent re-auth needed |
| Account switching | Yes — switch between CFD / spread bet / share dealing | Yes — different TradingAccountIds |

**Winner: IG.** The 6-hour session vs 20-minute timeout means far less auth overhead in a production system.

### 2.2 Market Data — Historical Prices

| Feature | IG Index | City Index |
|---------|----------|------------|
| Daily OHLCV bars | Yes | Yes |
| Volume included | **Yes — `lastTradedVolume` confirmed** | Unconfirmed — needs testing |
| Intraday resolutions | SECOND, MINUTE, 2/3/5/10/15/30 MIN, HOUR, 2/3/4 HOUR, DAY, WEEK, MONTH | TICK, MINUTE, HOUR, DAY, WEEK (with span multiplier) |
| Bid/Ask/Mid prices | All three returned separately | MID and BID |
| History depth | Up to 20 years | Unclear — not documented |
| Data allowance | **10,000 data points per week** | No documented limit |
| Equities price data | **CFD/spread bet epics only — no share dealing price data** | Via CFD market IDs |

**Winner: Mixed.** IG has richer resolution options and confirmed volume data. But IG's **10,000 data point weekly cap** is a serious constraint. For a universe of ~200 stocks needing 260 daily bars each, that's 52,000 data points — you'd blow through the weekly allowance in one scan. City Index has no documented cap.

**This is the single biggest operational risk with IG.**

### 2.3 Real-Time Streaming

| Feature | IG Index | City Index |
|---------|----------|------------|
| Technology | Lightstreamer | Lightstreamer |
| Real-time bid/ask | Yes | Yes |
| Tick data | Yes — `CHART:{epic}:TICK` | Yes |
| Candle subscriptions | Yes — `CHART:{epic}:{scale}` for any timeframe | Likely similar (less documented) |
| Account/trade updates | Yes — real-time P&L, position updates | Limited documentation |
| Subscription limit | **40 concurrent subscriptions per connection** | Not documented |
| Spread calculation | Real-time from BID/ASK stream | Real-time from BID/ASK stream |

**Winner: IG.** Better documented, more granular subscriptions, and real-time account updates.

### 2.4 Order Types

| Order Type | IG Index | City Index |
|-----------|----------|------------|
| Market order | Yes | Yes |
| Limit order | Yes | Yes |
| Stop order (entry) | Yes — via working orders | Yes |
| Stop-limit order | Yes — working order with stop + limit attached | Uncertain — needs testing |
| Guaranteed stop | Yes (premium charged) | Yes (premium charged) |
| Trailing stop | Yes — on positions (not on working orders) | Yes |
| OCO | **No native OCO for entry** — but stop + limit on an open position acts as OCO | Yes |
| Attached stop/limit on entry | Yes — `stopDistance` / `limitDistance` on position creation | Yes — via If-Done child orders |
| Working orders (pending) | Yes — `POST /workingorders/otc` with full parameters | Yes — but less documented |
| Order simulation/dry run | **Not documented** | **Yes — `/order/simulate/newtradeorder`** |

**Winner: IG for working orders and guaranteed stops. City Index for order simulation.** The City Index simulation endpoint is extremely valuable for paper trading. IG doesn't appear to have an equivalent — you'd need to build your own paper trading layer.

### 2.5 Position Management

| Feature | IG Index | City Index |
|---------|----------|------------|
| List open positions | Yes — `GET /positions` | Yes — `GET /order/openpositions` |
| Modify stop/limit | Yes — `PUT /positions/otc/{dealId}` | Yes — `POST /order/updatetradeorder` |
| Close position | Yes — `DELETE /positions/otc` | Yes — via close order |
| Deal confirmation | Yes — `GET /confirms/{dealReference}` | Via order status |
| Trade history | Yes — `GET /history/transactions` and `/history/activity` | Yes — `GET /order/tradehistory` |
| Real-time P&L streaming | Yes — via Lightstreamer | Limited |

**Winner: IG.** The deal confirmation endpoint and activity history are more robust.

### 2.6 Unique IG Features

| Feature | Details | Value for Our System |
|---------|---------|---------------------|
| **Client Sentiment** | `GET /clientsentiment/{marketId}` — % long vs % short among IG clients | **Useful as supplementary signal** — when 80%+ of retail is one direction, consider fading |
| **Market Categories/Navigation** | `GET /marketnavigation` — browse markets by category | Helps build universe programmatically |
| **Watchlists** | `GET /watchlists`, `POST /watchlists` — create and manage | Could sync our active watchlist |
| **Sprint Markets** | Binary-style short-duration trades | Not relevant to our system |

### 2.7 Unique City Index Features

| Feature | Details | Value for Our System |
|---------|---------|---------------------|
| **Order Simulation** | `POST /order/simulate/newtradeorder` | **Extremely valuable** for paper trading and testing |
| **No documented data cap** | No weekly data point limit mentioned | Critical for scanning a large universe |

---

## 3. Data Gap Analysis — Comparison

| Data Requirement | IG Index | City Index | External Needed? |
|-----------------|----------|------------|-----------------|
| Daily OHLCV with volume | **Yes (confirmed)** | Unconfirmed | IG: No. CI: Possibly. |
| Intraday 5-min bars | Yes (MINUTE_5) | Yes (MINUTE, span=5) | No |
| Real-time bid/ask | Yes | Yes | No |
| MA, EMA, ADX computation | **Local only** | **Local only** | Both: compute locally |
| RS percentile ranking | Local only | Local only | Both: compute locally |
| 52-week high/low | From 260 daily bars | From 260 daily bars | No |
| Earnings calendar | **No** | **No** | **Yes — both need FMP / Alpha Vantage** |
| Short interest % | **No** | **No** | **Yes — both need ORTEX / Fintel** |
| Days to cover | **No** | **No** | **Yes — derive from SI data** |
| Borrow availability | N/A (CFD model) | N/A (CFD model) | No — broker handles |
| Catalyst calendar (FDA etc.) | **No** | **No** | **Yes — both need external** |
| Client sentiment (long/short %) | **Yes — built in** | **No** | IG advantage |

**Key finding: Both brokers have identical gaps in supplementary data.** Neither provides earnings calendars, short interest, or catalyst data. Both require the same external API integrations (FMP, ORTEX, etc.). The indicator computation gap is also identical — neither provides server-side indicators.

---

## 4. The IG Data Allowance Problem

This is the critical issue that needs addressing.

**IG allows 10,000 historical data points per week.**

Our system needs:
- ~200 stocks in the active universe (US + UK)
- 260 daily bars each for MA(200) and 52-week high/low
- That's **52,000 data points** just for the daily scan

Even with aggressive caching (only refresh changed/new bars), the initial load and weekly refresh would exceed the cap.

### 4.1 Mitigation Strategies

| Strategy | How It Works | Data Points Saved |
|----------|-------------|-------------------|
| **Incremental updates** | After initial load, only fetch last 1–5 bars per stock daily | ~200 stocks × 1 bar = 200/day vs 52,000 |
| **Tiered universe** | Tier 1 (50 stocks): daily refresh. Tier 2 (150 stocks): refresh only when signal received | ~80% reduction |
| **External data source for history** | Use Alpha Vantage, Polygon, or Yahoo Finance for bulk historical data. Use IG only for real-time and recent bars | Virtually unlimited history externally |
| **Cache aggressively** | Store all bar data locally. Only request what's missing. | Near-zero after initial population |
| **Initial bulk load over 6 weeks** | Spread the initial 52,000 points across 6 weeks at ~9,000/week | Stays within cap |

**Recommended approach: Use an external source (Alpha Vantage free tier: 500 requests/day, or Polygon.io) for the bulk historical data load and daily universe scan. Use IG's API only for real-time streaming, intraday bars on active signals, and order execution.** This decouples the data layer from the broker layer, which is better architecture anyway.

---

## 5. The 40-Subscription Streaming Limit

IG allows 40 concurrent Lightstreamer subscriptions. For mid-session volume monitoring, we'd need to stream the tickers with open positions plus any with pending entry signals.

With a maximum of ~6 open positions (at 1% risk each = 6% total) plus maybe 5–10 active signals, we'd use 11–16 streams. Well within the 40 limit.

---

## 6. Recommendation

### 6.1 Primary Broker: IG Index

**For order execution, real-time streaming, and position management.**

Reasons:
- Better documented API with mature Python library (`trading-ig`)
- Confirmed volume in historical data
- Richer order types (working orders with attached stops, guaranteed stops)
- Client sentiment data (bonus signal)
- Real-time streaming with account/trade updates
- Longer session tokens (less auth overhead)
- Watchlist API for managing the active universe

### 6.2 Hybrid Data Architecture

**Do NOT rely on IG for bulk historical data.** Use:

| Data | Source | Reason |
|------|--------|--------|
| Bulk daily OHLCV (260 bars, full universe) | Alpha Vantage / Polygon.io / Yahoo Finance | Avoid IG's 10k/week cap |
| Intraday 5-min bars (active signals only) | IG API | Low volume, within cap |
| Real-time streaming (open positions + active signals) | IG Lightstreamer | Best-in-class |
| Earnings calendar | FMP / Alpha Vantage | Neither broker provides |
| Short interest / DTC | ORTEX / Fintel | Neither broker provides |
| Client sentiment | IG API | Built-in bonus |
| Order execution | IG API | Primary broker |

### 6.3 Keep City Index as Backup

City Index's order simulation endpoint and lack of data caps make it a useful secondary/backup broker. If IG has downtime or the data cap becomes problematic, City Index can step in.

---

## 7. Updated Architecture (IG Primary)

```
┌─────────────────┐
│  Signal Engine   │
│  (existing)      │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│          ENTRY REFINEMENT ENGINE             │
│                                              │
│  ┌─────────────┐  ┌──────────────────────┐  │
│  │ Indicators   │  │ Gates & Classifier   │  │
│  │ (local calc) │  │ L1-L3, S1-S4        │  │
│  └──────┬──────┘  └──────────┬───────────┘  │
│         │                     │              │
│  ┌──────┴──────┐  ┌──────────┴───────────┐  │
│  │ Risk Manager│  │ Order Constructor     │  │
│  │             │  │                       │  │
│  └──────┬──────┘  └──────────┬───────────┘  │
│         │                     │              │
└─────────┼─────────────────────┼──────────────┘
          │                     │
          ▼                     ▼
┌──────────────────┐  ┌─────────────────────┐
│ DATA LAYER       │  │ IG INDEX API        │
│                  │  │                     │
│ Alpha Vantage /  │  │ • POST /session     │
│ Polygon.io       │  │ • POST /positions   │
│ → Daily OHLCV    │  │ • POST /workingorders│
│ → Universe scan  │  │ • PUT /positions    │
│                  │  │ • GET /positions    │
│ FMP / EODHD      │  │ • GET /confirms     │
│ → Earnings cal   │  │ • GET /prices (intraday)│
│                  │  │ • Lightstreamer     │
│ ORTEX / Fintel   │  │   → Real-time BID/ASK│
│ → Short interest │  │   → Trade updates   │
│ → Days to cover  │  │                     │
│                  │  │ GET /clientsentiment │
│ IG Sentiment     │  │ → Long/short %      │
│ (via IG API)     │  │                     │
└──────────────────┘  └─────────────────────┘
          │                     │
          └──────────┬──────────┘
                     ▼
              ┌──────────────┐
              │  Audit Log   │
              │  (JSON / DB) │
              └──────────────┘
```

---

## 8. IG API Endpoint Reference (For Specification)

### 8.1 Authentication

```
POST /session
Headers: X-IG-API-KEY: {api_key}
Body: { "identifier": "username", "password": "password" }
Response: CST and X-SECURITY-TOKEN in headers

All subsequent calls include:
    X-IG-API-KEY: {api_key}
    CST: {cst_token}
    X-SECURITY-TOKEN: {security_token}
```

### 8.2 Market Data

```
GET /markets/{epic}
    → Market details, instrument info, snapshot price

GET /markets?searchTerm={query}
    → Search for markets by name/keyword, returns epics

GET /marketnavigation
GET /marketnavigation/{nodeId}
    → Browse market categories (useful for building universe)

GET /prices/{epic}/{resolution}/{startDate}/{endDate}
    → Historical OHLCV bars
    → Resolutions: MINUTE, MINUTE_5, MINUTE_15, HOUR, DAY, WEEK, MONTH
    → Returns: openPrice, highPrice, lowPrice, closePrice (bid/ask/mid), lastTradedVolume
    → Date format: 2026-03-27T00:00:00

GET /clientsentiment/{marketId}
    → { longPositionPercentage, shortPositionPercentage }
```

### 8.3 Dealing — Positions

```
POST /positions/otc
    → Open new position
    Body: {
        "epic": "KA.D.VOD.CASH.IP",
        "direction": "BUY" | "SELL",
        "size": 100,
        "orderType": "MARKET" | "LIMIT",
        "level": 125.50,                   // for LIMIT orders
        "stopDistance": 500,                // in points
        "limitDistance": 1500,              // in points
        "stopLevel": 120.00,               // alternative to stopDistance
        "limitLevel": 135.00,              // alternative to limitDistance
        "guaranteedStop": false,
        "trailingStop": false,
        "trailingStopIncrement": 50,       // if trailingStop = true
        "forceOpen": true,
        "currencyCode": "GBP",
        "expiry": "DFB",                   // Daily Funded Bet / - for CFDs
        "timeInForce": "EXECUTE_AND_ELIMINATE" | "FILL_OR_KILL"
    }
    Response: { "dealReference": "..." }

GET /confirms/{dealReference}
    → Confirm fill: dealId, dealStatus, level, size, profit, reason

PUT /positions/otc/{dealId}
    → Modify stop/limit on existing position
    Body: { "stopLevel": 122.00, "limitLevel": 140.00, "trailingStop": false }

DELETE /positions/otc
    → Close position
    Body: { "dealId": "...", "direction": "SELL", "size": 100, "orderType": "MARKET" }
```

### 8.4 Dealing — Working Orders (Pending Entry Orders)

```
POST /workingorders/otc
    → Create pending entry order
    Body: {
        "epic": "KA.D.VOD.CASH.IP",
        "direction": "BUY" | "SELL",
        "size": 100,
        "type": "LIMIT" | "STOP",
        "level": 130.00,                   // trigger price
        "stopDistance": 500,                // attached stop (in points)
        "limitDistance": 1500,              // attached limit (in points)
        "guaranteedStop": false,
        "forceOpen": true,
        "currencyCode": "GBP",
        "expiry": "DFB",
        "timeInForce": "GOOD_TILL_CANCELLED" | "GOOD_TILL_DATE",
        "goodTillDate": "2026-04-30T00:00:00"
    }

GET /workingorders
    → List all pending working orders

PUT /workingorders/otc/{dealId}
    → Modify a working order

DELETE /workingorders/otc/{dealId}
    → Cancel a working order
```

### 8.5 Account

```
GET /accounts
    → List all accounts (CFD, spread bet, share dealing)

GET /accounts/{accountId}
    → Account details, balance, margin

GET /history/transactions?from={date}&to={date}
    → Transaction history

GET /history/activity?from={date}&to={date}
    → Activity log (orders, fills, amendments)
```

### 8.6 Watchlists

```
GET /watchlists
    → List all watchlists

POST /watchlists
    → Create watchlist: { "name": "Active Signals", "epics": ["KA.D.VOD.CASH.IP", ...] }

PUT /watchlists/{watchlistId}
    → Add epic to watchlist

DELETE /watchlists/{watchlistId}/{epic}
    → Remove epic from watchlist
```

### 8.7 Streaming (Lightstreamer)

```
Subscriptions:
    CHART:{epic}:TICK           → Real-time tick data (BID, OFR, LTP, TTV)
    CHART:{epic}:{scale}        → Candle data (e.g., CHART:KA.D.VOD.CASH.IP:5MINUTE)
    MARKET:{epic}               → Market updates (BID, OFR, HIGH, LOW, MID_OPEN)
    TRADE:{accountId}           → Trade confirmations
    ACCOUNT:{accountId}         → Balance/margin updates

Fields:
    BID, OFR (ask), LTP (last traded price), TTV (tick traded volume)
    UTM (update time), DAY_OPEN_MID, DAY_HIGH, DAY_LOW

Max concurrent subscriptions: 40
```

---

## 9. IG-Specific Order Mapping to Entry Rules

| Entry Rule | IG Order Implementation |
|-----------|----------------------|
| **L-A: VCP Breakout (buy-stop)** | Working order: `type=STOP, level=pivot+1-2%, stopDistance=X, limitDistance=Y` |
| **L-B: Pullback to EMA (limit)** | Working order: `type=LIMIT, level=EMA_value, stopDistance=X` |
| **L-C: BGU — OR breakout** | Working order: `type=STOP, level=opening_range_high` |
| **L-C: BGU — pullback** | Working order: `type=LIMIT, level=MAX(gap_open, VWAP)` |
| **S-A: H&S Breakdown (sell-stop)** | Working order: `direction=SELL, type=STOP, level=neckline-1-2%` |
| **S-B: Rally to EMA (limit short)** | Working order: `direction=SELL, type=LIMIT, level=declining_EMA_value` |
| **Stop-loss (all)** | Attached via `stopDistance` or `stopLevel` on entry, or `PUT /positions` to modify |
| **Guaranteed stop (overnight/volatile)** | `guaranteedStop=true` on position creation (premium charged) |
| **Trailing stop (after move in favour)** | `PUT /positions` to add trailing: `trailingStop=true, trailingStopIncrement=X` |
| **Emergency cover (short 15% adverse)** | Triggered by streaming P&L monitor → `DELETE /positions` (market close) |
| **Earnings auto-exit** | Pre-market check → `DELETE /positions` for any position with earnings next day |

---

## 10. Development Impact

### 10.1 Changes from City Index Spec

| Module | Change Required |
|--------|----------------|
| `auth` | Rewrite for IG's CST/Security-Token auth model |
| `market_data` | Switch to IG endpoints + add external bulk data source |
| `indicators` | No change — still computed locally |
| `gates` | No change — logic is broker-agnostic |
| `entry_classifier` | No change |
| `risk_manager` | No change |
| `orders` | Rewrite for IG's position/working-order model |
| `streaming` | Update for IG's Lightstreamer subscription model |
| `audit_log` | Add IG-specific fields (dealReference, CST) |
| **NEW: `data_layer`** | New module to manage external data sources (Alpha Vantage etc.) |
| **NEW: `sentiment`** | New module to consume IG client sentiment data |

### 10.2 Python Library

IG has a mature open-source Python library: [`trading-ig`](https://github.com/ig-python/trading-ig) (pip installable). This significantly reduces development time vs building raw HTTP calls.

```python
from trading_ig import IGService

ig_service = IGService(username, password, api_key, acc_type="DEMO")
ig_service.create_session()

# Historical prices
prices = ig_service.fetch_historical_prices_by_epic_and_num_points(
    epic="KA.D.VOD.CASH.IP",
    resolution="DAY",
    numpoints=260
)

# Open position with stop
ig_service.create_open_position(
    epic="KA.D.VOD.CASH.IP",
    direction="BUY",
    size=100,
    order_type="MARKET",
    stop_distance=500,
    limit_distance=1500,
    force_open=True,
    currency_code="GBP",
    guaranteed_stop=False
)
```

### 10.3 Demo Account

IG provides a free demo account with full API access. Use for all development and paper trading. The demo API uses a different base URL but identical endpoints.

```
Live:  https://api.ig.com/gateway/deal
Demo:  https://demo-api.ig.com/gateway/deal
```
