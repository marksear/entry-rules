# ProRealTime Deployment Guide

## Screeners (ProScreener)

Deploy these on the ProScreener panel in ProRealTime. Each scan runs server-side against the selected market.

| File | Gate | Deploy On | Schedule |
|------|------|-----------|----------|
| `screeners/long_trend_template.prt` | L1 + L2 + L3 | FTSE 100, FTSE 250, S&P 500, NASDAQ 100 | Runs continuously |
| `screeners/short_inverse_template.prt` | S1 + S2 + S3 | Same markets | Runs continuously |
| `screeners/climax_top_scan.prt` | S1-ALT | Same markets | Runs continuously |

### How to deploy a screener

1. Open ProRealTime via IG
2. Go to **ProScreener** panel
3. Click **New ProScreener**
4. Select the target market (e.g., "FTSE 100")
5. Paste the code from the `.prt` file
6. Set timeframe to **Daily**
7. Click **Run ProScreener**

The scan results appear as a watchlist of qualifying stocks. The Python orchestrator then applies RS ranking and supplementary checks before deploying ProOrder systems.

## Entry Systems (ProOrder)

Deploy these per instrument when the Python orchestrator identifies a qualifying entry.

| File | Entry Type | Direction | Timeframe |
|------|-----------|-----------|-----------|
| `orders/long_vcp_breakout.prt` | L-A: VCP Breakout | Long | Daily |
| `orders/long_pullback_ema.prt` | L-B: First Pullback | Long | Daily |
| `orders/short_breakdown.prt` | S-A: H&S Breakdown | Short | Daily |
| `orders/short_rally_to_ema.prt` | S-B: Rally to EMA | Short | Daily |
| `orders/volume_monitor_5min.prt` | Volume check | Both | 5-min |

### How to deploy a ProOrder system

1. Open the chart for the target instrument
2. Go to **ProOrder** panel
3. Click **New Trading System**
4. Paste the code from the `.prt` file
5. Set the `pivotLevel` or `breakdownLevel` variable
6. Set `deployed = 1` to arm the system
7. Set position size (contracts)
8. Click **Start** (paper mode first, then live)

### Important notes

- **One system per instrument** — contact IG to enable multiple if needed
- **Set `deployed = 0`** to disarm without removing
- The volume monitor runs on 5-min bars alongside the daily entry system
- Position sizing is always calculated by the Python orchestrator
- RS percentile ranking is handled by Python (PRT can't do cross-universe ranking)
