"""
Alignment tests for TS_COS (Rolling Cosine Similarity, no mean-centering).

Reference: manual sum(xi*yi) / (sqrt(sum(xi^2)) * sqrt(sum(yi^2)))
"""

import math

import numpy as np
import pandas as pd
import pytest

import fe_ops
import fe_runtime as rt


WINDOWS = [2, 5, 10, 30, 60]
N = 500


def _pandas_cos(x: np.ndarray, y: np.ndarray, w: int) -> np.ndarray:
    sx = pd.Series(x.astype(np.float64))
    sy = pd.Series(y.astype(np.float64))
    sxy = (sx * sy).rolling(w, min_periods=w).sum()
    sxx = (sx * sx).rolling(w, min_periods=w).sum()
    syy = (sy * sy).rolling(w, min_periods=w).sum()
    denom = np.sqrt(sxx * syy)
    result = sxy / denom
    result[denom <= 0] = np.nan
    return result.values


class TestCosBatchAlignment:
    @pytest.fixture(autouse=True)
    def _data(self):
        rng = np.random.default_rng(42)
        self.x = rng.standard_normal(N).astype(np.float32) + 5.0
        self.y = rng.standard_normal(N).astype(np.float32) + 3.0

    @pytest.mark.parametrize("w", WINDOWS)
    def test_batch_vs_pandas(self, w):
        cpp = fe_ops.rolling_cos(self.x, self.y, w)
        ref = _pandas_cos(self.x, self.y, w)
        valid = ~np.isnan(ref)
        np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-4, rtol=1e-4)
        np.testing.assert_equal(np.isnan(cpp[:w - 1]).all(), True)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_vs_batch(self, w):
        batch = fe_ops.rolling_cos(self.x, self.y, w)
        g = rt.FactorGraph()
        cx = g.add_input("close")
        cy = g.add_input("volume")
        g.add_bivariate(rt.Op.TS_COS, cx, cy, w)
        g.compile()
        push_out = []
        for xv, yv in zip(self.x, self.y):
            g.push_bar(float(xv), volume=float(yv))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch)
        np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)

    def test_cos_self_equals_one(self):
        """cos(x, x) = 1.0 for positive values."""
        data = np.abs(np.random.default_rng(7).standard_normal(100).astype(np.float32)) + 1.0
        result = fe_ops.rolling_cos(data, data, 10)
        valid = ~np.isnan(result)
        np.testing.assert_allclose(result[valid], 1.0, atol=1e-5)

    def test_cos_opposite_equals_neg_one(self):
        """cos(x, -x) = -1.0."""
        data = np.random.default_rng(8).standard_normal(100).astype(np.float32) + 5.0
        result = fe_ops.rolling_cos(data, -data, 10)
        valid = ~np.isnan(result)
        np.testing.assert_allclose(result[valid], -1.0, atol=1e-5)


class TestCosEdgeCases:
    def test_negative_window(self):
        x = np.ones(10, dtype=np.float32)
        result = fe_ops.rolling_cos(x, x, -1)
        assert np.isnan(result).all()

    def test_window_1_returns_nan(self):
        x = np.arange(10, dtype=np.float32) + 1.0
        result = fe_ops.rolling_cos(x, x, 1)
        assert np.isnan(result).all()

    def test_graph_rejects_window_lt_2(self):
        g = rt.FactorGraph()
        cx = g.add_input("close")
        cy = g.add_input("volume")
        with pytest.raises(ValueError, match="window >= 2"):
            g.add_bivariate(rt.Op.TS_COS, cx, cy, 1)

    def test_zero_vector_returns_nan(self):
        x = np.zeros(20, dtype=np.float32)
        y = np.ones(20, dtype=np.float32)
        result = fe_ops.rolling_cos(x, y, 5)
        assert np.isnan(result[4:]).all()

    @pytest.mark.parametrize("nan_ratio", [0.05, 0.2])
    @pytest.mark.parametrize("nan_target", ["x", "y", "both"])
    def test_with_nans(self, nan_ratio, nan_target):
        rng = np.random.default_rng(123)
        x = rng.standard_normal(N).astype(np.float32) + 5.0
        y = rng.standard_normal(N).astype(np.float32) + 3.0
        mask = rng.random(N) < nan_ratio
        if nan_target in ("x", "both"):
            x[mask] = np.nan
        if nan_target in ("y", "both"):
            y[mask] = np.nan
        w = 10
        cpp = fe_ops.rolling_cos(x, y, w)
        ref = _pandas_cos(x, y, w)
        valid = ~np.isnan(ref) & ~np.isnan(cpp)
        if valid.any():
            np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-4, rtol=1e-4)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_matches_batch_with_nans(self, w):
        rng = np.random.default_rng(99)
        x = rng.standard_normal(N).astype(np.float32) + 5.0
        y = rng.standard_normal(N).astype(np.float32) + 3.0
        x[rng.random(N) < 0.1] = np.nan
        batch = fe_ops.rolling_cos(x, y, w)
        g = rt.FactorGraph()
        cx = g.add_input("close")
        cy = g.add_input("volume")
        g.add_bivariate(rt.Op.TS_COS, cx, cy, w)
        g.compile()
        push_out = []
        for xv, yv in zip(x, y):
            g.push_bar(float(xv), volume=float(yv))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch) & ~np.isnan(push_arr)
        if valid.any():
            np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)
