"""
Unit tests for the fetch_accounts shape-normalisation helpers in
``src/auth/ig_auth.py``.

Regression guard for the 2026-04-17 DEMO pre-flight bug: modern trading-ig
returns a pandas DataFrame from ``fetch_accounts``, the wrapper code was
written against the legacy ``{"accounts": [...]}`` dict shape, and the
mismatch silently returned ``balance: {}`` from ``get_account_balance`` —
which in turn made the monitor loop launch against no resolvable account.

No IG connection is required for these tests. We construct representative
DataFrame / dict payloads ourselves and verify the helpers extract the
expected account fields.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.auth.ig_auth import _accounts_to_records, _row_to_balance

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _accounts_to_records
# ---------------------------------------------------------------------------


def test_accounts_to_records_dataframe():
    """Modern trading-ig returns a DataFrame with flat per-account columns."""
    df = pd.DataFrame(
        [
            {
                "accountId": "Z67Q2F",
                "accountName": "Spread bet",
                "status": "ENABLED",
                "accountType": "SPREADBET",
                "balance": 11277.53,
                "available": 11277.53,
                "deposit": 0.0,
                "profitLoss": 0.0,
            },
            {
                "accountId": "Z67Q2G",
                "accountName": "CFD",
                "status": "ENABLED",
                "accountType": "CFD",
                "balance": 10000.00,
                "available": 10000.00,
                "deposit": 0.0,
                "profitLoss": 0.0,
            },
        ]
    )
    rows = _accounts_to_records(df)
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0]["accountId"] == "Z67Q2F"
    assert rows[0]["balance"] == 11277.53
    assert rows[1]["accountId"] == "Z67Q2G"


def test_accounts_to_records_legacy_dict():
    """Older trading-ig returns ``{"accounts": [...]}`` with nested balance dicts."""
    legacy = {
        "accounts": [
            {
                "accountId": "Z67Q2F",
                "accountName": "Spread bet",
                "balance": {
                    "balance": 11277.53,
                    "available": 11277.53,
                    "deposit": 0.0,
                    "profitLoss": 0.0,
                },
            }
        ]
    }
    rows = _accounts_to_records(legacy)
    assert len(rows) == 1
    assert rows[0]["accountId"] == "Z67Q2F"
    # nested balance sub-dict is preserved
    assert rows[0]["balance"]["available"] == 11277.53


def test_accounts_to_records_empty_dict():
    assert _accounts_to_records({}) == []
    assert _accounts_to_records({"accounts": []}) == []
    assert _accounts_to_records({"accounts": None}) == []


def test_accounts_to_records_unknown_shape_returns_empty():
    """Defensive: anything not DataFrame/dict → []."""
    assert _accounts_to_records(None) == []
    assert _accounts_to_records("garbage") == []
    assert _accounts_to_records(42) == []


# ---------------------------------------------------------------------------
# _row_to_balance
# ---------------------------------------------------------------------------


def test_row_to_balance_dataframe_row():
    """DataFrame-style row: flat columns → assemble a balance dict."""
    row = {
        "accountId": "Z67Q2F",
        "balance": 11277.53,
        "available": 11277.53,
        "deposit": 0.0,
        "profitLoss": 0.0,
        "accountType": "SPREADBET",  # extraneous — must be filtered out
    }
    bal = _row_to_balance(row)
    assert bal == {
        "balance": 11277.53,
        "available": 11277.53,
        "deposit": 0.0,
        "profitLoss": 0.0,
    }


def test_row_to_balance_legacy_nested_dict():
    """Legacy row: nested ``balance`` sub-dict → return it directly."""
    row = {
        "accountId": "Z67Q2F",
        "balance": {
            "balance": 11277.53,
            "available": 11277.53,
            "deposit": 0.0,
            "profitLoss": 0.0,
        },
    }
    bal = _row_to_balance(row)
    assert bal == {
        "balance": 11277.53,
        "available": 11277.53,
        "deposit": 0.0,
        "profitLoss": 0.0,
    }


def test_row_to_balance_partial_flat_row():
    """If some balance fields are missing, return only what's present."""
    row = {"accountId": "Z67Q2F", "balance": 100.0, "available": 50.0}
    bal = _row_to_balance(row)
    assert bal == {"balance": 100.0, "available": 50.0}


def test_row_to_balance_no_balance_fields():
    """Row with no recognisable balance fields → empty dict."""
    row = {"accountId": "Z67Q2F", "status": "ENABLED"}
    assert _row_to_balance(row) == {}


# ---------------------------------------------------------------------------
# End-to-end shape round-trip
# ---------------------------------------------------------------------------


def test_end_to_end_dataframe_matches_real_ig_demo_response():
    """Feeds in a DataFrame shaped exactly like the one observed from
    IG DEMO on 2026-04-17, then walks the same lookup-by-accountId path
    ``get_account_balance`` uses. Guards against any reversion to the
    old ``accounts.get("accounts", [])`` pattern.
    """
    df = pd.DataFrame(
        [
            {
                "accountId": "Z67Q2F",
                "accountName": "Spread bet",
                "status": "ENABLED",
                "accountType": "SPREADBET",
                "balance": 11277.53,
                "available": 11277.53,
                "deposit": 0.0,
                "profitLoss": 0.0,
            },
            {
                "accountId": "Z67Q2G",
                "accountName": "CFD",
                "status": "ENABLED",
                "accountType": "CFD",
                "balance": 10000.00,
                "available": 10000.00,
                "deposit": 0.0,
                "profitLoss": 0.0,
            },
        ]
    )
    rows = _accounts_to_records(df)
    configured = "Z67Q2F"
    bal = next(
        (_row_to_balance(r) for r in rows if r.get("accountId") == configured),
        {},
    )
    assert bal["available"] == 11277.53, "spread-bet available must resolve"
    assert bal["balance"] == 11277.53
