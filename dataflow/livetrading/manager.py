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

    Workers and caches are only created for enabled data sources.
    By default only bars are enabled.
    """

    def __init__(
        self,
        symbols: list[str],
        bar_agg_seconds: int = 5,
        bar_window_length: int = 1000,
        trade_window_length: int = 10_000,
        book_history_length: int = 1_000,
        enable_bars: bool = True,
        enable_trades: bool = False,
        trade_channels: tuple[str, ...] = ("trades-all",),
        enable_books: bool = False,
        book_channels: tuple[str, ...] = ("books5",),
    ):
        self.symbols = symbols
        self.enable_bars = enable_bars
        self.enable_trades = enable_trades
        self.enable_books = enable_books

        # --- bars ---
        self.bar_cache: BarCache | None = None
        self._bar_worker: BarDataflowWorker | None = None
        if enable_bars:
            self.bar_cache = BarCache(window_length=bar_window_length)
            self._bar_worker = BarDataflowWorker(
                symbols=symbols,
                agg_seconds=bar_agg_seconds,
                window_length=bar_window_length,
                bar_cache=self.bar_cache,
            )

        # --- trades ---
        self.trade_cache: TradeCache | None = None
        self._trade_worker: TradeDataflowWorker | None = None
        if enable_trades:
            self.trade_cache = TradeCache(window_length=trade_window_length)
            self._trade_worker = TradeDataflowWorker(
                symbols=symbols,
                trade_cache=self.trade_cache,
                channels=trade_channels,
            )

        # --- books ---
        self.book_cache: BookCache | None = None
        self._book_worker: BookDataflowWorker | None = None
        if enable_books:
            self.book_cache = BookCache(history_length=book_history_length)
            self._book_worker = BookDataflowWorker(
                symbols=symbols,
                book_cache=self.book_cache,
                channels=book_channels,
            )

    def start(self):
        if self._bar_worker is not None:
            self._bar_worker.start()
        if self._trade_worker is not None:
            self._trade_worker.start()
        if self._book_worker is not None:
            self._book_worker.start()

    def stop(self):
        if self._bar_worker is not None:
            self._bar_worker.stop()
        if self._trade_worker is not None:
            self._trade_worker.stop()
        if self._book_worker is not None:
            self._book_worker.stop()

    def get_bar_snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        if self.bar_cache is None:
            return {}
        return self.bar_cache.snapshot(symbols)

    def get_trade_snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        if self.trade_cache is None:
            return {}
        return self.trade_cache.snapshot(symbols)

    def get_book_snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        if self.book_cache is None:
            return {}
        return self.book_cache.snapshot(symbols)

    @property
    def bar_count(self) -> int:
        return self._bar_worker.bar_count if self._bar_worker is not None else 0

    @property
    def trade_count(self) -> int:
        return self._trade_worker.trade_count if self._trade_worker is not None else 0

    @property
    def book_count(self) -> int:
        return self._book_worker.book_count if self._book_worker is not None else 0

    @property
    def data_cache(self) -> dict[str, np.ndarray]:
        """Backward-compatible access to the bar cache storage."""
        if self.bar_cache is None:
            return {}
        return self.bar_cache.storage

    @property
    def lock(self) -> threading.Lock:
        """Backward-compatible access to the bar cache lock."""
        if self.bar_cache is None:
            return threading.Lock()
        return self.bar_cache.lock
