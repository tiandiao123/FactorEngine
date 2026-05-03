"""TradesWorker — manages the trades streaming thread.

Assembles OKXTradesCollector → TradesAccumulator into a daemon thread
that can be started / stopped by Engine.

The thread runs an asyncio event loop for the WebSocket collector; each
incoming trade is routed to the corresponding symbol's
:class:`TradesAccumulator` via a synchronous callback.

Flush modes:

* **kline_trades**: ``KlinePublisher`` reads accumulators directly via
  the :attr:`accumulators` dict.  Worker does not push to ``bar_queue``.
* **trades-only**: ``ClockDriver`` calls :meth:`flush` periodically.
  ``flush()`` snapshots all accumulators, builds BarPoint arrays from
  trades-derived OHLCV, and pushes ``(round_bars, round_trades)`` into
  the shared ``bar_queue``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import threading
import time

import numpy as np

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
    bar_queue : queue.Queue or None
        If provided (trades-only mode), :meth:`flush` pushes
        ``(round_bars, round_trades)`` here.  In kline_trades mode this
        is ``None`` — ``KlinePublisher`` reads accumulators directly.
    """

    def __init__(
        self,
        symbols: list[str],
        bar_queue: queue.Queue | None = None,
    ):
        self.symbols = list(symbols)
        self._bar_queue = bar_queue
        self._accumulators: dict[str, TradesAccumulator] = {
            sym: TradesAccumulator() for sym in self.symbols
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._collector: OKXTradesCollector | None = None
        # Written from the WS asyncio thread (_on_trade), read from main
        # thread (stats/stop). Safe in CPython (GIL guarantees atomic int
        # read/write) but not portable to free-threaded builds.
        self._trade_count = 0
        self._flush_count = 0
        self._freq_ms: float = 0.0  # set via set_freq(); 0 = no alignment

    # ── public API ─────────────────────────────────────────────

    @property
    def accumulators(self) -> dict[str, TradesAccumulator]:
        """Per-symbol accumulators — read by KlinePublisher or ClockDriver."""
        return self._accumulators

    def flush(self) -> None:
        """Snapshot all accumulators and push to bar_queue.

        Called by ``ClockDriver`` in trades-only mode.  Builds BarPoint
        arrays ``[ts, open, high, low, close, vol, NaN, NaN]`` from the
        accumulator's OHLCV fields so ``Engine._runtime_loop`` can
        process them identically to kline-sourced bars.
        """
        ts_ms = time.time() * 1000
        # Align to freq boundary (same logic as ClockDriver).
        if self._freq_ms > 0:
            ts_ms = ts_ms - (ts_ms % self._freq_ms)
        ts_ms = int(ts_ms)

        round_bars: dict[str, np.ndarray] = {}
        round_trades: dict[str, dict] = {}

        for sym in self.symbols:
            acc = self._accumulators.get(sym)
            if acc is None:
                continue
            snap = acc.snapshot_and_reset()
            round_trades[sym] = snap

            # Build BarPoint from trades-derived OHLCV.
            bar = np.array([
                ts_ms,
                snap.get("open", _NAN),
                snap.get("high", _NAN),
                snap.get("low", _NAN),
                snap.get("close", _NAN),
                snap.get("volume", 0.0),
                _NAN,  # vol_ccy — not available from trades
                _NAN,  # vol_ccy_quote
            ], dtype=np.float64)
            round_bars[sym] = bar

        payload = (round_bars, round_trades)
        if self._bar_queue is not None:
            try:
                self._bar_queue.put(payload, block=False)
            except queue.Full:
                logger.warning(
                    "TradesWorker.flush: bar_queue full, dropping round "
                    "(ts=%d, symbols=%d)", ts_ms, len(round_bars),
                )

        self._flush_count += 1

    def set_freq(self, freq_seconds: float) -> None:
        """Set the bar frequency for timestamp alignment in flush()."""
        self._freq_ms = freq_seconds * 1000

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
            "flush_count": self._flush_count,
            "ws_msg_count": self._collector.msg_count if self._collector else 0,
            "symbols": len(self.symbols),
        }

    # ── internal ──────────────────────────────────────────────

    def _on_trade(
        self, symbol: str, px: float, sz: float, side: str, ts_ms: int
    ) -> None:
        acc = self._accumulators.get(symbol)
        if acc is not None:
            acc.update(px, sz, side)
            self._trade_count += 1

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
        collector = OKXTradesCollector(
            symbols=self.symbols,
            on_trade=self._on_trade,
        )
        self._collector = collector
        try:
            await collector.run()
        except asyncio.CancelledError:
            pass
