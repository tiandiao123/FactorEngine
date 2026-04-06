"""Worker responsible for trade dataflow lifecycle."""

from __future__ import annotations

import asyncio
import logging
import threading

from ..cache import TradeCache
from ..events import TradeEvent
from ..okx.trade_collector import OKXTradeCollector

logger = logging.getLogger(__name__)


class TradeDataflowWorker:
    """Run trade collection in its own thread."""

    def __init__(
        self,
        symbols: list[str],
        trade_cache: TradeCache | None = None,
        channels: tuple[str, ...] = ("trades-all",),
    ):
        self.symbols = symbols
        self.channels = channels
        self._trade_cache = trade_cache or TradeCache()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._collector: OKXTradeCollector | None = None
        self._trade_count = 0

    def start(self):
        """Start the trade worker thread."""
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="dataflow-trades",
        )
        self._thread.start()
        logger.info(
            "Trade worker started (%d symbols, channels=%s)",
            len(self.symbols),
            ",".join(self.channels),
        )

    def stop(self):
        """Stop the trade worker thread gracefully."""
        if self._collector is not None:
            self._collector.stop()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_loop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("Trade worker stopped")

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, list[TradeEvent]]:
        return self._trade_cache.snapshot(symbols)

    @property
    def cache(self) -> TradeCache:
        return self._trade_cache

    @property
    def trade_count(self) -> int:
        return self._trade_count

    def _stop_loop(self):
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
        collector = OKXTradeCollector(
            symbols=self.symbols,
            on_trades=self._on_trades,
            channels=self.channels,
        )
        self._collector = collector
        try:
            await collector.run()
        except asyncio.CancelledError:
            pass

    def _on_trades(self, events: list[TradeEvent]):
        for event in events:
            self._trade_cache.append(event)
            self._trade_count += 1

