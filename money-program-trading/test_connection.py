"""
Quick connection test — verifies IG demo account is wired up correctly.
Run once, then delete.
"""

import sys
import os

# Ensure we can find our modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from config.settings import Settings

# Load settings from .env
settings = Settings(_env_file=".env")

print(f"API Key: {settings.ig_api_key[:8]}...{settings.ig_api_key[-4:]}")
print(f"Username: {settings.ig_username}")
print(f"Account type: {settings.ig_acc_type.value}")
print(f"Base URL: {settings.ig_base_url}")
print()

# Test connection
from trading_ig import IGService

print("Connecting to IG demo API...")
ig = IGService(
    username=settings.ig_username,
    password=settings.ig_password,
    api_key=settings.ig_api_key,
    acc_type=settings.ig_acc_type.value,
)

try:
    ig.create_session()
    print("SESSION ESTABLISHED")
    print()

    # Fetch accounts
    accounts = ig.fetch_accounts()
    print("=== ACCOUNTS ===")
    if hasattr(accounts, 'to_string'):
        print(accounts.to_string())
    else:
        for acc in accounts.get("accounts", [accounts] if isinstance(accounts, dict) else []):
            print(f"  Account ID: {acc.get('accountId', '?')}")
            print(f"  Account Name: {acc.get('accountName', '?')}")
            print(f"  Account Type: {acc.get('accountType', '?')}")
            balance = acc.get('balance', {})
            print(f"  Balance: {balance.get('balance', '?')}")
            print(f"  Available: {balance.get('available', '?')}")
            print(f"  P&L: {balance.get('profitLoss', '?')}")
            print()

    # Test market search
    print("=== MARKET SEARCH: AAPL ===")
    results = ig.search_markets("AAPL")
    if results is not None and not results.empty:
        for _, row in results.head(3).iterrows():
            print(f"  {row.get('epic', '?')} — {row.get('instrumentName', '?')}")
    print()

    # Test price fetch
    print("=== PRICE DATA: First result ===")
    if results is not None and not results.empty:
        epic = results.iloc[0]["epic"]
        print(f"  Fetching 5 daily bars for {epic}...")
        prices = ig.fetch_historical_prices_by_epic_and_num_points(
            epic=epic, resolution="DAY", numpoints=5
        )
        if prices and "prices" in prices:
            p = prices["prices"]
            print(f"  Got {len(p)} bars")
            print(f"  Columns: {list(p.columns)}")
            if not p.empty:
                last = p.iloc[-1]
                print(f"  Last bar: {last.to_dict()}")

    # Fetch open positions
    print()
    print("=== OPEN POSITIONS ===")
    positions = ig.fetch_open_positions()
    if positions is not None and hasattr(positions, '__len__'):
        print(f"  {len(positions)} open position(s)")
    else:
        print("  No positions or could not fetch")

    print()
    print("ALL CHECKS PASSED — IG demo account is wired up correctly.")

    ig.logout()

except Exception as e:
    print(f"CONNECTION FAILED: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
