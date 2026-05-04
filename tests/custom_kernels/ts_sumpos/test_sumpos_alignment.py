"""
Alignment tests for TS_SUM_POS and TS_POS_RATIO.

TS_SUM_POS: sum of strictly positive values in rolling window.
TS_POS_RATIO: fraction of strictly positive values in rolling window.
"""

import numpy as np
import pandas as pd
import pytest

import fe_ops
import fe_runtime as rt

WINDOWS = [1, 5, 14, 30]
N = 500


def _pandas_sumpos(x: np.ndarray, w: int) -> np.ndarray:
    s = pd.Series(x.astype(np.float64))
    return s.rolling(w).apply(lambda v: np.sum(v[v > 0]), raw=True).values


def _pandas_updown_ratio(x: np.ndarray, w: int) -> np.ndarray:
    s = pd.Series(x.astype(np.float64))
    def _ud(v):
        sp = np.sum(v[v > 0])
        sn = np.sum(np.abs(v[v < 0]))
        if sn < 1e-15:
            return 0.0 if sp < 1e-15 else np.nan
        return sp / sn
    return s.rolling(w).apply(_ud, raw=True).values


def _pandas_posratio(x: np.ndarray, w: int) -> np.ndarray:
    s = pd.Series(x.astype(np.float64))
    return s.rolling(w).apply(lambda v: np.sum(v > 0) / len(v), raw=True).values


# ═══════════════════════════════════════════════════════════════
#  TS_SUM_POS tests
# ═══════════════════════════════════════════════════════════════

class TestSumPosBatchAlignment:

    @pytest.mark.parametrize("w", WINDOWS)
    def test_batch_vs_pandas(self, w):
        rng = np.random.default_rng(42)
        data = rng.standard_normal(N).astype(np.float32)
        cpp = fe_ops.rolling_sumpos(data, w)
        ref = _pandas_sumpos(data, w)
        valid = ~np.isnan(ref)
        np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-4, rtol=1e-4)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_vs_batch(self, w):
        rng = np.random.default_rng(99)
        data = rng.standard_normal(N).astype(np.float32)
        batch = fe_ops.rolling_sumpos(data, w)

        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_SUM_POS, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)

        valid = ~np.isnan(batch)
        np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)


class TestSumPosEdgeCases:

    def test_window_0_batch_all_nan(self):
        data = np.arange(20, dtype=np.float32)
        result = fe_ops.rolling_sumpos(data, 0)
        assert np.isnan(result).all()

    def test_window_0_graph_rejected(self):
        with pytest.raises(Exception):
            g = rt.FactorGraph()
            c = g.add_input("close")
            g.add_rolling(rt.Op.TS_SUM_POS, c, 0)

    def test_window_gt_series_all_nan(self):
        data = np.arange(5, dtype=np.float32)
        result = fe_ops.rolling_sumpos(data, 10)
        assert np.isnan(result).all()

    def test_all_negative_returns_zero(self):
        data = -np.abs(np.arange(1, 51, dtype=np.float32))
        result = fe_ops.rolling_sumpos(data, 10)
        valid = ~np.isnan(result)
        np.testing.assert_allclose(result[valid], 0.0, atol=1e-7)

    def test_all_positive(self):
        data = np.arange(1, 51, dtype=np.float32)
        result = fe_ops.rolling_sumpos(data, 5)
        ref = _pandas_sumpos(data, 5)
        valid = ~np.isnan(ref)
        np.testing.assert_allclose(result[valid], ref[valid], atol=1e-4)

    def test_zeros_not_counted(self):
        data = np.zeros(20, dtype=np.float32)
        result = fe_ops.rolling_sumpos(data, 5)
        valid = ~np.isnan(result)
        np.testing.assert_allclose(result[valid], 0.0, atol=1e-7)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_with_nans(self, w):
        rng = np.random.default_rng(77)
        data = rng.standard_normal(N).astype(np.float32)
        data[rng.random(N) < 0.1] = np.nan
        batch = fe_ops.rolling_sumpos(data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_SUM_POS, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch) & ~np.isnan(push_arr)
        if valid.any():
            np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-4)


# ═══════════════════════════════════════════════════════════════
#  TS_POS_RATIO tests
# ═══════════════════════════════════════════════════════════════

class TestPosRatioBatchAlignment:

    @pytest.mark.parametrize("w", WINDOWS)
    def test_batch_vs_pandas(self, w):
        rng = np.random.default_rng(42)
        data = rng.standard_normal(N).astype(np.float32)
        cpp = fe_ops.rolling_posratio(data, w)
        ref = _pandas_posratio(data, w)
        valid = ~np.isnan(ref)
        np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-5, rtol=1e-5)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_vs_batch(self, w):
        rng = np.random.default_rng(99)
        data = rng.standard_normal(N).astype(np.float32)
        batch = fe_ops.rolling_posratio(data, w)

        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_POS_RATIO, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)

        valid = ~np.isnan(batch)
        np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)


class TestPosRatioEdgeCases:

    def test_window_0_batch_all_nan(self):
        data = np.arange(20, dtype=np.float32)
        result = fe_ops.rolling_posratio(data, 0)
        assert np.isnan(result).all()

    def test_window_0_graph_rejected(self):
        with pytest.raises(Exception):
            g = rt.FactorGraph()
            c = g.add_input("close")
            g.add_rolling(rt.Op.TS_POS_RATIO, c, 0)

    def test_all_nan_window_returns_nan(self):
        data = np.full(10, np.nan, dtype=np.float32)
        result = fe_ops.rolling_posratio(data, 5)
        assert np.isnan(result).all()

    def test_all_positive_ratio_one(self):
        data = np.arange(1, 51, dtype=np.float32)
        result = fe_ops.rolling_posratio(data, 10)
        valid = ~np.isnan(result)
        np.testing.assert_allclose(result[valid], 1.0, atol=1e-7)

    def test_all_negative_ratio_zero(self):
        data = -np.arange(1, 51, dtype=np.float32)
        result = fe_ops.rolling_posratio(data, 10)
        valid = ~np.isnan(result)
        np.testing.assert_allclose(result[valid], 0.0, atol=1e-7)

    def test_output_range_0_1(self):
        rng = np.random.default_rng(7)
        data = rng.standard_normal(1000).astype(np.float32)
        result = fe_ops.rolling_posratio(data, 14)
        valid = result[~np.isnan(result)]
        assert (valid >= 0.0 - 1e-7).all()
        assert (valid <= 1.0 + 1e-7).all()

    def test_zeros_not_positive(self):
        data = np.zeros(20, dtype=np.float32)
        result = fe_ops.rolling_posratio(data, 5)
        valid = ~np.isnan(result)
        np.testing.assert_allclose(result[valid], 0.0, atol=1e-7)

    def test_half_positive(self):
        data = np.array([-1, 1, -1, 1, -1, 1, -1, 1, -1, 1], dtype=np.float32)
        result = fe_ops.rolling_posratio(data, 10)
        assert result[-1] == pytest.approx(0.5, abs=1e-6)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_with_nans(self, w):
        rng = np.random.default_rng(77)
        data = rng.standard_normal(N).astype(np.float32)
        data[rng.random(N) < 0.1] = np.nan
        batch = fe_ops.rolling_posratio(data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_POS_RATIO, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch) & ~np.isnan(push_arr)
        if valid.any():
            np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)


# ═══════════════════════════════════════════════════════════════
#  TS_UP_DOWN_RATIO tests
# ═══════════════════════════════════════════════════════════════

class TestUpDownRatioBatchAlignment:

    @pytest.mark.parametrize("w", WINDOWS)
    def test_batch_vs_pandas(self, w):
        rng = np.random.default_rng(42)
        data = rng.standard_normal(N).astype(np.float32)
        cpp = fe_ops.rolling_updown_ratio(data, w)
        ref = _pandas_updown_ratio(data, w)
        valid = ~np.isnan(ref) & ~np.isnan(cpp)
        if valid.any():
            np.testing.assert_allclose(cpp[valid], ref[valid], atol=1e-4, rtol=1e-3)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_vs_batch(self, w):
        rng = np.random.default_rng(99)
        data = rng.standard_normal(N).astype(np.float32)
        batch = fe_ops.rolling_updown_ratio(data, w)

        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_UP_DOWN_RATIO, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)

        valid = ~np.isnan(batch) & ~np.isnan(push_arr)
        if valid.any():
            np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-4)


class TestUpDownRatioEdgeCases:

    def test_window_0_graph_rejected(self):
        with pytest.raises(Exception):
            g = rt.FactorGraph()
            c = g.add_input("close")
            g.add_rolling(rt.Op.TS_UP_DOWN_RATIO, c, 0)

    def test_all_positive_returns_nan(self):
        data = np.arange(1, 51, dtype=np.float32)
        result = fe_ops.rolling_updown_ratio(data, 10)
        valid = ~np.isnan(result)
        assert not valid[9:].any(), "All-positive window has no negatives → NaN"

    def test_all_negative_returns_zero(self):
        data = -np.arange(1, 51, dtype=np.float32)
        result = fe_ops.rolling_updown_ratio(data, 10)
        valid = ~np.isnan(result)
        if valid.any():
            np.testing.assert_allclose(result[valid], 0.0, atol=1e-7)

    def test_symmetric_returns_one(self):
        data = np.array([-1, 1, -1, 1, -1, 1, -1, 1, -1, 1], dtype=np.float32)
        result = fe_ops.rolling_updown_ratio(data, 10)
        assert result[-1] == pytest.approx(1.0, abs=1e-6)

    def test_output_positive(self):
        rng = np.random.default_rng(7)
        data = rng.standard_normal(1000).astype(np.float32)
        result = fe_ops.rolling_updown_ratio(data, 14)
        valid = result[~np.isnan(result)]
        assert (valid >= 0.0 - 1e-7).all()

    @pytest.mark.parametrize("w", WINDOWS)
    def test_with_nans(self, w):
        rng = np.random.default_rng(77)
        data = rng.standard_normal(N).astype(np.float32)
        data[rng.random(N) < 0.1] = np.nan
        batch = fe_ops.rolling_updown_ratio(data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_UP_DOWN_RATIO, c, w)
        g.compile()
        push_out = []
        for v in data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch) & ~np.isnan(push_arr)
        if valid.any():
            np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-4)
