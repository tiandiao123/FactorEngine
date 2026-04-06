"""OKX WebSocket collector for candle1s data."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import aiohttp

from .common import MAX_SUBS_PER_CONN, OKX_WS_BUSINESS, SUBS_BATCH_SIZE, chunk, now_ms

logger = logging.getLogger(__name__)


class OKXBarCollector:
    """Connect to OKX and stream candle1s data for a symbol set."""

    def __init__(self, symbols: list[str], on_candle1s: Callable[[list[dict]], None]):
        self.symbols = symbols
        self.on_candle1s = on_candle1s
        self._running = True

    async def run(self):
        async with aiohttp.ClientSession() as session:
            tasks = [
                asyncio.create_task(self._ws_loop(session, group))
                for group in chunk(self.symbols, MAX_SUBS_PER_CONN)
            ]
            await asyncio.gather(*tasks)

    async def _ws_loop(self, session: aiohttp.ClientSession, symbols: list[str]):
        while self._running:
            try:
                await self._connect_and_listen(session, symbols)
            except (aiohttp.WSServerHandshakeError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("WS candle1s disconnected: %s — reconnecting in 3s", exc)
            except Exception:
                logger.exception("Unexpected WS error — reconnecting in 5s")
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(3)

    async def _connect_and_listen(self, session: aiohttp.ClientSession, symbols: list[str]):
        async with session.ws_connect(OKX_WS_BUSINESS, heartbeat=20) as ws:
            logger.info("WS connected for candle1s (%d symbols)", len(symbols))
            args = [{"channel": "candle1s", "instId": symbol} for symbol in symbols]
            for batch in chunk(args, SUBS_BATCH_SIZE):
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
        channel = arg.get("channel", "")
        ts_recv = now_ms()
        wrapped = [
            {
                "instId": inst_id,
                "channel": channel,
                "ts_recv": ts_recv,
                "raw": record,
            }
            for record in records
        ]
        self.on_candle1s(wrapped)

    def stop(self):
        self._running = False

