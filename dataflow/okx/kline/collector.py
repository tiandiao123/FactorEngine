"""OKX WebSocket collector for kline (candlestick) streams.

Connects to OKX business WebSocket and streams candle data.  Each confirmed
candle fires the ``on_candle(symbol, raw)`` callback exactly once.

The collector is intentionally thin: it only handles WS lifecycle (connect,
subscribe, reconnect) and JSON parsing.  All filtering (confirmed-only),
aggregation, and publishing logic lives downstream in KlineAdapter /
KlinePublisher.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

import aiohttp

from dataflow.base_collector import BaseCollector

from ..common import MAX_SUBS_PER_CONN, OKX_WS_BUSINESS, SUBS_BATCH_SIZE

logger = logging.getLogger(__name__)


class OKXKlineCollector(BaseCollector):
    """Connect to OKX and stream candle updates for a symbol set.

    Parameters
    ----------
    symbols : list[str]
        Instrument IDs to subscribe, e.g. ``["BTC-USDT-SWAP"]``.
    on_candle : callable
        ``on_candle(symbol: str, raw: list)`` — called for every candle
        update (both in-progress and confirmed).  ``raw`` is the OKX
        candle array: ``[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]``.
    channel : str
        OKX candle channel name, e.g. ``"candle1s"``, ``"candle1m"``.
    """

    def __init__(
        self,
        symbols: list[str],
        on_candle: Callable[[str, list], None],
        channel: str = "candle1s",
    ):
        super().__init__(symbols, max_per_conn=MAX_SUBS_PER_CONN)
        self.on_candle = on_candle
        self.channel = channel

    async def _connect_and_listen(
        self, session: aiohttp.ClientSession, symbols: list[str]
    ) -> None:
        async with session.ws_connect(OKX_WS_BUSINESS, heartbeat=20) as ws:
            logger.info("WS connected for %s (%d symbols)", self.channel, len(symbols))
            args = [{"channel": self.channel, "instId": sym} for sym in symbols]
            for batch in self._chunk(args, SUBS_BATCH_SIZE):
                await ws.send_json({"op": "subscribe", "args": list(batch)})

            async for msg in ws:
                if not self._running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    def _dispatch(self, raw_text: str) -> None:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return

        if "event" in payload:
            if payload.get("event") == "error":
                logger.error("Kline subscription error: %s", payload)
            return

        records = payload.get("data")
        if not records:
            return

        arg = payload.get("arg", {})
        symbol = arg.get("instId", "")
        if not symbol:
            return

        for record in records:
            self.on_candle(symbol, record)
