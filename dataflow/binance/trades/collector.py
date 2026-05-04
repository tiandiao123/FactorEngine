"""Binance Futures WebSocket collector for aggTrade streams.

Connects to the Binance Futures WebSocket and streams real-time aggregate
trade data.  Each aggTrade fires the
``on_trade(symbol, px, sz, side, ts_ms)`` callback.

Trade direction is inferred from ``m`` (is_buyer_maker):
``m=true`` → sell, ``m=false`` → buy.

Symbols arrive in UPPER (``"BTCUSDT"``), converted to unified format
before calling the callback.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

import aiohttp

from dataflow.base_collector import BaseCollector

from ..common import (
    MAX_STREAMS_PER_CONN,
    SUBS_BATCH_SIZE,
    from_binance_symbol,
    get_ws_single_url,
    to_binance_symbol,
)

logger = logging.getLogger(__name__)

OnTradeCallback = Callable[[str, float, float, str, int], None]


class BinanceTradesCollector(BaseCollector):
    """Connect to Binance Futures and stream aggTrades for a symbol set.

    Parameters
    ----------
    symbols : list[str]
        Unified instrument IDs, e.g. ``["BTC-USDT", "ETH-USDT"]``.
    on_trade : callable
        ``on_trade(symbol, px, sz, side, ts_ms)`` — called for every
        aggregate trade.  ``symbol`` is in **unified** format.
    testnet : bool
        If True, connect to Binance Futures testnet instead of production.
    """

    def __init__(
        self,
        symbols: list[str],
        on_trade: OnTradeCallback,
        *,
        testnet: bool = False,
    ):
        self._binance_symbols = [to_binance_symbol(s) for s in symbols]
        super().__init__(self._binance_symbols, max_per_conn=MAX_STREAMS_PER_CONN)
        self._unified_symbols = list(symbols)
        self._sym_map: dict[str, str] = {
            to_binance_symbol(s): s for s in symbols
        }
        self.on_trade = on_trade
        self._testnet = testnet
        self._trade_count = 0
        self._msg_count = 0

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def msg_count(self) -> int:
        return self._msg_count

    async def _connect_and_listen(
        self, session: aiohttp.ClientSession, symbols: list[str]
    ) -> None:
        url = get_ws_single_url(testnet=self._testnet)

        async with session.ws_connect(url, heartbeat=20) as ws:
            logger.info(
                "Binance aggTrade WS connected (%d symbols, testnet=%s)",
                len(symbols), self._testnet,
            )

            streams = [f"{s}@aggTrade" for s in symbols]
            for batch_id, batch in enumerate(self._chunk(streams, SUBS_BATCH_SIZE), start=1):
                await ws.send_json({
                    "method": "SUBSCRIBE",
                    "params": list(batch),
                    "id": batch_id,
                })

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

    def _dispatch(self, raw_text: str) -> None:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return

        if "result" in payload:
            return

        data = payload.get("data")
        if data is None:
            data = payload
        if data.get("e") != "aggTrade":
            return

        self._msg_count += 1

        try:
            binance_sym = data["s"]
            px = float(data["p"])
            sz = float(data["q"])
            ts_ms = int(data["T"])
            is_buyer_maker = data["m"]
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("Malformed aggTrade record: %s — %s", data, exc)
            return

        side = "sell" if is_buyer_maker else "buy"

        unified_sym = self._sym_map.get(binance_sym.lower())
        if unified_sym is None:
            unified_sym = from_binance_symbol(binance_sym)

        self._trade_count += 1
        self.on_trade(unified_sym, px, sz, side, ts_ms)
