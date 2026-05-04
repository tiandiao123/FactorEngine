"""Shared constants and helpers for Binance Futures (USDT-M) collectors."""

from __future__ import annotations

import time

# Production: /market prefix required since 2026 routing upgrade.
# aggTrade, kline, markPrice etc. are "market streams" that only flow
# through the /market route on production fstream.
BINANCE_FSTREAM_WS = "wss://fstream.binance.com/market/stream"
BINANCE_FSTREAM_WS_SINGLE = "wss://fstream.binance.com/market/ws"
BINANCE_FSTREAM_REST = "https://fapi.binance.com"

# Testnet: no /market prefix needed.
BINANCE_TESTNET_WS = "wss://stream.binancefuture.com/stream"
BINANCE_TESTNET_WS_SINGLE = "wss://stream.binancefuture.com/ws"
BINANCE_TESTNET_REST = "https://testnet.binancefuture.com"

MAX_STREAMS_PER_CONN = 200
SUBS_BATCH_SIZE = 50

SECONDS_TO_BINANCE_INTERVAL: dict[int, str] = {
    60: "1m",
    180: "3m",
    300: "5m",
    900: "15m",
    1800: "30m",
    3600: "1h",
    7200: "2h",
    14400: "4h",
    28800: "8h",
    43200: "12h",
    86400: "1d",
}


def to_binance_symbol(unified: str) -> str:
    """Convert unified symbol to Binance format (lowercase).

    ``"BTC-USDT-SWAP"`` → ``"btcusdt"``
    ``"BTC-USDT"``      → ``"btcusdt"``
    ``"ETH-USDT-SWAP"`` → ``"ethusdt"``
    """
    return unified.upper().replace("-SWAP", "").replace("-", "").lower()


def from_binance_symbol(binance: str) -> str:
    """Convert Binance symbol back to unified format.

    ``"BTCUSDT"`` → ``"BTC-USDT"``
    ``"btcusdt"`` → ``"BTC-USDT"``
    """
    s = binance.upper()
    if s.endswith("USDT"):
        base = s[: -len("USDT")]
        return f"{base}-USDT"
    return s


def build_combined_stream_url(
    stream_names: list[str], *, testnet: bool = False
) -> str:
    """Build a Binance combined stream URL.

    ``["btcusdt@aggTrade", "ethusdt@aggTrade"]``
    → ``"wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade"``
    """
    base = BINANCE_TESTNET_WS if testnet else BINANCE_FSTREAM_WS
    return f"{base}?streams={'/'.join(stream_names)}"


def get_ws_single_url(*, testnet: bool = False) -> str:
    """Get the single-stream WS base URL."""
    return BINANCE_TESTNET_WS_SINGLE if testnet else BINANCE_FSTREAM_WS_SINGLE


def now_ms() -> int:
    return int(time.time() * 1000)
