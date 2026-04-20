"""End-to-end test: Engine (simulation mode) + C++ InferenceEngine.

Verifies the v2 signal-decoupled pipeline:
  1. Engine starts simulation dataflow (SimBarWorker generates bars → bar_queue)
  2. Engine starts runtime thread (bar_queue → InferenceEngine → signal_deque)
  3. Engine.get_factor_outputs() reads from signal_deque (O(1))
  4. Factor outputs match standalone FactorGraph pushed with the same data

Usage:
    pytest tests/runtime_engine/test_engine_integration.py -v
"""
import time

import numpy as np
import pytest

import fe_runtime as rt

import sys
sys.path.insert(0, ".")

from factorengine.engine import Engine
from factorengine.factors import FactorRegistry
from dataflow.simulation.symbols import DEFAULT_SYMBOLS

NUM_FACTORS = 60
SYMBOLS = DEFAULT_SYMBOLS[:3]  # BTC, ETH, SOL


class TestEngineWithFactors:
    """Integration tests for Engine + InferenceEngine (v2 signal-decoupled)."""

    def test_basic_flow(self):
        """Engine with factor_group produces factor outputs after collecting bars."""
        engine = Engine(
            symbols=SYMBOLS,
            mode="simulation",
            sim_bar_interval=0.01,
            sim_seed=42,
            bar_window_length=200,
            factor_group="okx_perp",
            num_threads=2,
        )
        engine.start()
        try:
            time.sleep(1.0)
            factors = engine.get_factor_outputs()
            assert set(factors.keys()) == set(SYMBOLS)
            for sym in SYMBOLS:
                assert len(factors[sym]) == NUM_FACTORS
                assert all(isinstance(v, float) for v in factors[sym].values())
        finally:
            engine.stop()

    def test_signal_deque_populated(self):
        """Signal deque is populated by the runtime thread."""
        engine = Engine(
            symbols=SYMBOLS[:1],
            mode="simulation",
            sim_bar_interval=0.01,
            sim_seed=42,
            bar_window_length=200,
            factor_group="okx_perp",
            num_threads=1,
            signal_buffer_size=3,
        )
        engine.start()
        try:
            time.sleep(0.5)
            assert len(engine.signal_deque) > 0
            assert len(engine.signal_deque) <= 3

            latest = engine.signal_deque[-1]
            assert "ts" in latest
            assert "bar_index" in latest
            assert "factors" in latest
            assert engine.bars_pushed > 0
        finally:
            engine.stop()

    def test_bars_pushed_increments(self):
        """bars_pushed counter increments as runtime thread processes bars."""
        engine = Engine(
            symbols=SYMBOLS[:1],
            mode="simulation",
            sim_bar_interval=0.01,
            sim_seed=99,
            bar_window_length=500,
            factor_group="okx_perp",
            num_threads=1,
        )
        engine.start()
        try:
            time.sleep(0.3)
            n1 = engine.bars_pushed
            time.sleep(0.5)
            n2 = engine.bars_pushed
            assert n2 > n1, "More bars should have been pushed"
        finally:
            engine.stop()

    def test_factor_ids(self):
        """Engine.factor_ids returns the registered factor IDs."""
        engine = Engine(
            symbols=SYMBOLS[:1],
            mode="simulation",
            sim_seed=1,
            factor_group="okx_perp",
            num_threads=1,
        )
        ids = engine.factor_ids
        assert len(ids) == NUM_FACTORS
        assert "0001" in ids
        assert "0100" in ids
        engine.stop()

    def test_no_factors(self):
        """Engine without factor_group works as before (no runtime thread)."""
        engine = Engine(
            symbols=SYMBOLS[:1],
            mode="simulation",
            sim_bar_interval=0.01,
            sim_seed=42,
        )
        engine.start()
        try:
            time.sleep(0.1)
            snapshot = engine.get_data()
            assert len(snapshot) > 0

            factors = engine.get_factor_outputs()
            assert factors == {}
            assert engine.factor_ids == []
        finally:
            engine.stop()

    def test_get_data_independent_of_factors(self):
        """get_data() and get_factor_outputs() are independent reads."""
        engine = Engine(
            symbols=SYMBOLS[:1],
            mode="simulation",
            sim_bar_interval=0.01,
            sim_seed=42,
            bar_window_length=200,
            factor_group="okx_perp",
            num_threads=1,
        )
        engine.start()
        try:
            time.sleep(0.5)
            snapshot = engine.get_data()
            sym = SYMBOLS[0]
            assert sym in snapshot
            assert len(snapshot[sym]) > 0

            factors = engine.get_factor_outputs()
            assert sym in factors
        finally:
            engine.stop()

    def test_outputs_match_standalone(self):
        """Engine factor outputs must match standalone FactorGraph results."""
        sym = SYMBOLS[0]
        engine = Engine(
            symbols=[sym],
            mode="simulation",
            sim_bar_interval=0.005,
            sim_seed=42,
            bar_window_length=600,
            factor_group="okx_perp",
            num_threads=1,
        )
        engine.start()
        try:
            time.sleep(3.0)
            snapshot = engine.get_data()
            engine_factors = engine.get_factor_outputs()
        finally:
            engine.stop()

        arr = snapshot[sym]
        n = len(arr)
        assert n > 100, f"Need enough bars for meaningful comparison, got {n}"

        reg = FactorRegistry()
        reg.load_group("okx_perp")
        graphs = reg.build_group("okx_perp")

        for i in range(n):
            row = arr[i]
            close, vol = float(row[4]), float(row[5])
            open_, high, low = float(row[1]), float(row[2]), float(row[3])
            prev_close = float(arr[i - 1, 4]) if i > 0 else close
            ret = (close / prev_close - 1.0) if prev_close != 0 else 0.0
            for g in graphs.values():
                g.push_bar(close, vol, open_, high, low, ret)

        for fid, g in graphs.items():
            standalone_val = g.output()
            engine_val = engine_factors[sym][fid]
            assert engine_val == pytest.approx(standalone_val, nan_ok=True, abs=1e-5), \
                f"Factor {fid}: engine={engine_val}, standalone={standalone_val}"

    def test_filtered_symbols(self):
        """get_factor_outputs() respects symbol filter."""
        engine = Engine(
            symbols=SYMBOLS,
            mode="simulation",
            sim_bar_interval=0.01,
            sim_seed=42,
            factor_group="okx_perp",
            num_threads=2,
        )
        engine.start()
        try:
            time.sleep(0.5)
            factors = engine.get_factor_outputs([SYMBOLS[0]])
            assert list(factors.keys()) == [SYMBOLS[0]]
            assert len(factors[SYMBOLS[0]]) == NUM_FACTORS
        finally:
            engine.stop()

    def test_three_thread_architecture(self):
        """Verify that dataflow and inference run on separate threads."""
        engine = Engine(
            symbols=SYMBOLS[:1],
            mode="simulation",
            sim_bar_interval=0.01,
            sim_seed=42,
            bar_window_length=200,
            factor_group="okx_perp",
            num_threads=1,
        )
        assert engine._bar_queue is not None, "bar_queue should be created"
        assert engine._runtime_thread is None, "runtime thread not started yet"

        engine.start()
        try:
            assert engine._runtime_thread is not None, "runtime thread should be running"
            assert engine._runtime_thread.is_alive()

            time.sleep(0.5)
            assert engine.bars_pushed > 0, "runtime thread should have processed bars"
            assert engine.bar_count > 0, "dataflow should have generated bars"
        finally:
            engine.stop()
            assert not engine._runtime_thread.is_alive() if engine._runtime_thread else True

    def test_no_bar_queue_without_factors(self):
        """Without factor_group, no bar_queue or runtime thread is created."""
        engine = Engine(
            symbols=SYMBOLS[:1],
            mode="simulation",
            sim_seed=42,
        )
        assert engine._bar_queue is None
        assert engine._runtime_thread is None
        engine.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
