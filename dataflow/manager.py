"""Top-level manager for dataflow workers and caches."""

from __future__ import annotations

import threading

import numpy as np

from .bars.worker import BarDataflowWorker
from .books.worker import BookDataflowWorker
from .cache import BarCache, BookCache, TradeCache
from .trades.worker import TradeDataflowWorker


class DataflowManager:
    """Manage the lifecycle of all dataflow workers and caches.

    Today the manager only starts the bar worker, but the cache layout is already
    split for future trades/books workers.
    """

    def __init__(
        self,
        symbols: list[str],
        bar_agg_seconds: int = 5,
        bar_window_length: int = 1000,
        trade_window_length: int = 10_000,
        book_history_length: int = 1_000,
        enable_trades: bool = False,
        trade_channels: tuple[str, ...] = ("trades-all",),
        enable_books: bool = False,
        book_channels: tuple[str, ...] = ("books5",),
    ):
        self.symbols = symbols
        self.enable_trades = enable_trades
        self.trade_channels = trade_channels
        self.enable_books = enable_books
        self.book_channels = book_channels

        self._bar_storage: dict[str, np.ndarray] = {}
        self._bar_lock = threading.Lock()
        self.bar_cache = BarCache(
            window_length=bar_window_length,
            storage=self._bar_storage,
            lock=self._bar_lock,
        )
        self.trade_cache = TradeCache(window_length=trade_window_length)
        self.book_cache = BookCache(history_length=book_history_length)

        self._bar_worker = BarDataflowWorker(
            symbols=symbols,
            agg_seconds=bar_agg_seconds,
            window_length=bar_window_length,
            bar_cache=self.bar_cache,
        )
        self._trade_worker = TradeDataflowWorker(
            symbols=symbols,
            trade_cache=self.trade_cache,
            channels=trade_channels,
        )
        self._book_worker = BookDataflowWorker(
            symbols=symbols,
            book_cache=self.book_cache,
            channels=book_channels,
        )

    def start(self):
        self._bar_worker.start()
        if self.enable_trades:
            self._trade_worker.start()
        if self.enable_books:
            self._book_worker.start()

    def stop(self):
        self._bar_worker.stop()
        if self.enable_trades:
            self._trade_worker.stop()
        if self.enable_books:
            self._book_worker.stop()

    def get_bar_snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        return self.bar_cache.snapshot(symbols)

    def get_trade_snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        return self.trade_cache.snapshot(symbols)

    def get_book_snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        return self.book_cache.snapshot(symbols)

    @property
    def bar_count(self) -> int:
        return self._bar_worker.bar_count

    @property
    def trade_count(self) -> int:
        return self._trade_worker.trade_count if self.enable_trades else 0

    @property
    def book_count(self) -> int:
        return self._book_worker.book_count if self.enable_books else 0

    @property
    def data_cache(self) -> dict[str, np.ndarray]:
        """Backward-compatible access to the bar cache storage."""
        return self.bar_cache.storage

    @property
    def lock(self) -> threading.Lock:
        """Backward-compatible access to the bar cache lock."""
        return self.bar_cache.lock
