# MONEY PROGRAM — TRADING PROGRAM

## Entry Refinement Engine — Technical Specification

**Target Broker: City Index (CIAPI / StoneX Platform API)**

**For Claude Code Implementation**

Version 1.0 | March 2026

---

## 1. Architecture Overview

The Entry Refinement Engine sits between the existing signal engine and the broker execution layer. It receives candidate signals (long and short), runs them through the qualification gates and execution rules defined in the Entry Refinement Masterclass v2, and either places orders via the City Index API or rejects/skips with a documented reason.

```
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  Signal Engine   │────▶│  ENTRY REFINEMENT    │────▶│  City Index     │
│  (existing)      │     │  ENGINE              │     │  CIAPI          │
│                  │     │                      │     │                 │
│  Outputs:        │     │  Gates 1–6           │     │  Endpoints:     │
│  - Ticker        │     │  Entry classification│     │  - /order/new   │
│  - Direction     │     │  Gap handling        │     │  - /order/update│
│  - Entry type    │     │  Position sizing     │     │  - /market/     │
│  - Pivot level   │     │  Order construction  │     │  - /barhistory  │
│  - Base stage    │     │  Audit logging       │     │  - /openpositions│
└─────────────────┘     └──────────────────────┘     └─────────────────┘
                               │         │
                    ┌──────────┘         └──────────┐
                    ▼                                ▼
            ┌──────────────┐              ┌──────────────────┐
            │ Supplementary│              │  Audit Log       │
            │ Data APIs    │              │  (JSON / DB)     │
            │ (see §3.2)  │              │                  │
            └──────────────┘              └──────────────────┘
```

---

## 2. City Index API — Capabilities Assessment

### 2.1 Confirmed Available Endpoints

Based on the CIAPI documentation, Python wrapper implementations, and community references, the following endpoints are confirmed available:

| Endpoint | Method | Purpose | Confirmed |
|----------|--------|---------|-----------|
| `/session` | POST | Authentication — obtain session token | Yes |
| `/session/deleteSession` | POST | Logout / session cleanup | Yes |
| `/useraccount/ClientAndTradingAccount` | GET | Retrieve client ID and trading account IDs | Yes |
| `/margin/clientaccountmargin` | GET | Account margin, balance, equity, P&L | Yes |
| `/market/fullsearchwithtags` | GET | Search markets by tag/keyword (find MarketIds) | Yes |
| `/market/{MarketId}/barhistory` | GET | Historical OHLCV price bars | Yes |
| `/order/newtradeorder` | POST | Place new trade (market order, close position) | Yes |
| `/order/simulate/newtradeorder` | POST | Simulate order without execution (dry run) | Yes |
| `/order/updatetradeorder` | POST | Modify stop-loss / take-profit on open position | Yes |
| `/order/openpositions` | GET | List all currently open positions | Yes |
| `/order/tradehistory` | GET | Retrieve completed trade history | Yes |
| `/order/{orderId}` | GET | Get details of a specific order | Yes |
| Lightstreamer streaming | WS | Real-time streaming price data | Yes |

### 2.2 Price Bar History — Parameters

The `/market/{MarketId}/barhistory` endpoint supports:

| Parameter | Values |
|-----------|--------|
| `interval` | `TICK`, `MINUTE`, `HOUR`, `DAY`, `WEEK` |
| `span` | Multiplier — e.g., span=5 with interval=MINUTE gives 5-min bars |
| `PriceBars` | Number of bars to retrieve |
| `PriceType` | `BID`, `MID`, `ASK` |

This gives us enough to compute all required indicators locally.

### 2.3 Order Types Available

City Index supports the following order types through the platform (confirmed via their trading academy and platform documentation):

| Order Type | Available | Notes |
|------------|-----------|-------|
| Market order | Yes | Immediate execution at current price |
| Limit order | Yes | Execute at specified price or better |
| Stop order | Yes | Trigger at specified price |
| Stop-limit order | Likely | Via stop + limit combination — needs API testing |
| Guaranteed stop | Yes | Closes at exact level even through gaps (premium charged) |
| Trailing stop | Yes | Follows profitable moves |
| OCO (One Cancels Other) | Yes | Stop + limit paired |
| If-Done (contingent) | Yes | Two-step: second order activates when first fills |

### 2.4 Direction and Short Selling

City Index supports both long and short via CFDs and spread bets:

- **Buy (long)** and **Sell (short)** directions available on all CFD markets
- **UK equities**: Shorting is via CFDs or spread bets (no direct short selling of shares)
- **US equities**: Available as CFDs through City Index
- **No separate borrow/locate process** — City Index handles this internally for CFD shorts

### 2.5 Key Limitations and Gaps

| What We Need | City Index Provides? | Gap? | Solution |
|-------------|---------------------|------|----------|
| Daily OHLCV history | Yes — via `/barhistory` with `interval=DAY` | No | Direct from API |
| Intraday 5-min bars | Yes — `interval=MINUTE, span=5` | No | Direct from API |
| Real-time streaming prices | Yes — Lightstreamer | No | Direct streaming |
| Bid-ask spread | Yes — via streaming BID/ASK prices | No | Calculate from streaming |
| MA(50), MA(150), MA(200) | **No** — raw bars only, no indicator calc | **Yes** | Compute locally from bar data |
| EMA(10), EMA(20) | **No** — no built-in indicators | **Yes** | Compute locally |
| ADX(14) | **No** — no built-in indicators | **Yes** | Compute locally from bar data |
| Relative Strength ranking | **No** — no cross-universe ranking | **Yes** | Compute locally across universe |
| 52-week high/low | **No** — not a discrete endpoint | **Yes** | Derive from 260-day bar history |
| VCP / H&S pattern detection | **No** | **Yes** | Existing signal engine or local pattern engine |
| Base / distribution stage count | **No** | **Yes** | Local state tracking |
| Earnings calendar | **No** | **Yes** | Supplementary API required |
| Catalyst calendar (FDA, legal) | **No** | **Yes** | Supplementary API required |
| Short interest % | **No** | **Yes** | Supplementary API required |
| Days to cover | **No** | **Yes** | Supplementary API required |
| Borrow fee / availability | **N/A** — CFD model, no borrow needed | Partial | CI handles borrow; we still need SI% for squeeze check |
| Account balance & margin | Yes — `/margin/clientaccountmargin` | No | Direct from API |
| Open positions & P&L | Yes — `/order/openpositions` | No | Direct from API |
| Place orders with stops | Yes — via order endpoints | No | Direct from API |
| Modify stops on open positions | Yes — `/order/updatetradeorder` | No | Direct from API |
| Order simulation (dry run) | Yes — `/order/simulate/newtradeorder` | No | Excellent for testing |

---

## 3. Data Strategy

### 3.1 City Index as Primary Data Source

City Index will provide:

- **Price data**: All OHLCV history (daily and intraday) via `/barhistory`
- **Streaming prices**: Real-time via Lightstreamer for mid-session volume checks and spread monitoring
- **Account data**: Balance, margin, open positions, trade history
- **Order execution**: All order placement, modification, and cancellation

### 3.2 Supplementary Data Sources Required

The following data is NOT available from City Index and must come from external APIs. These are listed in priority order.

| Data Need | Recommended Source | Alternative | Frequency |
|-----------|--------------------|-------------|-----------|
| Earnings calendar (US) | Alpha Vantage or Financial Modeling Prep (FMP) | Polygon.io | Daily sync |
| Earnings calendar (UK) | FMP or EOD Historical Data (EODHD) | Manual CSV | Daily sync |
| Short interest % (US) | ORTEX API or Fintel | FINRA (bi-monthly, delayed) | Weekly minimum |
| Short interest % (UK) | FCA short disclosure register | ORTEX | Weekly |
| Days to cover | ORTEX (derived from SI + volume) | Compute locally: SI / avg_volume | Weekly |
| Catalyst calendar (FDA, legal) | Biopharmcatalyst (FDA) + news API | Manual entry | Daily sync |
| Sector classification | FMP or Polygon.io | Static mapping table | On demand |

### 3.3 What We Compute Locally

All technical indicators are computed from raw OHLCV bars retrieved from City Index:

```
FROM /barhistory (interval=DAY, PriceBars=260):
    → MA(50), MA(150), MA(200)        — Simple moving averages
    → EMA(10), EMA(20)                — Exponential moving averages
    → ADX(14)                         — Average Directional Index
    → 52-week high, 52-week low       — From 260 daily bars
    → 50-day average volume            — Mean of last 50 volume values
    → RS percentile                    — Price performance rank vs. universe

FROM /barhistory (interval=MINUTE, span=5):
    → Opening range (first 15 min)     — High/low of first 3 × 5-min bars
    → Intraday VWAP                    — Cumulative (price × volume) / volume
    → Projected session volume          — (current_vol / elapsed_mins) × total_mins
```

---

## 4. Module Specification

### 4.1 Module: `ci_auth`

**Purpose:** Manage City Index API authentication.

```
FUNCTIONS:
    login(username, password, app_key) → session_token
    logout(session_token)
    refresh_session(session_token) → new_token (if nearing expiry)
    health_check() → boolean

CONFIG:
    base_url = "https://ciapi.cityindex.com/TradingAPI"
    session_timeout = 20 minutes (refresh at 15)

NOTES:
    - Session token required for all subsequent API calls
    - Passed as query param: ?Username={uid}&Session={token}
    - Implement auto-refresh to prevent mid-operation expiry
```

### 4.2 Module: `ci_market_data`

**Purpose:** Retrieve and cache price data from City Index.

```
FUNCTIONS:
    get_market_id(ticker, market="UK" | "US") → MarketId
        — Uses /market/fullsearchwithtags
        — Cache results (market IDs rarely change)

    get_daily_bars(market_id, num_bars=260) → [OHLCV]
        — GET /market/{id}/barhistory?interval=DAY&span=1&PriceBars=260&PriceType=MID

    get_intraday_bars(market_id, interval_mins=5, num_bars=100) → [OHLCV]
        — GET /market/{id}/barhistory?interval=MINUTE&span=5&PriceBars=100&PriceType=MID

    subscribe_streaming(market_id, callback)
        — Lightstreamer subscription for real-time BID/ASK/MID

CACHING:
    - Daily bars: refresh once per day after market close
    - Intraday bars: refresh every 5 minutes during session
    - Market IDs: cache indefinitely with daily validation
```

### 4.3 Module: `indicators`

**Purpose:** Compute all technical indicators from raw bar data.

```
FUNCTIONS:
    sma(bars, period) → float
        — Simple moving average of close prices

    ema(bars, period) → float
        — Exponential moving average

    adx(bars, period=14) → float
        — Average Directional Index
        — Requires: True Range, +DI, -DI, smoothed DX

    relative_strength(ticker_return, universe_returns) → percentile (0–100)
        — 6-month price change ranked against full universe

    fifty_two_week_high(bars_260) → float
    fifty_two_week_low(bars_260) → float

    avg_volume(bars, period=50) → float
        — Mean volume over last N sessions

    vwap(intraday_bars) → float
        — Volume-weighted average price (intraday only)

    opening_range(intraday_bars_5min, num_bars=3) → {high, low}
        — High and low of first 15 minutes (3 × 5-min bars)

    projected_volume(current_volume, elapsed_minutes, session_minutes) → float

ALL INDICATORS:
    - Must be deterministic (same input → same output)
    - Must handle missing bars gracefully (skip gaps)
    - Must be unit-tested against known reference values
```

### 4.4 Module: `gates`

**Purpose:** Implement the qualification gates from the Masterclass.

```
FUNCTION: evaluate_long_gates(ticker, bars_daily, bars_intraday, supplementary_data) → GateResult

    Gate L1: Trend Template (10 conditions)
        conditions = [
            close > sma(bars, 150),
            close > sma(bars, 200),
            sma(bars, 150) > sma(bars, 200),
            sma(bars, 200) > sma(bars[-22:], 200),  # trending up 1 month
            sma(bars, 50) > sma(bars, 150),
            sma(bars, 50) > sma(bars, 200),
            close > sma(bars, 50),
            close >= fifty_two_week_low(bars) * 1.25,
            close >= fifty_two_week_high(bars) * 0.75,
            relative_strength(ticker) >= 70,
        ]
        IF NOT all(conditions): return REJECT(R01, failed_conditions)

    Gate L2: ADX > 25
        IF adx(bars, 14) <= 25: return REJECT(R02, adx_value)

    Gate L3: Volume dry-up
        dry_count = count(v < avg_volume(bars, 50) * 0.60 for v in last_5_volumes)
        IF dry_count < 2: return REJECT(R03, dry_count)

    return PASS(all gate values for logging)


FUNCTION: evaluate_short_gates(ticker, bars_daily, entry_type, supplementary_data) → GateResult

    IF entry_type == "S-D":
        Gate S1-ALT: Climax Top (5 conditions)
        — See Masterclass Rule S1-ALT
    ELSE:
        Gate S1: Inverse Trend Template (10 conditions — all inverted)

    Gate S2: ADX > 25

    Gate S3: Distribution days
        dist_count = count(
            close[i] < close[i-1] AND volume[i] > avg_volume * 1.25
            for i in last_10_sessions
        )
        IF dist_count < 3: return REJECT(R12, dist_count)

    Gate S4: Squeeze check (from supplementary data)
        IF short_interest_pct > 20: return REJECT(R13)
        IF days_to_cover > 5: return REJECT(R14)
        — Note: borrow check N/A for City Index CFDs

    return PASS(all gate values for logging)
```

### 4.5 Module: `entry_classifier`

**Purpose:** Determine entry type and apply gap/execution rules.

```
FUNCTION: classify_and_execute(signal, bars, gates_result) → OrderInstruction | Skip | Reject

    1. Classify entry type (L-A through L-E, S-A through S-E)
       — Based on signal engine output + current price action

    2. Check for gap (> 2% from prior close)
       IF gapped:
           IF long AND gap_up:
               IF base_stage <= 2: apply BGU protocol (Rule L9)
               ELSE: return REJECT(R04, "late-stage exhaustion gap")
           IF long AND gap_down:
               return MONITOR(Rule L10, 3-day reclaim window)
           IF short AND gap_down:
               IF late_stage: apply SGD protocol (Rule S9)
               ELSE: return REJECT(R19, "early-stage shakeout risk")
           IF short AND gap_up:
               return MONITOR(Rule S10, 3-day reclaim window)

    3. Check chase rule
       IF long AND open > pivot * 1.03 AND NOT gap_classified:
           return SKIP(R08)
       IF short AND open < breakdown * 0.97 AND NOT gap_classified:
           return SKIP(R08)

    4. Calculate position size (see §4.6)

    5. Construct order instruction
       return OrderInstruction(
           direction, entry_type, order_type, price, stop, limit,
           tranche_1_size, tranche_2_trigger, risk_amount
       )
```

### 4.6 Module: `risk_manager`

**Purpose:** Position sizing, risk budget enforcement, and continuous monitoring.

```
FUNCTION: calculate_position(entry_price, stop_price, direction, portfolio_value, open_positions) → PositionResult

    risk_per_share = abs(entry_price - stop_price)
    risk_amount = portfolio_value * 0.01  # 1% risk
    shares = floor(risk_amount / risk_per_share)

    # Maximum single position check
    max_long = portfolio_value * 0.10
    max_short = portfolio_value * 0.08
    max_position = max_long if direction == LONG else max_short
    IF shares * entry_price > max_position:
        shares = floor(max_position / entry_price)

    # Total open risk check
    total_open_risk = sum(pos.risk_amount for pos in open_positions)
    IF total_open_risk + (shares * risk_per_share) > portfolio_value * 0.06:
        return SKIP(R10, "portfolio risk budget exhausted")

    # Short exposure cap
    IF direction == SHORT:
        total_short_exposure = sum(pos.value for pos in open_positions if pos.direction == SHORT)
        IF total_short_exposure + (shares * entry_price) > portfolio_value * 0.50:
            return SKIP(R16, "short exposure cap reached")

    # Stop distance check
    IF risk_per_share / entry_price > 0.08:
        return SKIP(R05, "stop distance exceeds 8%")

    # Overnight gap check
    gap_risk_shares = (portfolio_value * 0.01) / (entry_price * 0.10)
    shares = min(shares, floor(gap_risk_shares))

    # UK spread adjustment
    IF market == UK AND spread_pct > 0.005:
        return SKIP(R06, "spread too wide")
    IF market == UK AND spread_pct > 0.003:
        shares = floor(shares * 0.75)

    # Tranche split
    tranche_1 = floor(shares * 0.60)
    tranche_2 = shares - tranche_1  # remaining 40%

    return PositionResult(shares, tranche_1, tranche_2, risk_amount, stop_price)


FUNCTION: continuous_monitor(open_positions, earnings_calendar, catalyst_calendar)
    — Runs every session (or continuously if streaming)
    — Implements Rules 11, S11, and 12
    — Auto-exits before earnings
    — Emergency covers shorts at 15% adverse
    — Trims positions exceeding overnight gap limits
    — Flags UK positions with spread > 0.5%
```

### 4.7 Module: `ci_orders`

**Purpose:** Translate OrderInstructions into City Index API calls.

```
FUNCTION: place_order(instruction, session, trading_account_id) → OrderResult

    # Construct the order body for City Index
    order_body = {
        "MarketId": instruction.market_id,
        "Direction": "buy" if instruction.direction == LONG else "sell",
        "Quantity": instruction.tranche_1_size,
        "TradingAccountId": trading_account_id,
        "PositionMethodId": 1,  # LongOrShortOnly
    }

    # Order type mapping
    MATCH instruction.order_type:
        CASE "market":
            order_body["Type"] = "market"
        CASE "limit":
            order_body["Type"] = "limit"
            order_body["Price"] = instruction.limit_price
        CASE "stop":
            order_body["Type"] = "stop"
            order_body["TriggerPrice"] = instruction.stop_trigger
        CASE "stop_limit":
            # City Index may require If-Done or OCO for this
            # Test and confirm during integration
            order_body["Type"] = "stop"
            order_body["TriggerPrice"] = instruction.stop_trigger

    # Attach stop-loss via If-Done or separate order
    # City Index supports attaching stops to orders
    order_body["IfDone"] = [{
        "Stop": {
            "TriggerPrice": instruction.stop_loss,
            "Direction": "sell" if instruction.direction == LONG else "buy",
            "Quantity": instruction.tranche_1_size,
        }
    }]

    # Simulate first
    sim_result = POST /order/simulate/newtradeorder, body=order_body
    IF sim_result.status != "OK":
        return FAIL(sim_result.error)

    # Execute
    result = POST /order/newtradeorder, body=order_body
    return OrderResult(order_id, fill_price, status)


FUNCTION: modify_stop(order_id, new_stop_price, session) → ModifyResult
    — PUT /order/updatetradeorder
    — Used for: trailing stops, tightening to breakeven, emergency adjustments

FUNCTION: close_position(order_id, session) → CloseResult
    — POST /order/newtradeorder with close=true
    — Used for: volume confirmation failure, earnings auto-exit, emergency cover

FUNCTION: get_open_positions(session) → [Position]
    — GET /order/openpositions
    — Returns all open positions with unrealised P&L
```

### 4.8 Module: `audit_log`

**Purpose:** Log every decision with full context.

```
FUNCTION: log_decision(decision) → void

    entry = {
        timestamp: ISO-8601,
        signal_id: unique_id,
        ticker: string,
        market: "US" | "UK",
        direction: "LONG" | "SHORT",
        entry_type: "L-A" | ... | "S-E",
        decision: "ENTER" | "SKIP" | "REJECT",
        reason_code: "R01"–"R19" | null,
        gates: { ... all gate values ... },
        levels: { pivot, entry, stop, risk_per_share, position_size, risk_amount },
        volume: { trigger_vol, avg_50d, ratio },
        tranche: { t1_size, t2_eligible, t2_size },
        short_specific: { si_pct, dtc, borrow_fee } | null,
        uk_specific: { spread_pct, sdrt, cfd } | null,
        ci_order: { order_id, sim_result, fill_price } | null,
    }

    STORE to:
        - Local JSON log file (append)
        - Database table (for querying and analytics)

STORAGE:
    - One JSON file per trading day: audit_2026-03-27.json
    - Database: SQLite for local, PostgreSQL for production
    - Retention: indefinite (all history feeds back into signal refinement)
```

---

## 5. Execution Sequence

### 5.1 Daily Pre-Market Routine (Automated)

```
06:00 GMT (UK pre-market) / 07:00 ET (US pre-market):

1. ci_auth.login()
2. ci_market_data.refresh_daily_bars(universe)     — 260 daily bars per ticker
3. indicators.compute_all(universe)                 — MAs, ADX, RS, volume
4. supplementary.sync_earnings_calendar()           — Flag any positions at risk
5. supplementary.sync_short_interest()              — Weekly, or on sync day
6. risk_manager.continuous_monitor(open_positions)  — Pre-earnings exits, gap sizing
7. LOG pre-market state
```

### 5.2 Signal Processing (Event-Driven)

```
WHEN signal_received(signal):
    1. Determine direction (LONG / SHORT)
    2. Retrieve cached bars and indicators for ticker
    3. IF direction == LONG:
           gates_result = gates.evaluate_long_gates(...)
       ELSE:
           gates_result = gates.evaluate_short_gates(...)
    4. IF gates_result == REJECT:
           audit_log.log_decision(REJECT, reason)
           RETURN
    5. order_instruction = entry_classifier.classify_and_execute(signal, bars, gates_result)
    6. IF order_instruction is SKIP or REJECT:
           audit_log.log_decision(decision, reason)
           RETURN
    7. position = risk_manager.calculate_position(...)
    8. IF position is SKIP:
           audit_log.log_decision(SKIP, reason)
           RETURN
    9. result = ci_orders.place_order(order_instruction, session)
    10. audit_log.log_decision(ENTER, result)
    11. Schedule mid-session volume check
    12. Schedule tranche-2 eligibility monitor
```

### 5.3 Mid-Session Monitoring (Scheduled)

```
11:30 ET (US) / 12:15 GMT (UK):

FOR each position entered today:
    projected_vol = indicators.projected_volume(...)
    IF projected_vol < avg_volume_50d * 1.40:
        ci_orders.reduce_position(position, 0.50)
        audit_log.log(PILOT, "volume unconfirmed")

AT market close:
    FOR each pilot position:
        IF actual_volume < avg_volume_50d * 1.40:
            ci_orders.close_position(position)
            audit_log.log(CLOSED, R07, "volume confirmation failed")
```

### 5.4 Tranche 2 Monitoring (Ongoing)

```
FOR each position with tranche_2_pending:
    IF tranche_1.unrealised_pnl > 0:
        IF first_pullback_to_ema_held OR continuation_breakout:
            ci_orders.place_order(tranche_2_instruction)
            audit_log.log(T2_PLACED)
    ELSE:
        tranche_2 = BLOCKED
        audit_log.log(T2_BLOCKED, "T1 underwater")
```

---

## 6. City Index Integration Notes

### 6.1 Authentication Flow

```python
# Login
POST https://ciapi.cityindex.com/TradingAPI/session
Body: { "UserName": "...", "Password": "...", "AppKey": "..." }
Response: { "Session": "token-string" }

# All subsequent calls include:
?Username={username}&Session={token}

# Session refresh: re-login before 20-minute timeout
# Implement: heartbeat every 15 minutes
```

### 6.2 Market ID Resolution

City Index uses numeric MarketIds, not ticker symbols. Resolution is required:

```python
# Search for a market
GET /market/fullsearchwithtags?maxResults=10&query=AAPL&tagId=0

# Response includes MarketId, Name, and market details
# Cache the mapping: { "AAPL": 12345, "VOD.L": 67890 }

# IMPORTANT: CFD market IDs differ from spread bet market IDs
# Use the correct trading account type
```

### 6.3 Order Placement Considerations

```
IMPORTANT INTEGRATION NOTES:

1. STOP-LIMIT ORDERS: City Index may not support native stop-limit via API.
   FALLBACK: Use a stop order with a limit price field, or implement as
   two-step If-Done order. Must be tested during integration.

2. IF-DONE ORDERS: Supported — the stop-loss is attached to the entry order
   as an If-Done child. When the entry fills, the stop activates automatically.

3. ORDER SIMULATION: The /order/simulate/newtradeorder endpoint is extremely
   valuable. Use it for EVERY order before live execution during the first
   month of operation. Log both sim and live results for comparison.

4. GUARANTEED STOPS: Available for a premium. Consider using for:
   - Positions held overnight in volatile markets
   - UK AIM stocks with wide spreads
   - Any position approaching earnings that wasn't caught by auto-exit

5. CFD vs SPREAD BET: Different TradingAccountIds.
   - UK tax residents: spread bets are tax-free on gains
   - Non-UK: CFDs are standard
   - API calls are identical; only the account ID changes
```

### 6.4 Rate Limits and Error Handling

```
RATE LIMITS (estimated — not officially documented):
    - Price bar requests: ~1 per second per market
    - Order requests: ~1 per second
    - Market search: ~5 per minute

ERROR HANDLING:
    - HTTP 401: Session expired → auto-refresh and retry
    - HTTP 429: Rate limited → exponential backoff (1s, 2s, 4s, max 30s)
    - HTTP 500: Server error → retry once, then alert
    - Network timeout: 10-second timeout, retry once, then alert
    - All errors logged with full request/response for debugging
```

---

## 7. Gap Analysis — What City Index Cannot Do

| Requirement | Gap | Impact | Mitigation |
|-------------|-----|--------|-----------|
| Technical indicators (MA, EMA, ADX) | No server-side calculation | Low — standard computation | Compute locally from bar data. Well-understood algorithms. |
| Relative Strength ranking | No cross-universe ranking | Low | Compute locally. Requires daily scan of full universe (~1000 tickers). |
| Earnings calendar | Not provided | **High** — Rule 11 depends on this for auto-exit | Integrate FMP or Alpha Vantage API. Critical path. |
| Short interest % | Not provided | **High** — Gate S4 depends on this for squeeze check | Integrate ORTEX or Fintel API. Critical for short safety. |
| Days to cover | Not provided | Medium | Derive from SI% / avg volume, or get from ORTEX. |
| Catalyst calendar (FDA, legal) | Not provided | Medium | Biopharmcatalyst for FDA. Manual entry for others initially. |
| VCP / pattern detection | Not provided | Low | Existing signal engine handles this. |
| Stop-limit order (native) | Uncertain | Medium | Test during integration. Fallback: If-Done orders. |
| Historical volume on bar data | Unconfirmed if volume included in bars | **Medium** | Test immediately during integration. If missing, need supplementary source. |

### 7.1 Critical Path Items (Must Resolve Before Go-Live)

1. **Confirm volume data in bar history** — If `/barhistory` does not return volume, the entire indicator suite (volume dry-up, distribution days, volume confirmation) breaks. Test first.
2. **Earnings calendar integration** — Without this, Rule 11 (never hold through earnings) cannot function. This is a safety-critical rule.
3. **Short interest data source** — Without this, no short trades can be placed (Gate S4 fails by default). Integrate ORTEX or Fintel.
4. **Stop-limit order testing** — Confirm the exact API mechanism for attaching stops to entries.

---

## 8. Development Phases

### Phase 1: Foundation (Week 1–2)

- `ci_auth` module — login, session management, heartbeat
- `ci_market_data` module — bar history retrieval, market ID resolution
- `indicators` module — all technical indicator calculations
- Unit tests for all indicators against known reference values
- Confirm volume data availability in bar history

### Phase 2: Gates & Classification (Week 3–4)

- `gates` module — long gates (L1–L3) and short gates (S1–S4)
- `entry_classifier` module — type classification, gap handling
- Integration with supplementary data APIs (earnings, short interest)
- End-to-end gate evaluation tests with historical signals

### Phase 3: Risk & Orders (Week 5–6)

- `risk_manager` module — position sizing, risk budget, continuous monitor
- `ci_orders` module — order placement, modification, simulation
- `audit_log` module — full logging schema
- Paper trading mode using `/order/simulate/newtradeorder`

### Phase 4: Integration & Paper Trading (Week 7–8)

- Full pipeline: signal → gates → classify → risk → order → log
- 2 weeks of paper trading (simulation mode only)
- Compare simulated results against what the signal engine would have produced
- Tune any thresholds based on rejection rate analysis

### Phase 5: Live Trading (Week 9+)

- Switch from simulate to live order execution
- Start with 25% of target position sizes
- Monitor for 2 weeks
- Scale to 50%, then 100% based on audit log review

---

## 9. File Structure (Claude Code Project)

```
money-program-trading/
├── src/
│   ├── auth/
│   │   └── ci_auth.py
│   ├── data/
│   │   ├── ci_market_data.py
│   │   ├── supplementary.py          # Earnings, SI%, catalysts
│   │   └── cache.py                  # Local data caching
│   ├── indicators/
│   │   ├── moving_averages.py
│   │   ├── adx.py
│   │   ├── volume.py
│   │   ├── relative_strength.py
│   │   └── intraday.py               # VWAP, opening range, projected vol
│   ├── engine/
│   │   ├── gates.py                  # L1–L3, S1–S4
│   │   ├── entry_classifier.py       # Type classification + gap rules
│   │   ├── risk_manager.py           # Position sizing + continuous monitor
│   │   └── pipeline.py               # Main orchestrator
│   ├── broker/
│   │   ├── ci_orders.py              # Order placement + modification
│   │   └── ci_streaming.py           # Lightstreamer integration
│   ├── logging/
│   │   └── audit_log.py
│   └── config/
│       ├── settings.py               # API keys, thresholds, universe
│       └── rejection_codes.py        # R01–R19 definitions
├── tests/
│   ├── test_indicators.py
│   ├── test_gates.py
│   ├── test_risk_manager.py
│   ├── test_entry_classifier.py
│   └── test_ci_orders.py
├── data/
│   ├── universe_us.csv               # Ticker list — US equities
│   ├── universe_uk.csv               # Ticker list — UK equities
│   └── market_id_cache.json          # City Index MarketId mappings
├── logs/
│   └── audit_YYYY-MM-DD.json
├── docs/
│   ├── Entry_Refinement_Masterclass_v2.md
│   └── Entry_Rules_Desk_Reference.html
├── requirements.txt
├── CLAUDE.md                         # Claude Code project instructions
└── README.md
```

---

## 10. CLAUDE.md — Project Instructions for Claude Code

The following should be placed in the project's `CLAUDE.md` to guide Claude Code when working on this codebase:

```markdown
# Money Program — Trading Program — Entry Refinement Engine

## What This Is
An automated entry refinement system for swing trading US and UK equities.
It sits between a signal engine and City Index (CIAPI) broker execution.

## Architecture
Signal → Gates (qualify) → Classify (entry type) → Risk (position size) → Order (execute) → Log

## Key Rules
- Every trade must pass ALL qualification gates before execution
- Long trades: 3 gates (Trend Template, ADX > 25, Volume dry-up)
- Short trades: 4 gates (Inverse Template, ADX > 25, Distribution days, Squeeze check)
- Maximum 1% risk per trade, 6% total portfolio risk
- Never hold through earnings — auto-exit required
- Short positions auto-cover at 15% adverse move
- UK trades: limit orders only for spreads > 0.3%, skip if > 0.5%

## City Index API
- Base URL: https://ciapi.cityindex.com/TradingAPI
- Auth: POST /session → session token in all subsequent calls
- Price data: GET /market/{id}/barhistory
- Orders: POST /order/newtradeorder
- Simulation: POST /order/simulate/newtradeorder (use for testing)
- Positions: GET /order/openpositions

## Technical Indicators
ALL computed locally from raw OHLCV bars. No external indicator service.
MA(50,150,200), EMA(10,20), ADX(14), RS percentile, 52wk high/low, avg volume.

## Testing
- All indicator calculations must be unit-tested against known values
- Gate evaluation must be tested with synthetic and historical data
- Order placement must go through simulation endpoint first
- Paper trade for 2 weeks minimum before live execution

## Supplementary APIs
- Earnings calendar: FMP or Alpha Vantage (critical — safety rule)
- Short interest: ORTEX or Fintel (required for short gate S4)
- Catalyst calendar: Biopharmcatalyst (FDA) + manual for others

## Rejection Codes
R01–R19 defined in src/config/rejection_codes.py
Every decision (enter, skip, reject) must be logged with full context.
```
