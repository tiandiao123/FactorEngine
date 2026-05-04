"""
TS_BETA / rolling_beta alignment tests.

Layer A: fe_ops.rolling_beta(x, y, w) vs pandas Cov(x,y)/Var(x) (ddof=1, min_periods=w)
Layer B: FactorGraph TS_BETA push streaming vs fe_ops.rolling_beta batch

Note: Python API is rolling_beta(x, y, w) = Cov(x,y)/Var(x), i.e. y regressed on x.
      This matches rolling_corr(x, y, w) and rolling_cov(x, y, w) argument ordering.

Usage:
    cd FactorEngine
    pytest tests/custom_kernels/ts_beta/test_ts_beta_alignment.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import fe_ops
import fe_runtime as rt

Op = rt.Op

SEEDS = [42, 123, 999]
WINDOWS = [5, 10, 30, 60, 120]
N = 5000
BATCH_ATOL = 1e-3
PUSH_ATOL = 1e-5


def _random_series(n: int = N, seed: int = 42, nan_ratio: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n).astype(np.float32)
    if nan_ratio > 0:
        nan_idx = rng.choice(n, size=int(n * nan_ratio), replace=False)
        x[nan_idx] = np.nan
    return x


def _pandas_beta(x: np.ndarray, y: np.ndarray, w: int) -> np.ndarray:
    """Beta(y regressed on x) = Cov(x,y) / Var(x), matching fe_ops.rolling_beta(x, y, w)."""
    xs = pd.Series(x.astype(np.float64))
    ys = pd.Series(y.astype(np.float64))
    cov = ys.rolling(w, min_periods=w).cov(xs)
    var = xs.rolling(w, min_periods=w).var(ddof=1)
    beta = cov / var
    return beta.values.astype(np.float32)


def _assert_aligned(py_arr, cpp_arr, atol: float, label: str = "") -> None:
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


class TestBetaBatchAlignment:
    @pytest.mark.parametrize("seed_x", SEEDS)
    @pytest.mark.parametrize("seed_y", [7, 88])
    @pytest.mark.parametrize("w", WINDOWS)
    def test_basic(self, seed_x, seed_y, w):
        x = _random_series(seed=seed_x, nan_ratio=0)
        y = _random_series(seed=seed_y, nan_ratio=0)
        py_out = _pandas_beta(x, y, w)
        cpp_out = np.asarray(fe_ops.rolling_beta(x, y, w), dtype=np.float32)
        _assert_aligned(py_out, cpp_out, BATCH_ATOL, f"beta(w={w},sx={seed_x},sy={seed_y})")

    def test_beta_self_equals_one(self):
        """Beta(x, x) = Cov(x,x)/Var(x) = 1.0 for non-constant series."""
        x = _random_series(n=3000, seed=42, nan_ratio=0)
        w = 30
        cpp = np.asarray(fe_ops.rolling_beta(x, x, w), dtype=np.float32)
        valid = ~np.isnan(cpp)
        np.testing.assert_allclose(cpp[valid], 1.0, atol=1e-5,
                                   err_msg="Beta(x,x) should be 1.0")

    def test_beta_constant_x_returns_nan(self):
        """When x is constant, Var(x)=0, beta should be NaN."""
        x = np.full(200, 5.0, dtype=np.float32)
        y = _random_series(n=200, seed=42)
        cpp = np.asarray(fe_ops.rolling_beta(x, y, 20), dtype=np.float32)
        assert np.all(np.isnan(cpp[19:])), "Beta with constant x should be NaN"

    def test_beta_linear_relationship(self):
        """If y = a*x + b, beta should be a."""
        rng = np.random.default_rng(42)
        x = rng.standard_normal(2000).astype(np.float32)
        a, b = 2.5, 10.0
        y = (a * x + b).astype(np.float32)
        w = 30
        cpp = np.asarray(fe_ops.rolling_beta(x, y, w), dtype=np.float32)
        valid = ~np.isnan(cpp)
        np.testing.assert_allclose(cpp[valid], a, atol=1e-3,
                                   err_msg=f"Beta(y=ax+b, x) should be {a}")

    @pytest.mark.parametrize("nan_ratio", [0.01, 0.05, 0.1])
    @pytest.mark.parametrize("w", [10, 30, 60])
    @pytest.mark.parametrize("seed", SEEDS)
    def test_with_nans(self, seed, w, nan_ratio):
        x = _random_series(n=2500, seed=seed, nan_ratio=nan_ratio)
        y = _random_series(n=2500, seed=seed + 1, nan_ratio=nan_ratio)
        py_out = _pandas_beta(x, y, w)
        cpp_out = np.asarray(fe_ops.rolling_beta(x, y, w), dtype=np.float32)
        _assert_aligned(py_out, cpp_out, BATCH_ATOL, f"beta(nan,r={nan_ratio},w={w})")

    def test_negative_window_all_nan(self):
        x = _random_series(n=100, seed=42)
        y = _random_series(n=100, seed=43)
        out = np.asarray(fe_ops.rolling_beta(x, y, -3), dtype=np.float32)
        assert np.all(np.isnan(out))

    def test_zero_window_all_nan(self):
        x = _random_series(n=50, seed=42)
        y = _random_series(n=50, seed=43)
        out = np.asarray(fe_ops.rolling_beta(x, y, 0), dtype=np.float32)
        assert np.all(np.isnan(out))

    def test_window_one_all_nan(self):
        x = _random_series(n=50, seed=42)
        y = _random_series(n=50, seed=43)
        out = np.asarray(fe_ops.rolling_beta(x, y, 1), dtype=np.float32)
        assert np.all(np.isnan(out))

    def test_graph_rejects_window_lt_2(self):
        g = rt.FactorGraph()
        a = g.add_input("close")
        b = g.add_input("volume")
        with pytest.raises(ValueError, match="TS_BETA requires window >= 2"):
            g.add_bivariate(Op.TS_BETA, a, b, 1)


# ═══════════════════════════════════════════════════════════════
#  Layer B: push streaming vs batch
# ═══════════════════════════════════════════════════════════════


def _push_bivariate_xy(
    g: rt.FactorGraph, x: np.ndarray, y: np.ndarray
) -> np.ndarray:
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float32)
    for i in range(n):
        g.push_bar(close=float(x[i]), volume=float(y[i]))
        out[i] = g.raw_output()
    return out


class TestBetaPushVsBatch:
    @pytest.mark.parametrize("seed", SEEDS)
    @pytest.mark.parametrize("w", [10, 30, 60, 120])
    def test_push_matches_batch(self, seed, w):
        x = _random_series(n=2000, seed=seed)
        y = _random_series(n=2000, seed=seed + 100)
        batch_out = np.asarray(fe_ops.rolling_beta(x, y, w), dtype=np.float32)

        g = rt.FactorGraph()
        xc = g.add_input("close")
        yv = g.add_input("volume")
        g.add_bivariate(Op.TS_BETA, xc, yv, w)
        g.compile()
        push_out = _push_bivariate_xy(g, x, y)
        _assert_aligned(batch_out, push_out, PUSH_ATOL, f"push_vs_batch(w={w})")

    @pytest.mark.parametrize("nan_ratio", [0.02, 0.08])
    @pytest.mark.parametrize("w", [10, 30, 60])
    @pytest.mark.parametrize("seed", SEEDS)
    def test_push_matches_batch_with_nans(self, seed, w, nan_ratio):
        x = _random_series(n=1500, seed=seed, nan_ratio=nan_ratio)
        y = _random_series(n=1500, seed=seed + 50, nan_ratio=nan_ratio)
        batch_out = np.asarray(fe_ops.rolling_beta(x, y, w), dtype=np.float32)

        g = rt.FactorGraph()
        xc = g.add_input("close")
        yv = g.add_input("volume")
        g.add_bivariate(Op.TS_BETA, xc, yv, w)
        g.compile()
        push_out = _push_bivariate_xy(g, x, y)
        _assert_aligned(batch_out, push_out, PUSH_ATOL,
                        f"push_vs_batch_nan(w={w},r={nan_ratio})")
