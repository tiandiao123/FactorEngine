"""Simulation dataflow package — synthetic bar data for offline development."""

from .generator import BarGenerator
from .manager import SimDataflowManager
from .symbols import DEFAULT_SYMBOLS, EXTENDED_SYMBOLS, SYMBOL_BASE_PRICES
from .worker import SimBarWorker

__all__ = [
    "BarGenerator",
    "SimBarWorker",
    "SimDataflowManager",
    "DEFAULT_SYMBOLS",
    "EXTENDED_SYMBOLS",
    "SYMBOL_BASE_PRICES",
]
