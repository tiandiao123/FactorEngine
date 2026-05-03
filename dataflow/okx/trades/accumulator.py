"""TradesAccumulator — per-symbol streaming aggregation of trades data.

Each symbol gets its own ``TradesAccumulator`` instance.  The WS receiver
thread calls ``update()`` on every incoming trade (O(1) per call), while
the flush thread (KlinePublisher or ClockDriver) calls
``snapshot_and_reset()`` at bar boundaries to atomically read the
aggregated values and reset for the next bucket.

Thread safety is guaranteed by a ``threading.Lock`` — contention is
negligible because ``update()`` holds the lock for a few additions and
``snapshot_and_reset()`` is called at most once per bar interval.

Phase 2 addition: OHLCV tracking
---------------------------------
In ``trades``-only mode there is no kline WS feed, so the accumulator
derives OHLCV directly from the trades stream:

* **open**  — price of the first trade in the bucket
* **high**  — max price seen
* **low**   — min price seen
* **close** — price of the most recent trade
* **volume** — Σ(sz)  (same as ``buy_vol + sell_vol``)
"""

from __future__ import annotations

import math
import threading


class TradesAccumulator:
    """Incremental O(1) aggregator for a single symbol's trades stream.

    Maintained fields
    -----------------
    * **vwap** — volume-weighted average price: ``Σ(px × sz) / Σ(sz)``
    * **buy_vol** — total size where ``side == "buy"``
    * **sell_vol** — total size where ``side == "sell"``
    * **trade_count** — number of ``update()`` calls in the current bucket
    * **open / high / low / close / volume** — OHLCV derived from trades
    """

    __slots__ = (
        "_lock",
        "_sum_pv",
        "_sum_v",
        "_buy_vol",
        "_sell_vol",
        "_count",
        "_open",
        "_high",
        "_low",
        "_close",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sum_pv: float = 0.0
        self._sum_v: float = 0.0
        self._buy_vol: float = 0.0
        self._sell_vol: float = 0.0
        self._count: int = 0
        self._open: float = math.nan
        self._high: float = -math.inf
        self._low: float = math.inf
        self._close: float = math.nan

    # ── hot path (trades WS thread) ─────────────────────────────

    def update(self, px: float, sz: float, side: str) -> None:
        """Incorporate a single trade.  Called from the WS receiver thread.

        Parameters
        ----------
        px : float
            Trade price.
        sz : float
            Trade size (contracts / coins).
        side : str
            ``"buy"`` or ``"sell"`` — the aggressor side provided by OKX.
        """
        with self._lock:
            self._sum_pv += px * sz
            self._sum_v += sz
            if side == "buy":
                self._buy_vol += sz
            elif side == "sell":
                self._sell_vol += sz
            if self._count == 0:
                self._open = px
            if px > self._high:
                self._high = px
            if px < self._low:
                self._low = px
            self._close = px
            self._count += 1

    # ── cold path (flush thread) ────────────────────────────────

    def snapshot_and_reset(self) -> dict:
        """Atomically read current aggregation and reset for the next bucket.

        Returns
        -------
        dict
            Keys: ``vwap``, ``buy_vol``, ``sell_vol``, ``trade_count``,
            ``open``, ``high``, ``low``, ``close``, ``volume``.

            If no trades arrived in the bucket, price fields are ``NaN``
            and volumes / count are ``0``.
        """
        with self._lock:
            vwap = self._sum_pv / self._sum_v if self._sum_v > 0 else math.nan
            no_trades = self._count == 0
            result = {
                "vwap": vwap,
                "buy_vol": self._buy_vol,
                "sell_vol": self._sell_vol,
                "trade_count": self._count,
                "open": math.nan if no_trades else self._open,
                "high": math.nan if no_trades else self._high,
                "low": math.nan if no_trades else self._low,
                "close": math.nan if no_trades else self._close,
                "volume": self._sum_v,
            }
            self._sum_pv = 0.0
            self._sum_v = 0.0
            self._buy_vol = 0.0
            self._sell_vol = 0.0
            self._count = 0
            self._open = math.nan
            self._high = -math.inf
            self._low = math.inf
            self._close = math.nan
            return result

    # ── introspection (no lock — advisory only) ─────────────────

    @property
    def count(self) -> int:
        """Current trade count (non-atomic, for monitoring only)."""
        return self._count

    def __repr__(self) -> str:
        return (
            f"TradesAccumulator(count={self._count}, "
            f"sum_v={self._sum_v:.4f}, buy={self._buy_vol:.4f}, "
            f"sell={self._sell_vol:.4f}, "
            f"O={self._open} H={self._high} L={self._low} C={self._close})"
        )
