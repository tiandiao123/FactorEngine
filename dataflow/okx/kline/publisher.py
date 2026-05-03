"""KlinePublisher — batch BarPoints by round, then push into bar_queue.

v2 round-batching design:
  1. Collect bars from all symbols into a pending round.
  2. When all symbols arrive (or timeout + forward-fill), flush the
     complete round as a single ``bar_queue.put()``.
  3. Per-symbol deduplication via ``last_pushed_ts``.
  4. Timestamp gap detection and warning.
  5. Non-blocking ``bar_queue.put()`` with backpressure drop.
  6. Optional write to a debug BarCache.
  7. Counters for monitoring.

Queue payload format::

    (round_bars: dict[str, np.ndarray], round_trades: dict[str, dict] | None)

When ``accumulators`` is not provided (pure kline mode), ``round_trades``
is ``None``.  When provided (kline_trades mode), ``round_trades`` maps
each symbol to its ``TradesAccumulator.snapshot_and_reset()`` output.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)


class KlinePublisher:
    """Batch bars by round and push complete rounds into ``bar_queue``.

    Parameters
    ----------
    bar_queue : queue.Queue
        Destination queue consumed by ``Engine._runtime_loop()``.
    symbols : list[str]
        Complete list of symbols expected per round.
    debug_cache : object, optional
        If provided, must have an ``append(symbol, bar)`` method.
        Receives every bar immediately (not batched).
    expected_interval_ms : int
        Expected interval between consecutive bars for the same symbol.
        Used for gap detection.  Defaults to 5000 (5s).
    batch_timeout_s : float or None
        Max seconds to wait for all symbols before flushing with
        forward-fill.  ``None`` = auto (``min(expected_interval_ms/1000 * 0.8, 4.0)``).
        The first round never times out (must wait for all symbols).
    """

    def __init__(
        self,
        bar_queue: queue.Queue,
        symbols: list[str],
        debug_cache=None,
        expected_interval_ms: int = 5000,
        batch_timeout_s: float | None = None,
        accumulators: dict | None = None,
    ):
        self._bar_queue = bar_queue
        self._debug_cache = debug_cache
        self._expected_interval_ms = expected_interval_ms
        self._expected_symbols: set[str] = set(symbols)
        self._accumulators = accumulators

        if batch_timeout_s is not None:
            self._batch_timeout_s = batch_timeout_s
        else:
            self._batch_timeout_s = min(expected_interval_ms / 1000 * 0.8, 4.0)

        self._last_pushed_ts: dict[str, int] = {}
        self._lock = threading.Lock()

        # Round-batching state (accessed only from the WS callback thread).
        self._pending_round: dict[str, np.ndarray] = {}
        self._round_start_time: float | None = None
        self._last_round: dict[str, np.ndarray] | None = None

        # Counters.
        self._bars_published = 0
        self._rounds_flushed = 0
        self._forward_fill_count = 0
        self._dup_count = 0
        self._gap_count = 0
        self._drop_count = 0

    def publish(self, symbol: str, bar: np.ndarray) -> bool:
        """Collect one symbol's bar into the pending round.

        May trigger a round flush if all symbols arrive or timeout is reached.
        Returns ``True`` if a complete round was flushed to ``bar_queue``.
        """
        ts = int(bar[0])

        # ── Dedup + Gap detection ──
        with self._lock:
            last_ts = self._last_pushed_ts.get(symbol)
            if last_ts is not None:
                if ts <= last_ts:
                    self._dup_count += 1
                    return False
                actual_gap = ts - last_ts
                if actual_gap > self._expected_interval_ms * 2:
                    self._gap_count += 1
                    logger.warning(
                        "Kline gap: %s expected %dms got %dms",
                        symbol, self._expected_interval_ms, actual_gap,
                    )

        # ── Round boundary detection ──
        # If the new bar belongs to a different ts than the current pending
        # round, flush the old round first.
        flushed = False
        if self._pending_round:
            existing_ts = int(next(iter(self._pending_round.values()))[0])
            if ts != existing_ts:
                flushed = self._flush_round()

        # ── Debug cache (immediate, not batched) ──
        if self._debug_cache is not None:
            self._debug_cache.append(symbol, bar)

        # ── Accumulate ──
        self._pending_round[symbol] = bar
        if self._round_start_time is None:
            self._round_start_time = time.monotonic()

        # ── Check: all symbols arrived? ──
        if self._expected_symbols <= set(self._pending_round.keys()):
            return self._flush_round()

        # ── Check: timeout (skip for first round — no last_round to fill) ──
        if (
            self._last_round is not None
            and self._round_start_time is not None
        ):
            elapsed = time.monotonic() - self._round_start_time
            if elapsed >= self._batch_timeout_s:
                return self._flush_round()

        return flushed

    def flush(self) -> bool:
        """Force-flush the pending round (call on stop / shutdown)."""
        if not self._pending_round:
            return False
        return self._flush_round()

    # ── internal ───────────────────────────────────────────────

    def _flush_round(self) -> bool:
        """Assemble a complete round, forward-fill missing symbols, push."""
        round_bars: dict[str, np.ndarray] = dict(self._pending_round)

        # Forward-fill missing symbols from last round.
        if self._last_round is not None:
            fill_count = 0
            for sym in self._expected_symbols:
                if sym not in round_bars:
                    round_bars[sym] = self._last_round[sym].copy()
                    fill_count += 1
                    logger.info(
                        "Forward-fill: %s missing in round, using previous bar",
                        sym,
                    )
            if fill_count:
                with self._lock:
                    self._forward_fill_count += fill_count

        # First round must have all symbols — no forward-fill source.
        if self._last_round is None and set(round_bars.keys()) < self._expected_symbols:
            return False

        # Snapshot trades accumulators (if attached).
        round_trades: dict[str, dict] | None = None
        if self._accumulators:
            round_trades = {}
            for sym in round_bars:
                acc = self._accumulators.get(sym)
                if acc is not None:
                    round_trades[sym] = acc.snapshot_and_reset()

        # Update dedup timestamps.
        with self._lock:
            for sym, bar in round_bars.items():
                self._last_pushed_ts[sym] = int(bar[0])

        # Push to queue: tuple(round_bars, round_trades | None).
        payload = (round_bars, round_trades)
        try:
            self._bar_queue.put(payload, block=False)
        except queue.Full:
            with self._lock:
                self._drop_count += 1
            if self._drop_count % 100 == 1:
                logger.warning(
                    "bar_queue full — dropped round (total drops: %d)",
                    self._drop_count,
                )
            self._pending_round.clear()
            self._round_start_time = None
            return False

        with self._lock:
            self._bars_published += len(round_bars)
            self._rounds_flushed += 1

        self._last_round = round_bars
        self._pending_round.clear()
        self._round_start_time = None
        return True

    # ── stats ──────────────────────────────────────────────────

    @property
    def bars_published(self) -> int:
        with self._lock:
            return self._bars_published

    @property
    def rounds_flushed(self) -> int:
        with self._lock:
            return self._rounds_flushed

    @property
    def forward_fill_count(self) -> int:
        with self._lock:
            return self._forward_fill_count

    @property
    def dup_count(self) -> int:
        with self._lock:
            return self._dup_count

    @property
    def gap_count(self) -> int:
        with self._lock:
            return self._gap_count

    @property
    def drop_count(self) -> int:
        with self._lock:
            return self._drop_count

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "bars_published": self._bars_published,
                "rounds_flushed": self._rounds_flushed,
                "forward_fill_count": self._forward_fill_count,
                "dup_count": self._dup_count,
                "gap_count": self._gap_count,
                "drop_count": self._drop_count,
            }
