"""
Shared pytest fixtures + integration-gate logic.

Integration tests (marked ``@pytest.mark.integration``) hit IG DEMO live. They
require ``IG_USERNAME``, ``IG_PASSWORD`` and ``IG_API_KEY`` in the environment
(or .env). When those are unset we *auto-skip* integration tests so CI and
fresh checkouts don't fail — but the skip is opt-out, not opt-in: once creds
are present, integration tests run as part of the default ``pytest`` run.
"""

from __future__ import annotations

import os

import pytest


def _ig_creds_present() -> bool:
    return bool(
        os.getenv("IG_USERNAME")
        and os.getenv("IG_PASSWORD")
        and os.getenv("IG_API_KEY")
    )


def pytest_collection_modifyitems(config, items) -> None:
    """Auto-skip integration tests when IG creds aren't available."""
    if _ig_creds_present():
        return
    skip_integration = pytest.mark.skip(
        reason="IG credentials not set (IG_USERNAME / IG_PASSWORD / IG_API_KEY)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
