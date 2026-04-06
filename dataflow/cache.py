"""Thread-safe cache containers for dataflow streams."""

from __future__ import annotations

import copy
from collections import deque
import threading

import numpy as np

from .events import BarEvent, BookEvent, TradeEvent

BAR_NUM_FIELDS = 6


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

    def append(self, event: BarEvent):
        row = np.array(
            [event.ts_event, event.open, event.high, event.low, event.close, event.vol],
            dtype=np.float64,
        )
        with self._lock:
            if event.symbol not in self._data:
                self._data[event.symbol] = row.reshape(1, BAR_NUM_FIELDS)
                return

            arr = np.vstack([self._data[event.symbol], row])
            if len(arr) > self.window_length:
                arr = arr[-self.window_length :]
            self._data[event.symbol] = arr

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
    """Thread-safe rolling trade cache."""

    def __init__(
        self,
        window_length: int = 10_000,
        storage: dict[str, deque[TradeEvent]] | None = None,
        lock: threading.Lock | None = None,
    ):
        self.window_length = window_length
        self._data = storage if storage is not None else {}
        self._lock = lock or threading.Lock()

    def append(self, event: TradeEvent):
        with self._lock:
            buf = self._data.setdefault(event.symbol, deque(maxlen=self.window_length))
            buf.append(event)

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, list[TradeEvent]]:
        with self._lock:
            if symbols is None:
                target = self._data.items()
            else:
                target = (
                    (sym, self._data[sym])
                    for sym in symbols
                    if sym in self._data
                )
            return {sym: list(copy.deepcopy(buf)) for sym, buf in target}

    def get_window(self, symbol: str, limit: int | None = None) -> list[TradeEvent]:
        with self._lock:
            buf = self._data.get(symbol)
            if not buf:
                return []
            rows = list(buf)
            if limit is not None:
                rows = rows[-limit:]
            return copy.deepcopy(rows)

    def latest(self, symbol: str) -> TradeEvent | None:
        with self._lock:
            buf = self._data.get(symbol)
            if not buf:
                return None
            return copy.deepcopy(buf[-1])

    @property
    def lock(self) -> threading.Lock:
        return self._lock


class BookCache:
    """Thread-safe shallow order-book cache with latest snapshot and short history."""

    def __init__(
        self,
        history_length: int = 1_000,
        latest: dict[str, BookEvent] | None = None,
        history: dict[str, deque[BookEvent]] | None = None,
        lock: threading.Lock | None = None,
    ):
        self.history_length = history_length
        self._latest = latest if latest is not None else {}
        self._history = history if history is not None else {}
        self._lock = lock or threading.Lock()

    def update(self, event: BookEvent):
        with self._lock:
            self._latest[event.symbol] = event
            buf = self._history.setdefault(event.symbol, deque(maxlen=self.history_length))
            buf.append(event)

    def latest(self, symbol: str) -> BookEvent | None:
        with self._lock:
            event = self._latest.get(symbol)
            if event is None:
                return None
            return copy.deepcopy(event)

    def latest_snapshot(self, symbols: list[str] | None = None) -> dict[str, BookEvent]:
        with self._lock:
            if symbols is None:
                target = self._latest.items()
            else:
                target = (
                    (sym, self._latest[sym])
                    for sym in symbols
                    if sym in self._latest
                )
            return {sym: copy.deepcopy(event) for sym, event in target}

    def get_window(self, symbol: str, limit: int | None = None) -> list[BookEvent]:
        with self._lock:
            buf = self._history.get(symbol)
            if not buf:
                return []
            rows = list(buf)
            if limit is not None:
                rows = rows[-limit:]
            return copy.deepcopy(rows)

    @property
    def lock(self) -> threading.Lock:
        return self._lock

