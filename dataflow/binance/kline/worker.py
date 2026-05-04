"""BinanceKlineWorker — manages the Binance kline streaming thread.

Assembles BinanceKlineCollector → BinanceKlineAdapter → KlinePublisher
into a daemon thread.

The architecture mirrors :class:`dataflow.okx.kline.worker.KlineWorker`:
collector handles WS lifecycle, adapter parses/filters, publisher batches
by round.  ClockDriver calls ``flush()`` to retrieve the latest round.

Binance Futures minimum kline interval: 1m.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np

from dataflow.kline.publisher import KlinePublisher

from .adapter import BinanceKlineAdapter
from .collector import BinanceKlineCollector, resolve_binance_interval

logger = logging.getLogger(__name__)


class BinanceKlineWorker:
    """Daemon thread: Binance kline WS → adapter → publisher.

    Parameters
    ----------
    symbols : list[str]
        Unified instrument IDs, e.g. ``["BTC-USDT", "ETH-USDT"]``.
    freq_seconds : int
        Target bar frequency in seconds (must be ≥ 60 for Binance).
    debug_cache : object, optional
        If provided, must have ``append(symbol, bar)`` method.
    testnet : bool
        If True, connect to Binance Futures testnet.
    """

    def __init__(
        self,
        symbols: list[str],
        freq_seconds: int = 60,
        debug_cache=None,
        *,
        testnet: bool = False,
    ):
        self.symbols = list(symbols)
        self._freq_seconds = freq_seconds
        self._interval = resolve_binance_interval(freq_seconds)
        self._testnet = testnet

        self._adapter = BinanceKlineAdapter()
        self._publisher = KlinePublisher(
            symbols=self.symbols,
            debug_cache=debug_cache,
            expected_interval_ms=freq_seconds * 1000,
            batch_timeout_s=min(freq_seconds * 0.8, 30.0),
        )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._collector: BinanceKlineCollector | None = None
        self._bar_count = 0

    # ── flush interface (called by ClockDriver) ────────────────

    def flush(self) -> dict[str, np.ndarray] | None:
        """Return the latest complete round of kline bars."""
        return self._publisher.flush_for_clock()

    # ── lifecycle ─────────────────────────────────────────────

    def start(self):
        """Start the Binance kline worker daemon thread."""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="binance-kline",
        )
        self._thread.start()
        logger.info(
            "BinanceKlineWorker started: %d symbols, interval=%s, freq=%ds, testnet=%s",
            len(self.symbols), self._interval, self._freq_seconds, self._testnet,
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
            "BinanceKlineWorker stopped (bars=%d, rounds=%d, "
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

    def _on_candle(self, symbol: str, k: dict):
        """Collector callback: adapt → publish."""
        bar = self._adapter.on_candle(symbol, k)
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
            logger.debug(
                "BinanceKlineWorker event loop exited with RuntimeError: %s", exc,
            )
        finally:
            pending = asyncio.all_tasks(self._loop)
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _run(self):
        collector = BinanceKlineCollector(
            symbols=self.symbols,
            on_candle=self._on_candle,
            interval=self._interval,
            testnet=self._testnet,
        )
        self._collector = collector
        try:
            await collector.run()
        except asyncio.CancelledError:
            pass
