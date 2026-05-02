"""KlinePublisher — safely push BarPoints into the runtime bar_queue.

Responsibilities:
  1. Per-symbol deduplication via ``last_pushed_ts``.
  2. Timestamp gap detection and warning.
  3. Non-blocking ``bar_queue.put()`` with backpressure drop.
  4. Optional write to a debug BarCache.
  5. Counters for monitoring (published / dup / gap / drop).
"""

from __future__ import annotations

import logging
import queue
import threading

import numpy as np

logger = logging.getLogger(__name__)


class KlinePublisher:
    """Push BarPoints into ``bar_queue`` with streaming safety guarantees.

    Parameters
    ----------
    bar_queue : queue.Queue
        Destination queue consumed by ``Engine._runtime_loop()``.
    debug_cache : object, optional
        If provided, must have an ``append(symbol, bar)`` method.
        Receives every successfully published bar for debug/monitoring.
    expected_interval_ms : int
        Expected interval between consecutive bars for the same symbol.
        Used for gap detection.  Defaults to 5000 (5s).
    """

    def __init__(
        self,
        bar_queue: queue.Queue,
        debug_cache=None,
        expected_interval_ms: int = 5000,
    ):
        self._bar_queue = bar_queue
        self._debug_cache = debug_cache
        self._expected_interval_ms = expected_interval_ms

        self._last_pushed_ts: dict[str, int] = {}
        self._lock = threading.Lock()

        # Counters (read from any thread, written only from the WS callback
        # thread — but we use the lock for safety).
        self._bars_published = 0
        self._dup_count = 0
        self._gap_count = 0
        self._drop_count = 0

    def publish(self, symbol: str, bar: np.ndarray) -> bool:
        """Push one BarPoint for *symbol*.

        Returns ``True`` if the bar was successfully placed in the queue,
        ``False`` if it was deduplicated or dropped due to backpressure.
        """
        ts = int(bar[0])

        with self._lock:
            last_ts = self._last_pushed_ts.get(symbol)

            if last_ts is not None and ts <= last_ts:
                self._dup_count += 1
                return False

            if last_ts is not None:
                actual_gap = ts - last_ts
                if actual_gap > self._expected_interval_ms * 2:
                    self._gap_count += 1
                    logger.warning(
                        "Kline gap: %s expected %dms got %dms",
                        symbol, self._expected_interval_ms, actual_gap,
                    )

            self._last_pushed_ts[symbol] = ts

        if self._debug_cache is not None:
            self._debug_cache.append(symbol, bar)

        round_bars = {symbol: bar}
        try:
            self._bar_queue.put(round_bars, block=False)
        except queue.Full:
            with self._lock:
                self._drop_count += 1
            if self._drop_count % 100 == 1:
                logger.warning(
                    "bar_queue full — dropped round for %s (total drops: %d)",
                    symbol, self._drop_count,
                )
            return False

        with self._lock:
            self._bars_published += 1
        return True

    # ── stats ─────────────────────────────────────────────────

    @property
    def bars_published(self) -> int:
        with self._lock:
            return self._bars_published

    @property
    def dup_count(self) -> int:
        with self._lock:
            return self._dup_count

    @property
    def gap_count(self) -> int:
        with self._lock:
            return self._gap_count

    @property
    def drop_count(self) -> int:
        with self._lock:
            return self._drop_count

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "bars_published": self._bars_published,
                "dup_count": self._dup_count,
                "gap_count": self._gap_count,
                "drop_count": self._drop_count,
            }
