"""ClockDriver — unified wall-clock aligned periodic flush driver.

All registered callbacks are invoked at the same wall-clock boundary,
ensuring time alignment across heterogeneous data sources (trades,
orderbook, funding rate, etc.).

In ``trades``-only mode, ``ClockDriver`` replaces ``KlinePublisher`` as
the flush driver.  In ``kline_trades`` mode, kline arrival already
drives flush via ``KlinePublisher``; ``ClockDriver`` is available for
future non-kline data streams.

Architecture::

    ClockDriver(freq_seconds=5)
      every 5s (aligned to :00, :05, :10, ...):
        → callback_1()   e.g. trades_worker.flush()
        → callback_2()   e.g. orderbook_worker.flush()  (future)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


class ClockDriver:
    """Unified clock that drives periodic flush for all data streams.

    Parameters
    ----------
    freq_seconds : float
        Flush interval in seconds (e.g. 5.0 for 5s bars).
    """

    def __init__(self, freq_seconds: float):
        self._freq = freq_seconds
        self._freq_ms = freq_seconds * 1000
        self._callbacks: list[Callable[[], None]] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tick_count = 0

    def register(self, callback: Callable[[], None]) -> None:
        """Register a flush callback (e.g. ``worker.flush``).

        Must be called before :meth:`start`.
        """
        self._callbacks.append(callback)

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        """Start the clock driver in a daemon thread."""
        if not self._callbacks:
            logger.warning("ClockDriver started with no registered callbacks")
        self._running = True
        self._thread = threading.Thread(
            target=self._run_thread, daemon=True, name="clock-driver",
        )
        self._thread.start()
        logger.info(
            "ClockDriver started: freq=%.1fs, callbacks=%d",
            self._freq, len(self._callbacks),
        )

    def stop(self) -> None:
        """Stop the clock driver."""
        self._running = False
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=self._freq + 2)
        logger.info("ClockDriver stopped (ticks=%d)", self._tick_count)

    # ── stats ──────────────────────────────────────────────────

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def freq_seconds(self) -> float:
        return self._freq

    # ── internal ───────────────────────────────────────────────

    def _run_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._tick_loop())
        except RuntimeError:
            pass
        finally:
            self._loop.close()

    async def _tick_loop(self) -> None:
        """Fire callbacks at wall-clock aligned boundaries."""
        while self._running:
            now_ms = time.time() * 1000
            next_boundary = now_ms - (now_ms % self._freq_ms) + self._freq_ms
            sleep_s = (next_boundary - now_ms) / 1000
            await asyncio.sleep(max(sleep_s, 0.001))

            if not self._running:
                break

            ts_ms = int(next_boundary)
            self._tick_count += 1

            for cb in self._callbacks:
                try:
                    cb()
                except Exception:
                    logger.exception("ClockDriver callback error (tick=%d)", self._tick_count)

            logger.debug(
                "ClockDriver tick %d at ts=%d (%.3fs drift)",
                self._tick_count, ts_ms,
                (time.time() * 1000 - next_boundary) / 1000,
            )
