"""ClockDriver — unified wall-clock aligned flush driver and data aggregator.

All registered workers are flushed at the same wall-clock boundary,
ensuring time alignment across heterogeneous data sources (kline,
trades, orderbook, funding rate, etc.).

ClockDriver is the **single component** that pushes aggregated data to
``bar_queue``.  Individual workers never touch ``bar_queue`` directly.

Architecture::

    ClockDriver(freq_seconds=5, bar_queue=q)
      register("kline", kline_worker)
      register("trades", trades_worker)
      register("ob",  ob_worker)         # future

      every 5s (aligned to :00, :05, :10, ...):
        kline_data  = kline_worker.flush()   -> dict[sym, ndarray] | None
        trades_data = trades_worker.flush()  -> (dict[sym, ndarray], dict[sym, dict])
        ob_data     = ob_worker.flush()      -> ...

        → _aggregate(kline_data, trades_data, ob_data, ...)
        → bar_queue.put(  (merged_bars, merged_trades)  )

Workers must expose a ``flush()`` method.  The return type depends on
the worker type:

- **KlineWorker**: ``flush() -> dict[str, np.ndarray] | None``
- **TradesWorker**: ``flush() -> tuple[dict[str, np.ndarray], dict[str, dict]]``
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import queue
import threading
import time
from typing import Any, Callable, Protocol, runtime_checkable

import numpy as np

logger = logging.getLogger(__name__)


@runtime_checkable
class FlushableWorker(Protocol):
    """Any worker that exposes a ``flush()`` returning data."""
    def flush(self) -> Any: ...


class ClockDriver:
    """Unified clock that drives periodic flush and aggregation.

    Parameters
    ----------
    freq_seconds : float
        Flush interval in seconds (e.g. 5.0 for 5s bars).
    bar_queue : queue.Queue | None
        Destination queue consumed by ``Engine._runtime_loop()``.  May be
        ``None`` for audit-only live collection without factor inference.
    """

    def __init__(
        self,
        freq_seconds: float,
        bar_queue: queue.Queue | None,
        *,
        audit=None,
        exchange: str | None = None,
        data_source: str | None = None,
        after_flush_commit: Callable[[int], None] | None = None,
        structured_payload: bool = False,
        symbol_version_getter: Callable[[], str | None] | None = None,
        active_symbols_getter: Callable[[], set[str] | frozenset[str] | None] | None = None,
    ):
        self._freq = freq_seconds
        self._freq_ms = freq_seconds * 1000
        self._bar_queue = bar_queue
        self._audit = audit
        self._exchange = exchange or ""
        self._data_source = data_source or ""
        self._after_flush_commit = after_flush_commit
        self._structured_payload = structured_payload
        self._symbol_version_getter = symbol_version_getter
        self._active_symbols_getter = active_symbols_getter
        self._workers: dict[str, FlushableWorker] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tick_count = 0
        self._drop_count = 0

    def register(self, name: str, worker: FlushableWorker) -> None:
        """Register a named worker (e.g. ``"kline"``, ``"trades"``).

        Must be called before :meth:`start`.
        """
        if name in self._workers:
            raise ValueError(f"Worker '{name}' already registered")
        self._workers[name] = worker
        logger.info("ClockDriver: registered worker '%s'", name)

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        """Start the clock driver in a daemon thread."""
        if not self._workers:
            logger.warning("ClockDriver started with no registered workers")
        self._running = True
        self._thread = threading.Thread(
            target=self._run_thread, daemon=True, name="clock-driver",
        )
        self._thread.start()
        logger.info(
            "ClockDriver started: freq=%.1fs, workers=%s",
            self._freq, list(self._workers.keys()),
        )

    def stop(self) -> None:
        """Stop the clock driver."""
        self._running = False
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=min(self._freq + 2, 30.0))
        logger.info(
            "ClockDriver stopped (ticks=%d, drops=%d)",
            self._tick_count, self._drop_count,
        )

    # ── stats ──────────────────────────────────────────────────

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def drop_count(self) -> int:
        return self._drop_count

    @property
    def freq_seconds(self) -> float:
        return self._freq

    # ── internal ───────────────────────────────────────────────

    def _run_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._tick_loop())
        except RuntimeError:
            pass
        finally:
            self._loop.close()

    async def _tick_loop(self) -> None:
        """Fire flush + aggregate at wall-clock aligned boundaries."""
        while self._running:
            now_ms = time.time() * 1000
            next_boundary = now_ms - (now_ms % self._freq_ms) + self._freq_ms
            sleep_s = (next_boundary - now_ms) / 1000
            await asyncio.sleep(max(sleep_s, 0.001))

            if not self._running:
                break

            self._tick_count += 1
            self._flush_and_aggregate()

            logger.debug(
                "ClockDriver tick %d (%.3fs drift)",
                self._tick_count,
                (time.time() * 1000 - next_boundary) / 1000,
            )

    def _flush_and_aggregate(self) -> None:
        """Flush all workers and aggregate into a unified payload."""
        now_ms = int(time.time() * 1000)
        raw: dict[str, Any] = {}
        for name, worker in self._workers.items():
            try:
                raw[name] = _call_worker_flush(worker, now_ms)
            except Exception:
                logger.exception(
                    "ClockDriver: worker '%s' flush error (tick=%d)",
                    name, self._tick_count,
                )

        symbol_version = (
            self._symbol_version_getter()
            if self._symbol_version_getter is not None
            else None
        )
        active_symbols = (
            self._active_symbols_getter()
            if self._active_symbols_getter is not None
            else None
        )

        event_round = _event_time_trades_round(raw.get("trades"))
        if event_round is not None:
            bucket_ts_ms, trades_bars, trades_snaps = event_round
            round_raw = dict(raw)
            round_raw["trades"] = (trades_bars, trades_snaps)
            merged_bars, merged_trades = self._aggregate(round_raw)
            self._emit_round(
                merged_bars,
                merged_trades,
                bucket_ts_ms,
                symbol_version=symbol_version,
                active_symbols=active_symbols,
            )
            return

        merged_bars, merged_trades = self._aggregate(raw)
        bucket_ts_ms = _bucket_ts_ms(merged_bars)
        self._emit_round(
            merged_bars,
            merged_trades,
            bucket_ts_ms,
            symbol_version=symbol_version,
            active_symbols=active_symbols,
        )

    def _emit_round(
        self,
        merged_bars: dict[str, np.ndarray],
        merged_trades: dict[str, dict],
        bucket_ts_ms: int,
        *,
        symbol_version: str | None = None,
        active_symbols: set[str] | frozenset[str] | None = None,
    ) -> None:
        """Write one finalized round to audit, queue, and post-flush hook."""
        if merged_bars and self._audit is not None:
            try:
                self._audit.write_aggregate_batch(
                    exchange=self._exchange,
                    data_source=self._data_source,
                    freq_seconds=self._freq,
                    bars=merged_bars,
                    trades=merged_trades,
                    flush_ts_ms=int(time.time() * 1000),
                    symbol_version=symbol_version,
                    active_symbols_count=(
                        len(active_symbols) if active_symbols is not None else None
                    ),
                )
            except Exception:
                logger.exception(
                    "ClockDriver: audit aggregate write error (tick=%d)",
                    self._tick_count,
                )

        if merged_bars and self._bar_queue is not None:
            payload = self._build_payload(
                merged_bars,
                merged_trades,
                bucket_ts_ms,
                symbol_version=symbol_version,
                active_symbols=active_symbols,
            )
            try:
                self._bar_queue.put(payload, block=False)
            except queue.Full:
                self._drop_count += 1
                if self._drop_count % 100 == 1:
                    logger.warning(
                        "ClockDriver: bar_queue full — dropped tick %d "
                        "(total drops: %d)", self._tick_count, self._drop_count,
                    )

        if self._after_flush_commit is not None:
            try:
                self._after_flush_commit(bucket_ts_ms)
            except Exception:
                logger.exception(
                    "ClockDriver: after_flush_commit error (tick=%d)",
                    self._tick_count,
                )

    def _build_payload(
        self,
        merged_bars: dict[str, np.ndarray],
        merged_trades: dict[str, dict],
        bucket_ts_ms: int,
        *,
        symbol_version: str | None = None,
        active_symbols: set[str] | frozenset[str] | None = None,
    ) -> Any:
        if not self._structured_payload:
            return (merged_bars, merged_trades if merged_trades else None)
        return {
            "type": "bars",
            "bars": merged_bars,
            "trades": merged_trades if merged_trades else None,
            "bucket_ts_ms": bucket_ts_ms,
            "symbol_version": symbol_version,
            "active_symbols": set(active_symbols or set(merged_bars)),
        }

    def _aggregate(
        self, raw: dict[str, Any],
    ) -> tuple[dict[str, np.ndarray], dict[str, dict]]:
        """Merge worker outputs into unified (bars, trades) dicts.

        Kline bars take priority: if both kline and trades provide a
        bar for the same symbol, the kline bar is used (it has actual
        OHLCV from the exchange).  Trades-derived OHLCV is only used
        when kline data is absent for a symbol.
        """
        merged_bars: dict[str, np.ndarray] = {}
        merged_trades: dict[str, dict] = {}

        # ── kline worker ──
        kline_data = raw.get("kline")
        if kline_data is not None and isinstance(kline_data, dict):
            merged_bars.update(kline_data)

        # ── trades worker ──
        trades_data = raw.get("trades")
        if trades_data is not None and isinstance(trades_data, tuple):
            trades_bars, trades_snaps = trades_data
            if isinstance(trades_snaps, dict):
                merged_trades.update(trades_snaps)
            if isinstance(trades_bars, dict):
                for sym, bar in trades_bars.items():
                    if sym not in merged_bars:
                        merged_bars[sym] = bar

        return merged_bars, merged_trades


def _bucket_ts_ms(merged_bars: dict[str, np.ndarray]) -> int:
    for bar in merged_bars.values():
        try:
            return int(bar[0])
        except (IndexError, TypeError, ValueError):
            continue
    return int(time.time() * 1000)


def _call_worker_flush(worker: FlushableWorker, now_ms: int) -> Any:
    """Call flush(now_ms) for new workers, flush() for legacy workers."""
    try:
        params = inspect.signature(worker.flush).parameters
    except (TypeError, ValueError):
        return worker.flush()
    if any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in params.values()):
        return worker.flush(now_ms)
    if len(params) == 0:
        return worker.flush()
    return worker.flush(now_ms)


def _event_time_trades_round(
    trades_data: Any,
) -> tuple[int, dict[str, np.ndarray], dict[str, dict]] | None:
    if not isinstance(trades_data, tuple) or len(trades_data) != 3:
        return None
    bucket_ts_ms, bars, trades = trades_data
    if not isinstance(bars, dict) or not isinstance(trades, dict):
        return None
    try:
        bucket_ts_ms = int(bucket_ts_ms)
    except (TypeError, ValueError):
        return None
    return bucket_ts_ms, bars, trades
