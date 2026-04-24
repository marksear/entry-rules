"""
Time-handling helpers.

Centralises the one piece of boilerplate that touches every event-emitting
module in the codebase: producing "now" as a naive UTC ``datetime``.

Why a helper at all:

* ``datetime.utc_now()`` is **deprecated in Python 3.12+** and
  scheduled for removal in a future release. Running pytest on the repo
  currently emits ~300 ``DeprecationWarning`` lines, all pointing at
  inline ``utc_now()`` calls across src/ and tests/.
* The drop-in modern form ``datetime.datetime.now(timezone.utc)`` returns
  a **timezone-aware** datetime — differs semantically from the old
  naive UTC the codebase was built on. Swapping naive for aware without
  thought would cascade through SQLite serialisation, pydantic models,
  and arithmetic between our datetimes and the naive ones coming back
  from IG's REST responses.
* Bottom line: we want the deprecation silenced, we do NOT want to
  churn the model layer into timezone awareness right now. Hence this
  helper — returns a naive UTC datetime, matching the pre-existing
  convention verbatim.

Usage::

    from ..utils.time_utils import utc_now

    ts = utc_now()                 # naive datetime, UTC wall-clock
    session_opened_at = utc_now()

Call sites previously written as ``utc_now()`` translate 1:1 to
``utc_now()`` with no other change. When the codebase eventually
migrates to timezone-aware datetimes (separate PR, not this one),
change only this function's body and everything ripples correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current UTC time as a **naive** ``datetime``.

    Drop-in replacement for the deprecated ``utc_now()``. The
    returned datetime has no tzinfo (matches the codebase's prevailing
    convention — SQLite stores isoformat strings without offset, and
    our pydantic models don't set timezone-aware types).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
