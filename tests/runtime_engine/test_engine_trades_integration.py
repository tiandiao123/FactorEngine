"""Tests for Engine trades integration (v4 ClockDriver architecture).

Covers:
  - data_source parameter validation
  - data_source="kline" backward compatibility
  - _runtime_loop correctly unpacks (round_bars, round_trades) tuples
  - _runtime_loop builds BarData with trades fields (vwap, buy_vol, sell_vol)
  - TradesWorker.flush() returns correct payloads (no bar_queue)
  - ClockDriver aggregation logic
  - Engine wiring: ClockDriver registered for all live modes
  - Multi-group factor loading

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

    def test_invalid_trade_time_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid trade_time_mode"):
            Engine(
                symbols=["BTC-USDT-SWAP"],
                mode="simulation",
                sim_seed=42,
                trade_time_mode="event_time",
            )

    def test_negative_allowed_lateness_raises(self):
        with pytest.raises(ValueError, match="allowed_lateness_ms"):
            Engine(
                symbols=["BTC-USDT-SWAP"],
                mode="simulation",
                sim_seed=42,
                allowed_lateness_ms=-1,
            )

    def test_kline_trades_exchange_time_rejected(self):
        with pytest.raises(ValueError, match="kline_trades"):
            Engine(
                symbols=["BTC-USDT-SWAP"],
                mode="live",
                data_source="kline_trades",
                trade_time_mode="exchange_time",
                factor_group=["okx_perp", "okx_perp_trades"],
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

    def test_okx_trades_exchange_time_wires_worker_config(self):
        engine = Engine(
            symbols=["BTC-USDT-SWAP"],
            mode="live",
            data_source="trades",
            data_freq="1s",
            factor_group="okx_perp_trades",
            num_threads=1,
            trade_time_mode="exchange_time",
            allowed_lateness_ms=500,
        )

        try:
            assert engine._trade_time_mode == "exchange_time"
            assert engine._allowed_lateness_ms == 500
            assert engine._trades_worker._trade_time_mode == "exchange_time"
            assert engine._trades_worker._allowed_lateness_ms == 500
            assert engine._trades_worker._freq_ms == pytest.approx(1000.0)
        finally:
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
        assert any(fid.startswith("0") for fid in engine.factor_ids)
        assert any(fid.startswith("T") for fid in engine.factor_ids)
        engine.stop()


class TestTradesWorkerFlush:
    """Test TradesWorker.flush() returns correct payloads (no bar_queue)."""

    def test_flush_returns_bar_and_trades(self):
        from dataflow.okx.trades.worker import TradesWorker

        worker = TradesWorker(symbols=["BTC", "ETH"])
        worker.set_freq(5.0)

        worker._accumulators["BTC"].update(100.0, 2.0, "buy")
        worker._accumulators["BTC"].update(105.0, 1.0, "sell")
        worker._accumulators["ETH"].update(3000.0, 5.0, "buy")

        round_bars, round_trades = worker.flush()

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

        worker = TradesWorker(symbols=["BTC"])

        worker._accumulators["BTC"].update(100.0, 1.0, "buy")
        round_bars, round_trades = worker.flush()
        assert round_trades["BTC"]["trade_count"] == 1

        _, round_trades2 = worker.flush()
        assert round_trades2["BTC"]["trade_count"] == 0
        # vwap forward-filled from last known close (100.0)
        assert round_trades2["BTC"]["vwap"] == 100.0

    def test_flush_no_prior_close_returns_nan(self):
        """First flush with no trades ever → vwap and OHLC should be NaN."""
        from dataflow.okx.trades.worker import TradesWorker

        worker = TradesWorker(symbols=["BTC", "ETH"])
        round_bars, round_trades = worker.flush()
        for sym in ("BTC", "ETH"):
            assert round_trades[sym]["trade_count"] == 0
            assert math.isnan(round_trades[sym]["vwap"])
            assert math.isnan(round_trades[sym]["open"])
            assert math.isnan(round_trades[sym]["close"])
            bar = round_bars[sym]
            assert math.isnan(float(bar[1]))  # open
            assert math.isnan(float(bar[4]))  # close

    def test_flush_count_increments(self):
        from dataflow.okx.trades.worker import TradesWorker

        worker = TradesWorker(symbols=["BTC"])
        assert worker.flush_count == 0
        worker.flush()
        worker.flush()
        assert worker.flush_count == 2


class TestClockDriverAggregation:
    """Test ClockDriver._aggregate() merges worker outputs correctly."""

    def test_kline_only(self):
        from dataflow.clock_driver import ClockDriver

        q = queue.Queue(maxsize=4)
        cd = ClockDriver(freq_seconds=5.0, bar_queue=q)

        kline_data = {
            "BTC": np.array([5000, 100, 105, 99, 101, 50, 0, 0], dtype=np.float64),
        }
        merged_bars, merged_trades = cd._aggregate({"kline": kline_data})

        assert "BTC" in merged_bars
        assert merged_bars["BTC"][4] == 101.0
        assert merged_trades == {}

    def test_trades_only(self):
        from dataflow.clock_driver import ClockDriver

        q = queue.Queue(maxsize=4)
        cd = ClockDriver(freq_seconds=5.0, bar_queue=q)

        trades_bars = {
            "BTC": np.array([5000, 100, 105, 99, 101, 50, 0, 0], dtype=np.float64),
        }
        trades_snaps = {"BTC": {"vwap": 100.5, "buy_vol": 30, "sell_vol": 20}}
        trades_data = (trades_bars, trades_snaps)

        merged_bars, merged_trades = cd._aggregate({"trades": trades_data})

        assert "BTC" in merged_bars
        assert "BTC" in merged_trades
        assert merged_trades["BTC"]["vwap"] == 100.5

    def test_kline_overrides_trades_bar(self):
        """When both kline and trades provide bars, kline takes priority."""
        from dataflow.clock_driver import ClockDriver

        q = queue.Queue(maxsize=4)
        cd = ClockDriver(freq_seconds=5.0, bar_queue=q)

        kline_bar = np.array([5000, 100, 106, 98, 103, 80, 0, 0], dtype=np.float64)
        trades_bar = np.array([5000, 100, 105, 99, 101, 50, 0, 0], dtype=np.float64)
        trades_snaps = {"BTC": {"vwap": 100.5, "buy_vol": 30, "sell_vol": 20}}

        raw = {
            "kline": {"BTC": kline_bar},
            "trades": ({"BTC": trades_bar}, trades_snaps),
        }
        merged_bars, merged_trades = cd._aggregate(raw)

        assert merged_bars["BTC"][4] == 103.0  # kline close, not trades close
        assert merged_trades["BTC"]["vwap"] == 100.5

    def test_multi_symbol_merge(self):
        """Kline has BTC+ETH, trades has BTC+SOL → merged has all three."""
        from dataflow.clock_driver import ClockDriver

        q = queue.Queue(maxsize=4)
        cd = ClockDriver(freq_seconds=5.0, bar_queue=q)

        kline_data = {
            "BTC": np.array([5000, 100, 106, 98, 103, 80, 0, 0], dtype=np.float64),
            "ETH": np.array([5000, 3000, 3100, 2900, 3050, 400, 0, 0], dtype=np.float64),
        }
        trades_bars = {
            "BTC": np.array([5000, 100, 105, 99, 101, 50, 0, 0], dtype=np.float64),
            "SOL": np.array([5000, 150, 155, 145, 152, 200, 0, 0], dtype=np.float64),
        }
        trades_snaps = {
            "BTC": {"vwap": 100.5},
            "SOL": {"vwap": 152.3},
        }

        raw = {
            "kline": kline_data,
            "trades": (trades_bars, trades_snaps),
        }
        merged_bars, merged_trades = cd._aggregate(raw)

        assert set(merged_bars.keys()) == {"BTC", "ETH", "SOL"}
        assert merged_bars["BTC"][4] == 103.0   # kline priority
        assert merged_bars["SOL"][4] == 152.0   # from trades
        assert merged_bars["ETH"][4] == 3050.0  # kline only

    def test_flush_and_aggregate_pushes_to_queue(self):
        """End-to-end: ClockDriver._flush_and_aggregate pushes to bar_queue."""
        from dataflow.clock_driver import ClockDriver

        q = queue.Queue(maxsize=4)
        cd = ClockDriver(freq_seconds=5.0, bar_queue=q)

        class FakeKlineWorker:
            def flush(self):
                return {
                    "BTC": np.array([5000, 100, 106, 98, 103, 80, 0, 0], dtype=np.float64),
                }

        class FakeTradesWorker:
            def flush(self):
                bars = {
                    "BTC": np.array([5000, 100, 105, 99, 101, 50, 0, 0], dtype=np.float64),
                }
                snaps = {"BTC": {"vwap": 100.5, "buy_vol": 30, "sell_vol": 20}}
                return (bars, snaps)

        cd.register("kline", FakeKlineWorker())
        cd.register("trades", FakeTradesWorker())

        cd._flush_and_aggregate()

        assert q.qsize() == 1
        round_bars, round_trades = q.get_nowait()
        assert "BTC" in round_bars
        assert round_bars["BTC"][4] == 103.0
        assert round_trades["BTC"]["vwap"] == 100.5

    def test_empty_flush_no_push(self):
        """If all workers return empty, nothing is pushed."""
        from dataflow.clock_driver import ClockDriver

        q = queue.Queue(maxsize=4)
        cd = ClockDriver(freq_seconds=5.0, bar_queue=q)

        class EmptyWorker:
            def flush(self):
                return None

        cd.register("kline", EmptyWorker())
        cd._flush_and_aggregate()

        assert q.qsize() == 0


class TestEngineTradesOnlyMode:
    """Test Engine with data_source='trades' — ClockDriver driven."""

    def test_trades_only_runtime_loop(self):
        """Simulate trades-only: push trades-derived bars via queue and
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
                    100.0 + i * 0.1,
                    102.0 + i * 0.1,
                    99.0 + i * 0.1,
                    101.0 + i * 0.1,
                    1000.0 + i,
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


class TestClockStats:
    """Test Engine.clock_stats property."""

    def test_clock_stats_empty_simulation(self):
        engine = Engine(
            symbols=["BTC-USDT-SWAP"],
            mode="simulation",
            sim_seed=42,
        )
        assert engine.clock_stats == {}
        engine.stop()
