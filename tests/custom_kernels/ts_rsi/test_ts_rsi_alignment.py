"""
Alignment tests for TS_RSI (Relative Strength Index, Wilder's smoothing).

Reference: Classic Wilder's RSI — seed with SMA of first `window` deltas,
then exponential smoothing with alpha = 1/window.
"""

import math

import numpy as np
import pytest

import fe_ops
import fe_runtime as rt


WINDOWS = [2, 5, 14, 30]
N = 500


def _wilder_rsi(x: np.ndarray, w: int) -> np.ndarray:
    """Classic Wilder's RSI reference (simple-average seed)."""
    data = x.astype(np.float64)
    n = len(data)
    out = np.full(n, np.nan)
    if n <= w:
        return out
    deltas = np.diff(data)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_g = np.mean(gains[:w])
    avg_l = np.mean(losses[:w])

    def rsi_val(ag, al):
        if al < 1e-15:
            return 100.0 if ag > 1e-15 else 50.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    out[w] = rsi_val(avg_g, avg_l)
    for i in range(w, len(deltas)):
        avg_g = (avg_g * (w - 1) + gains[i]) / w
        avg_l = (avg_l * (w - 1) + losses[i]) / w
        out[i + 1] = rsi_val(avg_g, avg_l)
    return out


class TestRsiBatchAlignment:
    @pytest.fixture(autouse=True)
    def _data(self):
        rng = np.random.default_rng(42)
        self.data = rng.standard_normal(N).astype(np.float32).cumsum() + 100.0

    @pytest.mark.parametrize("w", WINDOWS)
    def test_batch_vs_wilder_ref(self, w):
        cpp = fe_ops.rolling_rsi(self.data, w)
        ref = _wilder_rsi(self.data, w)
        valid = ~np.isnan(ref)
        np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-3, rtol=1e-4)
        assert np.isnan(cpp[:w]).all()

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_vs_batch(self, w):
        batch = fe_ops.rolling_rsi(self.data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_RSI, c, w)
        g.compile()
        push_out = []
        for v in self.data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch)
        np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)

    def test_rsi_range_0_100(self):
        """RSI output should be in [0, 100]."""
        rng = np.random.default_rng(77)
        data = rng.standard_normal(200).astype(np.float32).cumsum() + 100.0
        result = fe_ops.rolling_rsi(data, 14)
        valid = ~np.isnan(result)
        assert (result[valid] >= 0.0 - 1e-5).all()
        assert (result[valid] <= 100.0 + 1e-5).all()

    def test_monotonic_up_rsi_near_100(self):
        """Strictly increasing series → RSI should approach 100."""
        data = np.arange(100, dtype=np.float32) + 1.0
        result = fe_ops.rolling_rsi(data, 14)
        assert result[-1] > 99.0

    def test_monotonic_down_rsi_near_0(self):
        """Strictly decreasing series → RSI should approach 0."""
        data = (100.0 - np.arange(100, dtype=np.float32))
        result = fe_ops.rolling_rsi(data, 14)
        assert result[-1] < 1.0


class TestRsiEdgeCases:
    def test_negative_window(self):
        x = np.ones(20, dtype=np.float32)
        result = fe_ops.rolling_rsi(x, -1)
        assert np.isnan(result).all()

    def test_window_0_returns_nan(self):
        x = np.arange(20, dtype=np.float32) + 1.0
        result = fe_ops.rolling_rsi(x, 0)
        assert np.isnan(result).all()

    def test_window_1_returns_nan(self):
        x = np.arange(20, dtype=np.float32)
        result = fe_ops.rolling_rsi(x, 1)
        assert np.isnan(result).all()

    def test_short_series_all_nan(self):
        """When len(x) <= window, entire result should be NaN."""
        for w in [5, 14, 30]:
            x = np.arange(w, dtype=np.float32) + 1.0
            result = fe_ops.rolling_rsi(x, w)
            assert np.isnan(result).all(), f"w={w}: expected all NaN for len(x)==w"

    def test_graph_rejects_window_lt_2(self):
        g = rt.FactorGraph()
        c = g.add_input("close")
        with pytest.raises(ValueError, match="window >= 2"):
            g.add_rolling(rt.Op.TS_RSI, c, 1)

    def test_constant_input_rsi_50(self):
        """Constant series: no gains or losses → RSI = 50."""
        const = np.full(50, 42.0, dtype=np.float32)
        result = fe_ops.rolling_rsi(const, 14)
        valid = ~np.isnan(result)
        if valid.any():
            np.testing.assert_allclose(result[valid], 50.0, atol=0.1)

    @pytest.mark.parametrize("nan_ratio", [0.05, 0.2])
    def test_with_nans_range_check(self, nan_ratio):
        rng = np.random.default_rng(123)
        data = rng.standard_normal(N).astype(np.float32).cumsum() + 100.0
        mask = rng.random(N) < nan_ratio
        data[mask] = np.nan
        w = 14
        result = fe_ops.rolling_rsi(data, w)
        valid = ~np.isnan(result)
        if valid.any():
            assert (result[valid] >= 0.0 - 1e-5).all()
            assert (result[valid] <= 100.0 + 1e-5).all()

    @pytest.mark.parametrize("w", WINDOWS)
    def test_nan_values_agree_push_batch(self, w):
        """Where both push and batch produce non-NaN, values must agree.
        RSI's NaN gap-skipping means push (stateful) may recover from NaN
        gaps differently than batch (uses same kernel but fresh state).
        Since both use the same RsiKernel, they must agree identically."""
        rng = np.random.default_rng(77)
        data = rng.standard_normal(N).astype(np.float32).cumsum() + 100.0
        data[rng.random(N) < 0.15] = np.nan
        batch = fe_ops.rolling_rsi(data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_RSI, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch) & ~np.isnan(push_arr)
        if valid.any():
            np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-4)
            assert (push_arr[valid] >= 0.0 - 1e-5).all()
            assert (push_arr[valid] <= 100.0 + 1e-5).all()

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_matches_batch_with_nans(self, w):
        rng = np.random.default_rng(99)
        data = rng.standard_normal(N).astype(np.float32).cumsum() + 100.0
        data[rng.random(N) < 0.1] = np.nan
        batch = fe_ops.rolling_rsi(data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_RSI, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch) & ~np.isnan(push_arr)
        if valid.any():
            np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)
