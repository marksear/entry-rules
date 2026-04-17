#!/usr/bin/env python3
"""Quick test: run entry monitor on AVGO with 3 checks, 10s apart."""

import sys
import json
import requests
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config.settings import get_settings
from src.engine.executor import EntryMonitor

settings = get_settings()
base = settings.ig_base_url

# Login
headers = {
    "X-IG-API-KEY": settings.ig_api_key,
    "Content-Type": "application/json",
    "Accept": "application/json; charset=UTF-8",
    "VERSION": "2",
}
r = requests.post(
    f"{base}/session",
    json={"identifier": settings.ig_username, "password": settings.ig_password},
    headers=headers,
)
if r.status_code != 200:
    print(f"Login failed: {r.json().get('errorCode', '?')}")
    sys.exit(1)

headers["CST"] = r.headers["CST"]
headers["X-SECURITY-TOKEN"] = r.headers["X-SECURITY-TOKEN"]

# Switch to spread bet
headers["VERSION"] = "1"
r2 = requests.put(
    f"{base}/session",
    json={"accountId": settings.ig_account_id, "defaultAccount": False},
    headers=headers,
)
if r2.status_code == 200:
    if "CST" in r2.headers:
        headers["CST"] = r2.headers["CST"]
    if "X-SECURITY-TOKEN" in r2.headers:
        headers["X-SECURITY-TOKEN"] = r2.headers["X-SECURITY-TOKEN"]
    print(f"Connected — {settings.ig_account_id} (Spread Bet)\n")

# Run entry monitor: test mode — 3 checks, 10s apart, window = now
from datetime import timezone, timedelta
est = timezone(timedelta(hours=-5))
now_est = datetime.now(est)
w_start = now_est.strftime("%H:%M")
w_end = (now_est + timedelta(minutes=5)).strftime("%H:%M")

monitor = EntryMonitor(
    ig_headers=headers,
    settings=settings,
    check_interval=10,
    max_checks=3,
    window_start=w_start,
    window_end=w_end,
    dry_run=True,
)
trades = monitor.run()

# Logout
headers["VERSION"] = "1"
requests.delete(f"{base}/session", headers=headers)
