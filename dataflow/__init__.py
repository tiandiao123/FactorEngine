"""Dataflow top-level package.

Sub-packages:
    dataflow.okx          — OKX exchange streaming adapters
    dataflow.simulation   — synthetic bar generator for offline dev
    dataflow.cache        — shared BarCache
"""

from .cache import BarCache, BAR_NUM_FIELDS

__all__ = [
    "BarCache",
    "BAR_NUM_FIELDS",
]
