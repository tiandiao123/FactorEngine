"""OKX WebSocket collector for trades streams.

Connects to the OKX **public** WebSocket and streams real-time trade data.
Each trade fires the ``on_trade(symbol, px, sz, side, ts_ms)`` callback.

The collector is intentionally thin — it only handles WS lifecycle
(connect, subscribe, reconnect) and JSON parsing.  All aggregation logic
lives downstream in :class:`TradesAccumulator`.

Key differences from :class:`OKXKlineCollector`:

* Uses the **public** WS endpoint (not business).
* Subscribes to the ``trades`` channel (not ``candle*``).
* No *confirm* concept — every message is a completed trade.
* Each ``data`` array entry is dispatched individually as
  ``(symbol, px, sz, side, ts_ms)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import aiohttp

from ..common import MAX_SUBS_PER_CONN, OKX_WS_PUBLIC, SUBS_BATCH_SIZE, chunk

logger = logging.getLogger(__name__)

OnTradeCallback = Callable[[str, float, float, str, int], None]


class OKXTradesCollector:
    """Connect to OKX and stream trades for a symbol set.

    Parameters
    ----------
    symbols : list[str]
        Instrument IDs to subscribe, e.g. ``["BTC-USDT-SWAP"]``.
    on_trade : callable
        ``on_trade(symbol, px, sz, side, ts_ms)`` — called for every
        individual trade.
    """

    def __init__(
        self,
        symbols: list[str],
        on_trade: OnTradeCallback,
    ):
        self.symbols = list(symbols)
        self.on_trade = on_trade
        self._running = True
        self._trade_count = 0
        self._msg_count = 0

    async def run(self):
        """Block forever, streaming trades and auto-reconnecting."""
        async with aiohttp.ClientSession() as session:
            tasks = [
                asyncio.create_task(self._ws_loop(session, group))
                for group in chunk(self.symbols, MAX_SUBS_PER_CONN)
            ]
            await asyncio.gather(*tasks)

    def stop(self):
        self._running = False

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def msg_count(self) -> int:
        return self._msg_count

    # ── internal ──────────────────────────────────────────────

    async def _ws_loop(self, session: aiohttp.ClientSession, symbols: list[str]):
        while self._running:
            try:
                await self._connect_and_listen(session, symbols)
            except asyncio.CancelledError:
                raise
            except (
                aiohttp.WSServerHandshakeError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as exc:
                logger.warning(
                    "Trades WS disconnected: %s — reconnecting in 3s", exc,
                )
            except Exception:
                logger.exception(
                    "Unexpected trades WS error — reconnecting in 5s"
                )
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(3)

    async def _connect_and_listen(
        self, session: aiohttp.ClientSession, symbols: list[str]
    ):
        async with session.ws_connect(OKX_WS_PUBLIC, heartbeat=20) as ws:
            logger.info(
                "Trades WS connected (%d symbols)", len(symbols),
            )
            args = [{"channel": "trades", "instId": sym} for sym in symbols]
            for batch in chunk(args, SUBS_BATCH_SIZE):
                await ws.send_json({"op": "subscribe", "args": batch})

            async for msg in ws:
                if not self._running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break

    def _dispatch(self, raw_text: str):
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return

        if "event" in payload:
            if payload.get("event") == "error":
                logger.error("Trades subscription error: %s", payload)
            return

        records = payload.get("data")
        if not records:
            return

        self._msg_count += 1

        arg = payload.get("arg", {})
        symbol = arg.get("instId", "")
        if not symbol:
            return

        for record in records:
            try:
                px = float(record["px"])
                sz = float(record["sz"])
                side = record["side"]
                ts_ms = int(record["ts"])
            except (KeyError, ValueError, TypeError) as exc:
                logger.debug("Malformed trade record: %s — %s", record, exc)
                continue
            self._trade_count += 1
            self.on_trade(symbol, px, sz, side, ts_ms)
