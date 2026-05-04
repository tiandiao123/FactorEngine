"""
Alignment tests for TS_CV (Coefficient of Variation = std/|mean|, ddof=0).

Reference: pandas rolling(w).std(ddof=0) / |rolling(w).mean()|
"""

import math

import numpy as np
import pandas as pd
import pytest

import fe_ops
import fe_runtime as rt


WINDOWS = [2, 5, 10, 30, 60]
N = 500


def _pandas_cv(x: np.ndarray, w: int) -> np.ndarray:
    s = pd.Series(x.astype(np.float64))
    std = s.rolling(w, min_periods=w).std(ddof=0)
    mean = s.rolling(w, min_periods=w).mean()
    cv = std / mean.abs()
    cv[mean.abs() < 1e-12] = np.nan
    return cv.values


class TestCvBatchAlignment:
    @pytest.fixture(autouse=True)
    def _data(self):
        rng = np.random.default_rng(42)
        self.data = rng.standard_normal(N).astype(np.float32) + 5.0

    @pytest.mark.parametrize("w", WINDOWS)
    def test_batch_vs_pandas(self, w):
        cpp = fe_ops.rolling_cv(self.data, w)
        ref = _pandas_cv(self.data, w)
        valid = ~np.isnan(ref)
        np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-4, rtol=1e-4)
        np.testing.assert_equal(np.isnan(cpp[:w - 1]).all(), True)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_vs_batch(self, w):
        batch = fe_ops.rolling_cv(self.data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_CV, c, w)
        g.compile()
        push_out = []
        for v in self.data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch)
        np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)

    def test_constant_input_returns_zero(self):
        """Constant series: std=0, mean!=0, so CV=0.0 exactly."""
        const = np.full(100, 3.14, dtype=np.float32)
        result = fe_ops.rolling_cv(const, 10)
        assert np.isnan(result[:9]).all()
        np.testing.assert_allclose(result[9:], 0.0, atol=1e-6)

    def test_zero_mean_returns_nan(self):
        data = np.array([1, -1] * 50, dtype=np.float32)
        result = fe_ops.rolling_cv(data, 2)
        for v in result[1:]:
            assert math.isnan(v)


class TestCvEdgeCases:
    def test_negative_window(self):
        x = np.ones(10, dtype=np.float32)
        result = fe_ops.rolling_cv(x, -1)
        assert np.isnan(result).all()

    def test_window_1_returns_nan(self):
        x = np.arange(10, dtype=np.float32) + 1.0
        result = fe_ops.rolling_cv(x, 1)
        assert np.isnan(result).all()

    def test_graph_rejects_window_lt_2(self):
        g = rt.FactorGraph()
        c = g.add_input("close")
        with pytest.raises(ValueError, match="window >= 2"):
            g.add_rolling(rt.Op.TS_CV, c, 1)

    @pytest.mark.parametrize("nan_ratio", [0.05, 0.2])
    def test_with_nans(self, nan_ratio):
        rng = np.random.default_rng(123)
        data = rng.standard_normal(N).astype(np.float32) + 5.0
        mask = rng.random(N) < nan_ratio
        data[mask] = np.nan
        w = 10
        cpp = fe_ops.rolling_cv(data, w)
        ref = _pandas_cv(data, w)
        valid = ~np.isnan(ref) & ~np.isnan(cpp)
        if valid.any():
            np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-4, rtol=1e-4)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_matches_batch_with_nans(self, w):
        rng = np.random.default_rng(99)
        data = rng.standard_normal(N).astype(np.float32) + 5.0
        data[rng.random(N) < 0.1] = np.nan
        batch = fe_ops.rolling_cv(data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_CV, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch) & ~np.isnan(push_arr)
        if valid.any():
            np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)
