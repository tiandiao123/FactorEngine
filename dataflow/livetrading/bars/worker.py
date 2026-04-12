"""Worker responsible for bar dataflow lifecycle."""

from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np

from ..cache import BarCache
from ..okx.bar_collector import OKXBarCollector
from ..okx.common import resolve_bar_channel
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

        # Resolve whether to subscribe a direct channel or aggregate from 1s.
        self._bar_channel, self._needs_aggregation = resolve_bar_channel(agg_seconds)

        self._aggregators: dict[str, BarAggregator] = {}
        if self._needs_aggregation:
            self._aggregators = {
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
            "Bar worker started (%d symbols, channel=%s, agg=%s, window=%d)",
            len(self.symbols),
            self._bar_channel,
            f"{self.agg_seconds}s" if self._needs_aggregation else "direct",
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
        callback = self._on_candle_aggregate if self._needs_aggregation else self._on_candle_direct
        collector = OKXBarCollector(
            symbols=self.symbols,
            on_candle=callback,
            channel=self._bar_channel,
        )
        self._collector = collector
        try:
            await collector.run()
        except asyncio.CancelledError:
            pass

    def _on_candle_direct(self, records: list[dict]):
        """Callback for direct channel mode — parse confirmed candle and write to cache."""
        for record in records:
            symbol = record.get("instId", "")
            bar = BarAggregator.parse_bar(record)
            if bar is None:
                continue
            self._bar_cache.append(symbol, bar)
            self._bar_count += 1

    def _on_candle_aggregate(self, records: list[dict]):
        """Callback for candle1s mode — aggregate N candles then write to cache."""
        for record in records:
            symbol = record.get("instId", "")
            agg = self._aggregators.get(symbol)
            if agg is None:
                continue

            bar = agg.on_candle1s(record)
            if bar is None:
                continue

            self._bar_cache.append(symbol, bar)
            self._bar_count += 1
