"""Helpers for querying OKX instrument metadata."""

from __future__ import annotations

import logging

import aiohttp

from .common import OKX_REST_INSTRUMENTS

logger = logging.getLogger(__name__)


async def fetch_all_swap_symbols(session: aiohttp.ClientSession) -> list[str]:
    """Fetch all live perpetual-swap instrument IDs from the OKX REST API."""
    async with session.get(OKX_REST_INSTRUMENTS) as resp:
        resp.raise_for_status()
        body = await resp.json()

    code = body.get("code")
    if code not in (None, "0", 0):
        raise RuntimeError(f"OKX instruments request failed: {body}")

    instruments = body.get("data", [])
    symbols = [inst["instId"] for inst in instruments if inst.get("state") == "live"]
    logger.info("Fetched %d live SWAP symbols from OKX", len(symbols))
    return sorted(symbols)

