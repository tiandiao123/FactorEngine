"""KlineAdapter — transform OKX candle payloads into BarPoint arrays.

Responsibilities:
  1. Filter out unconfirmed candles (``raw[-1] != "1"``).
  2. Direct mode (freq >= 60s): output a BarPoint immediately.
  3. Aggregation mode (freq < 60s): buffer confirmed 1s candles and
     merge into a single N-second BarPoint when a time-window boundary
     is crossed.

Time-window alignment (v2):
  All symbols share the same wall-clock window boundaries defined by
  ``ts_ms % (freq_seconds * 1000) == 0``.  This ensures that different
  symbols' aggregated bars always have the same output timestamp, so
  they can be batched into one round by KlinePublisher.

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
        Target bar frequency.  If < 60, the adapter aggregates confirmed
        1s candles into one bar using **time-aligned windows** (all symbols
        share the same ``ts % (freq*1000) == 0`` boundaries).  If >= 60,
        candles pass through directly (the collector subscribes the matching
        OKX channel).
    """

    def __init__(self, freq_seconds: int):
        if freq_seconds < 1:
            raise ValueError(f"freq_seconds must be >= 1, got {freq_seconds}")
        self._freq = freq_seconds
        self._freq_ms = freq_seconds * 1000
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

        if self._freq == 1:
            return bar

        return self._aggregate(symbol, bar)

    def reset_symbol(self, symbol: str):
        """Clear the aggregation buffer for *symbol* (e.g. after a gap)."""
        self._buffers.pop(symbol, None)

    # ── internal ──────────────────────────────────────────────

    def _window_start(self, ts_ms: int) -> int:
        """Return the aligned window start for a given timestamp."""
        return ts_ms - (ts_ms % self._freq_ms)

    def _aggregate(self, symbol: str, bar: np.ndarray) -> np.ndarray | None:
        ts_ms = int(bar[0])
        win_start = self._window_start(ts_ms)

        buf = self._buffers.get(symbol)
        if buf is None:
            buf = []
            self._buffers[symbol] = buf

        # If buffer is non-empty, check if new candle belongs to the same
        # time window. If not, the previous window is incomplete — drop it
        # and start fresh with the new window.
        if buf:
            buf_win = self._window_start(int(buf[0][0]))
            if win_start != buf_win:
                if len(buf) < self._freq:
                    logger.debug(
                        "Kline window incomplete for %s: got %d/%d candles "
                        "in window %d — dropping, starting window %d",
                        symbol, len(buf), self._freq, buf_win, win_start,
                    )
                buf.clear()

        # Gap detection within the same window.
        if buf:
            last_ts = int(buf[-1][0])
            gap_ms = ts_ms - last_ts
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

        merged = self._merge(buf, win_start)
        buf.clear()
        return merged

    @staticmethod
    def _merge(buf: list[np.ndarray], aligned_ts: int) -> np.ndarray:
        """Merge buffered candles into one bar with an aligned timestamp."""
        return np.array(
            [
                aligned_ts,                           # ts = aligned window start
                buf[0][1],                            # open = first candle open
                max(b[2] for b in buf),               # high = max
                min(b[3] for b in buf),               # low  = min
                buf[-1][4],                           # close = last candle close
                sum(b[5] for b in buf),               # vol
                sum(b[6] for b in buf),               # vol_ccy
                sum(b[7] for b in buf),               # vol_ccy_quote
            ],
            dtype=np.float64,
        )
