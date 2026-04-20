"""Background worker that feeds synthetic bar data into cache."""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

from dataflow.livetrading.cache import BarCache

from .generator import BarGenerator

logger = logging.getLogger(__name__)


class SimBarWorker:
    """Daemon thread that periodically generates synthetic bars into a BarCache.

    If ``bar_queue`` is provided, each round of bars is also pushed into
    the queue for downstream consumers (e.g. the factor inference thread).
    The worker does NOT block if the queue is full — it drops the round
    and logs a warning.
    """

    def __init__(
        self,
        symbols: list[str],
        bar_cache: BarCache,
        generators: dict[str, BarGenerator],
        interval_seconds: float = 1.0,
        bar_queue: queue.Queue | None = None,
    ):
        self.symbols = symbols
        self._bar_cache = bar_cache
        self._generators = generators
        self._interval = interval_seconds
        self._bar_queue = bar_queue
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._bar_count = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="sim-bars")
        self._thread.start()
        logger.info(
            "SimBarWorker started (%d symbols, interval=%.2fs, queue=%s)",
            len(self.symbols), self._interval, self._bar_queue is not None,
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
            round_bars: dict[str, np.ndarray] = {}
            for symbol in self.symbols:
                bar = self._generators[symbol].next_bar(ts_ms)
                self._bar_cache.append(symbol, bar)
                round_bars[symbol] = bar
                self._bar_count += 1
            if self._bar_queue is not None:
                try:
                    self._bar_queue.put(round_bars, block=False)
                except queue.Full:
                    logger.warning(
                        "bar_queue full, dropping bar round %d", self._bar_count
                    )
            self._stop_event.wait(self._interval)
