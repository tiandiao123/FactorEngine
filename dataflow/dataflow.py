"""Dataflow — data collection thread entry point.

Runs an asyncio event loop inside a thread. Subscribes to OKX candle1s,
aggregates into 5s bars, and writes to a shared data_cache dict.
"""

import asyncio
import logging
import threading

import numpy as np

from .collector import OKXCollector, fetch_all_swap_symbols

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger(__name__)

# data_cache fields: [ts, open, high, low, close, vol]
NUM_FIELDS = 6


class BarAggregator:
    """Accumulates confirmed 1s candles and emits an aggregated N-second bar."""

    def __init__(self, agg_seconds: int = 5):
        self.agg_seconds = agg_seconds
        self._buf: list[np.ndarray] = []  # each row: [ts, o, h, l, c, v]

    def on_candle1s(self, raw: list) -> np.ndarray | None:
        """Feed one 1s candle. Returns aggregated bar (1D array) or None."""
        # raw[-1] == "1" means confirmed
        if raw[-1] != "1":
            return None
        row = np.array([
            int(raw[0]),      # ts
            float(raw[1]),    # open
            float(raw[2]),    # high
            float(raw[3]),    # low
            float(raw[4]),    # close
            float(raw[5]),    # vol
        ])
        self._buf.append(row)
        if len(self._buf) >= self.agg_seconds:
            bar = self._merge()
            self._buf.clear()
            return bar
        return None

    def _merge(self) -> np.ndarray:
        buf = self._buf
        return np.array([
            buf[0][0],                          # ts (first bar's ts)
            buf[0][1],                          # open
            max(b[2] for b in buf),             # high
            min(b[3] for b in buf),             # low
            buf[-1][4],                         # close
            sum(b[5] for b in buf),             # vol
        ])


class Dataflow:
    """Data collection layer. Runs in its own thread with an asyncio event loop.

    Writes aggregated 5s bars into a shared data_cache dict.
    """

    def __init__(
        self,
        symbols: list[str],
        data_cache: dict[str, np.ndarray],
        lock: threading.Lock,
        agg_seconds: int = 5,
        window_length: int = 1000,
    ):
        self.symbols = symbols
        self._cache = data_cache
        self._lock = lock
        self.agg_seconds = agg_seconds
        self.window_length = window_length

        self._aggregators: dict[str, BarAggregator] = {
            s: BarAggregator(agg_seconds) for s in symbols
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._collector: OKXCollector | None = None
        self._session: aiohttp.ClientSession | None = None
        self._bar_count = 0

    def start(self):
        """Start the dataflow thread."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="dataflow")
        self._thread.start()
        logger.info("Dataflow thread started (%d symbols, %ds bars, window=%d)",
                     len(self.symbols), self.agg_seconds, self.window_length)

    def stop(self):
        """Stop the dataflow thread gracefully."""
        if self._collector is not None:
            self._collector.stop()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_loop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("Dataflow thread stopped")

    def _stop_loop(self):
        """Cancel all tasks then stop the loop (runs inside the loop thread)."""
        for task in asyncio.all_tasks(self._loop):
            task.cancel()
        self._loop.stop()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except RuntimeError:
            pass  # loop.stop() called from main thread — expected on shutdown
        finally:
            # Drain pending tasks so asyncio doesn't complain
            pending = asyncio.all_tasks(self._loop)
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _run(self):
        self._session = aiohttp.ClientSession()
        collector = OKXCollector(
            symbols=self.symbols,
            on_candle1s=self._on_candle1s,
        )
        self._collector = collector
        try:
            await collector.run()
        except asyncio.CancelledError:
            pass
        finally:
            await self._session.close()

    def _on_candle1s(self, records: list[dict]):
        """Callback from OKXCollector — aggregate and write to cache."""
        for rec in records:
            symbol = rec.get("instId", "")
            agg = self._aggregators.get(symbol)
            if agg is None:
                continue
            bar = agg.on_candle1s(rec["raw"])
            if bar is not None:
                self._write_cache(symbol, bar)
                self._bar_count += 1

    def _write_cache(self, symbol: str, bar: np.ndarray):
        """Append a 5s bar row to the cache, trim if over window_length."""
        with self._lock:
            if symbol not in self._cache:
                self._cache[symbol] = bar.reshape(1, -1)
            else:
                arr = self._cache[symbol]
                arr = np.vstack([arr, bar])
                if len(arr) > self.window_length:
                    arr = arr[-self.window_length:]
                self._cache[symbol] = arr

    @property
    def bar_count(self) -> int:
        return self._bar_count
