"""KlineWorker — manages the kline streaming thread.

Assembles OKXKlineCollector → KlineAdapter → KlinePublisher into a single
daemon thread that can be started / stopped by Engine.

The thread runs an asyncio event loop for the WebSocket collector; the
adapter and publisher are invoked synchronously inside the WS callback.

KlineWorker exposes ``flush()`` for ClockDriver to retrieve the latest
complete round of kline bars.  It does **not** know about trades,
orderbook, or any other data source.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np

from ..common import resolve_bar_channel
from .adapter import KlineAdapter
from .collector import OKXKlineCollector
from .publisher import KlinePublisher

logger = logging.getLogger(__name__)


class KlineWorker:
    """Daemon thread: OKX kline WS → adapter → publisher.

    Parameters
    ----------
    symbols : list[str]
        Instruments to subscribe.
    freq_seconds : int
        Target bar frequency in seconds (e.g. 5 for 5s bars).
    debug_cache : object, optional
        If provided, must have ``append(symbol, bar)`` — receives every
        published bar for debug / monitoring purposes.
    """

    def __init__(
        self,
        symbols: list[str],
        freq_seconds: int = 5,
        debug_cache=None,
    ):
        self.symbols = list(symbols)

        channel, _ = resolve_bar_channel(freq_seconds)
        self._channel = channel
        self._freq_seconds = freq_seconds

        self._adapter = KlineAdapter(freq_seconds=freq_seconds)
        self._publisher = KlinePublisher(
            symbols=self.symbols,
            debug_cache=debug_cache,
            expected_interval_ms=freq_seconds * 1000,
            batch_timeout_s=min(freq_seconds * 0.8, 4.0),
        )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._collector: OKXKlineCollector | None = None
        self._bar_count = 0

    # ── flush interface (called by ClockDriver) ────────────────

    def flush(self) -> dict[str, np.ndarray] | None:
        """Return the latest complete round of kline bars.

        Delegates to ``KlinePublisher.flush_for_clock()``.
        Returns ``None`` if no data is available yet.
        """
        return self._publisher.flush_for_clock()

    # ── lifecycle ─────────────────────────────────────────────

    def start(self):
        """Start the kline worker daemon thread."""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="okx-kline",
        )
        self._thread.start()
        logger.info(
            "KlineWorker started: %d symbols, channel=%s, freq=%ds",
            len(self.symbols), self._channel, self._freq_seconds,
        )

    def stop(self):
        """Stop the worker thread gracefully."""
        if self._collector is not None:
            self._collector.stop()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._cancel_loop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info(
            "KlineWorker stopped (bars=%d, rounds=%d, "
            "ffill=%d, dups=%d, gaps=%d)",
            self._bar_count,
            self._publisher.rounds_completed,
            self._publisher.forward_fill_count,
            self._publisher.dup_count,
            self._publisher.gap_count,
        )

    # ── stats ─────────────────────────────────────────────────

    @property
    def bar_count(self) -> int:
        return self._bar_count

    @property
    def publisher(self) -> KlinePublisher:
        return self._publisher

    def stats(self) -> dict[str, int]:
        s = self._publisher.stats()
        s["adapter_bar_count"] = self._bar_count
        return s

    # ── internal ──────────────────────────────────────────────

    def _on_candle(self, symbol: str, raw: list):
        """Collector callback: adapt → publish."""
        bar = self._adapter.on_candle(symbol, raw)
        if bar is None:
            return
        self._publisher.publish(symbol, bar)
        self._bar_count += 1

    def _cancel_loop(self):
        for task in asyncio.all_tasks(self._loop):
            task.cancel()
        self._loop.stop()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except RuntimeError as exc:
            logger.debug("KlineWorker event loop exited with RuntimeError: %s", exc)
        finally:
            pending = asyncio.all_tasks(self._loop)
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _run(self):
        collector = OKXKlineCollector(
            symbols=self.symbols,
            on_candle=self._on_candle,
            channel=self._channel,
        )
        self._collector = collector
        try:
            await collector.run()
        except asyncio.CancelledError:
            pass
