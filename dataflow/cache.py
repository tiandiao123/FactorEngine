"""Thread-safe array cache for bar data.

This module lives at the dataflow top level so that both live (okx)
and simulation sub-packages can share BarCache without depending on
each other.
"""

from __future__ import annotations

import threading

import numpy as np

BAR_NUM_FIELDS = 8  # ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote


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
