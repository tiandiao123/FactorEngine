"""OKX WebSocket collector — subscribes to candle1s for all SWAP contracts."""

import asyncio
import json
import logging
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)

OKX_WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"
OKX_REST_INSTRUMENTS = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
MAX_SUBS_PER_CONN = 200


async def fetch_all_swap_symbols(session: aiohttp.ClientSession) -> list[str]:
    """Fetch all perpetual-swap instrument IDs from OKX REST API."""
    async with session.get(OKX_REST_INSTRUMENTS) as resp:
        body = await resp.json()
    instruments = body.get("data", [])
    symbols = [inst["instId"] for inst in instruments if inst.get("state") == "live"]
    logger.info("Fetched %d live SWAP symbols from OKX", len(symbols))
    return sorted(symbols)


def _chunk(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


class OKXCollector:
    """Connects to OKX WebSocket, subscribes candle1s, forwards to callback."""

    def __init__(self, symbols: list[str], on_candle1s: Callable[[list[dict]], None]):
        self.symbols = symbols
        self.on_candle1s = on_candle1s
        self._running = True

    async def run(self):
        async with aiohttp.ClientSession() as session:
            tasks = []
            for chunk in _chunk(self.symbols, MAX_SUBS_PER_CONN):
                tasks.append(asyncio.create_task(self._ws_loop(session, chunk)))
            await asyncio.gather(*tasks)

    async def _ws_loop(self, session: aiohttp.ClientSession, symbols: list[str]):
        while self._running:
            try:
                await self._connect_and_listen(session, symbols)
            except (aiohttp.WSServerHandshakeError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("WS candle1s disconnected: %s — reconnecting in 3s", e)
            except Exception:
                logger.exception("Unexpected WS error — reconnecting in 5s")
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(3)

    async def _connect_and_listen(self, session: aiohttp.ClientSession, symbols: list[str]):
        async with session.ws_connect(OKX_WS_BUSINESS, heartbeat=20) as ws:
            logger.info("WS connected for candle1s (%d symbols)", len(symbols))
            for batch in _chunk([{"channel": "candle1s", "instId": s} for s in symbols], 50):
                await ws.send_json({"op": "subscribe", "args": batch})
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    def _dispatch(self, raw: str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        records = payload.get("data")
        if not records:
            return
        arg = payload.get("arg", {})
        inst_id = arg.get("instId", "")
        wrapped = [{"instId": inst_id, "raw": r} for r in records]
        self.on_candle1s(wrapped)

    def stop(self):
        self._running = False
