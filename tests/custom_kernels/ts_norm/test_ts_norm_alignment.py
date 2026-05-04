"""
Alignment tests for TS_NORM (Min-Max Normalization: (x - min) / (max - min)).

Reference: pandas (x - rolling.min) / (rolling.max - rolling.min)
"""

import math

import numpy as np
import pandas as pd
import pytest

import fe_ops
import fe_runtime as rt


WINDOWS = [2, 5, 10, 30, 60]
N = 500


def _pandas_norm(x: np.ndarray, w: int) -> np.ndarray:
    s = pd.Series(x.astype(np.float64))
    rmin = s.rolling(w, min_periods=w).min()
    rmax = s.rolling(w, min_periods=w).max()
    rng = rmax - rmin
    result = (s - rmin) / rng
    result[rng <= 0] = np.nan
    return result.values


class TestNormBatchAlignment:
    @pytest.fixture(autouse=True)
    def _data(self):
        rng = np.random.default_rng(42)
        self.data = rng.standard_normal(N).astype(np.float32)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_batch_vs_pandas(self, w):
        cpp = fe_ops.rolling_norm(self.data, w)
        ref = _pandas_norm(self.data, w)
        valid = ~np.isnan(ref)
        np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-4, rtol=1e-4)
        np.testing.assert_equal(np.isnan(cpp[:w - 1]).all(), True)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_vs_batch(self, w):
        batch = fe_ops.rolling_norm(self.data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_NORM, c, w)
        g.compile()
        push_out = []
        for v in self.data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch)
        np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)

    def test_output_range_01(self):
        """Normalized output should be in [0, 1]."""
        rng = np.random.default_rng(77)
        data = rng.standard_normal(200).astype(np.float32)
        result = fe_ops.rolling_norm(data, 20)
        valid = ~np.isnan(result)
        assert (result[valid] >= -1e-7).all()
        assert (result[valid] <= 1.0 + 1e-7).all()

    def test_max_is_one_min_is_zero(self):
        """Current max should map to 1.0, current min to 0.0."""
        data = np.array([1, 2, 3, 4, 5, 4, 3, 2, 1, 0], dtype=np.float32)
        result = fe_ops.rolling_norm(data, 5)
        assert result[4] == pytest.approx(1.0, abs=1e-5)
        assert result[9] == pytest.approx(0.0, abs=1e-5)


class TestNormEdgeCases:
    def test_constant_input_returns_nan(self):
        const = np.full(50, 3.14, dtype=np.float32)
        result = fe_ops.rolling_norm(const, 10)
        assert np.isnan(result).all()

    def test_negative_window(self):
        x = np.ones(10, dtype=np.float32)
        result = fe_ops.rolling_norm(x, -1)
        assert np.isnan(result).all()

    def test_window_1_returns_nan(self):
        x = np.arange(10, dtype=np.float32)
        result = fe_ops.rolling_norm(x, 1)
        assert np.isnan(result).all()

    def test_graph_rejects_window_lt_2(self):
        g = rt.FactorGraph()
        c = g.add_input("close")
        with pytest.raises(ValueError, match="window >= 2"):
            g.add_rolling(rt.Op.TS_NORM, c, 1)

    @pytest.mark.parametrize("nan_ratio", [0.05, 0.2])
    def test_with_nans(self, nan_ratio):
        rng = np.random.default_rng(123)
        data = rng.standard_normal(N).astype(np.float32)
        mask = rng.random(N) < nan_ratio
        data[mask] = np.nan
        w = 10
        cpp = fe_ops.rolling_norm(data, w)
        ref = _pandas_norm(data, w)
        valid = ~np.isnan(ref) & ~np.isnan(cpp)
        if valid.any():
            np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-4, rtol=1e-4)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_matches_batch_with_nans(self, w):
        rng = np.random.default_rng(99)
        data = rng.standard_normal(N).astype(np.float32)
        data[rng.random(N) < 0.1] = np.nan
        batch = fe_ops.rolling_norm(data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_NORM, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch) & ~np.isnan(push_arr)
        if valid.any():
            np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)
