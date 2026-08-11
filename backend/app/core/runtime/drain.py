"""Thread-safe process drain state used by HTTP and workflow boundaries."""

from __future__ import annotations

import threading
from datetime import UTC, datetime


class DrainController:
    """Monotonic lifecycle flag: once draining, the process never becomes ready again."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requested_at: datetime | None = None

    @property
    def is_draining(self) -> bool:
        return self._requested_at is not None

    @property
    def requested_at(self) -> datetime | None:
        return self._requested_at

    def request_drain(self) -> bool:
        """Enter drain mode and report whether this call changed the state."""
        with self._lock:
            if self._requested_at is not None:
                return False
            self._requested_at = datetime.now(UTC)
            return True


drain_controller = DrainController()
