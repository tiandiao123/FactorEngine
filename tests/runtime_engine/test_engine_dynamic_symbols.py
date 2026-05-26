from __future__ import annotations

import threading
import time

import numpy as np

from dataflow.symbols import StaticSymbolSource, SymbolDiff, SymbolProvider
from factorengine.engine import Engine


def test_runtime_loop_handles_structured_bars_and_filters_active_symbols():
    engine = Engine(
        symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        mode="simulation",
        sim_seed=42,
        factor_group="okx_perp",
        num_threads=1,
    )
    btc = "BTC-USDT-SWAP"
    eth = "ETH-USDT-SWAP"
    bar = np.array([1000.0, 100.0, 102.0, 99.0, 101.0, 5000.0, 0.0, 0.0])
    engine._bar_queue.put({
        "type": "bars",
        "bars": {btc: bar, eth: bar},
        "trades": None,
        "bucket_ts_ms": 1000,
        "symbol_version": "v1",
        "active_symbols": {btc},
    })

    engine._stop_event.clear()
    t = threading.Thread(target=engine._runtime_loop, daemon=True)
    t.start()
    time.sleep(0.3)
    engine._stop_event.set()
    t.join(timeout=2)

    latest = engine.signal_deque[-1]
    assert latest["symbol_version"] == "v1"
    assert latest["active_symbols"] == {btc}
    assert btc in latest["factors"]
    assert eth not in latest["factors"]
    assert btc in latest["symbol_status"]
    assert eth not in latest["symbol_status"]
    engine.stop()


def test_runtime_loop_consumes_symbol_diff_control_message_before_next_bar():
    engine = Engine(
        symbols=["BTC-USDT-SWAP"],
        mode="simulation",
        sim_seed=42,
        factor_group="okx_perp",
        num_threads=1,
    )
    sym = "BTC-USDT-SWAP"
    bar = np.array([1000.0, 100.0, 102.0, 99.0, 101.0, 5000.0, 0.0, 0.0])
    diff = SymbolDiff(
        version="v2",
        added=frozenset({"SOL-USDT-SWAP"}),
        removed=frozenset(),
        unchanged=frozenset({sym}),
        target=frozenset({sym, "SOL-USDT-SWAP"}),
    )

    engine._bar_queue.put({"type": "bars", "bars": {sym: bar}, "symbol_version": "v1"})
    engine._bar_queue.put({"type": "symbol_diff", "diff": diff})
    engine._bar_queue.put({"type": "bars", "bars": {sym: bar}, "symbol_version": "v2"})

    engine._stop_event.clear()
    t = threading.Thread(target=engine._runtime_loop, daemon=True)
    t.start()
    time.sleep(0.5)
    engine._stop_event.set()
    t.join(timeout=2)

    assert engine.bars_pushed == 2
    assert engine._last_symbol_diff == diff
    assert engine.signal_deque[0]["symbol_version"] == "v1"
    assert engine.signal_deque[-1]["symbol_version"] == "v2"
    engine.stop()


def test_runtime_symbol_diff_physically_updates_cpp_runtime_symbols():
    engine = Engine(
        symbols=["BTC-USDT-SWAP"],
        mode="simulation",
        sim_seed=42,
        factor_group="okx_perp",
        num_threads=1,
        enable_dynamic_symbols=True,
    )
    btc = "BTC-USDT-SWAP"
    eth = "ETH-USDT-SWAP"
    bar = np.array([1000.0, 100.0, 102.0, 99.0, 101.0, 5000.0, 0.0, 0.0])
    diff = SymbolDiff(
        version="v2",
        added=frozenset({eth}),
        removed=frozenset({btc}),
        unchanged=frozenset(),
        target=frozenset({eth}),
    )

    assert engine._inference.has_symbol(btc)
    assert not engine._inference.has_symbol(eth)
    engine._bar_queue.put({"type": "bars", "bars": {btc: bar}, "symbol_version": "v1"})
    engine._bar_queue.put({"type": "symbol_diff", "diff": diff})
    engine._bar_queue.put({
        "type": "bars",
        "bars": {btc: bar, eth: bar},
        "symbol_version": "v2",
        "active_symbols": {eth},
    })

    engine._stop_event.clear()
    t = threading.Thread(target=engine._runtime_loop, daemon=True)
    t.start()
    time.sleep(0.5)
    engine._stop_event.set()
    t.join(timeout=2)

    assert not engine._inference.has_symbol(btc)
    assert engine._inference.has_symbol(eth)
    assert engine._inference.symbols() == [eth]
    assert engine.signal_deque[-1]["symbol_version"] == "v2"
    assert set(engine.signal_deque[-1]["factors"]) == {eth}
    assert set(engine.signal_deque[-1]["symbol_status"]) == {eth}
    engine.stop()


def test_engine_exposes_symbol_status_and_ready_only_outputs():
    engine = Engine(
        symbols=["BTC-USDT-SWAP"],
        mode="simulation",
        sim_seed=42,
        factor_group="okx_perp",
        num_threads=1,
    )
    sym = "BTC-USDT-SWAP"
    bar = np.array([1000.0, 100.0, 102.0, 99.0, 101.0, 5000.0, 0.0, 0.0])
    warmup_bars = engine._inference.get_symbol_status(sym)["warmup_bars"]

    engine._bar_queue.put({"type": "bars", "bars": {sym: bar}, "symbol_version": "v1"})

    engine._stop_event.clear()
    t = threading.Thread(target=engine._runtime_loop, daemon=True)
    t.start()
    time.sleep(0.3)
    engine._stop_event.set()
    t.join(timeout=2)

    status = engine.get_symbol_status()[sym]
    assert status["bars_pushed"] == 1
    assert status["warmup_bars"] == warmup_bars
    assert status["ready"] == 0
    assert engine.get_factor_outputs()
    assert engine.get_ready_factor_outputs() == {}
    engine.stop()


def test_engine_update_symbols_stages_until_explicit_commit():
    engine = Engine(
        symbols=["BTC-USDT"],
        mode="live",
        exchange="binance",
        data_source="trades",
        data_freq="1s",
        factor_group=None,
        enable_dynamic_symbols=True,
    )

    diff = engine.update_symbols(["BTC-USDT", "ETH-USDT"], version="v2")

    assert diff is not None
    assert engine.pending_symbol_diff == diff
    assert engine.active_symbols == {"BTC-USDT"}
    assert engine.symbol_version == "initial"

    engine.commit_pending_symbol_diff(effective_from_ts_ms=1000)

    assert engine.pending_symbol_diff is None
    assert engine.active_symbols == {"BTC-USDT", "ETH-USDT"}
    assert engine.symbol_version == "v2"
    assert engine._trades_worker.active_symbols() == {"BTC-USDT", "ETH-USDT"}
    assert engine._trades_worker._rebuild_requested.is_set()
    engine.stop()


def test_engine_dynamic_symbols_notify_okx_trades_worker():
    engine = Engine(
        symbols=["BTC-USDT-SWAP"],
        mode="live",
        exchange="okx",
        data_source="trades",
        data_freq="1s",
        factor_group=None,
        enable_dynamic_symbols=True,
    )

    diff = engine.update_symbols(["BTC-USDT-SWAP", "ETH-USDT-SWAP"], version="v2")
    engine.commit_pending_symbol_diff(effective_from_ts_ms=1000)

    assert diff is not None
    assert engine.active_symbols == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
    assert engine.symbol_version == "v2"
    assert engine._trades_worker.active_symbols() == {
        "BTC-USDT-SWAP",
        "ETH-USDT-SWAP",
    }
    assert engine._trades_worker._rebuild_requested.is_set()
    engine.stop()


def test_clockdriver_commit_enqueues_symbol_diff_after_bar_payload():
    engine = Engine(
        symbols=["BTC-USDT"],
        mode="live",
        exchange="binance",
        data_source="trades",
        data_freq="1s",
        factor_group="okx_perp_trades",
        num_threads=1,
        enable_dynamic_symbols=True,
    )
    bar = np.array([1000.0, 100.0, 102.0, 99.0, 101.0, 5.0, 0.0, 0.0])
    engine.update_symbols(["BTC-USDT", "ETH-USDT"], version="v2")

    engine._clock_driver._aggregate = lambda _raw: ({"BTC-USDT": bar}, {})
    engine._clock_driver._flush_and_aggregate()

    first = engine._bar_queue.get_nowait()
    second = engine._bar_queue.get_nowait()
    assert first["type"] == "bars"
    assert first["symbol_version"] == "initial"
    assert first["active_symbols"] == {"BTC-USDT"}
    assert second["type"] == "symbol_diff"
    assert second["diff"].version == "v2"
    assert engine.active_symbols == {"BTC-USDT", "ETH-USDT"}
    engine.stop()


def test_refresh_symbols_once_stages_provider_universe_without_commit():
    provider = SymbolProvider(
        StaticSymbolSource(
            ["BTC-USDT", "ETH-USDT"],
            exchange="binance",
            version="v2",
        )
    )
    engine = Engine(
        symbols=["BTC-USDT"],
        mode="live",
        exchange="binance",
        data_source="trades",
        data_freq="1s",
        factor_group=None,
        enable_dynamic_symbols=True,
        symbol_provider=provider,
    )

    diff = engine.refresh_symbols_once()

    assert diff is not None
    assert diff.added == frozenset({"ETH-USDT"})
    assert engine.pending_symbol_diff == diff
    assert engine.active_symbols == {"BTC-USDT"}
    engine.stop()


def test_symbol_refresh_thread_lifecycle_starts_and_stops():
    provider = SymbolProvider(
        StaticSymbolSource(
            ["BTC-USDT", "ETH-USDT"],
            exchange="binance",
            version="v2",
        ),
        refresh_interval_seconds=60,
    )
    engine = Engine(
        symbols=["BTC-USDT"],
        mode="live",
        exchange="binance",
        data_source="trades",
        data_freq="1s",
        factor_group=None,
        enable_dynamic_symbols=True,
        symbol_provider=provider,
    )

    engine.start()
    try:
        assert engine._symbol_refresh_thread is not None
        assert engine._symbol_refresh_thread.is_alive()
    finally:
        engine.stop()

    assert engine._symbol_refresh_thread is None
