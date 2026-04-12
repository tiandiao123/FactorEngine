"""Shared constants and helpers for OKX collectors."""

from __future__ import annotations

import time

OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"
OKX_REST_INSTRUMENTS = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"

MAX_SUBS_PER_CONN = 200
SUBS_BATCH_SIZE = 50

# Mapping from seconds to OKX direct candle channel names.
# Frequencies >= 60s that appear here can be subscribed directly.
SECONDS_TO_OKX_CHANNEL: dict[int, str] = {
    1: "candle1s",
    60: "candle1m",
    180: "candle3m",
    300: "candle5m",
    900: "candle15m",
    1800: "candle30m",
    3600: "candle1H",
    7200: "candle2H",
    14400: "candle4H",
}


def resolve_bar_channel(freq_seconds: int) -> tuple[str, bool]:
    """Resolve data_freq (seconds) to (okx_channel, needs_aggregation).

    Returns:
        (channel, needs_aggregation)
        - freq < 60s  → ("candle1s", True)   aggregate locally
        - freq >= 60s → (direct channel, False)  subscribe directly

    Raises:
        ValueError: if freq >= 60s but has no matching OKX channel.
    """
    if freq_seconds < 60:
        return "candle1s", True
    channel = SECONDS_TO_OKX_CHANNEL.get(freq_seconds)
    if channel is None:
        supported = ", ".join(
            f"{s}s" for s in sorted(SECONDS_TO_OKX_CHANNEL) if s >= 60
        )
        raise ValueError(
            f"No direct OKX candle channel for {freq_seconds}s. "
            f"Supported direct channels (>=1min): {supported}. "
            f"For sub-minute frequencies, use a value < 60s (e.g. '5s', '10s', '30s')."
        )
    return channel, False


def chunk(lst: list, size: int):
    for idx in range(0, len(lst), size):
        yield lst[idx : idx + size]


def now_ms() -> int:
    return int(time.time() * 1000)

