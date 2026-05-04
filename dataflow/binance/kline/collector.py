"""Binance Futures WebSocket collector for kline streams.

Connects to the Binance Futures WebSocket and streams kline (candlestick)
data.  Each kline update fires the ``on_candle(symbol, kline_dict)`` callback.

The collector delivers **all** kline updates (both in-progress and closed).
Filtering for confirmed-only (``k.x == true``) is handled downstream by
:class:`BinanceKlineAdapter`.

Minimum supported interval: 1m (Binance Futures does not support 1s klines).
"""

from __future__ import annotations

import json
import logging
from typing import Callable

import aiohttp

from dataflow.base_collector import BaseCollector

from ..common import (
    MAX_STREAMS_PER_CONN,
    SECONDS_TO_BINANCE_INTERVAL,
    SUBS_BATCH_SIZE,
    from_binance_symbol,
    get_ws_single_url,
    to_binance_symbol,
)

logger = logging.getLogger(__name__)

OnCandleCallback = Callable[[str, dict], None]


class BinanceKlineCollector(BaseCollector):
    """Connect to Binance Futures and stream klines for a symbol set.

    Parameters
    ----------
    symbols : list[str]
        Unified instrument IDs, e.g. ``["BTC-USDT", "ETH-USDT"]``.
    on_candle : callable
        ``on_candle(symbol: str, kline: dict)`` — called for every kline
        update.  ``symbol`` is in **unified** format.  ``kline`` is the
        Binance ``k`` object from the kline event.
    interval : str
        Binance kline interval, e.g. ``"1m"``, ``"5m"``, ``"1h"``.
    testnet : bool
        If True, connect to Binance Futures testnet.
    """

    def __init__(
        self,
        symbols: list[str],
        on_candle: OnCandleCallback,
        interval: str = "1m",
        *,
        testnet: bool = False,
    ):
        self._binance_symbols = [to_binance_symbol(s) for s in symbols]
        super().__init__(self._binance_symbols, max_per_conn=MAX_STREAMS_PER_CONN)
        self._unified_symbols = list(symbols)
        self._sym_map: dict[str, str] = {
            to_binance_symbol(s): s for s in symbols
        }
        self.on_candle = on_candle
        self._interval = interval
        self._testnet = testnet
        self._msg_count = 0

    @property
    def msg_count(self) -> int:
        return self._msg_count

    async def _connect_and_listen(
        self, session: aiohttp.ClientSession, symbols: list[str]
    ) -> None:
        url = get_ws_single_url(testnet=self._testnet)

        async with session.ws_connect(url, heartbeat=20) as ws:
            logger.info(
                "Binance kline WS connected (%d symbols, interval=%s, testnet=%s)",
                len(symbols), self._interval, self._testnet,
            )

            streams = [f"{s}@kline_{self._interval}" for s in symbols]
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
        if data.get("e") != "kline":
            return

        self._msg_count += 1

        k = data.get("k")
        if k is None:
            return

        binance_sym = data.get("s", "")
        unified_sym = self._sym_map.get(binance_sym.lower())
        if unified_sym is None:
            unified_sym = from_binance_symbol(binance_sym)

        self.on_candle(unified_sym, k)


def resolve_binance_interval(freq_seconds: int) -> str:
    """Resolve data_freq (seconds) to a Binance kline interval string.

    Raises ValueError if the frequency has no matching Binance interval.
    """
    interval = SECONDS_TO_BINANCE_INTERVAL.get(freq_seconds)
    if interval is None:
        supported = ", ".join(
            f"{s}s ({v})" for s, v in sorted(SECONDS_TO_BINANCE_INTERVAL.items())
        )
        raise ValueError(
            f"No Binance kline interval for {freq_seconds}s. "
            f"Supported: {supported}."
        )
    return interval
