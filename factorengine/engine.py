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
import threading

import numpy as np

from dataflow.dataflow import Dataflow

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
    """Top-level entry point. Owns the Dataflow thread and shared cache."""

    def __init__(
        self,
        symbols: list[str],
        data_freq: str = "5s",
        pull_interval: str = "10s",
        window_length: int = 1000,
    ):
        self.symbols = symbols
        self.data_freq = data_freq
        self.pull_interval = pull_interval
        self.window_length = window_length

        self.data_freq_seconds = parse_freq(data_freq)
        self.pull_interval_seconds = parse_freq(pull_interval)

        self._data_cache: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()

        self._dataflow = Dataflow(
            symbols=symbols,
            data_cache=self._data_cache,
            lock=self._lock,
            agg_seconds=self.data_freq_seconds,
            window_length=window_length,
        )

    def start(self):
        """Start the dataflow collection thread."""
        self._dataflow.start()
        logger.info("Engine started: %d symbols, data_freq=%s (%ds), pull_interval=%s (%ds), window=%d",
                     len(self.symbols), self.data_freq, self.data_freq_seconds,
                     self.pull_interval, self.pull_interval_seconds, self.window_length)

    def stop(self):
        """Stop the dataflow collection thread."""
        self._dataflow.stop()
        logger.info("Engine stopped")

    def get_data(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        """Get a snapshot (copy) of the data cache.

        Args:
            symbols: If provided, only return data for these symbols.
                     If None, return all symbols in the cache.

        Returns:
            {symbol: ndarray of shape (N, 6)} where columns are
            [ts, open, high, low, close, vol].
            Each array is an independent copy — safe to use freely.
        """
        with self._lock:
            if symbols is None:
                return {sym: arr.copy() for sym, arr in self._data_cache.items()}
            return {
                sym: self._data_cache[sym].copy()
                for sym in symbols
                if sym in self._data_cache
            }

    @property
    def bar_count(self) -> int:
        """Total number of 5s bars aggregated so far."""
        return self._dataflow.bar_count
