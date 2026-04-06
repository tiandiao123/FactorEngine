"""Worker responsible for shallow order-book dataflow lifecycle."""

from __future__ import annotations

import asyncio
import logging
import threading

from ..cache import BookCache
from ..events import BookEvent
from ..okx.book_collector import OKXBookCollector

logger = logging.getLogger(__name__)


class BookDataflowWorker:
    """Run shallow order-book collection in its own thread."""

    def __init__(
        self,
        symbols: list[str],
        book_cache: BookCache | None = None,
        channels: tuple[str, ...] = ("books5",),
    ):
        self.symbols = symbols
        self.channels = channels
        self._book_cache = book_cache or BookCache()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._collector: OKXBookCollector | None = None
        self._book_count = 0

    def start(self):
        """Start the book worker thread."""
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="dataflow-books",
        )
        self._thread.start()
        logger.info(
            "Book worker started (%d symbols, channels=%s)",
            len(self.symbols),
            ",".join(self.channels),
        )

    def stop(self):
        """Stop the book worker thread gracefully."""
        if self._collector is not None:
            self._collector.stop()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_loop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("Book worker stopped")

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, BookEvent]:
        return self._book_cache.latest_snapshot(symbols)

    @property
    def cache(self) -> BookCache:
        return self._book_cache

    @property
    def book_count(self) -> int:
        return self._book_count

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
        collector = OKXBookCollector(
            symbols=self.symbols,
            on_books=self._on_books,
            channels=self.channels,
        )
        self._collector = collector
        try:
            await collector.run()
        except asyncio.CancelledError:
            pass

    def _on_books(self, events: list[BookEvent]):
        for event in events:
            self._book_cache.update(event)
            self._book_count += 1

