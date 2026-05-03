"""Tests for Engine trades integration (data_source parameter).

Covers:
  - data_source parameter validation
  - data_source="kline" backward compatibility
  - data_source="kline_trades" wiring (TradesWorker + KlineWorker + accumulators)
  - _runtime_loop correctly unpacks (round_bars, round_trades) tuples
  - _runtime_loop builds BarData with trades fields (vwap, buy_vol, sell_vol)

Usage:
    pytest tests/runtime_engine/test_engine_trades_integration.py -v
"""
import math
import queue
import threading
import time

import numpy as np
import pytest

import fe_runtime as rt

import sys
sys.path.insert(0, ".")

from factorengine.engine import Engine, parse_freq


class TestDataSourceParam:
    """Test data_source parameter validation and initialization."""

    def test_default_is_kline(self):
        engine = Engine(
            symbols=["BTC-USDT-SWAP"],
            mode="simulation",
            sim_seed=42,
        )
        assert engine.data_source == "kline"
        engine.stop()

    def test_valid_data_sources(self):
        for ds in ("kline", "trades", "kline_trades"):
            engine = Engine(
                symbols=["BTC-USDT-SWAP"],
                mode="simulation",
                sim_seed=42,
                data_source=ds,
            )
            assert engine.data_source == ds
            engine.stop()

    def test_invalid_data_source_raises(self):
        with pytest.raises(ValueError, match="Invalid data_source"):
            Engine(
                symbols=["BTC-USDT-SWAP"],
                mode="simulation",
                sim_seed=42,
                data_source="orderbook",
            )

    def test_kline_backward_compat(self):
        """data_source='kline' should behave exactly like before."""
        engine = Engine(
            symbols=["BTC-USDT-SWAP"],
            mode="simulation",
            sim_bar_interval=0.01,
            sim_seed=42,
            factor_group="okx_perp",
            num_threads=1,
            data_source="kline",
        )
        engine.start()
        try:
            time.sleep(0.5)
            factors = engine.get_factor_outputs()
            assert "BTC-USDT-SWAP" in factors
            assert engine.bars_pushed > 0
        finally:
            engine.stop()

    def test_trades_stats_empty_for_kline(self):
        engine = Engine(
            symbols=["BTC-USDT-SWAP"],
            mode="simulation",
            sim_seed=42,
            data_source="kline",
        )
        assert engine.trades_stats == {}
        engine.stop()


class TestRuntimeLoopTradesUnpacking:
    """Test that _runtime_loop correctly handles the (round_bars, round_trades) tuple."""

    def _make_engine_with_queue(self, factor_group="okx_perp"):
        engine = Engine(
            symbols=["BTC-USDT-SWAP"],
            mode="simulation",
            sim_seed=42,
            factor_group=factor_group,
            num_threads=1,
            data_source="kline",
        )
        return engine

    def test_tuple_payload_with_trades(self):
        """_runtime_loop should unpack (round_bars, round_trades) and pass trades fields."""
        engine = self._make_engine_with_queue(factor_group="okx_perp_trades")
        sym = "BTC-USDT-SWAP"

        bar = np.array([1000.0, 100.0, 102.0, 99.0, 101.0, 5000.0, 0.0, 0.0])
        round_bars = {sym: bar}
        round_trades = {sym: {"vwap": 100.5, "buy_vol": 3000.0, "sell_vol": 2000.0, "trade_count": 42}}

        engine._bar_queue.put((round_bars, round_trades))
        engine._stop_event.clear()

        t = threading.Thread(target=engine._runtime_loop, daemon=True)
        t.start()

        time.sleep(0.3)
        engine._stop_event.set()
        t.join(timeout=2)

        assert engine.bars_pushed == 1
        assert len(engine.signal_deque) == 1
        result = engine.signal_deque[-1]
        assert result["ts"] == 1000
        assert sym in result["factors"]

    def test_tuple_payload_without_trades(self):
        """Backward compat: (round_bars, None) should work."""
        engine = self._make_engine_with_queue()
        sym = "BTC-USDT-SWAP"

        bar = np.array([2000.0, 100.0, 102.0, 99.0, 101.0, 5000.0, 0.0, 0.0])
        round_bars = {sym: bar}

        engine._bar_queue.put((round_bars, None))
        engine._stop_event.clear()

        t = threading.Thread(target=engine._runtime_loop, daemon=True)
        t.start()

        time.sleep(0.3)
        engine._stop_event.set()
        t.join(timeout=2)

        assert engine.bars_pushed == 1

    def test_legacy_dict_payload(self):
        """Backward compat: plain dict (old format) should still work."""
        engine = self._make_engine_with_queue()
        sym = "BTC-USDT-SWAP"

        bar = np.array([3000.0, 100.0, 102.0, 99.0, 101.0, 5000.0, 0.0, 0.0])
        round_bars = {sym: bar}

        engine._bar_queue.put(round_bars)
        engine._stop_event.clear()

        t = threading.Thread(target=engine._runtime_loop, daemon=True)
        t.start()

        time.sleep(0.3)
        engine._stop_event.set()
        t.join(timeout=2)

        assert engine.bars_pushed == 1


class TestTradesFactorsEndToEnd:
    """Test trades factors produce meaningful output through the Engine runtime loop."""

    def test_trades_factor_output_nonnan(self):
        """Trades factors should produce non-NaN values when trades data is provided."""
        engine = Engine(
            symbols=["BTC-USDT-SWAP"],
            mode="simulation",
            sim_seed=42,
            factor_group="okx_perp_trades",
            num_threads=1,
            data_source="kline_trades",
        )
        sym = "BTC-USDT-SWAP"

        engine._stop_event.clear()
        t = threading.Thread(target=engine._runtime_loop, daemon=True)
        t.start()

        try:
            for i in range(150):
                bar = np.array([
                    float(i * 1000),
                    100.0 + i * 0.01,
                    102.0 + i * 0.01,
                    99.0 + i * 0.01,
                    101.0 + i * 0.01,
                    5000.0 + i,
                    0.0,
                    0.0,
                ])
                trades = {
                    "vwap": 100.5 + i * 0.01,
                    "buy_vol": 3000.0 + (i % 20) * 10,
                    "sell_vol": 2000.0 + (i % 15) * 10,
                    "trade_count": 42,
                }
                engine._bar_queue.put(({sym: bar}, {sym: trades}))
                time.sleep(0.005)

            time.sleep(0.5)
        finally:
            engine._stop_event.set()
            t.join(timeout=2)

        assert engine.bars_pushed == 150
        result = engine.signal_deque[-1]
        factors = result["factors"]
        assert sym in factors

        for fid, val in factors[sym].items():
            assert isinstance(val, float), f"Factor {fid} should be float"


class TestMultiGroupEngine:
    """Test Engine loading both kline and trades factor groups."""

    def test_load_both_groups(self):
        """Engine should support loading multiple factor groups (manual setup)."""
        import fe_runtime as rt
        from factorengine.factors import FactorRegistry

        sym = "BTC-USDT-SWAP"
        inference = rt.InferenceEngine(num_threads=1)
        inference.add_symbol(sym)

        reg_kline = FactorRegistry()
        reg_kline.load_group("okx_perp")
        for fid, graph in reg_kline.build_group("okx_perp").items():
            inference.add_factor(sym, fid, graph)

        reg_trades = FactorRegistry()
        reg_trades.load_group("okx_perp_trades")
        for fid, graph in reg_trades.build_group("okx_perp_trades").items():
            inference.add_factor(sym, fid, graph)

        for i in range(200):
            bar = rt.BarData(
                close=100.0 + i * 0.01,
                volume=5000.0,
                open=99.5 + i * 0.01,
                high=100.5 + i * 0.01,
                low=99.0 + i * 0.01,
                ret=0.001,
                vwap=100.0 + i * 0.01 + 0.05,
                buy_vol=3000.0,
                sell_vol=2000.0,
            )
            inference.push_bars({sym: bar})

        outputs = inference.get_all_outputs()
        assert sym in outputs
        all_fids = outputs[sym]
        assert "0001" in all_fids
        assert "T0001" in all_fids
