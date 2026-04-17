"""
IG Index session management.

Uses the trading-ig library for authentication and wraps it with:
- Auto-refresh before session timeout
- Health checking
- Graceful reconnection
- Account switching (CFD / spread bet)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from trading_ig import IGService
from trading_ig.rest import IGException, TokenInvalidException

from ..config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# IG sessions last ~6 hours. Refresh well before expiry.
SESSION_REFRESH_MINUTES = 300  # Refresh after 5 hours


class IGSession:
    """
    Manages an authenticated session with the IG REST API.

    Usage:
        session = IGSession()
        session.connect()
        ig = session.service  # trading_ig.IGService instance

        # All subsequent calls go through ig:
        positions = ig.fetch_open_positions()
    """

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._service: IGService | None = None
        self._connected_at: datetime | None = None
        self._account_info: dict | None = None

    @property
    def service(self) -> IGService:
        """Get the active IGService, refreshing if needed."""
        if self._service is None:
            raise RuntimeError("Not connected. Call connect() first.")
        if self._needs_refresh():
            logger.info("Session approaching expiry, refreshing...")
            self._refresh()
        return self._service

    @property
    def is_connected(self) -> bool:
        return self._service is not None and self._connected_at is not None

    @property
    def account_id(self) -> str:
        return self._settings.ig_account_id

    def connect(self) -> None:
        """Establish a new session with IG."""
        s = self._settings
        if not all([s.ig_api_key, s.ig_username, s.ig_password]):
            raise ValueError(
                "IG credentials not configured. "
                "Set IG_API_KEY, IG_USERNAME, IG_PASSWORD in .env"
            )

        logger.info(
            "Connecting to IG (%s) as %s...",
            s.ig_acc_type.value,
            s.ig_username,
        )

        self._service = IGService(
            username=s.ig_username,
            password=s.ig_password,
            api_key=s.ig_api_key,
            acc_type=s.ig_acc_type.value,
        )

        try:
            self._service.create_session()
            self._connected_at = datetime.utcnow()
            logger.info("IG session established at %s", self._connected_at.isoformat())

            # Switch to configured account if specified
            if s.ig_account_id:
                self._switch_account(s.ig_account_id)

            # Cache account info
            self._account_info = self._fetch_account_info()
            logger.info(
                "Account: %s | Balance: %s",
                self._account_info.get("accountId", "?"),
                self._account_info.get("balance", "?"),
            )

        except IGException as e:
            self._service = None
            self._connected_at = None
            logger.error("IG connection failed: %s", e)
            raise ConnectionError(f"Failed to connect to IG: {e}") from e

    def disconnect(self) -> None:
        """Close the IG session."""
        if self._service:
            try:
                self._service.logout()
                logger.info("IG session closed.")
            except Exception as e:
                logger.warning("Error during IG logout: %s", e)
            finally:
                self._service = None
                self._connected_at = None

    def health_check(self) -> bool:
        """Quick check that the session is alive."""
        if not self.is_connected:
            return False
        try:
            # Lightweight call — just fetch accounts
            self._service.fetch_accounts()
            return True
        except Exception as e:
            logger.warning("IG health check failed: %s", e)
            return False

    def get_account_balance(self) -> dict:
        """Fetch current account balance and margin info.

        Handles both shapes ``trading_ig.IGService.fetch_accounts`` can return
        depending on library version:
        - Modern (>=0.0.25): a pandas DataFrame with flat columns per account
          (``accountId``, ``balance``, ``available``, ``deposit``, ``profitLoss``, …).
        - Legacy: a dict ``{"accounts": [{"accountId": ..., "balance": {...}}]}``.
        """
        accounts = self.service.fetch_accounts()
        rows = _accounts_to_records(accounts)
        for row in rows:
            if row.get("accountId") == self._settings.ig_account_id:
                return _row_to_balance(row)
        # Fallback — no configured account matched; return the first we saw.
        if rows:
            return _row_to_balance(rows[0])
        return {}

    # ── Internal ──────────────────────────────────────────────

    def _needs_refresh(self) -> bool:
        if self._connected_at is None:
            return True
        elapsed = datetime.utcnow() - self._connected_at
        return elapsed > timedelta(minutes=SESSION_REFRESH_MINUTES)

    def _refresh(self) -> None:
        """Reconnect to get a fresh session token."""
        try:
            self.disconnect()
        except Exception:
            pass
        # Brief pause before reconnecting
        time.sleep(1)
        self.connect()

    def _switch_account(self, account_id: str) -> None:
        """Switch to a specific IG account (CFD, spread bet, etc.).

        trading-ig's ``IGService.switch_account`` requires a ``default_account``
        flag — we always pass ``False`` because we're switching for the
        duration of this process only, not permanently flipping the default
        on IG's side.
        """
        try:
            self._service.switch_account(account_id, False)
            logger.info("Switched to account: %s", account_id)
        except (IGException, TokenInvalidException) as e:
            # IG DEMO sometimes 401s the switch call right after create_session
            # (rate-limit / token-warmup race). Swallow it — if we're already on
            # the right account the subsequent calls will just succeed; if not,
            # the balance / fetch_accounts warning below will surface it.
            logger.warning(
                "Could not switch to account %s: %s: %s",
                account_id,
                type(e).__name__,
                e,
            )

    def _fetch_account_info(self) -> dict:
        """Get basic account details. Handles DataFrame + legacy dict shapes."""
        try:
            accounts = self._service.fetch_accounts()
            rows = _accounts_to_records(accounts)
            for acc in rows:
                if acc.get("accountId") == self._settings.ig_account_id:
                    return acc
            if rows:
                return rows[0]
        except Exception as e:
            logger.warning("Could not fetch account info: %s", e)
        return {}

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False


# ---------------------------------------------------------------------------
# Module-level helpers — shape-normalisers for fetch_accounts()
# ---------------------------------------------------------------------------
#
# ``trading_ig.IGService.fetch_accounts()`` returns a pandas DataFrame in
# modern versions (>=0.0.25) and a dict ``{"accounts": [...]}`` in older ones.
# These helpers accept either and normalise to a list of account-row dicts.


def _accounts_to_records(accounts) -> list[dict]:
    """Normalise fetch_accounts() output to a list of per-account dicts.

    DataFrame → records orientation (one dict per row, flat columns).
    Dict (legacy) → ``accounts.get("accounts", [])``.
    Anything else → empty list (defensive).
    """
    if hasattr(accounts, "to_dict"):
        # pandas DataFrame. ``orient="records"`` gives us a list of row dicts.
        return accounts.to_dict(orient="records")
    if isinstance(accounts, dict):
        return accounts.get("accounts", []) or []
    return []


def _row_to_balance(row: dict) -> dict:
    """Extract balance fields from an account row.

    DataFrame rows are flat — balance fields sit at the top level as columns
    (``balance``, ``available``, ``deposit``, ``profitLoss``). Legacy dict
    rows nest them under a ``balance`` sub-dict. Support both; return the
    sub-dict when present, otherwise assemble one from the flat fields.
    """
    nested = row.get("balance")
    if isinstance(nested, dict):
        return nested
    fields = ("balance", "available", "deposit", "profitLoss")
    return {k: row[k] for k in fields if k in row}
