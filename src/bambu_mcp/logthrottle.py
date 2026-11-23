"""Rate-limited logging for conditions that repeat while they last (printer off, bad token)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

DEFAULT_INTERVAL_S = 3600.0


class Throttle:
    """Logs the first record for a key, then at most one per interval with a count.

    Keys are fixed strings chosen by the caller, so the table stays small.
    Thread-safe: the printer link logs from paho's thread.
    """

    def __init__(
        self,
        logger: logging.Logger,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._interval = interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, tuple[float, int]] = {}  # key -> (last logged at, suppressed)

    def log(self, level: int, key: str, msg: str, *args: object) -> None:
        now = self._clock()
        with self._lock:
            last = self._state.get(key)
            if last is not None and now - last[0] < self._interval:
                self._state[key] = (last[0], last[1] + 1)
                return
            suppressed = last[1] if last is not None else 0
            self._state[key] = (now, 0)
        if suppressed:
            msg = f"{msg} (%d similar suppressed in the last hour)"
            args = (*args, suppressed)
        self._logger.log(level, msg, *args)

    def warning(self, key: str, msg: str, *args: object) -> None:
        self.log(logging.WARNING, key, msg, *args)

    def info(self, key: str, msg: str, *args: object) -> None:
        self.log(logging.INFO, key, msg, *args)

    def reset(self, key: str) -> None:
        """The condition cleared: the next occurrence is logged at once."""
        with self._lock:
            self._state.pop(key, None)
