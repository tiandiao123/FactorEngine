"""
Alignment tests for TS_CCI (Commodity Channel Index).

CCI = (TP - SMA(TP, w)) / (0.015 * MAD(TP, w))

Reference: pure-Python implementation of Lambert's CCI formula.
"""

import numpy as np
import pytest

import fe_ops
import fe_runtime as rt


WINDOWS = [2, 5, 14, 30]
N = 500


def _python_cci(x: np.ndarray, w: int) -> np.ndarray:
    """Pure-Python CCI reference using Lambert's constant 0.015."""
    data = x.astype(np.float64)
    n = len(data)
    out = np.full(n, np.nan)
    for i in range(w - 1, n):
        window_data = data[i - w + 1 : i + 1]
        mean = np.mean(window_data)
        mad = np.mean(np.abs(window_data - mean))
        if mad < 1e-12:
            out[i] = 0.0
        else:
            out[i] = (data[i] - mean) / (0.015 * mad)
    return out


class TestCciBatchAlignment:

    @pytest.mark.parametrize("w", WINDOWS)
    def test_batch_vs_python_ref(self, w):
        rng = np.random.default_rng(42)
        data = rng.standard_normal(N).astype(np.float32).cumsum() + 100.0
        cpp = fe_ops.rolling_cci(data, w)
        ref = _python_cci(data, w)
        valid = ~np.isnan(ref)
        np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-2, rtol=1e-3)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_vs_batch(self, w):
        rng = np.random.default_rng(99)
        data = rng.standard_normal(N).astype(np.float32).cumsum() + 100.0
        batch = fe_ops.rolling_cci(data, w)

        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_CCI, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)

        valid = ~np.isnan(batch)
        np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-4)

    def test_cci_range_typical(self):
        rng = np.random.default_rng(7)
        data = rng.standard_normal(2000).astype(np.float32).cumsum() + 100.0
        result = fe_ops.rolling_cci(data, 14)
        valid = result[~np.isnan(result)]
        within = np.sum((valid >= -100) & (valid <= 100)) / len(valid)
        assert within > 0.5, f"Only {within:.1%} within [-100, +100]"


class TestCciEdgeCases:

    def test_window_lt_2_rejected(self):
        with pytest.raises(Exception):
            g = rt.FactorGraph()
            c = g.add_input("close")
            g.add_rolling(rt.Op.TS_CCI, c, 1)

    def test_window_0_all_nan(self):
        data = np.arange(20, dtype=np.float32)
        result = fe_ops.rolling_cci(data, 0)
        assert np.isnan(result).all()

    def test_constant_input_returns_zero(self):
        const = np.full(100, 42.0, dtype=np.float32)
        result = fe_ops.rolling_cci(const, 10)
        valid = ~np.isnan(result)
        if valid.any():
            np.testing.assert_allclose(result[valid], 0.0, atol=1e-6)

    def test_monotonic_up_positive(self):
        data = np.arange(1, 101, dtype=np.float32)
        result = fe_ops.rolling_cci(data, 14)
        valid = result[~np.isnan(result)]
        assert (valid > 0).all(), "CCI should be positive for monotonic uptrend"

    def test_monotonic_down_negative(self):
        data = np.arange(100, 0, -1, dtype=np.float32)
        result = fe_ops.rolling_cci(data, 14)
        valid = result[~np.isnan(result)]
        assert (valid < 0).all(), "CCI should be negative for monotonic downtrend"

    @pytest.mark.parametrize("nan_ratio", [0.05, 0.2])
    def test_with_nans_range_check(self, nan_ratio):
        rng = np.random.default_rng(123)
        data = rng.standard_normal(N).astype(np.float32).cumsum() + 100.0
        mask = rng.random(N) < nan_ratio
        data[mask] = np.nan
        result = fe_ops.rolling_cci(data, 14)
        valid = ~np.isnan(result)
        assert valid.sum() < N

    @pytest.mark.parametrize("w", WINDOWS)
    def test_nan_values_agree_push_batch(self, w):
        """Where both push and batch produce non-NaN, values must agree."""
        rng = np.random.default_rng(77)
        data = rng.standard_normal(N).astype(np.float32).cumsum() + 100.0
        data[rng.random(N) < 0.15] = np.nan
        batch = fe_ops.rolling_cci(data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_CCI, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch) & ~np.isnan(push_arr)
        if valid.any():
            np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-3)

    def test_short_series_all_nan(self):
        for w in [5, 14]:
            data = np.arange(w - 1, dtype=np.float32) + 1.0
            result = fe_ops.rolling_cci(data, w)
            assert np.isnan(result).all()
