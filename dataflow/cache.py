"""Thread-safe array caches for bars, trades and books."""

from __future__ import annotations

import threading

import numpy as np

from .events import BAR_NUM_FIELDS, BOOK_NUM_FIELDS, TRADE_NUM_FIELDS


class BarCache:
    """Thread-safe rolling bar cache backed by numpy arrays."""

    def __init__(
        self,
        window_length: int = 1000,
        storage: dict[str, np.ndarray] | None = None,
        lock: threading.Lock | None = None,
    ):
        self.window_length = window_length
        self._data = storage if storage is not None else {}
        self._lock = lock or threading.Lock()

    def append(self, symbol: str, row: np.ndarray):
        row = np.asarray(row, dtype=np.float64).reshape(1, BAR_NUM_FIELDS)
        with self._lock:
            if symbol not in self._data:
                self._data[symbol] = row
                return

            arr = np.vstack([self._data[symbol], row])
            if len(arr) > self.window_length:
                arr = arr[-self.window_length :]
            self._data[symbol] = arr

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        with self._lock:
            if symbols is None:
                return {sym: arr.copy() for sym, arr in self._data.items()}
            return {
                sym: self._data[sym].copy()
                for sym in symbols
                if sym in self._data
            }

    def latest(self, symbol: str) -> np.ndarray | None:
        with self._lock:
            arr = self._data.get(symbol)
            if arr is None or len(arr) == 0:
                return None
            return arr[-1].copy()

    @property
    def storage(self) -> dict[str, np.ndarray]:
        return self._data

    @property
    def lock(self) -> threading.Lock:
        return self._lock


class TradeCache:
    """Thread-safe rolling trade cache backed by dense numeric arrays."""

    def __init__(
        self,
        window_length: int = 10_000,
        storage: dict[str, np.ndarray] | None = None,
        lock: threading.Lock | None = None,
    ):
        self.window_length = window_length
        self._data = storage if storage is not None else {}
        self._lock = lock or threading.Lock()

    def extend(self, symbol: str, rows: np.ndarray):
        rows = np.asarray(rows, dtype=np.float64)
        if rows.size == 0:
            return
        rows = rows.reshape(-1, TRADE_NUM_FIELDS)
        with self._lock:
            if symbol not in self._data:
                self._data[symbol] = rows[-self.window_length :]
                return

            arr = np.vstack([self._data[symbol], rows])
            if len(arr) > self.window_length:
                arr = arr[-self.window_length :]
            self._data[symbol] = arr

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        with self._lock:
            if symbols is None:
                return {sym: arr.copy() for sym, arr in self._data.items()}
            return {
                sym: self._data[sym].copy()
                for sym in symbols
                if sym in self._data
            }

    def get_window(self, symbol: str, limit: int | None = None) -> np.ndarray:
        with self._lock:
            arr = self._data.get(symbol)
            if arr is None or len(arr) == 0:
                return np.empty((0, TRADE_NUM_FIELDS), dtype=np.float64)
            rows = arr
            if limit is not None:
                rows = rows[-limit:]
            return rows.copy()

    def latest(self, symbol: str) -> np.ndarray | None:
        with self._lock:
            arr = self._data.get(symbol)
            if arr is None or len(arr) == 0:
                return None
            return arr[-1].copy()

    @property
    def lock(self) -> threading.Lock:
        return self._lock


class BookCache:
    """Thread-safe shallow order-book cache backed by dense numeric arrays."""

    def __init__(
        self,
        history_length: int = 1_000,
        storage: dict[str, np.ndarray] | None = None,
        lock: threading.Lock | None = None,
    ):
        self.history_length = history_length
        self._data = storage if storage is not None else {}
        self._lock = lock or threading.Lock()

    def extend(self, symbol: str, rows: np.ndarray):
        rows = np.asarray(rows, dtype=np.float64)
        if rows.size == 0:
            return
        rows = rows.reshape(-1, BOOK_NUM_FIELDS)
        with self._lock:
            if symbol not in self._data:
                self._data[symbol] = rows[-self.history_length :]
                return

            arr = np.vstack([self._data[symbol], rows])
            if len(arr) > self.history_length:
                arr = arr[-self.history_length :]
            self._data[symbol] = arr

    def latest(self, symbol: str) -> np.ndarray | None:
        with self._lock:
            arr = self._data.get(symbol)
            if arr is None or len(arr) == 0:
                return None
            return arr[-1].copy()

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        with self._lock:
            if symbols is None:
                return {sym: arr.copy() for sym, arr in self._data.items()}
            return {
                sym: self._data[sym].copy()
                for sym in symbols
                if sym in self._data
            }

    def latest_snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        return self.snapshot(symbols)

    def get_window(self, symbol: str, limit: int | None = None) -> np.ndarray:
        with self._lock:
            arr = self._data.get(symbol)
            if arr is None or len(arr) == 0:
                return np.empty((0, BOOK_NUM_FIELDS), dtype=np.float64)
            rows = arr
            if limit is not None:
                rows = rows[-limit:]
            return rows.copy()

    @property
    def lock(self) -> threading.Lock:
        return self._lock
