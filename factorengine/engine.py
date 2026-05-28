"""FactorEngine — the top-level Engine class.

Engine is the single entry point: it owns the Dataflow thread, optionally
runs a separate factor inference thread, and exposes get_data() /
get_factor_outputs() for pulling snapshots.

Architecture (v4 — ClockDriver unified flush):

    [dataflow thread(s)]  — each worker collects data independently
        okx-kline  → KlineWorker   (WS → adapter → publisher buffer)
        okx-trades → TradesWorker  (WS → per-symbol accumulators)

    [clock-driver thread]  — unified flush & aggregation
        ClockDriver(freq=5s)
          every tick:
            kline_data  = kline_worker.flush()
            trades_data = trades_worker.flush()
            → aggregate → bar_queue.put((merged_bars, merged_trades))

    [runtime thread]  factor-infer
        bar_queue.get() → push_bars (C++ parallel) → signal_deque.append()

    [main thread]  strategy / consumer
        get_factor_outputs() → signal_deque[-1]   (O(1), no compute)
        get_data()           → debug BarCache.snapshot() (copy, optional)

data_source modes:
    "kline"        — kline WS only (default, backward compatible)
    "trades"       — trades WS only; ClockDriver flushes trades
    "kline_trades" — both kline + trades WS, ClockDriver flushes both

In all live modes with factor_group, ClockDriver is the single component
that pushes aggregated data to bar_queue.  Workers never touch bar_queue.

Usage (live with kline + trades factors — OKX):
    engine = Engine(symbols=["BTC-USDT-SWAP"], mode="live", data_freq="5s",
                    factor_group=["okx_perp", "okx_perp_trades"],
                    num_threads=4, data_source="kline_trades")
    engine.start()
    time.sleep(30)
    factors = engine.get_factor_outputs()
    engine.stop()

Usage (live trades-only — Binance Futures):
    engine = Engine(symbols=["BTC-USDT", "ETH-USDT"], mode="live",
                    exchange="binance", data_freq="1s",
                    factor_group="okx_perp_trades", data_source="trades",
                    num_threads=4)
    engine.start()

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
import time
from collections import deque

import numpy as np

from dataflow.audit.config import AuditConfig
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
    _VALID_EXCHANGES = ("okx", "binance")
    _VALID_TRADE_TIME_MODES = ("receive_time", "exchange_time")

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
        exchange: str = "okx",
        testnet: bool = False,
        audit_config: AuditConfig | None = None,
        enable_dynamic_symbols: bool = False,
        symbol_provider=None,
        symbol_refresh_interval_seconds: float | None = None,
        trade_time_mode: str = "receive_time",
        allowed_lateness_ms: int = 1000,
    ):
        if data_source not in self._VALID_DATA_SOURCES:
            raise ValueError(
                f"Invalid data_source: {data_source!r}. "
                f"Use one of {self._VALID_DATA_SOURCES}"
            )
        if exchange not in self._VALID_EXCHANGES:
            raise ValueError(
                f"Invalid exchange: {exchange!r}. "
                f"Use one of {self._VALID_EXCHANGES}"
            )
        if trade_time_mode not in self._VALID_TRADE_TIME_MODES:
            raise ValueError(
                f"Invalid trade_time_mode: {trade_time_mode!r}. "
                f"Use one of {self._VALID_TRADE_TIME_MODES}"
            )
        if allowed_lateness_ms < 0:
            raise ValueError("allowed_lateness_ms must be non-negative")
        if data_source == "kline_trades" and trade_time_mode == "exchange_time":
            raise ValueError(
                "trade_time_mode='exchange_time' is not supported with "
                "data_source='kline_trades' in phase 1; use data_source='trades'."
            )

        self.symbols = symbols
        self.data_freq = data_freq
        self.pull_interval = pull_interval
        self.bar_window_length = bar_window_length
        self.mode = mode
        self._data_source = data_source
        self._exchange = exchange
        self._testnet = testnet
        self._trade_time_mode = trade_time_mode
        self._allowed_lateness_ms = int(allowed_lateness_ms)
        self._dynamic_symbols_enabled = enable_dynamic_symbols
        self._symbol_manager = None
        self._symbol_provider = symbol_provider
        self._symbol_refresh_interval_seconds = symbol_refresh_interval_seconds
        self._active_symbols: set[str] = set(symbols)
        self._symbol_version = "initial"
        self._audit = None
        if audit_config is not None and audit_config.enabled:
            from dataflow.audit.writer import AuditWriter

            self._audit = AuditWriter(audit_config)

        self.data_freq_seconds = parse_freq(data_freq)
        self.pull_interval_seconds = parse_freq(pull_interval)

        # Normalize factor_group to list.
        if isinstance(factor_group, str):
            self._factor_groups: list[str] = [factor_group]
        elif isinstance(factor_group, list):
            self._factor_groups = list(factor_group)
        else:
            self._factor_groups = []
        self._factor_group = factor_group

        # C++ factor inference engine (optional).
        self._inference = None
        self._num_threads = num_threads
        self._factor_ids: list[str] = []
        self._factor_registry = None
        self._signal_deque: deque[dict] = deque(maxlen=signal_buffer_size)
        self._prev_closes: dict[str, float] = {}
        self._bars_pushed = 0
        self._last_bar_debug: dict | None = None
        self._last_symbol_diff = None

        if enable_dynamic_symbols:
            from dataflow.symbols import SymbolManager

            self._symbol_manager = SymbolManager(
                symbols,
                exchange=exchange,
                version=self._symbol_version,
            )

        # Inter-thread communication: ClockDriver → runtime.
        self._bar_queue: queue.Queue | None = None
        self._bar_queue_timeout = bar_queue_timeout
        self._runtime_thread: threading.Thread | None = None
        self._symbol_refresh_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._symbol_refresh_stop_event = threading.Event()

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
            self._init_live(data_source, symbols, enable_debug_cache)
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

    def _init_live(self, data_source: str, symbols: list[str], enable_debug_cache: bool):
        """Wire up live mode workers and ClockDriver."""
        from dataflow.cache import BarCache

        exchange = self._exchange

        # Binance Futures does not support kline below 1m.
        if exchange == "binance" and data_source in ("kline", "kline_trades"):
            if self.data_freq_seconds < 60:
                raise ValueError(
                    f"Binance Futures does not support kline below 1m. "
                    f"Use data_source='trades' for {self.data_freq} frequency."
                )

        if exchange == "okx":
            bar_channel, _ = resolve_bar_channel(self.data_freq_seconds)
            logger.info(
                "Live mode [OKX]: data_freq=%s (%ds) → channel=%s, "
                "factors=%s, data_source=%s",
                self.data_freq, self.data_freq_seconds, bar_channel,
                self._factor_group or "none", data_source,
            )
        else:
            logger.info(
                "Live mode [%s]: data_freq=%s (%ds), factors=%s, data_source=%s",
                exchange.upper(), self.data_freq, self.data_freq_seconds,
                self._factor_group or "none", data_source,
            )

        if enable_debug_cache:
            self._debug_cache = BarCache(window_length=self.bar_window_length)

        # ── Create workers based on data_source + exchange ──
        if data_source in ("kline", "kline_trades"):
            if exchange == "okx":
                from dataflow.okx.kline.worker import KlineWorker

                self._kline_worker = KlineWorker(
                    symbols=symbols,
                    freq_seconds=self.data_freq_seconds,
                    debug_cache=self._debug_cache,
                )
            elif exchange == "binance":
                from dataflow.binance.kline.worker import BinanceKlineWorker

                self._kline_worker = BinanceKlineWorker(
                    symbols=symbols,
                    freq_seconds=self.data_freq_seconds,
                    debug_cache=self._debug_cache,
                    testnet=self._testnet,
                )

        if data_source in ("trades", "kline_trades"):
            if exchange == "okx":
                from dataflow.okx.trades.worker import TradesWorker

                self._trades_worker = TradesWorker(
                    symbols=symbols,
                    trade_time_mode=self._trade_time_mode,
                    allowed_lateness_ms=self._allowed_lateness_ms,
                    freq_seconds=self.data_freq_seconds,
                )
            elif exchange == "binance":
                from dataflow.binance.trades.worker import BinanceTradesWorker

                self._trades_worker = BinanceTradesWorker(
                    symbols=symbols,
                    testnet=self._testnet,
                    trade_time_mode=self._trade_time_mode,
                    allowed_lateness_ms=self._allowed_lateness_ms,
                    freq_seconds=self.data_freq_seconds,
                )
            self._trades_worker.set_freq(self.data_freq_seconds)

        # ── ClockDriver: unified flush & aggregation ──
        if self._bar_queue is not None or self._audit is not None:
            from dataflow.clock_driver import ClockDriver

            self._clock_driver = ClockDriver(
                freq_seconds=self.data_freq_seconds,
                bar_queue=self._bar_queue,
                audit=self._audit,
                exchange=self._exchange,
                data_source=self._data_source,
                after_flush_commit=(
                    self.commit_pending_symbol_diff
                    if self._dynamic_symbols_enabled
                    else None
                ),
                structured_payload=self._dynamic_symbols_enabled,
                symbol_version_getter=lambda: self._symbol_version,
                active_symbols_getter=lambda: set(self._active_symbols),
            )

            if self._kline_worker is not None:
                self._clock_driver.register("kline", self._kline_worker)
            if self._trades_worker is not None:
                self._clock_driver.register("trades", self._trades_worker)
        else:
            logger.info(
                "Live mode without factor_group/audit: ClockDriver not created "
                "(no bar_queue consumer or audit writer).",
            )

    # ── Factor inference ──────────────────────────────────────

    def _init_inference(self, groups: list[str], num_threads: int):
        import fe_runtime as rt
        from factorengine.factors import FactorRegistry

        reg = FactorRegistry()
        for group in groups:
            reg.load_group(group)

        self._factor_registry = reg
        self._inference = rt.InferenceEngine(num_threads=num_threads)
        for sym in self.symbols:
            self._add_runtime_symbol_with_factors(sym)

        self._factor_ids = []
        for group in groups:
            self._factor_ids.extend(reg.factor_ids_by_group(group))
        self._factor_ids.sort()

        logger.info(
            "InferenceEngine initialized: %d symbols × %d factors (%s), %d threads",
            len(self.symbols), len(self._factor_ids),
            "+".join(groups), num_threads,
        )

    def _add_runtime_symbol_with_factors(self, symbol: str) -> None:
        """Create one C++ SymbolRunner and attach fresh factor graphs."""
        if self._inference is None or self._factor_registry is None:
            return
        if hasattr(self._inference, "has_symbol") and self._inference.has_symbol(symbol):
            raise RuntimeError(f"Runtime symbol already registered: {symbol}")

        self._inference.add_symbol(symbol)
        for group in self._factor_groups:
            graphs = self._factor_registry.build_group(group)
            for fid, graph in graphs.items():
                self._inference.add_factor(symbol, fid, graph)

    def _runtime_loop(self):
        """Runtime thread main loop: pull bars from queue, infer, write deque."""
        import fe_runtime as rt

        nan = float("nan")
        logger.info("factor-infer thread started")
        while not self._stop_event.is_set():
            try:
                payload = self._bar_queue.get(timeout=self._bar_queue_timeout)
            except queue.Empty:
                continue

            parsed = self._parse_runtime_payload(payload)
            if parsed is None:
                continue
            round_bars, round_trades, payload_meta = parsed

            ts_ms = 0
            batch: dict[str, rt.BarData] = {}
            active_symbols = payload_meta.get("active_symbols")
            for sym, bar in round_bars.items():
                if active_symbols is not None and sym not in active_symbols:
                    continue
                ts_ms = int(bar[0])
                close = float(bar[4])
                volume = float(bar[5])
                open_ = float(bar[1])
                high = float(bar[2])
                low = float(bar[3])
                prev_close = self._prev_closes.get(sym, close)
                if close != close or prev_close != prev_close:
                    ret = 0.0
                elif prev_close != 0:
                    ret = close / prev_close - 1.0
                else:
                    ret = 0.0
                if close == close:
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

            self._last_bar_debug = {
                "ts": ts_ms,
                "bar_index": self._bars_pushed,
                "bars": {
                    sym: {
                        "open": bd.open, "high": bd.high, "low": bd.low,
                        "close": bd.close, "volume": bd.volume, "ret": bd.ret,
                        "vwap": bd.vwap, "buy_vol": bd.buy_vol, "sell_vol": bd.sell_vol,
                    }
                    for sym, bd in batch.items()
                },
            }

            result = self._inference.get_all_outputs()
            symbol_status = (
                self._inference.get_all_symbol_status()
                if hasattr(self._inference, "get_all_symbol_status")
                else {}
            )
            if active_symbols is not None:
                result = {
                    sym: factors
                    for sym, factors in result.items()
                    if sym in active_symbols
                }
                symbol_status = {
                    sym: status
                    for sym, status in symbol_status.items()
                    if sym in active_symbols
                }
            self._signal_deque.append({
                "ts": ts_ms,
                "bar_index": self._bars_pushed,
                "symbol_version": payload_meta.get("symbol_version"),
                "active_symbols": active_symbols,
                "factors": result,
                "symbol_status": symbol_status,
            })

        logger.info("factor-infer thread stopped (bars_pushed=%d)", self._bars_pushed)

    def _parse_runtime_payload(self, payload):
        """Normalize legacy and structured runtime queue payloads."""
        if isinstance(payload, dict) and payload.get("type") == "symbol_diff":
            diff = payload.get("diff")
            if diff is not None:
                self._apply_symbol_diff_to_runtime(diff)
            return None
        if isinstance(payload, dict) and payload.get("type") == "bars":
            meta = {
                "symbol_version": payload.get("symbol_version"),
                "active_symbols": (
                    set(payload["active_symbols"])
                    if payload.get("active_symbols") is not None
                    else None
                ),
            }
            return payload.get("bars", {}), payload.get("trades"), meta
        if isinstance(payload, tuple):
            round_bars, round_trades = payload
            return round_bars, round_trades, {}
        return payload, None, {}

    def _apply_symbol_diff_to_runtime(self, diff) -> None:
        """Synchronize committed symbol diff into C++ runtime state.

        This must run on the factor runtime thread, ordered with bar payloads.
        ClockDriver only enqueues the control message and never mutates C++.
        """
        self._last_symbol_diff = diff
        self._active_symbols = set(diff.target)
        self._symbol_version = diff.version

        if self._inference is None:
            return

        for sym in sorted(diff.removed):
            if (
                hasattr(self._inference, "has_symbol")
                and self._inference.has_symbol(sym)
            ):
                self._inference.remove_symbol(sym)
            self._prev_closes.pop(sym, None)

        for sym in sorted(diff.added):
            if (
                hasattr(self._inference, "has_symbol")
                and self._inference.has_symbol(sym)
            ):
                self._inference.remove_symbol(sym)
            self._add_runtime_symbol_with_factors(sym)

    def update_symbols(
        self,
        symbols: list[str],
        *,
        version: str | None = None,
        metadata: dict | None = None,
        allow_empty: bool = False,
    ):
        """Stage a full target symbol list for the next post-flush commit."""
        if not self._dynamic_symbols_enabled or self._symbol_manager is None:
            raise RuntimeError("Dynamic symbols are not enabled for this Engine")
        if version is None:
            version = f"manual:{int(time.time() * 1000)}"
        return self._symbol_manager.update_symbols(
            symbols,
            version=version,
            metadata=metadata,
            allow_empty=allow_empty,
        )

    def refresh_symbols_once(self):
        """Fetch provider universe once and stage the resulting diff."""
        if self._symbol_provider is None:
            raise RuntimeError("No symbol_provider configured")
        if not self._dynamic_symbols_enabled or self._symbol_manager is None:
            raise RuntimeError("Dynamic symbols are not enabled for this Engine")
        universe = self._symbol_provider.fetch()
        return self._symbol_manager.stage_universe(universe)

    def _symbol_refresh_loop(self) -> None:
        interval = self._symbol_refresh_interval()
        logger.info("symbol-refresh thread started: interval=%.1fs", interval)
        self._refresh_symbols_safely()
        while not self._symbol_refresh_stop_event.wait(interval):
            self._refresh_symbols_safely()
        logger.info("symbol-refresh thread stopped")

    def _refresh_symbols_safely(self) -> None:
        try:
            self.refresh_symbols_once()
        except Exception:
            logger.exception("symbol-refresh failed")

    def _symbol_refresh_interval(self) -> float:
        if self._symbol_refresh_interval_seconds is not None:
            interval = self._symbol_refresh_interval_seconds
        else:
            interval = getattr(
                self._symbol_provider,
                "refresh_interval_seconds",
                None,
            )
            if interval is None:
                interval = 1800.0
        interval = float(interval)
        if interval <= 0:
            raise ValueError("symbol refresh interval must be positive")
        return interval

    def commit_pending_symbol_diff(self, effective_from_ts_ms: int | None = None) -> None:
        """Commit staged symbol diff after ClockDriver has flushed a bucket."""
        if self._symbol_manager is None:
            return
        diff = self._symbol_manager.commit_pending()
        if diff is None:
            return

        self._active_symbols = set(diff.target)
        self._symbol_version = diff.version
        self.symbols = sorted(diff.target)

        if self._trades_worker is not None and hasattr(self._trades_worker, "apply_symbol_diff"):
            self._trades_worker.apply_symbol_diff(diff)

        self._write_symbol_diff_audit(diff, effective_from_ts_ms)

        if self._bar_queue is not None:
            try:
                self._bar_queue.put({
                    "type": "symbol_diff",
                    "diff": diff,
                    "effective_from_ts_ms": effective_from_ts_ms,
                }, block=False)
            except queue.Full:
                logger.warning("Dynamic symbol diff dropped because bar_queue is full")

    def _write_symbol_diff_audit(self, diff, effective_from_ts_ms: int | None) -> None:
        if self._audit is None:
            return
        try:
            from dataflow.audit import SymbolDiffEvent

            self._audit.write_symbol_diff(
                SymbolDiffEvent(
                    commit_ts_ms=int(time.time() * 1000),
                    effective_from_ts_ms=effective_from_ts_ms,
                    exchange=self._exchange,
                    data_source=self._data_source,
                    symbol_version=diff.version,
                    added=sorted(diff.added),
                    removed=sorted(diff.removed),
                    unchanged_count=len(diff.unchanged),
                    target_count=len(diff.target),
                    reason=diff.reason,
                )
            )
        except Exception:
            logger.exception("Dynamic symbol diff audit write failed")

    def get_factor_outputs(
        self,
        symbols: list[str] | None = None,
        *,
        ready_only: bool = False,
    ) -> dict[str, dict[str, float]]:
        """Get the latest factor values for each symbol.

        Reads from the signal_deque (O(1), no computation triggered).

        Returns:
            {symbol: {factor_id: float_value}}
        """
        if not self._signal_deque:
            return {}
        latest_snapshot = self._signal_deque[-1]
        latest = latest_snapshot["factors"]
        if ready_only:
            status = latest_snapshot.get("symbol_status", {})
            latest = {
                sym: factors
                for sym, factors in latest.items()
                if status.get(sym, {}).get("ready")
            }
        if symbols is None:
            return latest
        return {sym: latest[sym] for sym in symbols if sym in latest}

    def get_symbol_status(self, symbols: list[str] | None = None) -> dict[str, dict]:
        """Get latest per-symbol runtime readiness metadata."""
        if not self._signal_deque:
            return {}
        status = self._signal_deque[-1].get("symbol_status", {})
        if symbols is None:
            return status
        return {sym: status[sym] for sym in symbols if sym in status}

    def get_ready_factor_outputs(
        self,
        symbols: list[str] | None = None,
    ) -> dict[str, dict[str, float]]:
        """Get factor outputs only for symbols whose full factor set is ready."""
        return self.get_factor_outputs(symbols, ready_only=True)

    @property
    def factor_ids(self) -> list[str]:
        """List of registered factor IDs (empty if no factor_group configured)."""
        return self._factor_ids if self._inference else []

    @property
    def active_symbols(self) -> set[str]:
        """Current committed active symbol set."""
        return set(self._active_symbols)

    @property
    def pending_symbol_diff(self):
        """Pending dynamic symbol diff, if any."""
        if self._symbol_manager is None:
            return None
        return self._symbol_manager.pending_diff

    @property
    def symbol_version(self) -> str:
        """Current committed symbol universe version."""
        return self._symbol_version

    @property
    def symbol_stats(self) -> dict:
        """Dynamic symbol manager stats."""
        if self._symbol_manager is None:
            return {
                "enabled": False,
                "version": self._symbol_version,
                "active_count": len(self._active_symbols),
            }
        stats = self._symbol_manager.stats()
        stats["enabled"] = True
        return stats

    @property
    def bars_pushed(self) -> int:
        """Number of bar rounds pushed to InferenceEngine so far."""
        return self._bars_pushed

    @property
    def signal_deque(self) -> deque:
        """Access the signal buffer (for advanced use / debugging)."""
        return self._signal_deque

    @property
    def last_bar_debug(self) -> dict | None:
        """Last BarData batch sent to C++ InferenceEngine (for debugging).

        Returns dict with keys: ts, bar_index, bars.
        ``bars`` maps symbol → {open, high, low, close, volume, ret, vwap, buy_vol, sell_vol}.
        """
        return self._last_bar_debug

    # ── Dataflow API ──────────────────────────────────────────

    def start(self):
        """Start the dataflow collection and (optionally) the inference thread."""
        if self._audit is not None:
            self._audit.start()

        if (
            self._dynamic_symbols_enabled
            and self._symbol_provider is not None
            and self._symbol_refresh_thread is None
        ):
            self._symbol_refresh_stop_event.clear()
            self._symbol_refresh_thread = threading.Thread(
                target=self._symbol_refresh_loop,
                daemon=True,
                name="symbol-refresh",
            )
            self._symbol_refresh_thread.start()

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
            "Engine started: mode=%s, exchange=%s, %d symbols, data_freq=%s (%ds), "
            "bar_window=%d, factors=%s, data_source=%s",
            self.mode, self._exchange, len(self.symbols), self.data_freq,
            self.data_freq_seconds, self.bar_window_length,
            "+".join(self._factor_groups) if self._factor_groups else "none",
            self._data_source,
        )

    def stop(self):
        """Stop the dataflow collection and inference thread."""
        if self._symbol_refresh_thread is not None:
            self._symbol_refresh_stop_event.set()
            self._symbol_refresh_thread.join(timeout=5)
            self._symbol_refresh_thread = None

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
        if self._audit is not None:
            self._audit.stop()
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
    def exchange(self) -> str:
        """Active exchange."""
        return self._exchange

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
            return self._trades_worker.stats()
        return {}

    @property
    def clock_stats(self) -> dict[str, int]:
        """ClockDriver statistics (empty dict if not active)."""
        if self._clock_driver is not None:
            return {
                "clock_ticks": self._clock_driver.tick_count,
                "clock_drops": self._clock_driver.drop_count,
            }
        return {}

    @property
    def audit_stats(self) -> dict:
        """Audit writer statistics (empty dict if audit logging is disabled)."""
        if self._audit is not None:
            return self._audit.stats()
        return {}
