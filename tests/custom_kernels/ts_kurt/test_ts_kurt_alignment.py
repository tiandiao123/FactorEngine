"""
TS_KURT alignment tests — three layers of verification:

Layer A: batch C++ (fe_ops.rolling_kurt) vs pandas .rolling().kurt()
Layer B: push streaming (FactorGraph) vs batch C++ (fe_ops.rolling_kurt)

Usage:
    cd FactorEngine
    pytest tests/custom_kernels/ts_kurt/test_ts_kurt_alignment.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import fe_ops
import fe_runtime as rt

Op = rt.Op

# ── test config ──────────────────────────────────────────────

SEEDS = [42, 123, 999]
WINDOWS = [5, 10, 30, 60, 120]
N = 5000
BATCH_ATOL = 1e-3
PUSH_ATOL = 1e-5


# ── helpers ──────────────────────────────────────────────────

def _random_series(n: int = N, seed: int = 42, nan_ratio: float = 0.0):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n).astype(np.float32)
    if nan_ratio > 0:
        nan_idx = rng.choice(n, size=int(n * nan_ratio), replace=False)
        x[nan_idx] = np.nan
    return x


def _constant(val: float, n: int = 500):
    return np.full(n, val, dtype=np.float32)


def _monotonic(n: int = 1000, up: bool = True):
    if up:
        return np.linspace(-5, 5, n).astype(np.float32)
    return np.linspace(5, -5, n).astype(np.float32)


def _pandas_kurt(x: np.ndarray, w: int) -> np.ndarray:
    return pd.Series(x).rolling(w, min_periods=w).kurt().values.astype(np.float32)


def _assert_aligned(py_arr, cpp_arr, atol, label=""):
    py_arr = np.asarray(py_arr, dtype=np.float32)
    cpp_arr = np.asarray(cpp_arr, dtype=np.float32)
    assert py_arr.shape == cpp_arr.shape, f"{label}: shape mismatch"

    py_nan = np.isnan(py_arr)
    cpp_nan = np.isnan(cpp_arr)
    nan_mismatch = int((py_nan != cpp_nan).sum())
    assert nan_mismatch == 0, (
        f"{label}: {nan_mismatch} NaN position mismatches "
        f"(first at idx={np.where(py_nan != cpp_nan)[0][0]})"
    )

    valid = ~py_nan
    if valid.sum() == 0:
        return
    max_diff = float(np.max(np.abs(py_arr[valid] - cpp_arr[valid])))
    assert max_diff <= atol, f"{label}: max_diff={max_diff:.2e} > atol={atol:.0e}"


# ═══════════════════════════════════════════════════════════════
#  Layer A: batch C++ vs pandas
# ═══════════════════════════════════════════════════════════════

class TestKurtBatchAlignment:
    """fe_ops.rolling_kurt() vs pandas .rolling().kurt()"""

    @pytest.mark.parametrize("seed", SEEDS)
    @pytest.mark.parametrize("w", WINDOWS)
    def test_basic(self, seed, w):
        x = _random_series(seed=seed)
        py_out = _pandas_kurt(x, w)
        cpp_out = np.asarray(fe_ops.rolling_kurt(x, w), dtype=np.float32)
        _assert_aligned(py_out, cpp_out, BATCH_ATOL, f"kurt(w={w},seed={seed})")

    def test_constant_input(self):
        """Constant input → variance=0 → kurtosis undefined.
        pandas returns a specific value; C++ returns 0 when m2=0.
        We only verify NaN positions match for the first (warmup) region."""
        c = _constant(5.0)
        for w in [5, 30]:
            py_out = _pandas_kurt(c, w)
            cpp_out = np.asarray(fe_ops.rolling_kurt(c, w), dtype=np.float32)
            py_nan = np.isnan(py_out)
            cpp_nan = np.isnan(cpp_out)
            assert (py_nan[:w - 1] == cpp_nan[:w - 1]).all(), "warmup NaN mismatch"

    def test_small_window(self):
        """window < 4 → all NaN"""
        x = _random_series(n=1000, seed=42)
        for w in [1, 2, 3]:
            cpp_out = np.asarray(fe_ops.rolling_kurt(x, w), dtype=np.float32)
            assert np.all(np.isnan(cpp_out)), f"w={w} should be all NaN"

    def test_window_4(self):
        """Minimum valid window"""
        x = _random_series(n=500, seed=42)
        py_out = _pandas_kurt(x, 4)
        cpp_out = np.asarray(fe_ops.rolling_kurt(x, 4), dtype=np.float32)
        _assert_aligned(py_out, cpp_out, BATCH_ATOL, "kurt(w=4)")

    @pytest.mark.parametrize("w", [480, 1440, 2880])
    def test_large_window(self, w):
        x = _random_series(n=5000, seed=42)
        py_out = _pandas_kurt(x, w)
        cpp_out = np.asarray(fe_ops.rolling_kurt(x, w), dtype=np.float32)
        _assert_aligned(py_out, cpp_out, BATCH_ATOL, f"kurt(w={w})")

    @pytest.mark.parametrize("up", [True, False])
    def test_monotonic(self, up):
        x = _monotonic(up=up)
        for w in [30, 120]:
            py_out = _pandas_kurt(x, w)
            cpp_out = np.asarray(fe_ops.rolling_kurt(x, w), dtype=np.float32)
            _assert_aligned(py_out, cpp_out, BATCH_ATOL,
                            f"kurt(mono_{'up' if up else 'down'},w={w})")

    @pytest.mark.parametrize("nan_ratio", [0.01, 0.05, 0.1])
    @pytest.mark.parametrize("w", [10, 30, 60])
    @pytest.mark.parametrize("seed", SEEDS)
    def test_with_nans(self, seed, w, nan_ratio):
        """NaN-injected series: C++ and pandas NaN positions must match."""
        x = _random_series(n=2000, seed=seed, nan_ratio=nan_ratio)
        py_out = _pandas_kurt(x, w)
        cpp_out = np.asarray(fe_ops.rolling_kurt(x, w), dtype=np.float32)
        py_nan = np.isnan(py_out)
        cpp_nan = np.isnan(cpp_out)
        assert np.array_equal(py_nan, cpp_nan), (
            f"NaN position mismatch (w={w}, seed={seed}, nan_ratio={nan_ratio}): "
            f"{int((py_nan != cpp_nan).sum())} positions differ"
        )
        valid = ~py_nan
        if valid.sum() > 0:
            max_diff = float(np.max(np.abs(py_out[valid] - cpp_out[valid])))
            assert max_diff <= BATCH_ATOL, (
                f"max_diff={max_diff:.2e} (w={w}, seed={seed}, nan_ratio={nan_ratio})"
            )

    def test_negative_window(self):
        """Negative window → all NaN, no crash."""
        x = _random_series(n=100, seed=42)
        cpp_out = np.asarray(fe_ops.rolling_kurt(x, -5), dtype=np.float32)
        assert np.all(np.isnan(cpp_out))

    def test_graph_rejects_small_window(self):
        """FactorGraph should reject TS_KURT with window < 4."""
        g = rt.FactorGraph()
        c = g.add_input("close")
        with pytest.raises(Exception):
            g.add_rolling(Op.TS_KURT, c, 2)


# ═══════════════════════════════════════════════════════════════
#  Layer B: push streaming vs batch
# ═══════════════════════════════════════════════════════════════

class TestKurtPushVsBatch:
    """FactorGraph push-level streaming vs fe_ops.rolling_kurt batch"""

    @pytest.mark.parametrize("seed", SEEDS)
    @pytest.mark.parametrize("w", [10, 30, 60, 120])
    def test_push_matches_batch(self, seed, w):
        x = _random_series(n=2000, seed=seed)
        batch_out = np.asarray(fe_ops.rolling_kurt(x, w), dtype=np.float32)

        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(Op.TS_KURT, c, w)
        g.compile()

        push_out = np.empty(len(x), dtype=np.float32)
        for i in range(len(x)):
            g.push_bar(float(x[i]))
            push_out[i] = g.raw_output()

        _assert_aligned(batch_out, push_out, PUSH_ATOL,
                        f"push_vs_batch(w={w},seed={seed})")

    def test_push_warmup_bars(self):
        """warmup_bars should equal window size"""
        for w in [4, 10, 30, 60]:
            g = rt.FactorGraph()
            c = g.add_input("close")
            g.add_rolling(Op.TS_KURT, c, w)
            g.compile()
            assert g.warmup_bars() == w, f"warmup for w={w}"

    def test_push_not_ready_before_warmup(self):
        w = 30
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(Op.TS_KURT, c, w)
        g.compile()

        x = _random_series(n=50, seed=42)
        for i in range(w - 1):
            g.push_bar(float(x[i]))
            assert not g.ready(), f"should not be ready at bar {i}"
        g.push_bar(float(x[w - 1]))
        assert not g.ready(), "should require one extra bar past warmup boundary"
        g.push_bar(float(x[w]))
        assert g.ready(), "should be ready after warmup plus one guard bar"

    def test_reset_clears_state(self):
        w = 20
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(Op.TS_KURT, c, w)
        g.compile()

        x = _random_series(n=50, seed=42)
        for v in x:
            g.push_bar(float(v))
        out_before = g.output()

        g.reset()
        assert not g.ready()
        for v in x:
            g.push_bar(float(v))
        out_after = g.output()

        assert abs(out_before - out_after) < 1e-6, "reset should reproduce same output"
