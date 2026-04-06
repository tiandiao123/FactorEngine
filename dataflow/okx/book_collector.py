"""OKX WebSocket collector for shallow order-book streams."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import aiohttp

from ..events import BookEvent, BookLevel
from .common import MAX_SUBS_PER_CONN, OKX_WS_PUBLIC, SUBS_BATCH_SIZE, chunk, now_ms

logger = logging.getLogger(__name__)

_PUBLIC_BOOK_CHANNELS = {"books5"}


class OKXBookCollector:
    """Connect to OKX and stream shallow order-book updates for a symbol set."""

    def __init__(
        self,
        symbols: list[str],
        on_books: Callable[[list[BookEvent]], None],
        channels: tuple[str, ...] = ("books5",),
    ):
        invalid = set(channels) - _PUBLIC_BOOK_CHANNELS
        if invalid:
            raise ValueError(f"Unsupported book channels: {sorted(invalid)}")

        self.symbols = symbols
        self.channels = channels
        self.on_books = on_books
        self._running = True

    async def run(self):
        async with aiohttp.ClientSession() as session:
            tasks = [
                asyncio.create_task(self._stream_loop(session, args))
                for args in self._build_args()
            ]
            await asyncio.gather(*tasks)

    def _build_args(self) -> list[list[dict]]:
        args = [
            {"channel": channel, "instId": symbol}
            for channel in self.channels
            for symbol in self.symbols
        ]
        return list(chunk(args, MAX_SUBS_PER_CONN))

    async def _stream_loop(self, session: aiohttp.ClientSession, args: list[dict]):
        while self._running:
            try:
                async with session.ws_connect(OKX_WS_PUBLIC, heartbeat=20) as ws:
                    channels = sorted({arg["channel"] for arg in args})
                    logger.info("WS connected for %s (%d args)", ",".join(channels), len(args))
                    for batch in chunk(args, SUBS_BATCH_SIZE):
                        await ws.send_json({"op": "subscribe", "args": batch})

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._dispatch(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except (aiohttp.WSServerHandshakeError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("WS book stream disconnected: %s — reconnecting in 3s", exc)
            except Exception:
                logger.exception("Unexpected book WS error — reconnecting in 5s")
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(3)

    def _dispatch(self, raw: str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        if "event" in payload:
            if payload.get("event") == "error":
                logger.error("Book subscription error: %s", payload)
            return

        rows = payload.get("data")
        if not rows:
            return

        arg = payload.get("arg", {})
        channel = arg.get("channel", "")
        inst_id = arg.get("instId", "")
        ts_recv = now_ms()
        events: list[BookEvent] = []
        for row in rows:
            bids = [
                BookLevel(
                    px=float(level[0]),
                    sz=float(level[1]),
                    orders=int(level[3]) if len(level) > 3 and level[3] else None,
                )
                for level in row.get("bids", [])
            ]
            asks = [
                BookLevel(
                    px=float(level[0]),
                    sz=float(level[1]),
                    orders=int(level[3]) if len(level) > 3 and level[3] else None,
                )
                for level in row.get("asks", [])
            ]
            if not bids or not asks:
                continue

            events.append(
                BookEvent(
                    symbol=inst_id,
                    channel=channel,
                    ts_event=int(row["ts"]),
                    ts_recv=ts_recv,
                    best_bid_px=bids[0].px,
                    best_bid_sz=bids[0].sz,
                    best_ask_px=asks[0].px,
                    best_ask_sz=asks[0].sz,
                    bids=bids,
                    asks=asks,
                )
            )

        if events:
            self.on_books(events)

    def stop(self):
        self._running = False

