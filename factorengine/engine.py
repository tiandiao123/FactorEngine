"""FactorEngine — the top-level Engine class.

Engine is the single entry point: it owns the Dataflow thread, optionally
runs a separate factor inference thread, and exposes get_data() /
get_factor_outputs() for pulling snapshots.

Architecture (v2 — signal-decoupled, 3 threads):

    [dataflow thread]  sim-bars
        generate bar → BarCache.append() → bar_queue.put()

    [runtime thread]  factor-infer
        bar_queue.get() → push_bars (C++ parallel) → signal_deque.append()

    [main thread]  strategy / consumer
        get_factor_outputs() → signal_deque[-1]   (O(1), no compute)
        get_data()           → BarCache.snapshot() (copy)

Usage (without factors):
    engine = Engine(symbols=["BTC-USDT-SWAP"], mode="simulation", sim_seed=42)
    engine.start()
    snapshot = engine.get_data()
    engine.stop()

Usage (with C++ factor inference):
    engine = Engine(symbols=["BTC-USDT-SWAP"], mode="simulation", sim_seed=42,
                    factor_group="okx_perp", num_threads=4)
    engine.start()
    time.sleep(10)                                # wait for warmup
    factors  = engine.get_factor_outputs()        # {symbol: {factor_id: float}}
    snapshot = engine.get_data()                  # bar cache snapshot
    engine.stop()
"""

import logging
import queue
import re
import threading
from collections import deque

import numpy as np

from dataflow.manager import DataflowManager
from dataflow.okx.common import resolve_bar_channel

logger = logging.getLogger(__name__)

_FREQ_RE = re.compile(r"^(\d+)(s|sec|m|min|h|hr)$", re.IGNORECASE)
_UNIT_TO_SECONDS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600}


def parse_freq(freq: str) -> int:
    """Parse a frequency string like '5s', '10s', '1min', '1h' into seconds."""
    m = _FREQ_RE.match(freq.strip())
    if not m:
        raise ValueError(f"Invalid frequency: {freq!r}. Examples: '1s', '5s', '10s', '1min', '1h'")
    return int(m.group(1)) * _UNIT_TO_SECONDS[m.group(2).lower()]


class Engine:
    """Top-level entry point. Owns the dataflow manager and shared bar cache."""

    def __init__(
        self,
        symbols: list[str],
        data_freq: str = "5s",
        pull_interval: str = "10s",
        bar_window_length: int = 1000,
        trade_window_length: int = 10_000,
        book_history_length: int = 1_000,
        enable_bars: bool = True,
        enable_trades: bool = False,
        trade_channels: tuple[str, ...] = ("trades-all",),
        enable_books: bool = False,
        book_channels: tuple[str, ...] = ("books5",),
        mode: str = "live",
        sim_bar_interval: float | None = None,
        sim_seed: int | None = None,
        factor_group: str | None = None,
        num_threads: int = 4,
        signal_buffer_size: int = 3,
        bar_queue_size: int = 16,
        bar_queue_timeout: float = 0.5,
    ):
        self.symbols = symbols
        self.data_freq = data_freq
        self.pull_interval = pull_interval
        self.bar_window_length = bar_window_length
        self.trade_window_length = trade_window_length
        self.book_history_length = book_history_length
        self.enable_bars = enable_bars
        self.enable_trades = enable_trades
        self.trade_channels = trade_channels
        self.enable_books = enable_books
        self.book_channels = book_channels
        self.mode = mode

        self.data_freq_seconds = parse_freq(data_freq)
        self.pull_interval_seconds = parse_freq(pull_interval)

        # C++ factor inference engine (optional).
        self._inference = None
        self._factor_group = factor_group
        self._num_threads = num_threads
        self._factor_ids: list[str] = []
        self._signal_deque: deque[dict] = deque(maxlen=signal_buffer_size)
        self._prev_closes: dict[str, float] = {}
        self._bars_pushed = 0

        # Inter-thread communication: dataflow → runtime.
        self._bar_queue: queue.Queue | None = None
        self._bar_queue_timeout = bar_queue_timeout
        self._runtime_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        if factor_group:
            self._init_inference(factor_group, num_threads)
            self._bar_queue = queue.Queue(maxsize=bar_queue_size)

        if mode == "live":
            bar_channel, needs_agg = resolve_bar_channel(self.data_freq_seconds)
            logger.info(
                "Bar config: data_freq=%s (%ds) → channel=%s, aggregation=%s",
                data_freq, self.data_freq_seconds, bar_channel,
                f"{self.data_freq_seconds}s" if needs_agg else "none (direct)",
            )

            self._dataflow = DataflowManager(
                symbols=symbols,
                bar_agg_seconds=self.data_freq_seconds,
                bar_window_length=bar_window_length,
                trade_window_length=trade_window_length,
                book_history_length=book_history_length,
                enable_bars=enable_bars,
                enable_trades=enable_trades,
                trade_channels=trade_channels,
                enable_books=enable_books,
                book_channels=book_channels,
            )
        elif mode == "simulation":
            from dataflow.simulation.manager import SimDataflowManager
            self._dataflow = SimDataflowManager(
                symbols=symbols,
                bar_interval_seconds=sim_bar_interval if sim_bar_interval is not None else 1.0,
                bar_window_length=bar_window_length,
                seed=sim_seed,
                bar_queue=self._bar_queue,
            )
            logger.info(
                "Simulation mode: %d symbols, bar_interval=%.2fs, seed=%s",
                len(symbols),
                sim_bar_interval if sim_bar_interval is not None else 1.0,
                sim_seed,
            )
        else:
            raise ValueError(f"Unknown mode: {mode!r}. Use 'live' or 'simulation'.")

        # Backward-compatible accessors for the current bar cache.
        self._data_cache = self._dataflow.data_cache
        self._lock = self._dataflow.lock

    # ── Factor inference ──────────────────────────────────────

    def _init_inference(self, group: str, num_threads: int):
        import fe_runtime as rt
        from factorengine.factors import FactorRegistry

        reg = FactorRegistry()
        reg.load_group(group)
        self._factor_ids = reg.factor_ids_by_group(group)

        self._inference = rt.InferenceEngine(num_threads=num_threads)
        for sym in self.symbols:
            self._inference.add_symbol(sym)
            for fid, graph in reg.build_group(group).items():
                self._inference.add_factor(sym, fid, graph)

        logger.info(
            "InferenceEngine initialized: %d symbols × %d factors, %d threads",
            len(self.symbols), len(self._factor_ids), num_threads,
        )

    def _runtime_loop(self):
        """Runtime thread main loop: pull bars from queue, infer, write deque."""
        import fe_runtime as rt

        logger.info("factor-infer thread started")
        while not self._stop_event.is_set():
            try:
                round_bars = self._bar_queue.get(timeout=self._bar_queue_timeout)
            except queue.Empty:
                continue

            ts_ms = 0
            batch: dict[str, rt.BarData] = {}
            for sym, bar in round_bars.items():
                ts_ms = int(bar[0])
                close = float(bar[4])
                volume = float(bar[5])
                open_ = float(bar[1])
                high = float(bar[2])
                low = float(bar[3])
                prev_close = self._prev_closes.get(sym, close)
                ret = (close / prev_close - 1.0) if prev_close != 0 else 0.0
                batch[sym] = rt.BarData(close, volume, open_, high, low, ret)
                self._prev_closes[sym] = close

            self._inference.push_bars(batch)
            self._bars_pushed += 1

            result = self._inference.get_all_outputs()
            self._signal_deque.append({
                "ts": ts_ms,
                "bar_index": self._bars_pushed,
                "factors": result,
            })

        logger.info("factor-infer thread stopped (bars_pushed=%d)", self._bars_pushed)

    def get_factor_outputs(
        self, symbols: list[str] | None = None
    ) -> dict[str, dict[str, float]]:
        """Get the latest factor values for each symbol.

        Reads from the signal_deque (O(1), no computation triggered).

        Returns:
            {symbol: {factor_id: float_value}}
        """
        if not self._signal_deque:
            return {}
        latest = self._signal_deque[-1]["factors"]
        if symbols is None:
            return latest
        return {sym: latest[sym] for sym in symbols if sym in latest}

    @property
    def factor_ids(self) -> list[str]:
        """List of registered factor IDs (empty if no factor_group configured)."""
        return self._factor_ids if self._inference else []

    @property
    def bars_pushed(self) -> int:
        """Number of bar rounds pushed to InferenceEngine so far."""
        return self._bars_pushed

    @property
    def signal_deque(self) -> deque:
        """Access the signal buffer (for advanced use / debugging)."""
        return self._signal_deque

    # ── Dataflow API ──────────────────────────────────────────

    def start(self):
        """Start the dataflow collection and (optionally) the inference thread."""
        if self._bar_queue is not None:
            self._stop_event.clear()
            self._runtime_thread = threading.Thread(
                target=self._runtime_loop, daemon=True, name="factor-infer"
            )
            self._runtime_thread.start()
        self._dataflow.start()
        logger.info(
            "Engine started: %d symbols, data_freq=%s (%ds), pull_interval=%s (%ds), "
            "bar_window=%d, factors=%s",
            len(self.symbols), self.data_freq, self.data_freq_seconds,
            self.pull_interval, self.pull_interval_seconds, self.bar_window_length,
            self._factor_group or "none",
        )

    def stop(self):
        """Stop the dataflow collection and inference thread."""
        self._dataflow.stop()
        if self._runtime_thread is not None:
            self._stop_event.set()
            self._runtime_thread.join(timeout=5)
            self._runtime_thread = None
        logger.info("Engine stopped")

    def get_data(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        """Get a snapshot (copy) of the bar cache.

        Returns:
            {symbol: ndarray of shape (N, 8)} where columns are
            [ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote].
        """
        return self._dataflow.get_bar_snapshot(symbols)

    def get_trade_data(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        """Get a snapshot (copy) of the trade cache."""
        return self._dataflow.get_trade_snapshot(symbols)

    def get_book_data(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        """Get a snapshot (copy) of the book cache."""
        return self._dataflow.get_book_snapshot(symbols)

    @property
    def bar_count(self) -> int:
        """Total number of bars aggregated so far."""
        return self._dataflow.bar_count

    @property
    def trade_count(self) -> int:
        """Total number of trade events captured so far."""
        return self._dataflow.trade_count

    @property
    def book_count(self) -> int:
        """Total number of book events captured so far."""
        return self._dataflow.book_count
