"""KlineAdapter — transform OKX candle payloads into BarPoint arrays.

Responsibilities:
  1. Filter out unconfirmed candles (``raw[-1] != "1"``).
  2. Direct mode (freq >= 60s): output a BarPoint immediately.
  3. Aggregation mode (freq < 60s): buffer N confirmed 1s candles per
     symbol, merge into a single N-second BarPoint when the buffer fills.

A BarPoint is a ``np.ndarray`` of shape ``(8,)`` with dtype ``float64``:
  ``[ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]``
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

BAR_FIELDS = 8  # ts, o, h, l, c, vol, vol_ccy, vol_ccy_quote


def _parse_confirmed_bar(raw: list) -> np.ndarray | None:
    """Parse an OKX candle array into a BarPoint if confirmed, else None."""
    if str(raw[-1]) != "1":
        return None
    return np.array(
        [
            int(raw[0]),      # ts (epoch ms)
            float(raw[1]),    # open
            float(raw[2]),    # high
            float(raw[3]),    # low
            float(raw[4]),    # close
            float(raw[5]),    # vol
            float(raw[6]),    # volCcy
            float(raw[7]),    # volCcyQuote
        ],
        dtype=np.float64,
    )


class KlineAdapter:
    """Convert OKX candle records into BarPoints with optional aggregation.

    Parameters
    ----------
    freq_seconds : int
        Target bar frequency.  If < 60, the adapter aggregates that many
        confirmed 1s candles into one bar.  If >= 60, candles pass through
        directly (no aggregation — the collector is expected to subscribe
        the matching direct channel).
    """

    def __init__(self, freq_seconds: int):
        if freq_seconds < 1:
            raise ValueError(f"freq_seconds must be >= 1, got {freq_seconds}")
        self._freq = freq_seconds
        self._needs_agg = freq_seconds < 60
        self._buffers: dict[str, list[np.ndarray]] = {}

    def on_candle(self, symbol: str, raw: list) -> np.ndarray | None:
        """Process one OKX candle record.

        Returns a BarPoint ``ndarray(8,)`` when a complete bar is ready,
        otherwise ``None``.
        """
        bar = _parse_confirmed_bar(raw)
        if bar is None:
            return None

        if not self._needs_agg:
            return bar

        return self._aggregate(symbol, bar)

    def reset_symbol(self, symbol: str):
        """Clear the aggregation buffer for *symbol* (e.g. after a gap)."""
        self._buffers.pop(symbol, None)

    # ── internal ──────────────────────────────────────────────

    def _aggregate(self, symbol: str, bar: np.ndarray) -> np.ndarray | None:
        buf = self._buffers.get(symbol)
        if buf is None:
            buf = []
            self._buffers[symbol] = buf

        # Gap detection: if there is an existing buffer and the new bar's
        # timestamp jumps by more than 2× the expected 1s interval, drop
        # the partial buffer and restart.
        if buf:
            last_ts = int(buf[-1][0])
            this_ts = int(bar[0])
            gap_ms = this_ts - last_ts
            if gap_ms > 2000:
                logger.warning(
                    "Kline gap for %s: expected ~1000ms, got %dms — "
                    "dropping %d buffered candles",
                    symbol, gap_ms, len(buf),
                )
                buf.clear()

        buf.append(bar)
        if len(buf) < self._freq:
            return None

        merged = self._merge(buf)
        buf.clear()
        return merged

    @staticmethod
    def _merge(buf: list[np.ndarray]) -> np.ndarray:
        return np.array(
            [
                buf[0][0],                        # ts   = first candle ts
                buf[0][1],                        # open = first candle open
                max(b[2] for b in buf),           # high = max
                min(b[3] for b in buf),           # low  = min
                buf[-1][4],                       # close = last candle close
                sum(b[5] for b in buf),           # vol
                sum(b[6] for b in buf),           # vol_ccy
                sum(b[7] for b in buf),           # vol_ccy_quote
            ],
            dtype=np.float64,
        )
