# MONEY PROGRAM — IG + ProRealTime Analysis

## Does ProRealTime Change the Recommendation?

**Yes — significantly.** ProRealTime via IG is a potential game-changer for this project. It provides three capabilities that solve major gaps in our architecture.

Version 1.0 | March 2026

---

## 1. What ProRealTime Brings to the Table

### 1.1 Server-Side Everything

ProRealTime runs 100% server-side. This is not "indicators computed in your browser" — it's dedicated infrastructure hosted by ProRealTime's servers. When you run a ProOrder automated trading system, it executes even when your computer is off, your internet is down, or your power fails. This is exactly the hands-off automation requirement we specified.

### 1.2 The Three Killer Features

| Feature | What It Does | What It Solves For Us |
|---------|-------------|----------------------|
| **ProScreener** | Server-side market scanning with custom code. Scans entire markets in real-time against your criteria. | Replaces our need for a local universe scanner. The Trend Template, ADX filter, and volume dry-up checks could all run as ProScreener scans — server-side, 24/7, no data caps. |
| **ProOrder** | Automated trading system execution. Runs on PRT servers. Executes via IG. | The entire entry refinement engine could run as a ProOrder system — gates, classification, order placement, stop management — all server-side, all automated. |
| **ProBuilder** | Programming language with 200+ built-in functions including all the indicators we need. | Eliminates the need to compute MA, EMA, ADX, volume metrics, RS locally. All built-in. |

### 1.3 Complete Built-In Indicator Library

Every indicator our system requires is a native ProBuilder function:

| Our Requirement | ProBuilder Function | Notes |
|----------------|-------------------|-------|
| MA(50, 150, 200) | `Average[N](close)` or `SimpleAverage[N]` | Built-in, any period |
| EMA(10, 20) | `ExponentialAverage[N](close)` | Built-in |
| ADX(14) | `ADX[14]` | Built-in, returns the value directly |
| +DI / -DI | `DIplus[14]`, `DIminus[14]` | Built-in components |
| 52-week high | `Highest[260](high)` | Built-in lookback function |
| 52-week low | `Lowest[260](low)` | Built-in lookback function |
| Volume | `Volume` | Direct access, every bar |
| 50-day avg volume | `Average[50](Volume)` | Combine built-ins |
| RSI | `RSI[14](close)` | Built-in |
| VWAP | Computable from price × volume | Standard calc |
| Projected volume | `EstimatedVolume` | Built-in function |

**This eliminates the entire `indicators` module from our architecture.** No local computation needed.

---

## 2. ProScreener — Server-Side Universe Scanning

### 2.1 How It Works

ProScreener lets you write custom scans in ProBuilder code that run server-side against any market. You can scan FTSE 100, FTSE 250, AIM, S&P 500, NASDAQ, etc.

### 2.2 Our Trend Template as a ProScreener

The long-side Trend Template (Rule L1) translates directly to ProScreener:

```
// MONEY PROGRAM — Long Trend Template Scan
// Scans for stocks passing all 10 conditions

myMA50 = Average[50](close)
myMA150 = Average[150](close)
myMA200 = Average[200](close)
myMA200_1mo = Average[200](close)[22]  // 200 MA one month ago
my52wkHigh = Highest[260](high)
my52wkLow = Lowest[260](low)

c1 = close > myMA150
c2 = close > myMA200
c3 = myMA150 > myMA200
c4 = myMA200 > myMA200_1mo          // 200 MA trending up
c5 = myMA50 > myMA150
c6 = myMA50 > myMA200
c7 = close > myMA50
c8 = close >= my52wkLow * 1.25      // 25% above 52-week low
c9 = close >= my52wkHigh * 0.75     // within 25% of 52-week high
c10 = ADX[14] > 25                   // Gate 2 combined

SCREENER[c1 AND c2 AND c3 AND c4 AND c5 AND c6 AND c7 AND c8 AND c9 AND c10] (ADX[14] AS "ADX")
```

This would run server-side, continuously, against the full market. No data caps. No local computation. No weekly allowance issues.

### 2.3 Short-Side Inverse Template as ProScreener

```
// MONEY PROGRAM — Short Inverse Trend Template Scan
myMA50 = Average[50](close)
myMA150 = Average[150](close)
myMA200 = Average[200](close)
myMA200_1mo = Average[200](close)[22]
my52wkHigh = Highest[260](high)
my52wkLow = Lowest[260](low)

c1 = close < myMA150
c2 = close < myMA200
c3 = myMA150 < myMA200
c4 = myMA200 < myMA200_1mo          // 200 MA trending down
c5 = myMA50 < myMA150
c6 = myMA50 < myMA200
c7 = close < myMA50
c8 = close <= my52wkHigh * 0.75     // 25% below 52-week high
c9 = close <= my52wkLow * 1.25      // within 25% of 52-week low
c10 = ADX[14] > 25

SCREENER[c1 AND c2 AND c3 AND c4 AND c5 AND c6 AND c7 AND c8 AND c9 AND c10] (ADX[14] AS "ADX")
```

### 2.4 Volume Dry-Up Scan (Gate 3)

```
// MONEY PROGRAM — Volume Dry-Up Filter
avgVol = Average[50](Volume)
threshold = avgVol * 0.60

dry1 = Volume[1] < threshold
dry2 = Volume[2] < threshold
dry3 = Volume[3] < threshold
dry4 = Volume[4] < threshold
dry5 = Volume[5] < threshold

dryCount = dry1 + dry2 + dry3 + dry4 + dry5

SCREENER[dryCount >= 2] (dryCount AS "Dry Sessions")
```

---

## 3. ProOrder — Server-Side Automated Execution

### 3.1 How It Works

ProOrder executes trading strategies written in ProBuilder. The code runs on ProRealTime's servers and sends orders directly to IG. It runs 24/7 even when your platform is closed.

### 3.2 Key Capabilities

| Feature | Detail |
|---------|--------|
| Order types | `BUY`, `SELL`, `SELLSHORT`, `EXITSHORT` with `AT MARKET`, `AT price LIMIT`, `AT price STOP` |
| Stop-loss | `SET STOP LOSS x` (in points or price) |
| Take-profit | `SET TARGET PROFIT x` |
| Trailing stop | `SET STOP TRAILING x` |
| Position sizing | Quantity specified in code, capped by max position parameter |
| Max systems | **Up to 100 simultaneous** on Premium |
| One system per instrument | Yes — but you can contact IG to allow multiple per instrument |
| Evaluation frequency | Code evaluated at **each candle close** (not tick-by-tick for strategies) |
| Hedging | Each system is independent; opposite directions across systems are allowed with Force Open |

### 3.3 Example: VCP Breakout Entry (Rule L4 + L5 + L7)

```
// MONEY PROGRAM — VCP Breakout Entry System
// Runs on: a specific instrument identified by ProScreener
// Timeframe: Daily bars

DEFPARAM CumulateOrders = false  // No pyramiding by default

// ── GATE 1: Trend Template ──
myMA50 = Average[50](close)
myMA150 = Average[150](close)
myMA200 = Average[200](close)
trendOK = close > myMA50 AND close > myMA150 AND close > myMA200
trendOK = trendOK AND myMA50 > myMA150 AND myMA150 > myMA200
trendOK = trendOK AND myMA200 > myMA200[22]
trendOK = trendOK AND close >= Lowest[260](low) * 1.25
trendOK = trendOK AND close >= Highest[260](high) * 0.75

// ── GATE 2: ADX ──
adxOK = ADX[14] > 25

// ── GATE 3: Volume dry-up ──
avgVol = Average[50](Volume)
dryCount = 0
FOR i = 1 TO 5
    IF Volume[i] < avgVol * 0.60 THEN
        dryCount = dryCount + 1
    ENDIF
NEXT
volDryOK = dryCount >= 2

// ── ALL GATES PASS ──
allGates = trendOK AND adxOK AND volDryOK

// ── ENTRY: Pivot breakout with volume confirmation ──
// pivotLevel would be set externally or calculated from recent highs
pivotLevel = Highest[20](high)[1]  // Simplified: highest high of last 20 bars
breakoutVol = Volume > avgVol * 1.40

IF allGates AND close > pivotLevel AND breakoutVol AND NOT ONMARKET THEN
    BUY 1 CONTRACT AT MARKET
    SET STOP LOSS pivotLevel * 0.07  // 7% below entry (in price distance)
ENDIF

// ── VOLUME MID-SESSION CHECK ──
// Note: ProOrder evaluates at candle close on daily, so intraday checks
// require running a secondary system on 5-minute bars

// ── TRAILING STOP after 3:1 profit ──
IF ONMARKET AND close > POSITIONPRICE * 1.21 THEN
    SET STOP TRAILING 3%
ENDIF
```

### 3.4 ProOrder Limitations We Must Work Around

| Limitation | Impact | Workaround |
|-----------|--------|-----------|
| **One system per instrument** | Can't run long + short systems on the same stock | Contact IG to enable multiple systems, or use a combined long/short system |
| **Evaluation at candle close (daily)** | Mid-session volume checks can't run on the daily timeframe | Run a secondary 5-minute system for intraday monitoring |
| **No external data access** | Can't call earnings calendars, ORTEX, etc. from ProBuilder | Hybrid approach: use IG REST API + Python for supplementary data checks, ProOrder for execution |
| **No inter-system communication** | One ProOrder system can't signal another | Use the Python/REST layer as the orchestrator |
| **Position sizing is in contracts/lots** | No direct portfolio-percentage sizing | Calculate the correct lot size externally, pass as a parameter or hard-code |
| **Limited string/data handling** | ProBuilder is a trading language, not a general-purpose language | Keep complex logic in Python; use ProOrder for execution only |
| **No native RS percentile ranking** | RSI exists, but cross-universe relative strength ranking doesn't | Compute RS externally in Python, pass qualifying tickers to ProOrder |

---

## 4. Revised Architecture: Hybrid IG REST + ProRealTime

The optimal architecture uses both the IG REST API and ProRealTime, each for what it does best.

```
┌──────────────────────────────────────────────────────────┐
│                    PYTHON ORCHESTRATOR                     │
│                    (runs on your server)                   │
│                                                           │
│  ┌───────────────┐  ┌─────────────┐  ┌────────────────┐ │
│  │ Signal Engine  │  │ RS Ranking   │  │ Supplementary  │ │
│  │ (existing)     │  │ (universe)   │  │ Data           │ │
│  │               │  │              │  │ • Earnings cal │ │
│  │               │  │              │  │ • Short interest│ │
│  │               │  │              │  │ • Catalyst cal  │ │
│  └───────┬───────┘  └──────┬──────┘  └───────┬────────┘ │
│          │                  │                  │          │
│          └──────────────────┼──────────────────┘          │
│                             │                             │
│                    ┌────────┴────────┐                    │
│                    │ Risk Manager     │                    │
│                    │ (position sizing,│                    │
│                    │  portfolio risk,  │                    │
│                    │  earnings check)  │                    │
│                    └────────┬────────┘                    │
│                             │                             │
└─────────────────────────────┼─────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼                               ▼
┌──────────────────────┐        ┌──────────────────────────┐
│  IG REST API          │        │  ProRealTime              │
│                       │        │  (server-side)            │
│  Used for:            │        │                          │
│  • Account data       │        │  ProScreener:            │
│  • Position queries   │        │  • Trend Template scan   │
│  • Client sentiment   │        │  • Inverse Template scan │
│  • Earnings auto-exit │        │  • Volume dry-up filter  │
│    (via Python cron)  │        │  • Distribution scan     │
│  • Emergency covers   │        │                          │
│  • Trade history      │        │  ProOrder:               │
│                       │        │  • Breakout entry systems │
│                       │        │  • Pullback entry systems│
│                       │        │  • Stop management       │
│                       │        │  • Trailing stops        │
│                       │        │                          │
│  Also: bulk history   │        │  ProBuilder indicators:  │
│  for RS ranking       │        │  • All MAs, EMAs, ADX    │
│  (within data cap)    │        │  • Volume functions      │
│                       │        │  • Highest/Lowest        │
└──────────────────────┘        └──────────────────────────┘
              │                               │
              └───────────────┬───────────────┘
                              ▼
                    ┌──────────────────┐
                    │   IG EXECUTION    │
                    │   (orders flow    │
                    │   through IG      │
                    │   from both       │
                    │   REST + PRT)     │
                    └──────────────────┘
```

### 4.1 What Runs Where

| Component | Runs In | Why |
|-----------|---------|-----|
| Trend Template scan (L1, S1) | **ProScreener** | Server-side, no data caps, continuous, all indicators built-in |
| ADX filter (L2, S2) | **ProScreener** | Combined with Trend Template scan |
| Volume dry-up / distribution (L3, S3) | **ProScreener** | Volume is a native ProBuilder function |
| RS percentile ranking | **Python** | Requires cross-universe comparison — PRT can't do this natively |
| Earnings calendar check (Rule 11) | **Python + IG REST** | PRT has no access to external calendars; Python checks and auto-exits via REST API |
| Short interest / squeeze check (S4) | **Python** | PRT has no access to ORTEX/Fintel |
| Breakout / breakdown entry | **ProOrder** | Server-side execution with attached stops |
| Pullback / rally-to-EMA entry | **ProOrder** | Limit orders with server-side stop management |
| Mid-session volume check | **ProOrder (5-min)** | Secondary system on 5-min bars |
| Position sizing | **Python** | Portfolio-percentage calculation, then passed to ProOrder or REST |
| Trailing stop management | **ProOrder** | `SET STOP TRAILING` is native |
| Emergency short cover (15% adverse) | **ProOrder** | Can check `POSITIONPRICE` vs current price |
| Audit logging | **Python** | Full JSON logging of all decisions |
| UK spread monitoring | **Python + IG REST** | Check streaming BID/ASK before entry |

### 4.2 The Split Decision: ProOrder vs IG REST for Orders

There are two valid approaches:

**Option A: ProOrder Primary (Recommended for simplicity)**
- ProScreener identifies qualifying stocks
- ProOrder systems are deployed per qualifying stock
- ProOrder handles entry, stops, and trailing stops server-side
- Python handles earnings exits, short interest checks, and RS ranking
- Python uses IG REST only for account queries and emergency overrides

**Option B: Python/REST Primary (Recommended for maximum control)**
- ProScreener identifies qualifying stocks (server-side scan)
- Python receives the qualifying list and runs remaining gates (RS, earnings, SI)
- Python calculates position size and constructs the order
- Python places orders via IG REST API
- ProOrder handles only stop management and trailing stops on open positions

**Option A is better for hands-off operation. Option B is better for full audit trail and complex logic.** A phased approach would start with Option B (more control during development) and migrate qualifying entry types to Option A (ProOrder) once validated.

---

## 5. ProRealTime Pricing with IG

| Scenario | Monthly Cost |
|----------|-------------|
| IG account, 4+ trades per month | **Free** (IG rebates the PRT subscription) |
| IG account, fewer than 4 trades | ~€30/month or $40/month |
| Premium version (100 systems, more history) | Additional fee — contact PRT |
| ProOrder automated trading | **Included** in PRT subscription |
| ProScreener market scanning | **Included** in PRT subscription |

For our system, 4 trades per month is virtually guaranteed, so the effective cost is **zero**.

---

## 6. ProRealTime Limitations — Honest Assessment

| Limitation | Severity | Impact on Our System |
|-----------|----------|---------------------|
| **No external API calls from ProBuilder** | High | Cannot check earnings calendars, ORTEX, or any external data from within PRT. The Python layer is still essential. |
| **One system per instrument** | Medium | Need to combine long + short logic into one system, or get IG to enable multiple. |
| **Evaluation at candle close** | Medium | Daily systems miss intraday signals. Need 5-min secondary systems for mid-session volume checks. |
| **No cross-universe ranking** | Medium | RS percentile must be computed externally. PRT's RSI is single-stock, not relative. |
| **ProBuilder is not Python** | Low-Medium | Another language to maintain. But it's simple (BASIC-like) and well-documented. |
| **Position sizing in lots/contracts** | Low | Calculate in Python, pass to PRT or use REST for entry. |
| **40-subscription streaming limit (IG)** | Low | Applies to IG REST streaming, not PRT. PRT has its own data. |
| **10,000 data point weekly cap (IG REST)** | Low | **PRT bypasses this entirely for its own scans and systems.** This is a huge advantage. |
| **Historical data depth** | Low | PRT Premium has extensive history. Standard is sufficient for 260-day lookbacks. |

### 6.1 The Critical Point

**ProRealTime's server-side scanning and execution completely bypasses IG's REST API data cap.** PRT has its own data infrastructure. When ProScreener scans 500 stocks against the Trend Template, it's using PRT's data, not consuming your IG REST API allowance. This single fact eliminates the biggest technical risk we identified in the IG analysis.

---

## 7. Updated Recommendation

### Before PRT Analysis
- IG REST API for execution
- External data source (Alpha Vantage / Polygon) for bulk historical data to avoid IG's 10k/week cap
- All indicators computed locally in Python
- All scanning done locally

### After PRT Analysis
- **ProScreener** for server-side market scanning (Trend Template, ADX, volume gates) — eliminates the data cap problem entirely
- **ProOrder** for server-side automated execution with stop management — runs even when your computer is off
- **Python + IG REST API** for supplementary data (earnings, short interest), RS ranking, position sizing, audit logging, and emergency overrides
- **No need for Alpha Vantage / Polygon** for the primary scanning workflow — PRT handles it

### What This Means in Practice

The entry refinement engine becomes a three-layer system:

1. **ProScreener (always running, server-side):** Continuously scans the market for stocks passing Gates 1-3. Outputs a watchlist of qualifying candidates.

2. **Python orchestrator (runs on schedule + event-driven):** Takes the ProScreener output, applies supplementary checks (RS ranking, earnings proximity, short interest), calculates position sizes, and either deploys ProOrder systems or places orders via REST.

3. **ProOrder (always running per active stock, server-side):** Handles the actual entry execution, stop management, trailing stops, and volume confirmation — even when you're asleep.

**The system is now genuinely hands-off.** The only human touchpoint is the weekly audit log review — exactly as the Masterclass specified.

---

## 8. Development Phase Impact

### Revised Phase Plan

| Phase | What | Where |
|-------|------|-------|
| **Phase 1 (Week 1-2)** | IG REST auth + account module. ProScreener: Trend Template scan (long + short). Basic Python orchestrator. | Python + PRT |
| **Phase 2 (Week 3-4)** | ProScreener: Volume dry-up + distribution scans. Python: RS ranking, earnings calendar integration, ORTEX integration. | Python + PRT |
| **Phase 3 (Week 5-6)** | ProOrder: Build entry systems for Type L-A (VCP breakout) and S-A (H&S breakdown). Python: Risk manager, position sizing. | PRT + Python |
| **Phase 4 (Week 7-8)** | ProOrder: Remaining entry types (L-B through L-E, S-B through S-E). Python: Audit logging, earnings auto-exit via REST. | PRT + Python |
| **Phase 5 (Week 9-10)** | Paper trading. ProOrder in simulation mode. Compare ProOrder vs REST execution quality. | Full system |
| **Phase 6 (Week 11+)** | Live trading. Start at 25% size. Scale to full over 4 weeks. | Full system |

### What We No Longer Need to Build

| Module from Original Spec | Status |
|--------------------------|--------|
| `indicators` module (all local MA/EMA/ADX computation) | **Eliminated** — ProBuilder has all built-in |
| External data source for bulk history (Alpha Vantage / Polygon) | **Eliminated** — PRT has its own data |
| Local universe scanner | **Eliminated** — ProScreener does this server-side |
| Lightstreamer streaming integration | **Reduced** — still useful for Python-side monitoring, but PRT handles its own streaming |

### What We Still Need

| Module | Why |
|--------|-----|
| `auth` (IG REST) | For Python-side account queries and emergency orders |
| `risk_manager` (Python) | Portfolio-percentage position sizing, total risk tracking |
| `supplementary` (Python) | Earnings calendar, ORTEX short interest, catalyst calendar |
| `audit_log` (Python) | Full decision logging — PRT doesn't provide this level of detail |
| `orchestrator` (Python) | Coordinates ProScreener output → supplementary checks → ProOrder deployment |
| ProScreener code (PRT) | Long template, short template, volume scans |
| ProOrder systems (PRT) | One per entry type, or combined systems |
