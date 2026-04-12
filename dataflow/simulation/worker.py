"""Background worker that feeds synthetic bar data into cache."""

from __future__ import annotations

import logging
import threading
import time

from dataflow.livetrading.cache import BarCache

from .generator import BarGenerator

logger = logging.getLogger(__name__)


class SimBarWorker:
    """Daemon thread that periodically generates synthetic bars into a BarCache."""

    def __init__(
        self,
        symbols: list[str],
        bar_cache: BarCache,
        generators: dict[str, BarGenerator],
        interval_seconds: float = 1.0,
    ):
        self.symbols = symbols
        self._bar_cache = bar_cache
        self._generators = generators
        self._interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._bar_count = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="sim-bars")
        self._thread.start()
        logger.info(
            "SimBarWorker started (%d symbols, interval=%.2fs)",
            len(self.symbols), self._interval,
        )

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("SimBarWorker stopped (bars=%d)", self._bar_count)

    @property
    def bar_count(self) -> int:
        return self._bar_count

    def _run(self):
        while not self._stop_event.is_set():
            ts_ms = int(time.time() * 1000)
            for symbol in self.symbols:
                bar = self._generators[symbol].next_bar(ts_ms)
                self._bar_cache.append(symbol, bar)
                self._bar_count += 1
            self._stop_event.wait(self._interval)
