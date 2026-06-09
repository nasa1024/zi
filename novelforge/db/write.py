"""Write-path helpers: optimistic-lock retry for single-writer SQLite.

ProjectionError MUST NOT be retried — it routes to human review instead.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable, TypeVar

from ..contracts import OptimisticLockError

T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    *,
    max_retries: int = 5,
    delay_ms: int = 100,
) -> T:
    """Retry fn on OptimisticLockError or SQLITE_BUSY. Exponential back-off."""
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except (OptimisticLockError, sqlite3.OperationalError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(delay_ms / 1000 * (2**attempt))
    raise last_exc  # type: ignore[misc]
