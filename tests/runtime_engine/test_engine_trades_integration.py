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

    def test_engine_multi_group_param(self):
        """Engine(factor_group=[...]) should load both groups."""
        engine = Engine(
            symbols=["BTC-USDT-SWAP"],
            mode="simulation",
            sim_seed=42,
            sim_bar_interval=0.01,
            factor_group=["okx_perp", "okx_perp_trades"],
            num_threads=1,
            data_source="kline",
        )
        # Should have factors from both groups.
        assert any(fid.startswith("0") for fid in engine.factor_ids)
        assert any(fid.startswith("T") for fid in engine.factor_ids)
        engine.stop()


class TestTradesWorkerFlush:
    """Test TradesWorker.flush() builds correct payloads."""

    def test_flush_builds_bar_and_trades(self):
        from dataflow.okx.trades.worker import TradesWorker

        q = queue.Queue(maxsize=4)
        worker = TradesWorker(symbols=["BTC", "ETH"], bar_queue=q)
        worker.set_freq(5.0)

        worker._accumulators["BTC"].update(100.0, 2.0, "buy")
        worker._accumulators["BTC"].update(105.0, 1.0, "sell")
        worker._accumulators["ETH"].update(3000.0, 5.0, "buy")

        worker.flush()

        payload = q.get_nowait()
        assert isinstance(payload, tuple)
        round_bars, round_trades = payload

        assert "BTC" in round_bars
        assert "ETH" in round_bars
        assert "BTC" in round_trades
        assert "ETH" in round_trades

        btc_bar = round_bars["BTC"]
        assert btc_bar.shape == (8,)
        assert btc_bar[1] == pytest.approx(100.0)   # open
        assert btc_bar[2] == pytest.approx(105.0)    # high
        assert btc_bar[3] == pytest.approx(100.0)    # low
        assert btc_bar[4] == pytest.approx(105.0)    # close (last trade)
        assert btc_bar[5] == pytest.approx(3.0)      # volume

        btc_trades = round_trades["BTC"]
        assert btc_trades["buy_vol"] == pytest.approx(2.0)
        assert btc_trades["sell_vol"] == pytest.approx(1.0)
        assert btc_trades["trade_count"] == 2

    def test_flush_resets_accumulators(self):
        from dataflow.okx.trades.worker import TradesWorker

        q = queue.Queue(maxsize=4)
        worker = TradesWorker(symbols=["BTC"], bar_queue=q)

        worker._accumulators["BTC"].update(100.0, 1.0, "buy")
        worker.flush()
        q.get_nowait()

        worker.flush()
        _, round_trades = q.get_nowait()
        assert round_trades["BTC"]["trade_count"] == 0
        assert math.isnan(round_trades["BTC"]["vwap"])

    def test_flush_without_bar_queue(self):
        from dataflow.okx.trades.worker import TradesWorker

        worker = TradesWorker(symbols=["BTC"], bar_queue=None)
        worker._accumulators["BTC"].update(100.0, 1.0, "buy")
        worker.flush()
        assert worker.flush_count == 1

    def test_flush_count_increments(self):
        from dataflow.okx.trades.worker import TradesWorker

        q = queue.Queue(maxsize=4)
        worker = TradesWorker(symbols=["BTC"], bar_queue=q)
        assert worker.flush_count == 0
        worker.flush()
        worker.flush()
        assert worker.flush_count == 2


class TestEngineTradesOnlyMode:
    """Test Engine with data_source='trades' — ClockDriver driven."""

    def test_trades_only_runtime_loop(self):
        """Simulate trades-only: push trades-derived bars via flush and
        verify _runtime_loop processes them correctly."""
        engine = Engine(
            symbols=["BTC-USDT-SWAP"],
            mode="simulation",
            sim_seed=42,
            factor_group="okx_perp_trades",
            num_threads=1,
            data_source="kline",
        )

        sym = "BTC-USDT-SWAP"
        engine._stop_event.clear()
        t = threading.Thread(target=engine._runtime_loop, daemon=True)
        t.start()

        try:
            for i in range(150):
                bar = np.array([
                    float(i * 5000),
                    100.0 + i * 0.1,    # open
                    102.0 + i * 0.1,    # high
                    99.0 + i * 0.1,     # low
                    101.0 + i * 0.1,    # close
                    1000.0 + i,         # volume
                    0.0, 0.0,
                ])
                trades = {
                    "vwap": 100.5 + i * 0.1,
                    "buy_vol": 600.0 + i,
                    "sell_vol": 400.0 + i,
                    "trade_count": 50,
                    "open": 100.0 + i * 0.1,
                    "high": 102.0 + i * 0.1,
                    "low": 99.0 + i * 0.1,
                    "close": 101.0 + i * 0.1,
                    "volume": 1000.0 + i,
                }
                engine._bar_queue.put(({sym: bar}, {sym: trades}))
                time.sleep(0.005)

            time.sleep(0.5)
        finally:
            engine._stop_event.set()
            t.join(timeout=2)

        assert engine.bars_pushed == 150
        factors = engine.get_factor_outputs()
        assert sym in factors
        fvals = factors[sym]
        assert len(fvals) > 0
