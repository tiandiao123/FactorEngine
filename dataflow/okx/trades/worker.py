"""TradesWorker — manages the trades streaming thread.

Assembles OKXTradesCollector → TradesAccumulator into a daemon thread
that can be started / stopped by Engine.

The thread runs an asyncio event loop for the WebSocket collector; each
incoming trade is routed to the corresponding symbol's
:class:`TradesAccumulator` via a synchronous callback.

ClockDriver calls :meth:`flush` periodically.  ``flush()`` snapshots
all accumulators and returns ``(round_bars, round_trades)``.  The
worker does **not** push to ``bar_queue`` — that is ClockDriver's job.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time

import numpy as np

from dataflow.symbols import SymbolDiff
from dataflow.trades.accumulator import EventTimeTradesAccumulator

from .accumulator import TradesAccumulator
from .collector import OKXTradesCollector

logger = logging.getLogger(__name__)

_NAN = float("nan")


class TradesWorker:
    """Daemon thread: OKX trades WS → per-symbol accumulators.

    Parameters
    ----------
    symbols : list[str]
        Instruments to subscribe, e.g. ``["BTC-USDT-SWAP", ...]``.
    """

    def __init__(
        self,
        symbols: list[str],
        *,
        trade_time_mode: str = "receive_time",
        allowed_lateness_ms: int = 1000,
        freq_seconds: float | None = None,
    ):
        if trade_time_mode not in ("receive_time", "exchange_time"):
            raise ValueError("trade_time_mode must be 'receive_time' or 'exchange_time'")
        if allowed_lateness_ms < 0:
            raise ValueError("allowed_lateness_ms must be non-negative")
        if trade_time_mode == "exchange_time" and freq_seconds is None:
            raise ValueError("freq_seconds is required for exchange_time trades")
        self.symbols = list(symbols)
        self._trade_time_mode = trade_time_mode
        self._allowed_lateness_ms = int(allowed_lateness_ms)
        self._freq_ms: float = (freq_seconds or 0.0) * 1000
        self._symbol_lock = threading.RLock()
        self._active_symbols: set[str] = set(self.symbols)
        self._target_symbols: set[str] = set(self.symbols)
        self._accumulators: dict[str, TradesAccumulator | EventTimeTradesAccumulator] = {
            sym: self._new_accumulator() for sym in self.symbols
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._collector: OKXTradesCollector | None = None
        self._rebuild_requested = threading.Event()
        # Written from the WS asyncio thread (_on_trade), read from main
        # thread (stats/stop). Safe in CPython (GIL guarantees atomic int
        # read/write) but not portable to free-threaded builds.
        self._trade_count = 0
        self._dropped_late_trade_count = 0
        self._flush_count = 0
        self._rebuild_count = 0
        self._target_bucket_start_ms: int | None = None

    @property
    def accumulators(self) -> dict[str, TradesAccumulator | EventTimeTradesAccumulator]:
        """Per-symbol accumulators (read-only access for tests/monitoring)."""
        return self._accumulators

    def active_symbols(self) -> set[str]:
        """Return the currently active symbol set."""
        with self._symbol_lock:
            return set(self._active_symbols)

    # ── flush interface (called by ClockDriver) ────────────────

    def flush(
        self, now_ms: int | None = None
    ) -> tuple[dict[str, np.ndarray], dict[str, dict]] | tuple[int, dict[str, np.ndarray], dict[str, dict]] | None:
        """Snapshot all accumulators and return (round_bars, round_trades).

        Builds BarPoint arrays ``[ts, open, high, low, close, vol, NaN, NaN]``
        from the accumulator's OHLCV fields so ``Engine._runtime_loop`` can
        process them identically to kline-sourced bars.

        Returns
        -------
        tuple
            ``(round_bars, round_trades)`` where ``round_bars`` maps each
            symbol to a BarPoint ``ndarray(8,)`` and ``round_trades`` maps
            each symbol to the accumulator snapshot dict.
        """
        if self._trade_time_mode == "exchange_time":
            return self._flush_exchange_time(now_ms)
        return self._flush_receive_time()

    def _flush_receive_time(self) -> tuple[dict[str, np.ndarray], dict[str, dict]]:
        ts_ms = time.time() * 1000
        if self._freq_ms > 0:
            ts_ms = ts_ms - (ts_ms % self._freq_ms)
        ts_ms = int(ts_ms)

        round_bars: dict[str, np.ndarray] = {}
        round_trades: dict[str, dict] = {}

        with self._symbol_lock:
            accumulators = {
                sym: self._accumulators.get(sym)
                for sym in sorted(self._active_symbols)
            }

        for sym, acc in accumulators.items():
            if acc is None:
                continue
            snap = acc.snapshot_and_reset()
            round_trades[sym] = snap

            bar = np.array([
                ts_ms,
                snap.get("open", _NAN),
                snap.get("high", _NAN),
                snap.get("low", _NAN),
                snap.get("close", _NAN),
                snap.get("volume", 0.0),
                _NAN,
                _NAN,
            ], dtype=np.float64)
            round_bars[sym] = bar

        self._flush_count += 1
        return (round_bars, round_trades)

    def _flush_exchange_time(
        self, now_ms: int | None = None
    ) -> tuple[int, dict[str, np.ndarray], dict[str, dict]] | None:
        self._flush_count += 1
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        with self._symbol_lock:
            target_bucket_start_ms = self._target_bucket_start_ms
            accumulators = {
                sym: self._accumulators.get(sym)
                for sym in sorted(self._active_symbols)
            }
        if target_bucket_start_ms is None:
            return None

        target_bucket_end_ms = target_bucket_start_ms + int(self._freq_ms)
        watermark_ms = now_ms - self._allowed_lateness_ms
        if target_bucket_end_ms > watermark_ms:
            return None

        round_bars: dict[str, np.ndarray] = {}
        round_trades: dict[str, dict] = {}

        for sym, acc in accumulators.items():
            if not isinstance(acc, EventTimeTradesAccumulator):
                continue
            snap = acc.pop_ready_bucket(target_bucket_start_ms, now_ms, active=True)
            if snap is None:
                continue
            snap_dict = snap.to_dict()
            snap_dict["bucket_time_basis"] = "exchange_time"
            snap_dict["allowed_lateness_ms"] = self._allowed_lateness_ms
            round_trades[sym] = snap_dict
            round_bars[sym] = self._bar_from_snapshot(snap_dict)

        with self._symbol_lock:
            if self._target_bucket_start_ms == target_bucket_start_ms:
                self._target_bucket_start_ms += int(self._freq_ms)

        if not round_bars:
            return None
        return (target_bucket_start_ms, round_bars, round_trades)

    def set_freq(self, freq_seconds: float) -> None:
        """Set the bar frequency for timestamp alignment in flush()."""
        self._freq_ms = freq_seconds * 1000

    def apply_symbol_diff(self, diff: SymbolDiff) -> None:
        """Apply a committed universe diff and request collector rebuild."""
        with self._symbol_lock:
            for sym in diff.added:
                self._accumulators[sym] = self._new_accumulator()
            for sym in diff.removed:
                self._accumulators.pop(sym, None)
            self._active_symbols = set(diff.target)
            self._target_symbols = set(diff.target)
            self.symbols = sorted(diff.target)
        self._request_collector_rebuild()

    # ── lifecycle ──────────────────────────────────────────────

    def start(self):
        """Start the trades worker daemon thread."""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="okx-trades",
        )
        self._thread.start()
        logger.info(
            "TradesWorker started: %d symbols", len(self.symbols),
        )

    def stop(self):
        """Stop the worker thread gracefully."""
        if self._collector is not None:
            self._collector.stop()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._cancel_loop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info(
            "TradesWorker stopped (trades=%d, flushes=%d, ws_msgs=%d)",
            self._trade_count, self._flush_count,
            self._collector.msg_count if self._collector else 0,
        )

    # ── stats ─────────────────────────────────────────────────

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def flush_count(self) -> int:
        return self._flush_count

    def stats(self) -> dict[str, int]:
        return {
            "trade_count": self._trade_count,
            "dropped_late_trade_count": self._dropped_late_trade_count,
            "flush_count": self._flush_count,
            "ws_msg_count": self._collector.msg_count if self._collector else 0,
            "symbols": len(self.active_symbols()),
            "rebuild_count": self._rebuild_count,
        }

    # ── internal ──────────────────────────────────────────────

    def _on_trade(
        self, symbol: str, px: float, sz: float, side: str, ts_ms: int
    ) -> None:
        with self._symbol_lock:
            acc = self._accumulators.get(symbol)
        if acc is not None:
            if self._trade_time_mode == "exchange_time":
                receive_ts_ms = int(time.time() * 1000)
                bucket_start_ms = self._bucket_start_ms(ts_ms)
                with self._symbol_lock:
                    target_bucket_start_ms = self._target_bucket_start_ms
                if (
                    target_bucket_start_ms is not None
                    and bucket_start_ms < target_bucket_start_ms
                ):
                    self._dropped_late_trade_count += 1
                    return
                accepted = acc.update(px, sz, side, ts_ms, receive_ts_ms)
                if accepted:
                    with self._symbol_lock:
                        if self._target_bucket_start_ms is None:
                            self._target_bucket_start_ms = bucket_start_ms
                    self._trade_count += 1
                else:
                    self._dropped_late_trade_count += 1
            else:
                acc.update(px, sz, side)
                self._trade_count += 1

    def _new_accumulator(self) -> TradesAccumulator | EventTimeTradesAccumulator:
        if self._trade_time_mode == "exchange_time":
            return EventTimeTradesAccumulator(
                freq_ms=int(self._freq_ms),
                allowed_lateness_ms=self._allowed_lateness_ms,
            )
        return TradesAccumulator()

    def _bucket_start_ms(self, ts_ms: int) -> int:
        freq_ms = int(self._freq_ms)
        return int(ts_ms) - (int(ts_ms) % freq_ms)

    def _bar_from_snapshot(self, snap: dict) -> np.ndarray:
        return np.array([
            snap["bucket_start_ms"],
            snap.get("open", _NAN),
            snap.get("high", _NAN),
            snap.get("low", _NAN),
            snap.get("close", _NAN),
            snap.get("volume", 0.0),
            _NAN,
            _NAN,
        ], dtype=np.float64)

    def _request_collector_rebuild(self) -> None:
        self._rebuild_requested.set()
        if self._collector is not None:
            self._collector.stop()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)

    def _cancel_loop(self):
        for task in asyncio.all_tasks(self._loop):
            task.cancel()
        self._loop.stop()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except RuntimeError as exc:
            logger.debug(
                "TradesWorker event loop exited with RuntimeError: %s", exc
            )
        finally:
            pending = asyncio.all_tasks(self._loop)
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _run(self):
        while True:
            with self._symbol_lock:
                symbols = sorted(self._target_symbols)
            self._rebuild_requested.clear()
            collector = OKXTradesCollector(
                symbols=symbols,
                on_trade=self._on_trade,
            )
            self._collector = collector
            try:
                await collector.run()
            except asyncio.CancelledError:
                pass
            if not self._rebuild_requested.is_set():
                break
            self._rebuild_count += 1
            logger.info(
                "TradesWorker rebuilding collector: %d symbols",
                len(self.active_symbols()),
            )
