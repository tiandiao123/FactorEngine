"""BinanceKlineAdapter — transform Binance kline payloads into BarPoint arrays.

Responsibilities:
  1. Filter out unconfirmed klines (``k["x"] != True``).
  2. Convert Binance kline dict to a BarPoint ``ndarray(8,)``.

Binance kline ``k`` object fields used:
  - ``t`` : kline start time (ms)
  - ``o`` : open price
  - ``h`` : high price
  - ``l`` : low price
  - ``c`` : close price
  - ``v`` : base asset volume
  - ``q`` : quote asset volume (USDT)
  - ``x`` : is this kline closed? (boolean)

A BarPoint is a ``np.ndarray`` of shape ``(8,)`` with dtype ``float64``:
  ``[ts, open, high, low, close, vol, quote_vol, 0.0]``

Note: Binance volumes are in base asset (``v``) and quote asset (``q``).
OKX has vol (contracts), volCcy (base), volCcyQuote (quote).
We map: slot 5 = base volume (``v``), slot 6 = quote volume (``q``),
slot 7 = 0.0 (unused, matching OKX layout).
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


class BinanceKlineAdapter:
    """Convert Binance kline events into BarPoint arrays.

    Only confirmed klines (``k["x"] == True``) are emitted.
    """

    def on_candle(self, symbol: str, k: dict) -> np.ndarray | None:
        """Process one Binance kline event.

        Returns a BarPoint ``ndarray(8,)`` if the kline is confirmed,
        otherwise ``None``.
        """
        if not k.get("x", False):
            return None

        try:
            return np.array(
                [
                    int(k["t"]),          # ts (kline start time, epoch ms)
                    float(k["o"]),        # open
                    float(k["h"]),        # high
                    float(k["l"]),        # low
                    float(k["c"]),        # close
                    float(k["v"]),        # base asset volume
                    float(k["q"]),        # quote asset volume (USDT)
                    0.0,                  # placeholder
                ],
                dtype=np.float64,
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("Malformed Binance kline: %s — %s", k, exc)
            return None
