"""OKX WebSocket collector for trade streams."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import aiohttp

from ..events import TradeEvent
from .common import (
    MAX_SUBS_PER_CONN,
    OKX_WS_BUSINESS,
    OKX_WS_PUBLIC,
    SUBS_BATCH_SIZE,
    chunk,
    now_ms,
)

logger = logging.getLogger(__name__)

_PUBLIC_TRADE_CHANNELS = {"trades"}
_BUSINESS_TRADE_CHANNELS = {"trades-all"}


class OKXTradeCollector:
    """Connect to OKX and stream trade-level updates for a symbol set."""

    def __init__(
        self,
        symbols: list[str],
        on_trades: Callable[[list[TradeEvent]], None],
        channels: tuple[str, ...] = ("trades-all",),
    ):
        invalid = set(channels) - (_PUBLIC_TRADE_CHANNELS | _BUSINESS_TRADE_CHANNELS)
        if invalid:
            raise ValueError(f"Unsupported trade channels: {sorted(invalid)}")

        self.symbols = symbols
        self.channels = channels
        self.on_trades = on_trades
        self._running = True

    async def run(self):
        async with aiohttp.ClientSession() as session:
            tasks = []
            public_channels = [channel for channel in self.channels if channel in _PUBLIC_TRADE_CHANNELS]
            if public_channels:
                tasks.extend(
                    asyncio.create_task(self._stream_loop(session, OKX_WS_PUBLIC, args))
                    for args in self._build_args(public_channels)
                )

            business_channels = [channel for channel in self.channels if channel in _BUSINESS_TRADE_CHANNELS]
            if business_channels:
                tasks.extend(
                    asyncio.create_task(self._stream_loop(session, OKX_WS_BUSINESS, args))
                    for args in self._build_args(business_channels)
                )

            await asyncio.gather(*tasks)

    def _build_args(self, channels: list[str]) -> list[list[dict]]:
        args = [
            {"channel": channel, "instId": symbol}
            for channel in channels
            for symbol in self.symbols
        ]
        return list(chunk(args, MAX_SUBS_PER_CONN))

    async def _stream_loop(self, session: aiohttp.ClientSession, url: str, args: list[dict]):
        while self._running:
            try:
                async with session.ws_connect(url, heartbeat=20) as ws:
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
                logger.warning("WS trade stream disconnected: %s — reconnecting in 3s", exc)
            except Exception:
                logger.exception("Unexpected trade WS error — reconnecting in 5s")
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
                logger.error("Trade subscription error: %s", payload)
            return

        rows = payload.get("data")
        if not rows:
            return

        arg = payload.get("arg", {})
        channel = arg.get("channel", "")
        inst_id = arg.get("instId", "")
        ts_recv = now_ms()
        events: list[TradeEvent] = []
        for row in rows:
            count = row.get("count")
            try:
                count_int = int(count) if count is not None else 1
            except (TypeError, ValueError):
                count_int = 1
            events.append(
                TradeEvent(
                    symbol=row.get("instId", inst_id),
                    channel=channel,
                    ts_event=int(row["ts"]),
                    ts_recv=ts_recv,
                    trade_id=row.get("tradeId"),
                    px=float(row["px"]),
                    sz=float(row["sz"]),
                    side=row.get("side", ""),
                    count=count_int,
                    is_aggregated=channel == "trades",
                )
            )

        self.on_trades(events)

    def stop(self):
        self._running = False
