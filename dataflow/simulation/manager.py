"""Simulation DataflowManager — drop-in replacement for live DataflowManager.

Implements the same public interface so that Engine can switch between
live and simulation mode transparently.
"""

from __future__ import annotations

import threading

import numpy as np

from dataflow.livetrading.cache import BarCache

from .generator import BarGenerator
from .symbols import DEFAULT_BASE_PRICE, SYMBOL_BASE_PRICES
from .worker import SimBarWorker


class SimDataflowManager:
    """Manage a simulated bar data stream.

    Public interface mirrors ``dataflow.livetrading.manager.DataflowManager``
    so that ``Engine`` can delegate without knowing which backend is active.
    """

    def __init__(
        self,
        symbols: list[str],
        bar_interval_seconds: float = 1.0,
        bar_window_length: int = 1000,
        volatility: float = 0.001,
        base_volume: float = 100.0,
        seed: int | None = None,
    ):
        self.symbols = symbols

        # --- bar cache ---
        self.bar_cache = BarCache(window_length=bar_window_length)

        # --- generators (one per symbol, with deterministic seeding) ---
        generators: dict[str, BarGenerator] = {}
        for idx, symbol in enumerate(symbols):
            base_price = SYMBOL_BASE_PRICES.get(symbol, DEFAULT_BASE_PRICE)
            sym_seed = (seed + idx) if seed is not None else None
            generators[symbol] = BarGenerator(
                base_price=base_price,
                volatility=volatility,
                base_volume=base_volume,
                seed=sym_seed,
            )

        # --- worker ---
        self._bar_worker = SimBarWorker(
            symbols=symbols,
            bar_cache=self.bar_cache,
            generators=generators,
            interval_seconds=bar_interval_seconds,
        )

    # ---- lifecycle ----

    def start(self):
        self._bar_worker.start()

    def stop(self):
        self._bar_worker.stop()

    # ---- snapshot API (same as DataflowManager) ----

    def get_bar_snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        return self.bar_cache.snapshot(symbols)

    def get_trade_snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        return {}

    def get_book_snapshot(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        return {}

    # ---- counters ----

    @property
    def bar_count(self) -> int:
        return self._bar_worker.bar_count

    @property
    def trade_count(self) -> int:
        return 0

    @property
    def book_count(self) -> int:
        return 0

    # ---- backward-compatible accessors ----

    @property
    def data_cache(self) -> dict[str, np.ndarray]:
        return self.bar_cache.storage

    @property
    def lock(self) -> threading.Lock:
        return self.bar_cache.lock
