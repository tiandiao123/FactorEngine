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

Forward-fill on empty buckets
-----------------------------
At high frequencies (e.g. 1s), less liquid symbols may have no trades
in a given bucket.  To prevent NaN propagation into downstream
rolling-window factors, ``snapshot_and_reset()`` forward-fills OHLC
with the **last known close** price when ``trade_count == 0``.
Volume stays 0 and vwap stays NaN (no trades = no VWAP).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
        "_last_close",
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
        self._last_close: float = math.nan

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
            ``"buy"`` or ``"sell"`` — the aggressor side.
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

            If no trades arrived in the bucket but a previous close is
            known, OHLC **and vwap** are forward-filled with that close
            (volume = 0).  This prevents NaN propagation into downstream
            rolling-window factors that depend on vwap.

            If no trades have *ever* been seen, price fields are ``NaN``.
        """
        with self._lock:
            vwap = self._sum_pv / self._sum_v if self._sum_v > 0 else math.nan
            no_trades = self._count == 0

            if not no_trades:
                self._last_close = self._close

            # Forward-fill OHLC + vwap from last known close when bucket empty.
            if no_trades and not math.isnan(self._last_close):
                fill = self._last_close
                ohlc = (fill, fill, fill, fill)
                vwap = fill
            elif no_trades:
                ohlc = (math.nan, math.nan, math.nan, math.nan)
            else:
                ohlc = (self._open, self._high, self._low, self._close)

            result = {
                "vwap": vwap,
                "buy_vol": self._buy_vol,
                "sell_vol": self._sell_vol,
                "trade_count": self._count,
                "open": ohlc[0],
                "high": ohlc[1],
                "low": ohlc[2],
                "close": ohlc[3],
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
            f"O={self._open} H={self._high} L={self._low} C={self._close}, "
            f"last_close={self._last_close})"
        )


@dataclass(frozen=True)
class TradeBucketSnapshot:
    """Immutable event-time aggregate for one symbol and one bucket."""

    bucket_start_ms: int
    bucket_end_ms: int
    vwap: float
    buy_vol: float
    sell_vol: float
    trade_count: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    first_trade_ts_ms: int | None
    last_trade_ts_ms: int | None
    first_receive_ts_ms: int | None
    last_receive_ts_ms: int | None
    is_complete: bool = True

    def to_dict(self) -> dict:
        """Return a dict compatible with the existing trades snapshot shape."""
        return asdict(self)


class TradeBucket:
    """O(1) aggregation state for a single exchange-time bucket."""

    __slots__ = (
        "bucket_start_ms",
        "bucket_end_ms",
        "_sum_pv",
        "_sum_v",
        "_buy_vol",
        "_sell_vol",
        "_count",
        "_open",
        "_high",
        "_low",
        "_close",
        "_first_trade_ts_ms",
        "_last_trade_ts_ms",
        "_first_receive_ts_ms",
        "_last_receive_ts_ms",
    )

    def __init__(self, bucket_start_ms: int, bucket_end_ms: int) -> None:
        self.bucket_start_ms = bucket_start_ms
        self.bucket_end_ms = bucket_end_ms
        self._sum_pv = 0.0
        self._sum_v = 0.0
        self._buy_vol = 0.0
        self._sell_vol = 0.0
        self._count = 0
        self._open = math.nan
        self._high = -math.inf
        self._low = math.inf
        self._close = math.nan
        self._first_trade_ts_ms: int | None = None
        self._last_trade_ts_ms: int | None = None
        self._first_receive_ts_ms: int | None = None
        self._last_receive_ts_ms: int | None = None

    def update(
        self,
        px: float,
        sz: float,
        side: str,
        trade_ts_ms: int,
        receive_ts_ms: int,
    ) -> None:
        self._sum_pv += px * sz
        self._sum_v += sz
        if side == "buy":
            self._buy_vol += sz
        elif side == "sell":
            self._sell_vol += sz

        if self._count == 0:
            self._open = px
            self._high = px
            self._low = px
            self._close = px
            self._first_trade_ts_ms = trade_ts_ms
            self._last_trade_ts_ms = trade_ts_ms
            self._first_receive_ts_ms = receive_ts_ms
            self._last_receive_ts_ms = receive_ts_ms
        else:
            if px > self._high:
                self._high = px
            if px < self._low:
                self._low = px
            if self._first_trade_ts_ms is None or trade_ts_ms < self._first_trade_ts_ms:
                self._open = px
                self._first_trade_ts_ms = trade_ts_ms
            if self._last_trade_ts_ms is None or trade_ts_ms >= self._last_trade_ts_ms:
                self._close = px
                self._last_trade_ts_ms = trade_ts_ms
            if (
                self._first_receive_ts_ms is None
                or receive_ts_ms < self._first_receive_ts_ms
            ):
                self._first_receive_ts_ms = receive_ts_ms
            if self._last_receive_ts_ms is None or receive_ts_ms > self._last_receive_ts_ms:
                self._last_receive_ts_ms = receive_ts_ms

        self._count += 1

    def snapshot(self) -> TradeBucketSnapshot:
        vwap = self._sum_pv / self._sum_v if self._sum_v > 0 else math.nan
        return TradeBucketSnapshot(
            bucket_start_ms=self.bucket_start_ms,
            bucket_end_ms=self.bucket_end_ms,
            vwap=vwap,
            buy_vol=self._buy_vol,
            sell_vol=self._sell_vol,
            trade_count=self._count,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._sum_v,
            first_trade_ts_ms=self._first_trade_ts_ms,
            last_trade_ts_ms=self._last_trade_ts_ms,
            first_receive_ts_ms=self._first_receive_ts_ms,
            last_receive_ts_ms=self._last_receive_ts_ms,
        )


class EventTimeTradesAccumulator:
    """Per-symbol exchange-time trades aggregator with allowed lateness.

    ``trade_ts_ms`` chooses the bucket. ``receive_ts_ms`` determines whether a
    trade is too late to accept. ``now_ms`` in :meth:`flush_ready` determines
    which buckets are mature enough to emit.
    """

    __slots__ = (
        "_lock",
        "_freq_ms",
        "_allowed_lateness_ms",
        "_buckets",
        "_last_close",
        "_next_emit_bucket_start_ms",
        "_late_trade_count",
        "_dropped_late_trade_count",
        "_max_lateness_ms",
    )

    def __init__(self, *, freq_ms: int, allowed_lateness_ms: int) -> None:
        if freq_ms <= 0:
            raise ValueError("freq_ms must be positive")
        if allowed_lateness_ms < 0:
            raise ValueError("allowed_lateness_ms must be non-negative")
        self._lock = threading.Lock()
        self._freq_ms = int(freq_ms)
        self._allowed_lateness_ms = int(allowed_lateness_ms)
        self._buckets: dict[int, TradeBucket] = {}
        self._last_close = math.nan
        self._next_emit_bucket_start_ms: int | None = None
        self._late_trade_count = 0
        self._dropped_late_trade_count = 0
        self._max_lateness_ms = 0

    def update(
        self,
        px: float,
        sz: float,
        side: str,
        trade_ts_ms: int,
        receive_ts_ms: int,
    ) -> bool:
        """Incorporate one trade, returning ``False`` if it is too late."""
        bucket_start_ms = self._bucket_start_ms(trade_ts_ms)
        bucket_end_ms = bucket_start_ms + self._freq_ms
        receive_watermark_ms = receive_ts_ms - self._allowed_lateness_ms
        lateness_ms = receive_ts_ms - bucket_end_ms

        with self._lock:
            if (
                bucket_end_ms <= receive_watermark_ms
                or (
                    self._next_emit_bucket_start_ms is not None
                    and bucket_start_ms < self._next_emit_bucket_start_ms
                )
            ):
                self._record_late_drop(lateness_ms)
                return False

            if self._next_emit_bucket_start_ms is None:
                self._next_emit_bucket_start_ms = bucket_start_ms

            bucket = self._buckets.get(bucket_start_ms)
            if bucket is None:
                bucket = TradeBucket(bucket_start_ms, bucket_end_ms)
                self._buckets[bucket_start_ms] = bucket
            bucket.update(px, sz, side, trade_ts_ms, receive_ts_ms)

            if lateness_ms > 0:
                self._late_trade_count += 1
                if lateness_ms > self._max_lateness_ms:
                    self._max_lateness_ms = lateness_ms
            return True

    def flush_ready(self, now_ms: int, *, active: bool = True) -> list[TradeBucketSnapshot]:
        """Return mature bucket snapshots in ascending bucket order."""
        watermark_ms = now_ms - self._allowed_lateness_ms
        latest_ready_start_ms = self._latest_ready_start_ms(watermark_ms)

        snapshots: list[TradeBucketSnapshot] = []
        while True:
            with self._lock:
                next_bucket_start_ms = self._next_emit_bucket_start_ms
            if (
                next_bucket_start_ms is None
                or next_bucket_start_ms > latest_ready_start_ms
            ):
                return snapshots
            snap = self.pop_ready_bucket(
                next_bucket_start_ms,
                now_ms,
                active=active,
            )
            if snap is not None:
                snapshots.append(snap)

    def pop_ready_bucket(
        self,
        bucket_start_ms: int,
        now_ms: int,
        *,
        active: bool = True,
    ) -> TradeBucketSnapshot | None:
        """Pop a specific mature bucket, or return ``None`` if not available.

        This is the API the TradesWorker should use when it owns a global
        target bucket cursor. It only advances the accumulator when the
        requested bucket is exactly the next bucket this symbol may emit.
        """
        bucket_start_ms = int(bucket_start_ms)
        watermark_ms = now_ms - self._allowed_lateness_ms
        if bucket_start_ms + self._freq_ms > watermark_ms:
            return None

        with self._lock:
            if self._next_emit_bucket_start_ms is None:
                return None
            if bucket_start_ms != self._next_emit_bucket_start_ms:
                return None

            bucket = self._buckets.pop(bucket_start_ms, None)
            if bucket is None:
                snap = (
                    self._forward_fill_snapshot(bucket_start_ms)
                    if active and not math.isnan(self._last_close)
                    else None
                )
            else:
                snap = bucket.snapshot()
                if snap.trade_count > 0 and not math.isnan(snap.close):
                    self._last_close = snap.close

            self._next_emit_bucket_start_ms += self._freq_ms
            return snap

    @property
    def late_trade_count(self) -> int:
        return self._late_trade_count

    @property
    def dropped_late_trade_count(self) -> int:
        return self._dropped_late_trade_count

    @property
    def max_lateness_ms(self) -> int:
        return self._max_lateness_ms

    @property
    def pending_bucket_count(self) -> int:
        return len(self._buckets)

    def stats(self) -> dict[str, int]:
        return {
            "late_trade_count": self._late_trade_count,
            "dropped_late_trade_count": self._dropped_late_trade_count,
            "max_lateness_ms": self._max_lateness_ms,
            "pending_bucket_count": len(self._buckets),
        }

    def _bucket_start_ms(self, trade_ts_ms: int) -> int:
        return int(trade_ts_ms) - (int(trade_ts_ms) % self._freq_ms)

    def _latest_ready_start_ms(self, watermark_ms: int) -> int:
        return ((int(watermark_ms) - self._freq_ms) // self._freq_ms) * self._freq_ms

    def _record_late_drop(self, lateness_ms: int) -> None:
        self._late_trade_count += 1
        self._dropped_late_trade_count += 1
        if lateness_ms > self._max_lateness_ms:
            self._max_lateness_ms = lateness_ms

    def _forward_fill_snapshot(self, bucket_start_ms: int) -> TradeBucketSnapshot:
        bucket_end_ms = bucket_start_ms + self._freq_ms
        fill = self._last_close
        return TradeBucketSnapshot(
            bucket_start_ms=bucket_start_ms,
            bucket_end_ms=bucket_end_ms,
            vwap=fill,
            buy_vol=0.0,
            sell_vol=0.0,
            trade_count=0,
            open=fill,
            high=fill,
            low=fill,
            close=fill,
            volume=0.0,
            first_trade_ts_ms=None,
            last_trade_ts_ms=None,
            first_receive_ts_ms=None,
            last_receive_ts_ms=None,
        )
