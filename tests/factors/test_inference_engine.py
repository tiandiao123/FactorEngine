"""
End-to-end integration test: FactorRegistry → InferenceEngine → push → collect.

Tests the full pipeline:
  1. FactorRegistry auto-discovers factor builders
  2. InferenceEngine registers symbols and factors
  3. Push OHLCV bars through the engine
  4. Collect outputs and compare with individual FactorGraph results
"""
import numpy as np
import pytest

import fe_runtime as rt
from factorengine.factors import FactorRegistry

N = 500
SYMBOLS = ["BTC-USDT", "ETH-USDT"]
NUM_FACTORS = 60


def make_ohlcv(seed: int, n: int = N):
    rng = np.random.RandomState(seed)
    close = 100.0 + np.cumsum(rng.randn(n) * 0.5).astype(np.float32)
    volume = (1000 + rng.rand(n) * 5000).astype(np.float32)
    noise = rng.rand(n).astype(np.float32)
    high = close + np.abs(rng.randn(n) * 0.3).astype(np.float32)
    low = close - np.abs(rng.randn(n) * 0.3).astype(np.float32)
    open_ = low + noise * (high - low)
    ret = np.zeros(n, dtype=np.float32)
    ret[1:] = close[1:] / close[:-1] - 1.0
    return close, volume, open_, high, low, ret


class TestFactorRegistry:
    def test_load_all(self):
        reg = FactorRegistry()
        reg.load_all()
        assert len(reg) == NUM_FACTORS

    def test_load_group(self):
        reg = FactorRegistry()
        reg.load_group("okx_perp")
        assert len(reg) == NUM_FACTORS
        assert reg.groups == ["okx_perp"]

    def test_build_all(self):
        reg = FactorRegistry()
        reg.load_all()
        graphs = reg.build_all()
        assert len(graphs) == NUM_FACTORS
        for fid, g in graphs.items():
            assert g.num_nodes() > 0
            assert g.warmup_bars() > 0

    def test_build_group(self):
        reg = FactorRegistry()
        reg.load_group("okx_perp")
        graphs = reg.build_group("okx_perp")
        assert len(graphs) == NUM_FACTORS

    def test_build_single(self):
        reg = FactorRegistry()
        reg.load_all()
        g = reg.build("0001")
        assert g.num_nodes() == 5
        assert g.warmup_bars() == 120

    def test_build_with_group(self):
        reg = FactorRegistry()
        reg.load_all()
        g = reg.build("0001", group="okx_perp")
        assert g.num_nodes() == 5


class TestSymbolRunner:
    def test_basic_push(self):
        reg = FactorRegistry()
        reg.load_all()

        runner = rt.SymbolRunner("BTC-USDT")
        graphs = reg.build_all()
        for fid in sorted(graphs):
            runner.add_factor(fid, graphs[fid])

        assert runner.num_factors() == NUM_FACTORS
        assert runner.symbol() == "BTC-USDT"

        close, volume, open_, high, low, ret = make_ohlcv(42)
        for i in range(N):
            runner.push_bar(close[i], volume[i], open_[i], high[i], low[i], ret[i])

        assert runner.bars_pushed() == N
        outputs = runner.outputs()
        assert len(outputs) == NUM_FACTORS
        fids = runner.factor_ids()
        assert fids == sorted(graphs.keys())

    def test_outputs_match_individual_graphs(self):
        """SymbolRunner outputs must match individually-pushed FactorGraphs."""
        reg = FactorRegistry()
        reg.load_all()

        close, volume, open_, high, low, ret = make_ohlcv(99)

        runner = rt.SymbolRunner("TEST")
        individual = {}
        for fid in reg.factor_ids:
            g_runner = reg.build(fid)
            runner.add_factor(fid, g_runner)
            individual[fid] = reg.build(fid)

        for i in range(N):
            runner.push_bar(close[i], volume[i], open_[i], high[i], low[i], ret[i])
            for fid, g in individual.items():
                g.push_bar(close[i], volume[i], open_[i], high[i], low[i], ret[i])

        for idx, fid in enumerate(runner.factor_ids()):
            runner_val = runner.output(idx)
            indiv_val = individual[fid].output()
            assert runner_val == pytest.approx(indiv_val, nan_ok=True, abs=1e-6), \
                f"Factor {fid}: runner={runner_val}, individual={indiv_val}"


class TestInferenceEngine:
    def test_multi_symbol(self):
        reg = FactorRegistry()
        reg.load_all()

        engine = rt.InferenceEngine()
        for sym in SYMBOLS:
            engine.add_symbol(sym)
            graphs = reg.build_all()
            for fid in sorted(graphs):
                engine.add_factor(sym, fid, graphs[fid])

        assert engine.num_symbols() == 2
        assert set(engine.symbols()) == set(SYMBOLS)

        data = {}
        for i, sym in enumerate(SYMBOLS):
            data[sym] = make_ohlcv(seed=42 + i)

        for bar_idx in range(N):
            for sym in SYMBOLS:
                c, v, o, h, lo, r = data[sym]
                engine.push_bar(sym, c[bar_idx], v[bar_idx],
                                o[bar_idx], h[bar_idx], lo[bar_idx], r[bar_idx])

        for sym in SYMBOLS:
            outputs = engine.get_outputs(sym)
            assert len(outputs) == NUM_FACTORS
            fids = engine.get_factor_ids(sym)
            assert len(fids) == NUM_FACTORS

    def test_engine_vs_standalone_runner(self):
        """InferenceEngine outputs must match standalone SymbolRunner."""
        reg = FactorRegistry()
        reg.load_all()

        engine = rt.InferenceEngine()
        engine.add_symbol("BTC")
        runner = rt.SymbolRunner("BTC")

        for fid in reg.factor_ids:
            engine.add_factor("BTC", fid, reg.build(fid))
            runner.add_factor(fid, reg.build(fid))

        close, volume, open_, high, low, ret = make_ohlcv(77)
        for i in range(N):
            engine.push_bar("BTC", close[i], volume[i], open_[i], high[i], low[i], ret[i])
            runner.push_bar(close[i], volume[i], open_[i], high[i], low[i], ret[i])

        engine_out = engine.get_outputs("BTC")
        runner_out = runner.outputs()
        for j in range(NUM_FACTORS):
            assert engine_out[j] == pytest.approx(runner_out[j], nan_ok=True, abs=1e-6)

    def test_different_symbols_independent(self):
        """Different symbols must have independent states."""
        reg = FactorRegistry()
        reg.load_all()

        engine = rt.InferenceEngine()
        engine.add_symbol("A")
        engine.add_symbol("B")
        for sym in ["A", "B"]:
            for fid in reg.factor_ids:
                engine.add_factor(sym, fid, reg.build(fid))

        close_a, vol_a, open_a, high_a, low_a, ret_a = make_ohlcv(1)
        close_b, vol_b, open_b, high_b, low_b, ret_b = make_ohlcv(2)

        for i in range(N):
            engine.push_bar("A", close_a[i], vol_a[i], open_a[i], high_a[i], low_a[i], ret_a[i])
            engine.push_bar("B", close_b[i], vol_b[i], open_b[i], high_b[i], low_b[i], ret_b[i])

        out_a = engine.get_outputs("A")
        out_b = engine.get_outputs("B")
        assert out_a != out_b, "Different data should produce different outputs"

    def test_reset(self):
        reg = FactorRegistry()
        reg.load_all()

        engine = rt.InferenceEngine()
        engine.add_symbol("X")
        for fid in reg.factor_ids:
            engine.add_factor("X", fid, reg.build(fid))

        close, volume, open_, high, low, ret = make_ohlcv(42)
        for i in range(N):
            engine.push_bar("X", close[i], volume[i], open_[i], high[i], low[i], ret[i])

        out1 = list(engine.get_outputs("X"))
        engine.reset()

        for i in range(N):
            engine.push_bar("X", close[i], volume[i], open_[i], high[i], low[i], ret[i])
        out2 = list(engine.get_outputs("X"))

        for j in range(NUM_FACTORS):
            assert out1[j] == pytest.approx(out2[j], nan_ok=True, abs=1e-6)


class TestMultiThreaded:
    """Tests for push_bars() parallel batch push via thread pool."""

    NUM_SYMBOLS = 20
    N_BARS = 500

    def _make_engine_and_data(self, num_threads=4):
        reg = FactorRegistry()
        reg.load_all()

        engine = rt.InferenceEngine(num_threads=num_threads)
        symbols = [f"SYM-{i:03d}" for i in range(self.NUM_SYMBOLS)]
        data = {}

        for i, sym in enumerate(symbols):
            engine.add_symbol(sym)
            for fid, graph in reg.build_all().items():
                engine.add_factor(sym, fid, graph)
            data[sym] = make_ohlcv(seed=1000 + i, n=self.N_BARS)

        return engine, symbols, data, reg

    def test_num_threads(self):
        engine = rt.InferenceEngine(num_threads=4)
        assert engine.num_threads() == 4

    def test_default_threads(self):
        engine = rt.InferenceEngine()
        assert engine.num_threads() >= 1

    def test_push_bars_matches_push_bar(self):
        """push_bars() parallel results must exactly match sequential push_bar()."""
        reg = FactorRegistry()
        reg.load_all()

        symbols = [f"SYM-{i:03d}" for i in range(10)]
        data = {}
        for i, sym in enumerate(symbols):
            data[sym] = make_ohlcv(seed=2000 + i, n=N)

        engine_seq = rt.InferenceEngine(num_threads=1)
        engine_par = rt.InferenceEngine(num_threads=4)

        for sym in symbols:
            engine_seq.add_symbol(sym)
            engine_par.add_symbol(sym)
            for fid, graph in reg.build_all().items():
                engine_seq.add_factor(sym, fid, graph)
            for fid, graph in reg.build_all().items():
                engine_par.add_factor(sym, fid, graph)

        for bar_idx in range(N):
            bars = {}
            for sym in symbols:
                c, v, o, h, lo, r = data[sym]
                engine_seq.push_bar(sym, c[bar_idx], v[bar_idx],
                                    o[bar_idx], h[bar_idx], lo[bar_idx], r[bar_idx])
                bars[sym] = rt.BarData(c[bar_idx], v[bar_idx],
                                       o[bar_idx], h[bar_idx], lo[bar_idx], r[bar_idx])
            engine_par.push_bars(bars)

        for sym in symbols:
            out_seq = engine_seq.get_outputs(sym)
            out_par = engine_par.get_outputs(sym)
            for j in range(len(out_seq)):
                assert out_seq[j] == pytest.approx(out_par[j], nan_ok=True, abs=1e-6), \
                    f"{sym} factor {j}: seq={out_seq[j]}, par={out_par[j]}"

    def test_push_bars_many_symbols(self):
        """Stress test: 20 symbols × factors × 500 bars via push_bars()."""
        engine, symbols, data, reg = self._make_engine_and_data(num_threads=4)

        for bar_idx in range(self.N_BARS):
            bars = {}
            for sym in symbols:
                c, v, o, h, lo, r = data[sym]
                bars[sym] = rt.BarData(c[bar_idx], v[bar_idx],
                                       o[bar_idx], h[bar_idx], lo[bar_idx], r[bar_idx])
            engine.push_bars(bars)

        for sym in symbols:
            outputs = engine.get_outputs(sym)
            assert len(outputs) == NUM_FACTORS
            assert all(np.isfinite(v) or v == 0.0 for v in outputs)

    def test_push_bars_deterministic(self):
        """Running push_bars() twice with same data produces identical results."""
        reg = FactorRegistry()
        reg.load_all()
        symbols = [f"T-{i}" for i in range(8)]

        results = []
        for run in range(2):
            engine = rt.InferenceEngine(num_threads=4)
            for sym in symbols:
                engine.add_symbol(sym)
                for fid, graph in reg.build_all().items():
                    engine.add_factor(sym, fid, graph)

            data = {sym: make_ohlcv(seed=3000 + i, n=200) for i, sym in enumerate(symbols)}
            for bar_idx in range(200):
                bars = {}
                for sym in symbols:
                    c, v, o, h, lo, r = data[sym]
                    bars[sym] = rt.BarData(c[bar_idx], v[bar_idx],
                                           o[bar_idx], h[bar_idx], lo[bar_idx], r[bar_idx])
                engine.push_bars(bars)

            run_outputs = {sym: list(engine.get_outputs(sym)) for sym in symbols}
            results.append(run_outputs)

        for sym in symbols:
            for j in range(NUM_FACTORS):
                assert results[0][sym][j] == pytest.approx(
                    results[1][sym][j], nan_ok=True, abs=1e-6
                ), f"Run mismatch: {sym} factor {j}"

    def test_push_bars_perf(self):
        """Benchmark: push_bars should not be dramatically slower than push_bar."""
        import time

        engine, symbols, data, reg = self._make_engine_and_data(num_threads=4)

        start = time.perf_counter()
        for bar_idx in range(self.N_BARS):
            bars = {}
            for sym in symbols:
                c, v, o, h, lo, r = data[sym]
                bars[sym] = rt.BarData(c[bar_idx], v[bar_idx],
                                       o[bar_idx], h[bar_idx], lo[bar_idx], r[bar_idx])
            engine.push_bars(bars)
        elapsed_par = time.perf_counter() - start

        engine.reset()
        start = time.perf_counter()
        for bar_idx in range(self.N_BARS):
            for sym in symbols:
                c, v, o, h, lo, r = data[sym]
                engine.push_bar(sym, c[bar_idx], v[bar_idx],
                                o[bar_idx], h[bar_idx], lo[bar_idx], r[bar_idx])
        elapsed_seq = time.perf_counter() - start

        print(f"\n  push_bars (parallel): {elapsed_par:.3f}s")
        print(f"  push_bar  (sequential): {elapsed_seq:.3f}s")
        print(f"  ratio: {elapsed_seq / elapsed_par:.2f}x")

        assert elapsed_par < elapsed_seq * 5, \
            f"Parallel should not be >5x slower than sequential"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
