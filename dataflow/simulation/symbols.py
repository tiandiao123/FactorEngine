"""Pre-defined symbol lists for simulation mode.

These mirror the naming convention used by OKX perpetual swaps
so that downstream code (Engine, Scheduler, FactorRuntime) can
work with the exact same symbol format as live trading.
"""

from __future__ import annotations

# Major perpetual swap contracts — commonly used subset.
DEFAULT_SYMBOLS: list[str] = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "XRP-USDT-SWAP",
]

# Broader universe for stress / capacity testing.
EXTENDED_SYMBOLS: list[str] = DEFAULT_SYMBOLS + [
    "BNB-USDT-SWAP",
    "ADA-USDT-SWAP",
    "AVAX-USDT-SWAP",
    "DOT-USDT-SWAP",
    "LINK-USDT-SWAP",
    "MATIC-USDT-SWAP",
    "UNI-USDT-SWAP",
    "ATOM-USDT-SWAP",
    "LTC-USDT-SWAP",
    "ARB-USDT-SWAP",
    "OP-USDT-SWAP",
    "APT-USDT-SWAP",
    "FIL-USDT-SWAP",
    "NEAR-USDT-SWAP",
    "SUI-USDT-SWAP",
]

# Rough reference base prices (USD) for synthetic data generators.
# Only needs to be in the right order of magnitude.
SYMBOL_BASE_PRICES: dict[str, float] = {
    "BTC-USDT-SWAP": 85000.0,
    "ETH-USDT-SWAP": 3000.0,
    "SOL-USDT-SWAP": 150.0,
    "DOGE-USDT-SWAP": 0.15,
    "XRP-USDT-SWAP": 0.55,
    "BNB-USDT-SWAP": 600.0,
    "ADA-USDT-SWAP": 0.45,
    "AVAX-USDT-SWAP": 35.0,
    "DOT-USDT-SWAP": 7.0,
    "LINK-USDT-SWAP": 15.0,
    "MATIC-USDT-SWAP": 0.50,
    "UNI-USDT-SWAP": 7.0,
    "ATOM-USDT-SWAP": 9.0,
    "LTC-USDT-SWAP": 90.0,
    "ARB-USDT-SWAP": 1.10,
    "OP-USDT-SWAP": 1.80,
    "APT-USDT-SWAP": 8.0,
    "FIL-USDT-SWAP": 5.0,
    "NEAR-USDT-SWAP": 5.0,
    "SUI-USDT-SWAP": 3.5,
}

# Fallback price for unknown symbols.
DEFAULT_BASE_PRICE: float = 100.0
