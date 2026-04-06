"""FactorEngine — the top-level Engine class.

Engine is the single entry point: it owns the Dataflow thread and exposes
get_data() for pulling snapshots from the shared data_cache.

Usage:
    engine = Engine(symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                    data_freq="5s", pull_interval="10s")
    engine.start()
    snapshot = engine.get_data()        # {symbol: ndarray(N, 6)}
    snapshot = engine.get_data(["BTC-USDT-SWAP"])  # filtered
    engine.stop()
"""

import logging
import re

import numpy as np

from dataflow.manager import DataflowManager

logger = logging.getLogger(__name__)

_FREQ_RE = re.compile(r"^(\d+)(s|sec|m|min|h|hr)$", re.IGNORECASE)
_UNIT_TO_SECONDS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600}


def parse_freq(freq: str) -> int:
    """Parse a frequency string like '5s', '10s', '1min', '1h' into seconds."""
    m = _FREQ_RE.match(freq.strip())
    if not m:
        raise ValueError(f"Invalid frequency: {freq!r}. Examples: '1s', '5s', '10s', '1min', '1h'")
    return int(m.group(1)) * _UNIT_TO_SECONDS[m.group(2).lower()]


class Engine:
    """Top-level entry point. Owns the dataflow manager and shared bar cache."""

    def __init__(
        self,
        symbols: list[str],
        data_freq: str = "5s",
        pull_interval: str = "10s",
        bar_window_length: int = 1000,
        trade_window_length: int = 10_000,
        book_history_length: int = 1_000,
        enable_trades: bool = False,
        trade_channels: tuple[str, ...] = ("trades-all",),
        enable_books: bool = False,
        book_channels: tuple[str, ...] = ("books5",),
    ):
        self.symbols = symbols
        self.data_freq = data_freq
        self.pull_interval = pull_interval
        self.bar_window_length = bar_window_length
        self.trade_window_length = trade_window_length
        self.book_history_length = book_history_length
        self.enable_trades = enable_trades
        self.trade_channels = trade_channels
        self.enable_books = enable_books
        self.book_channels = book_channels

        self.data_freq_seconds = parse_freq(data_freq)
        self.pull_interval_seconds = parse_freq(pull_interval)

        self._dataflow = DataflowManager(
            symbols=symbols,
            bar_agg_seconds=self.data_freq_seconds,
            bar_window_length=bar_window_length,
            trade_window_length=trade_window_length,
            book_history_length=book_history_length,
            enable_trades=enable_trades,
            trade_channels=trade_channels,
            enable_books=enable_books,
            book_channels=book_channels,
        )
        # Backward-compatible accessors for the current bar cache.
        self._data_cache = self._dataflow.data_cache
        self._lock = self._dataflow.lock

    def start(self):
        """Start the dataflow collection threads."""
        self._dataflow.start()
        logger.info(
            "Engine started: %d symbols, data_freq=%s (%ds), pull_interval=%s (%ds), "
            "bar_window=%d, trade_window=%d, book_history=%d",
                     len(self.symbols), self.data_freq, self.data_freq_seconds,
                     self.pull_interval, self.pull_interval_seconds, self.bar_window_length,
                     self.trade_window_length, self.book_history_length)

    def stop(self):
        """Stop the dataflow collection thread."""
        self._dataflow.stop()
        logger.info("Engine stopped")

    def get_data(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        """Get a snapshot (copy) of the bar cache.

        Args:
            symbols: If provided, only return data for these symbols.
                     If None, return all symbols in the cache.

        Returns:
            {symbol: ndarray of shape (N, 6)} where columns are
            [ts, open, high, low, close, vol].
            Each array is an independent copy — safe to use freely.
        """
        return self._dataflow.get_bar_snapshot(symbols)

    def get_trade_data(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        """Get a snapshot (copy) of the trade cache.

        Returns:
            {symbol: ndarray of shape (N, 3)} with columns [px, sz, side].
        """
        return self._dataflow.get_trade_snapshot(symbols)

    def get_book_data(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        """Get a snapshot (copy) of the book cache.

        Returns:
            {symbol: ndarray of shape (N, 20)} representing books5 rows.
        """
        return self._dataflow.get_book_snapshot(symbols)

    @property
    def bar_count(self) -> int:
        """Total number of 5s bars aggregated so far."""
        return self._dataflow.bar_count

    @property
    def trade_count(self) -> int:
        """Total number of trade events captured so far."""
        return self._dataflow.trade_count

    @property
    def book_count(self) -> int:
        """Total number of book events captured so far."""
        return self._dataflow.book_count
