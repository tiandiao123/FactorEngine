"""Helpers for querying Binance Futures instrument metadata."""

from __future__ import annotations

import logging

import aiohttp

from .common import BINANCE_FSTREAM_REST

logger = logging.getLogger(__name__)

EXCHANGE_INFO_URL = f"{BINANCE_FSTREAM_REST}/fapi/v1/exchangeInfo"


async def fetch_all_perp_symbols(session: aiohttp.ClientSession) -> list[str]:
    """Fetch all active USDT-margined perpetual symbols from Binance Futures.

    Returns unified format symbols, e.g. ``["BTC-USDT", "ETH-USDT", ...]``.
    """
    async with session.get(EXCHANGE_INFO_URL) as resp:
        resp.raise_for_status()
        body = await resp.json()

    symbols: list[str] = []
    for info in body.get("symbols", []):
        if (
            info.get("contractType") == "PERPETUAL"
            and info.get("quoteAsset") == "USDT"
            and info.get("status") == "TRADING"
        ):
            base = info["baseAsset"]
            symbols.append(f"{base}-USDT")

    logger.info("Fetched %d live USDT-M perpetual symbols from Binance", len(symbols))
    return sorted(symbols)


DEFAULT_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "DOGE-USDT",
    "XRP-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT",
    "SUI-USDT", "NEAR-USDT", "APT-USDT", "ARB-USDT", "OP-USDT",
    "PEPE-USDT", "WIF-USDT", "SEI-USDT", "TIA-USDT", "INJ-USDT",
    "FET-USDT", "RENDER-USDT", "JUP-USDT", "WLD-USDT", "ONDO-USDT",
    "FIL-USDT", "LTC-USDT", "UNI-USDT", "ATOM-USDT", "MATIC-USDT",
]
