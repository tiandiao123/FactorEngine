"""Simple timer-driven scheduler for factor evaluation."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


class Scheduler:
    """Invoke a callback at a fixed interval on a dedicated thread."""

    def __init__(self, interval_seconds: float, on_tick: Callable[[int, int], None]):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")

        self.interval_seconds = interval_seconds
        self.on_tick = on_tick
        self._running = False
        self._tick_id = 0
        self._thread: threading.Thread | None = None

    def start(self):
        """Start the scheduler thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="factor-scheduler",
        )
        self._thread.start()
        logger.info("Scheduler started (interval=%.3fs)", self.interval_seconds)

    def stop(self):
        """Stop the scheduler thread."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 1.0)
        logger.info("Scheduler stopped")

    def _run_loop(self):
        next_deadline = time.monotonic() + self.interval_seconds
        while self._running:
            remaining = next_deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

            if not self._running:
                break

            self._tick_id += 1
            ts_eval_ms = int(time.time() * 1000)
            try:
                self.on_tick(self._tick_id, ts_eval_ms)
            except Exception:
                logger.exception("Scheduler tick %d failed", self._tick_id)

            next_deadline += self.interval_seconds

