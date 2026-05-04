"""KlinePublisher — batch BarPoints by round.

v3 design (ClockDriver refactor):
  1. Collect bars from all symbols into a pending round.
  2. When all symbols arrive (or timeout + forward-fill), the round is
     marked complete internally.
  3. Per-symbol deduplication via ``last_pushed_ts``.
  4. Timestamp gap detection and warning.
  5. Optional write to a debug BarCache.
  6. Counters for monitoring.

KlinePublisher no longer pushes to ``bar_queue`` or knows about trades.
ClockDriver calls ``flush_for_clock()`` to retrieve the latest complete
round, then handles aggregation and queue pushing.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)


class KlinePublisher:
    """Batch bars by round and expose complete rounds via ``flush_for_clock()``.

    Parameters
    ----------
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
        symbols: list[str],
        debug_cache=None,
        expected_interval_ms: int = 5000,
        batch_timeout_s: float | None = None,
    ):
        self._debug_cache = debug_cache
        self._expected_interval_ms = expected_interval_ms
        self._expected_symbols: set[str] = set(symbols)

        if batch_timeout_s is not None:
            self._batch_timeout_s = batch_timeout_s
        else:
            self._batch_timeout_s = min(expected_interval_ms / 1000 * 0.8, 4.0)

        self._last_pushed_ts: dict[str, int] = {}
        self._lock = threading.Lock()

        # Round-batching state — written by the WS callback thread (publish),
        # read by the ClockDriver thread (flush_for_clock fallback paths).
        # GIL-safe in CPython (dict/float reads are atomic); no lock needed.
        self._pending_round: dict[str, np.ndarray] = {}
        self._round_start_time: float | None = None
        self._last_round: dict[str, np.ndarray] | None = None

        # Buffer: the most recent complete round, ready for ClockDriver.
        # Written by _complete_round (WS thread), read by flush_for_clock
        # (ClockDriver thread) — guarded by _ready_lock.
        self._ready_lock = threading.Lock()
        self._ready_round: dict[str, np.ndarray] | None = None

        # Counters.
        self._bars_received = 0
        self._rounds_completed = 0
        self._forward_fill_count = 0
        self._dup_count = 0
        self._gap_count = 0

    def publish(self, symbol: str, bar: np.ndarray) -> None:
        """Collect one symbol's bar into the pending round.

        Called from the WS callback thread.  May trigger an internal
        round completion if all symbols arrive or timeout is reached.
        """
        ts = int(bar[0])

        # ── Dedup + Gap detection ──
        with self._lock:
            last_ts = self._last_pushed_ts.get(symbol)
            if last_ts is not None:
                if ts <= last_ts:
                    self._dup_count += 1
                    return
                actual_gap = ts - last_ts
                if actual_gap > self._expected_interval_ms * 2:
                    self._gap_count += 1
                    logger.warning(
                        "Kline gap: %s expected %dms got %dms",
                        symbol, self._expected_interval_ms, actual_gap,
                    )

        # ── Round boundary detection ──
        if self._pending_round:
            existing_ts = int(next(iter(self._pending_round.values()))[0])
            if ts != existing_ts:
                self._complete_round()

        # ── Debug cache (immediate, not batched) ──
        if self._debug_cache is not None:
            self._debug_cache.append(symbol, bar)

        # ── Accumulate ──
        self._pending_round[symbol] = bar
        if self._round_start_time is None:
            self._round_start_time = time.monotonic()

        self._bars_received += 1

        # ── Check: all symbols arrived? ──
        if self._expected_symbols <= set(self._pending_round.keys()):
            self._complete_round()
            return

        # ── Check: timeout (skip for first round — no last_round to fill) ──
        if (
            self._last_round is not None
            and self._round_start_time is not None
        ):
            elapsed = time.monotonic() - self._round_start_time
            if elapsed >= self._batch_timeout_s:
                self._complete_round()

    def flush_for_clock(self) -> dict[str, np.ndarray] | None:
        """Return the latest complete round of kline bars.

        Called by ClockDriver on each tick.  Returns ``None`` if no
        complete round is available yet (e.g. first round still filling).

        Three paths, tried in order:

        1. **Ready buffer** — a freshly completed round is waiting.
           This is the normal fast path when kline data arrives on time.

        2. **Pending + last_round** — some bars arrived but the round
           isn't complete.  Missing symbols are forward-filled from the
           previous round.

        3. **Full forward-fill** — no new bars at all since the last
           tick (e.g. kline WS delayed).  Returns the last known round
           as-is.  ``_runtime_loop`` will see ret≈0 for these bars
           because close hasn't changed.  This is rare in practice:
           OKX closes 1s klines at/before the wall-clock boundary.
        """
        # Path 1: take the ready buffer if available.
        with self._ready_lock:
            if self._ready_round is not None:
                result = self._ready_round
                self._ready_round = None
                return result

        # Path 2: assemble from pending + last_round.
        if self._pending_round:
            return self._build_round_from_pending()

        # Path 3: full forward-fill from last round.
        if self._last_round is not None:
            with self._lock:
                self._forward_fill_count += len(self._last_round)
            return dict(self._last_round)

        return None

    # ── internal ───────────────────────────────────────────────

    def _complete_round(self) -> None:
        """Mark the current pending round as complete."""
        round_bars = self._build_round_from_pending()
        if round_bars is None:
            return

        with self._lock:
            for sym, bar in round_bars.items():
                self._last_pushed_ts[sym] = int(bar[0])
            self._rounds_completed += 1

        self._last_round = round_bars
        self._pending_round.clear()
        self._round_start_time = None

        with self._ready_lock:
            self._ready_round = round_bars

    def _build_round_from_pending(self) -> dict[str, np.ndarray] | None:
        """Build a round from pending bars + forward-fill from last_round."""
        round_bars: dict[str, np.ndarray] = dict(self._pending_round)

        if self._last_round is not None:
            fill_count = 0
            for sym in self._expected_symbols:
                if sym not in round_bars:
                    round_bars[sym] = self._last_round[sym].copy()
                    fill_count += 1
            if fill_count:
                with self._lock:
                    self._forward_fill_count += fill_count

        # First round must have all symbols.
        if self._last_round is None and set(round_bars.keys()) < self._expected_symbols:
            return None

        return round_bars

    # ── stats ──────────────────────────────────────────────────

    @property
    def bars_received(self) -> int:
        return self._bars_received

    @property
    def rounds_completed(self) -> int:
        with self._lock:
            return self._rounds_completed

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

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "bars_received": self._bars_received,
                "rounds_completed": self._rounds_completed,
                "forward_fill_count": self._forward_fill_count,
                "dup_count": self._dup_count,
                "gap_count": self._gap_count,
            }
