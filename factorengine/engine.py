"""FactorEngine — the top-level Engine class.

Engine is the single entry point: it owns the Dataflow thread, optionally
runs a separate factor inference thread, and exposes get_data() /
get_factor_outputs() for pulling snapshots.

Architecture (v3.1 — streaming, 3+ threads, trades support):

    [dataflow thread(s)]
        okx-kline  → KlineWorker  → bar_queue.put((round_bars, round_trades))
        okx-trades → TradesWorker → accumulators (read by KlinePublisher)

    [runtime thread]  factor-infer
        bar_queue.get() → push_bars (C++ parallel) → signal_deque.append()

    [main thread]  strategy / consumer
        get_factor_outputs() → signal_deque[-1]   (O(1), no compute)
        get_data()           → debug BarCache.snapshot() (copy, optional)

data_source modes:
    "kline"        — kline WS only (default, backward compatible)
    "trades"       — trades WS only; ClockDriver drives periodic flush
    "kline_trades" — both kline + trades WS, kline flush drives round

Usage (live with kline + trades factors):
    engine = Engine(symbols=["BTC-USDT-SWAP"], mode="live", data_freq="5s",
                    factor_group="okx_perp", num_threads=4,
                    data_source="kline_trades")
    engine.start()
    time.sleep(30)
    factors = engine.get_factor_outputs()
    engine.stop()

Usage (simulation with factors):
    engine = Engine(symbols=["BTC-USDT-SWAP"], mode="simulation", sim_seed=42,
                    factor_group="okx_perp", num_threads=4)
    engine.start()
    time.sleep(10)
    factors  = engine.get_factor_outputs()
    snapshot = engine.get_data()
    engine.stop()
"""

import logging
import queue
import re
import threading
from collections import deque

import numpy as np

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

    _VALID_DATA_SOURCES = ("kline", "trades", "kline_trades")

    def __init__(
        self,
        symbols: list[str],
        data_freq: str = "5s",
        pull_interval: str = "10s",
        bar_window_length: int = 1000,
        mode: str = "live",
        sim_bar_interval: float | None = None,
        sim_seed: int | None = None,
        factor_group: str | list[str] | None = None,
        num_threads: int = 4,
        signal_buffer_size: int = 3,
        bar_queue_size: int = 16,
        bar_queue_timeout: float = 0.5,
        enable_debug_cache: bool = True,
        data_source: str = "kline",
    ):
        if data_source not in self._VALID_DATA_SOURCES:
            raise ValueError(
                f"Invalid data_source: {data_source!r}. "
                f"Use one of {self._VALID_DATA_SOURCES}"
            )

        self.symbols = symbols
        self.data_freq = data_freq
        self.pull_interval = pull_interval
        self.bar_window_length = bar_window_length
        self.mode = mode
        self._data_source = data_source

        self.data_freq_seconds = parse_freq(data_freq)
        self.pull_interval_seconds = parse_freq(pull_interval)

        # Normalize factor_group to list.
        if isinstance(factor_group, str):
            self._factor_groups: list[str] = [factor_group]
        elif isinstance(factor_group, list):
            self._factor_groups = list(factor_group)
        else:
            self._factor_groups = []
        self._factor_group = factor_group  # original value for logging

        # C++ factor inference engine (optional).
        self._inference = None
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

        if self._factor_groups:
            self._init_inference(self._factor_groups, num_threads)
            self._bar_queue = queue.Queue(maxsize=bar_queue_size)

        # Debug bar cache (optional, not on the main compute path).
        self._debug_cache = None
        self._kline_worker = None
        self._trades_worker = None
        self._clock_driver = None
        self._dataflow = None

        if mode == "live":
            from dataflow.cache import BarCache

            bar_channel, _ = resolve_bar_channel(self.data_freq_seconds)
            logger.info(
                "Live mode: data_freq=%s (%ds) → channel=%s, factors=%s, data_source=%s",
                data_freq, self.data_freq_seconds, bar_channel,
                factor_group or "none", data_source,
            )

            if enable_debug_cache:
                self._debug_cache = BarCache(window_length=bar_window_length)

            # Start TradesWorker if data_source includes trades.
            accumulators = None
            if data_source in ("trades", "kline_trades"):
                from dataflow.okx.trades.worker import TradesWorker

                trades_queue = self._bar_queue if data_source == "trades" else None
                self._trades_worker = TradesWorker(
                    symbols=symbols,
                    bar_queue=trades_queue,
                )
                self._trades_worker.set_freq(self.data_freq_seconds)
                accumulators = self._trades_worker.accumulators

            # Start KlineWorker if data_source includes kline.
            if self._bar_queue is not None and data_source in ("kline", "kline_trades"):
                from dataflow.okx.kline.worker import KlineWorker

                self._kline_worker = KlineWorker(
                    symbols=symbols,
                    bar_queue=self._bar_queue,
                    freq_seconds=self.data_freq_seconds,
                    debug_cache=self._debug_cache,
                    accumulators=accumulators,
                )
            elif self._bar_queue is None:
                logger.info(
                    "Live mode without factor_group: KlineWorker not started "
                    "(no bar_queue consumer).",
                )

            # trades-only mode: ClockDriver drives periodic flush.
            if data_source == "trades" and self._trades_worker is not None:
                from dataflow.clock_driver import ClockDriver

                self._clock_driver = ClockDriver(freq_seconds=self.data_freq_seconds)
                self._clock_driver.register(self._trades_worker.flush)

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

    # ── Factor inference ──────────────────────────────────────

    def _init_inference(self, groups: list[str], num_threads: int):
        import fe_runtime as rt
        from factorengine.factors import FactorRegistry

        reg = FactorRegistry()
        for group in groups:
            reg.load_group(group)

        self._inference = rt.InferenceEngine(num_threads=num_threads)
        for sym in self.symbols:
            self._inference.add_symbol(sym)
            for group in groups:
                graphs = reg.build_group(group)
                for fid, graph in graphs.items():
                    self._inference.add_factor(sym, fid, graph)

        # Set factor ID list once after all symbols are wired up.
        self._factor_ids = []
        for group in groups:
            self._factor_ids.extend(reg.factor_ids_by_group(group))
        self._factor_ids.sort()

        logger.info(
            "InferenceEngine initialized: %d symbols × %d factors (%s), %d threads",
            len(self.symbols), len(self._factor_ids),
            "+".join(groups), num_threads,
        )

    def _runtime_loop(self):
        """Runtime thread main loop: pull bars from queue, infer, write deque."""
        import math
        import fe_runtime as rt

        nan = float("nan")
        logger.info("factor-infer thread started")
        while not self._stop_event.is_set():
            try:
                payload = self._bar_queue.get(timeout=self._bar_queue_timeout)
            except queue.Empty:
                continue

            # Unpack: KlinePublisher sends (round_bars, round_trades | None).
            if isinstance(payload, tuple):
                round_bars, round_trades = payload
            else:
                round_bars, round_trades = payload, None

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
                self._prev_closes[sym] = close

                vwap = nan
                buy_vol = nan
                sell_vol = nan
                if round_trades:
                    trades = round_trades.get(sym)
                    if trades:
                        vwap = trades.get("vwap", nan)
                        buy_vol = trades.get("buy_vol", nan)
                        sell_vol = trades.get("sell_vol", nan)

                batch[sym] = rt.BarData(
                    close, volume, open_, high, low, ret,
                    vwap, buy_vol, sell_vol,
                )

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
        if self._bar_queue is not None and self._inference is not None:
            self._stop_event.clear()
            self._runtime_thread = threading.Thread(
                target=self._runtime_loop, daemon=True, name="factor-infer"
            )
            self._runtime_thread.start()

        if self.mode == "live":
            if self._trades_worker is not None:
                self._trades_worker.start()
            if self._kline_worker is not None:
                self._kline_worker.start()
            if self._clock_driver is not None:
                self._clock_driver.start()
        elif self._dataflow is not None:
            self._dataflow.start()

        logger.info(
            "Engine started: mode=%s, %d symbols, data_freq=%s (%ds), "
            "bar_window=%d, factors=%s, data_source=%s",
            self.mode, len(self.symbols), self.data_freq,
            self.data_freq_seconds, self.bar_window_length,
            "+".join(self._factor_groups) if self._factor_groups else "none",
            self._data_source,
        )

    def stop(self):
        """Stop the dataflow collection and inference thread."""
        if self.mode == "live":
            if self._clock_driver is not None:
                self._clock_driver.stop()
            if self._kline_worker is not None:
                self._kline_worker.stop()
            if self._trades_worker is not None:
                self._trades_worker.stop()
        elif self._dataflow is not None:
            self._dataflow.stop()

        if self._runtime_thread is not None:
            self._stop_event.set()
            self._runtime_thread.join(timeout=5)
            self._runtime_thread = None
        logger.info("Engine stopped")

    def get_data(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        """Get a snapshot of the bar cache (debug / monitoring only).

        In live mode this reads from the optional debug cache.
        In simulation mode this reads from the SimDataflowManager bar cache.

        Returns:
            {symbol: ndarray of shape (N, 8)} where columns are
            [ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote].
        """
        if self.mode == "live":
            if self._debug_cache is None:
                return {}
            return self._debug_cache.snapshot(symbols)
        if self._dataflow is not None:
            return self._dataflow.get_bar_snapshot(symbols)
        return {}

    @property
    def bar_count(self) -> int:
        """Total number of bars produced by the dataflow layer."""
        if self._kline_worker is not None:
            return self._kline_worker.bar_count
        if self._dataflow is not None:
            return self._dataflow.bar_count
        return 0

    @property
    def data_source(self) -> str:
        """Active data source mode."""
        return self._data_source

    @property
    def kline_stats(self) -> dict[str, int]:
        """Live kline worker statistics (empty dict in simulation mode)."""
        if self._kline_worker is not None:
            return self._kline_worker.stats()
        return {}

    @property
    def trades_stats(self) -> dict[str, int]:
        """Live trades worker statistics (empty dict if not active)."""
        if self._trades_worker is not None:
            stats = self._trades_worker.stats()
            if self._clock_driver is not None:
                stats["clock_ticks"] = self._clock_driver.tick_count
            return stats
        return {}
