"""Worker responsible for bar dataflow lifecycle."""

from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np

from ..cache import BarCache
from ..okx.bar_collector import OKXBarCollector
from .aggregator import BarAggregator

logger = logging.getLogger(__name__)


class BarDataflowWorker:
    """Run bar collection and aggregation in its own thread."""

    def __init__(
        self,
        symbols: list[str],
        data_cache: dict[str, np.ndarray] | None = None,
        lock: threading.Lock | None = None,
        agg_seconds: int = 5,
        window_length: int = 1000,
        bar_cache: BarCache | None = None,
    ):
        self.symbols = symbols
        self.agg_seconds = agg_seconds
        self.window_length = window_length
        self._bar_cache = bar_cache or BarCache(
            window_length=window_length,
            storage=data_cache,
            lock=lock,
        )
        self._cache = self._bar_cache.storage
        self._lock = self._bar_cache.lock

        self._aggregators: dict[str, BarAggregator] = {
            symbol: BarAggregator(agg_seconds) for symbol in symbols
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._collector: OKXBarCollector | None = None
        self._bar_count = 0

    def start(self):
        """Start the bar worker thread."""
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="dataflow-bars",
        )
        self._thread.start()
        logger.info(
            "Bar worker started (%d symbols, %ds bars, window=%d)",
            len(self.symbols),
            self.agg_seconds,
            self.window_length,
        )

    def stop(self):
        """Stop the bar worker thread gracefully."""
        if self._collector is not None:
            self._collector.stop()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_loop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("Bar worker stopped")

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        return self._bar_cache.snapshot(symbols)

    @property
    def cache(self) -> BarCache:
        return self._bar_cache

    @property
    def bar_count(self) -> int:
        return self._bar_count

    def _stop_loop(self):
        """Cancel all tasks then stop the loop (runs inside the loop thread)."""
        for task in asyncio.all_tasks(self._loop):
            task.cancel()
        self._loop.stop()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except RuntimeError:
            pass
        finally:
            pending = asyncio.all_tasks(self._loop)
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _run(self):
        collector = OKXBarCollector(
            symbols=self.symbols,
            on_candle1s=self._on_candle1s,
        )
        self._collector = collector
        try:
            await collector.run()
        except asyncio.CancelledError:
            pass

    def _on_candle1s(self, records: list[dict]):
        """Callback from OKX bar collector — aggregate and write to cache."""
        for record in records:
            symbol = record.get("instId", "")
            agg = self._aggregators.get(symbol)
            if agg is None:
                continue

            bar = agg.on_candle1s(record)
            if bar is None:
                continue

            self._bar_cache.append(bar)
            self._bar_count += 1

