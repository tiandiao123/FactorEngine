"""BinanceTradesWorker — manages the Binance aggTrade streaming thread.

Assembles BinanceTradesCollector → TradesAccumulator into a daemon thread
that can be started / stopped by Engine.

The architecture mirrors :class:`dataflow.okx.trades.worker.TradesWorker`:
a daemon thread runs an asyncio event loop for the WebSocket collector;
each incoming aggTrade is routed to the corresponding symbol's
:class:`TradesAccumulator` via a synchronous callback.

ClockDriver calls :meth:`flush` periodically.  ``flush()`` snapshots
all accumulators and returns ``(round_bars, round_trades)``.  The
worker does **not** push to ``bar_queue`` — that is ClockDriver's job.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import numpy as np

from dataflow.trades.accumulator import TradesAccumulator

from .collector import BinanceTradesCollector

logger = logging.getLogger(__name__)

_NAN = float("nan")


class BinanceTradesWorker:
    """Daemon thread: Binance aggTrade WS → per-symbol accumulators.

    Parameters
    ----------
    symbols : list[str]
        Unified instrument IDs, e.g. ``["BTC-USDT", "ETH-USDT"]``.
    """

    def __init__(self, symbols: list[str], *, testnet: bool = False):
        self.symbols = list(symbols)
        self._testnet = testnet
        self._accumulators: dict[str, TradesAccumulator] = {
            sym: TradesAccumulator() for sym in self.symbols
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._collector: BinanceTradesCollector | None = None
        self._trade_count = 0
        self._flush_count = 0
        self._freq_ms: float = 0.0

    @property
    def accumulators(self) -> dict[str, TradesAccumulator]:
        """Per-symbol accumulators (read-only access for tests/monitoring)."""
        return self._accumulators

    # ── flush interface (called by ClockDriver) ────────────────

    def flush(self) -> tuple[dict[str, np.ndarray], dict[str, dict]]:
        """Snapshot all accumulators and return (round_bars, round_trades).

        Builds BarPoint arrays ``[ts, open, high, low, close, vol, NaN, NaN]``
        from the accumulator's OHLCV fields so ``Engine._runtime_loop`` can
        process them identically to kline-sourced bars.
        """
        ts_ms = time.time() * 1000
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

    def set_freq(self, freq_seconds: float) -> None:
        """Set the bar frequency for timestamp alignment in flush()."""
        self._freq_ms = freq_seconds * 1000

    # ── lifecycle ──────────────────────────────────────────────

    def start(self):
        """Start the Binance trades worker daemon thread."""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="binance-trades",
        )
        self._thread.start()
        logger.info(
            "BinanceTradesWorker started: %d symbols", len(self.symbols),
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
            "BinanceTradesWorker stopped (trades=%d, flushes=%d, ws_msgs=%d)",
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
                "BinanceTradesWorker event loop exited with RuntimeError: %s",
                exc,
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
        collector = BinanceTradesCollector(
            symbols=self.symbols,
            on_trade=self._on_trade,
            testnet=self._testnet,
        )
        self._collector = collector
        try:
            await collector.run()
        except asyncio.CancelledError:
            pass
