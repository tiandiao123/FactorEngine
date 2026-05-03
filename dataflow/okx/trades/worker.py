"""TradesWorker — manages the trades streaming thread.

Assembles OKXTradesCollector → TradesAccumulator into a daemon thread
that can be started / stopped by Engine.

The thread runs an asyncio event loop for the WebSocket collector; each
incoming trade is routed to the corresponding symbol's
:class:`TradesAccumulator` via a synchronous callback.

**The worker does not trigger round flush.** In ``kline_trades`` mode the
flush is driven by ``KlinePublisher``; in ``trades``-only mode by
``ClockDriver``.  Both read accumulators via the public
:attr:`accumulators` dict.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from .accumulator import TradesAccumulator
from .collector import OKXTradesCollector

logger = logging.getLogger(__name__)


class TradesWorker:
    """Daemon thread: OKX trades WS → per-symbol accumulators.

    Parameters
    ----------
    symbols : list[str]
        Instruments to subscribe, e.g. ``["BTC-USDT-SWAP", ...]``.
    """

    def __init__(self, symbols: list[str]):
        self.symbols = list(symbols)
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

    # ── public API ─────────────────────────────────────────────

    @property
    def accumulators(self) -> dict[str, TradesAccumulator]:
        """Per-symbol accumulators — read by KlinePublisher or ClockDriver."""
        return self._accumulators

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
            "TradesWorker stopped (trades=%d, ws_msgs=%d)",
            self._trade_count,
            self._collector.msg_count if self._collector else 0,
        )

    # ── stats ─────────────────────────────────────────────────

    @property
    def trade_count(self) -> int:
        return self._trade_count

    def stats(self) -> dict[str, int]:
        return {
            "trade_count": self._trade_count,
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
