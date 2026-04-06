"""Shared constants and helpers for OKX collectors."""

from __future__ import annotations

import time

OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"
OKX_REST_INSTRUMENTS = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"

MAX_SUBS_PER_CONN = 200
SUBS_BATCH_SIZE = 50


def chunk(lst: list, size: int):
    for idx in range(0, len(lst), size):
        yield lst[idx : idx + size]


def now_ms() -> int:
    return int(time.time() * 1000)

