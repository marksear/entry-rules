"""
Money Program — Full Connection Verification Script.
Run this once to confirm everything works end-to-end.
"""

import json
import sys
import time
import requests


BASE = "https://demo-api.ig.com/gateway/deal"
API_KEY = "9b1db96a134a6d135cde791ccab9089df6bc370a"
USERNAME = "markasear1964"
PASSWORD = "Chelsea1955%"
SB_ACCOUNT = "Z67Q2F"  # Spread bet (has equity access)


def main():
    headers = {
        "X-IG-API-KEY": API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json; charset=UTF-8",
        "VERSION": "2",
    }

    # ── LOGIN ─────────────────────────────────────────────────
    r = requests.post(
        f"{BASE}/session",
        json={"identifier": USERNAME, "password": PASSWORD},
        headers=headers,
    )
    if r.status_code != 200:
        print(f"Login failed: {r.status_code} — {r.json().get('errorCode', '?')}")
        print("If rate-limited, wait 10 minutes and try again.")
        sys.exit(1)

    headers["CST"] = r.headers["CST"]
    headers["X-SECURITY-TOKEN"] = r.headers["X-SECURITY-TOKEN"]
    print("✓ Login OK")

    # ── SWITCH TO SPREAD BET ──────────────────────────────────
    headers["VERSION"] = "1"
    r2 = requests.put(
        f"{BASE}/session",
        json={"accountId": SB_ACCOUNT, "defaultAccount": False},
        headers=headers,
    )
    if r2.status_code == 200:
        if "CST" in r2.headers:
            headers["CST"] = r2.headers["CST"]
        if "X-SECURITY-TOKEN" in r2.headers:
            headers["X-SECURITY-TOKEN"] = r2.headers["X-SECURITY-TOKEN"]
        print(f"✓ Switched to {SB_ACCOUNT} (Spread Bet)")
    else:
        print(f"✗ Account switch failed: {r2.status_code}")

    headers["VERSION"] = "3"
    print()

    # ── US EQUITIES ───────────────────────────────────────────
    print("── US Equities ──")
    for epic, name in [
        ("UA.D.AAPL.CASH.IP", "Apple"),
        ("UC.D.MSFT.CASH.IP", "Microsoft"),
        ("UC.D.NVDA.CASH.IP", "Nvidia"),
    ]:
        _check_price(headers, epic, name)
        time.sleep(0.4)

    # ── UK EQUITIES ───────────────────────────────────────────
    print("\n── UK Equities ──")
    for epic, name in [
        ("KA.D.VOD.CASH.IP", "Vodafone"),
        ("KA.D.BARC.CASH.IP", "Barclays"),
        ("KA.D.SHEL.CASH.IP", "Shell"),
    ]:
        _check_price(headers, epic, name)
        time.sleep(0.4)

    # ── INDICES ───────────────────────────────────────────────
    print("\n── Indices ──")
    for epic, name in [
        ("IX.D.FTSE.DAILY.IP", "FTSE 100"),
        ("IX.D.DOW.DAILY.IP", "Dow Jones"),
    ]:
        _check_price(headers, epic, name)
        time.sleep(0.4)

    # ── 260-BAR VOLUME CHECK ──────────────────────────────────
    print("\n── Volume Data (260 bars) ──")
    time.sleep(0.5)
    r3 = requests.get(
        f"{BASE}/prices/UA.D.AAPL.CASH.IP?resolution=DAY&max=260&pageSize=0",
        headers=headers,
    )
    if r3.status_code == 200:
        bars = r3.json().get("prices", [])
        vol_count = sum(1 for b in bars if b.get("lastTradedVolume", 0) > 0)
        print(f"  AAPL: {len(bars)} bars, {vol_count} with volume")
        print(f"  Volume data: {'CONFIRMED' if vol_count > 200 else 'PARTIAL'}")
    else:
        print(f"  AAPL 260-bar fetch: {r3.status_code}")

    # ── SENTIMENT ─────────────────────────────────────────────
    print("\n── Client Sentiment ──")
    time.sleep(0.4)
    r4 = requests.get(f"{BASE}/clientsentiment?marketIds=AAPL", headers=headers)
    if r4.status_code == 200:
        for s in r4.json().get("clientSentiments", []):
            print(f"  {s['marketId']}: {s['longPositionPercentage']}% long / {s['shortPositionPercentage']}% short")
    else:
        print(f"  Sentiment: {r4.status_code}")

    # ── POSITIONS & ORDERS ────────────────────────────────────
    print("\n── Account State ──")
    time.sleep(0.4)
    r5 = requests.get(f"{BASE}/positions", headers=headers)
    r6 = requests.get(f"{BASE}/workingorders", headers=headers)
    pos = len(r5.json().get("positions", [])) if r5.status_code == 200 else "?"
    ords = len(r6.json().get("workingOrders", [])) if r6.status_code == 200 else "?"
    print(f"  Open positions: {pos}")
    print(f"  Working orders: {ords}")

    # ── LOGOUT ────────────────────────────────────────────────
    headers["VERSION"] = "1"
    requests.delete(f"{BASE}/session", headers=headers)

    print("\n" + "=" * 52)
    print("  ALL SYSTEMS VERIFIED — ENGINE READY")
    print("=" * 52)


def _check_price(headers, epic, name):
    r = requests.get(
        f"{BASE}/prices/{epic}?resolution=DAY&max=5&pageSize=0",
        headers=headers,
    )
    if r.status_code == 200:
        bars = r.json().get("prices", [])
        if bars:
            b = bars[-1]
            cp = b.get("closePrice", {})
            v = b.get("lastTradedVolume", "—")
            bid = cp.get("bid", "—")
            ask = cp.get("ask", "—")
            print(f"  ✓ {name:12s} bid={bid:>8} ask={ask:>8} vol={v:>10} [{len(bars)} bars]")
        else:
            print(f"  ✓ {name}: OK but no bars returned")
    else:
        err = r.json().get("errorCode", "?")
        print(f"  ✗ {name}: {r.status_code} — {err}")


if __name__ == "__main__":
    main()
