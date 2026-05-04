"""Dataflow top-level package.

Sub-packages:
    dataflow.kline        — exchange-agnostic kline components (KlinePublisher)
    dataflow.trades       — exchange-agnostic trades components (TradesAccumulator)
    dataflow.okx          — OKX exchange streaming adapters
    dataflow.binance      — Binance Futures (USDT-M) streaming adapters
    dataflow.simulation   — synthetic bar generator for offline dev
    dataflow.cache        — shared BarCache
    dataflow.base_collector — BaseCollector ABC with auto-reconnect
"""

from .cache import BarCache, BAR_NUM_FIELDS

__all__ = [
    "BarCache",
    "BAR_NUM_FIELDS",
]
