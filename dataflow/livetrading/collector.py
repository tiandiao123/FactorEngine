"""Backward-compatible exports for the OKX bar collector."""

from .okx.bar_collector import OKXBarCollector as OKXCollector
from .okx.symbols import fetch_all_swap_symbols

__all__ = ["OKXCollector", "fetch_all_swap_symbols"]
