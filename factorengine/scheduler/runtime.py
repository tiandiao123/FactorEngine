"""Minimal factor runtime for the Python scheduler prototype."""

from __future__ import annotations

import time

import numpy as np

from dataflow.events import ASK_SZ_SLICE, BAR_NUM_FIELDS, BID_SZ_SLICE, BOOK_NUM_FIELDS, TRADE_NUM_FIELDS
from factorengine.engine import Engine

from .factor_snapshot import FactorSnapshot
from .factor_spec import FactorSpec


SOURCE_TO_WIDTH = {
    "bars": BAR_NUM_FIELDS,
    "trades": TRADE_NUM_FIELDS,
    "books": BOOK_NUM_FIELDS,
}


def compute_bar_momentum(window: np.ndarray) -> float:
    """Return close-to-close momentum over the selected bar window."""
    if len(window) < 2:
        return 0.0
    start = window[0, 4]
    end = window[-1, 4]
    if start == 0:
        return 0.0
    return float(end / start - 1.0)


def compute_trade_imbalance(window: np.ndarray) -> float:
    """Compute signed trade-volume imbalance over the selected trade window."""
    if len(window) == 0:
        return 0.0
    sz = window[:, 1]
    side = window[:, 2]
    denom = np.sum(np.abs(sz))
    if denom == 0:
        return 0.0
    return float(np.sum(sz * side) / denom)


def compute_book_l1_imbalance(window: np.ndarray) -> float:
    """Compute L1 size imbalance from the latest book row."""
    if len(window) == 0:
        return 0.0
    latest = window[-1]
    bid_sz1 = latest[BID_SZ_SLICE.start]
    ask_sz1 = latest[ASK_SZ_SLICE.start]
    denom = bid_sz1 + ask_sz1
    if denom == 0:
        return 0.0
    return float((bid_sz1 - ask_sz1) / denom)


def compute_book_l5_imbalance(window: np.ndarray) -> float:
    """Compute top-5 size imbalance from the latest book row."""
    if len(window) == 0:
        return 0.0
    latest = window[-1]
    bid_sum = float(np.nansum(latest[BID_SZ_SLICE]))
    ask_sum = float(np.nansum(latest[ASK_SZ_SLICE]))
    denom = bid_sum + ask_sum
    if denom == 0:
        return 0.0
    return float((bid_sum - ask_sum) / denom)


class FactorRuntime:
    """Pull current cache snapshots, slice windows and compute factor values."""

    def __init__(self, engine: Engine, symbols: list[str], factor_specs: list[FactorSpec]):
        self.engine = engine
        self.symbols = symbols
        self.factor_specs = factor_specs
        self._sources = {spec.source for spec in factor_specs}

    def evaluate(self, tick_id: int, ts_eval_ms: int) -> FactorSnapshot:
        """Run one evaluation tick across all configured symbols and factors."""
        started = time.perf_counter()
        snapshots = self._fetch_snapshots()
        values: dict[str, dict[str, float]] = {}

        for symbol in self.symbols:
            symbol_values: dict[str, float] = {}
            for spec in self.factor_specs:
                raw = snapshots[spec.source].get(symbol)
                window = self._slice_window(raw, spec.source, spec.window)
                symbol_values[spec.name] = float(spec.compute_fn(window))
            values[symbol] = symbol_values

        duration_ms = (time.perf_counter() - started) * 1000
        return FactorSnapshot(
            tick_id=tick_id,
            ts_eval_ms=ts_eval_ms,
            duration_ms=duration_ms,
            values=values,
        )

    def _fetch_snapshots(self) -> dict[str, dict[str, np.ndarray]]:
        snapshots: dict[str, dict[str, np.ndarray]] = {}
        if "bars" in self._sources:
            snapshots["bars"] = self.engine.get_data(self.symbols)
        if "trades" in self._sources:
            snapshots["trades"] = self.engine.get_trade_data(self.symbols)
        if "books" in self._sources:
            snapshots["books"] = self.engine.get_book_data(self.symbols)
        return snapshots

    def _slice_window(self, arr: np.ndarray | None, source: str, window: int) -> np.ndarray:
        if arr is None or len(arr) == 0:
            return np.empty((0, SOURCE_TO_WIDTH[source]), dtype=np.float64)
        if len(arr) <= window:
            return arr.copy()
        return arr[-window:].copy()
