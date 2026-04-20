"""Alignment + benchmark: TreapTsRank vs BruteForceTsRank vs Python TsRank.

Tests:
  1. Treap matches brute-force exactly (both via FactorGraph push)
  2. Treap matches Python pandas reference
  3. Benchmark: per-push latency across window sizes

Usage:
    pytest tests/kernel/test_treap_rank.py -v -s
"""
import time

import numpy as np
import pandas as pd
import pytest

import fe_runtime as rt

# Python reference
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "reference"))
from ts_ops import TsRank


WINDOWS = [10, 30, 60, 120, 240, 480, 1000, 4320]
N = 5000


def make_data(seed: int, n: int = N):
    rng = np.random.RandomState(seed)
    return (100.0 + np.cumsum(rng.randn(n) * 0.5)).astype(np.float32)


def build_rank_graph(window: int, use_treap: bool) -> rt.FactorGraph:
    g = rt.FactorGraph()
    inp = g.add_input("close")
    op = rt.Op.TREAP_TS_RANK if use_treap else rt.Op.TS_RANK
    g.add_rolling(op, inp, window)
    g.compile()
    return g


class TestTreapAlignment:
    """Treap TsRank must match brute-force TsRank exactly."""

    @pytest.mark.parametrize("window", [10, 30, 60, 120, 240])
    @pytest.mark.parametrize("seed", [42, 99, 7])
    def test_treap_vs_bruteforce(self, window, seed):
        data = make_data(seed, n=max(N, window + 200))

        g_brute = build_rank_graph(window, use_treap=False)
        g_treap = build_rank_graph(window, use_treap=True)

        for i in range(len(data)):
            g_brute.push_bar(data[i])
            g_treap.push_bar(data[i])

        brute_val = g_brute.output()
        treap_val = g_treap.output()
        assert treap_val == pytest.approx(brute_val, nan_ok=True, abs=1e-6), \
            f"window={window}: treap={treap_val}, brute={brute_val}"

    @pytest.mark.parametrize("window", [10, 30, 60, 120, 240])
    @pytest.mark.parametrize("seed", [42, 99, 7])
    def test_treap_vs_python(self, window, seed):
        data = make_data(seed, n=max(N, window + 200))

        py_out = TsRank(pd.Series(data), window).values
        g_treap = build_rank_graph(window, use_treap=True)

        treap_outputs = []
        for i in range(len(data)):
            g_treap.push_bar(data[i])
            treap_outputs.append(g_treap.output())

        treap_arr = np.array(treap_outputs, dtype=np.float32)
        valid = ~np.isnan(py_out) & ~np.isnan(treap_arr)
        if valid.sum() == 0:
            return

        max_diff = float(np.max(np.abs(py_out[valid] - treap_arr[valid])))
        assert max_diff < 1e-5, \
            f"window={window}: max_diff={max_diff}"

    @pytest.mark.parametrize("window", [10, 30, 120])
    def test_full_series_match(self, window):
        """Every output value must match between treap and brute-force."""
        data = make_data(42, n=1000)

        g_brute = build_rank_graph(window, use_treap=False)
        g_treap = build_rank_graph(window, use_treap=True)

        for i in range(len(data)):
            g_brute.push_bar(data[i])
            g_treap.push_bar(data[i])

            bv = g_brute.output()
            tv = g_treap.output()
            assert tv == pytest.approx(bv, nan_ok=True, abs=1e-6), \
                f"Mismatch at bar {i}: treap={tv}, brute={bv}"

    def test_with_nan(self):
        """Treap handles NaN values correctly."""
        data = make_data(42, n=500)
        data[50] = np.nan
        data[100] = np.nan
        data[200:210] = np.nan

        g_brute = build_rank_graph(30, use_treap=False)
        g_treap = build_rank_graph(30, use_treap=True)

        for i in range(len(data)):
            g_brute.push_bar(data[i])
            g_treap.push_bar(data[i])

            bv = g_brute.output()
            tv = g_treap.output()
            assert tv == pytest.approx(bv, nan_ok=True, abs=1e-6), \
                f"NaN mismatch at bar {i}: treap={tv}, brute={bv}"

    def test_constant_values(self):
        """Treap handles constant (all-tied) data."""
        data = np.full(200, 42.0, dtype=np.float32)

        g_brute = build_rank_graph(30, use_treap=False)
        g_treap = build_rank_graph(30, use_treap=True)

        for i in range(len(data)):
            g_brute.push_bar(data[i])
            g_treap.push_bar(data[i])

            bv = g_brute.output()
            tv = g_treap.output()
            assert tv == pytest.approx(bv, nan_ok=True, abs=1e-6), \
                f"Constant mismatch at bar {i}: treap={tv}, brute={bv}"

    def test_reset(self):
        """Reset produces same results on second run."""
        data = make_data(42, n=300)

        g = build_rank_graph(30, use_treap=True)
        for i in range(len(data)):
            g.push_bar(data[i])
        out1 = g.output()

        g.reset()
        for i in range(len(data)):
            g.push_bar(data[i])
        out2 = g.output()

        assert out1 == pytest.approx(out2, nan_ok=True, abs=1e-6)


class TestTreapBenchmark:
    """Benchmark: per-push latency of Treap vs brute-force across window sizes."""

    def test_benchmark(self):
        data = make_data(42, n=N)

        print(f"\n{'window':>8} | {'brute (us/push)':>16} | {'treap (us/push)':>16} | {'speedup':>8}")
        print("-" * 60)

        for window in WINDOWS:
            n_push = min(N, max(window * 3, 1000))
            push_data = data[:n_push]

            # Brute-force
            g_brute = build_rank_graph(window, use_treap=False)
            t0 = time.perf_counter()
            for i in range(n_push):
                g_brute.push_bar(push_data[i])
                _ = g_brute.output()
            t_brute = (time.perf_counter() - t0) / n_push * 1e6

            # Treap
            g_treap = build_rank_graph(window, use_treap=True)
            t0 = time.perf_counter()
            for i in range(n_push):
                g_treap.push_bar(push_data[i])
                _ = g_treap.output()
            t_treap = (time.perf_counter() - t0) / n_push * 1e6

            speedup = t_brute / t_treap if t_treap > 0 else 0

            print(f"{window:>8} | {t_brute:>16.2f} | {t_treap:>16.2f} | {speedup:>7.2f}x")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
