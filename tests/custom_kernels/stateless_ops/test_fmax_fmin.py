"""
Alignment tests for FMAX and FMIN binary ops.

FMAX: element-wise max(a, b), NaN if either input is NaN.
FMIN: element-wise min(a, b), NaN if either input is NaN.
"""

import numpy as np
import pytest

import fe_ops
import fe_runtime as rt


N = 500


class TestFmaxBatch:

    def test_basic(self):
        a = np.array([1, 5, 3, -2, 0], dtype=np.float32)
        b = np.array([4, 2, 3, -1, 0], dtype=np.float32)
        result = fe_ops.fmax(a, b)
        expected = np.maximum(a, b)
        np.testing.assert_array_equal(result, expected)

    def test_nan_propagation(self):
        a = np.array([np.nan, 2.0, 3.0], dtype=np.float32)
        b = np.array([1.0, np.nan, 3.0], dtype=np.float32)
        result = fe_ops.fmax(a, b)
        assert np.isnan(result[0])
        assert np.isnan(result[1])
        assert result[2] == 3.0

    def test_large_random(self):
        rng = np.random.default_rng(42)
        a = rng.standard_normal(N).astype(np.float32)
        b = rng.standard_normal(N).astype(np.float32)
        result = fe_ops.fmax(a, b)
        expected = np.maximum(a, b)
        np.testing.assert_array_equal(result, expected)

    def test_identical_inputs(self):
        a = np.arange(10, dtype=np.float32)
        result = fe_ops.fmax(a, a)
        np.testing.assert_array_equal(result, a)


class TestFminBatch:

    def test_basic(self):
        a = np.array([1, 5, 3, -2, 0], dtype=np.float32)
        b = np.array([4, 2, 3, -1, 0], dtype=np.float32)
        result = fe_ops.fmin(a, b)
        expected = np.minimum(a, b)
        np.testing.assert_array_equal(result, expected)

    def test_nan_propagation(self):
        a = np.array([np.nan, 2.0, 3.0], dtype=np.float32)
        b = np.array([1.0, np.nan, 3.0], dtype=np.float32)
        result = fe_ops.fmin(a, b)
        assert np.isnan(result[0])
        assert np.isnan(result[1])
        assert result[2] == 3.0

    def test_large_random(self):
        rng = np.random.default_rng(42)
        a = rng.standard_normal(N).astype(np.float32)
        b = rng.standard_normal(N).astype(np.float32)
        result = fe_ops.fmin(a, b)
        expected = np.minimum(a, b)
        np.testing.assert_array_equal(result, expected)


class TestFmaxFminPush:

    def test_fmax_push(self):
        g = rt.FactorGraph()
        a = g.add_input("close")
        b = g.add_input("volume")
        g.add_binary(rt.Op.FMAX, a, b)
        g.compile()

        pairs = [(3.0, 5.0, 5.0), (-1.0, -2.0, -1.0), (0.0, 0.0, 0.0)]
        for va, vb, expected in pairs:
            g.push_bar(va, vb)
            assert g.output() == pytest.approx(expected, abs=1e-6)

    def test_fmin_push(self):
        g = rt.FactorGraph()
        a = g.add_input("close")
        b = g.add_input("volume")
        g.add_binary(rt.Op.FMIN, a, b)
        g.compile()

        pairs = [(3.0, 5.0, 3.0), (-1.0, -2.0, -2.0), (0.0, 0.0, 0.0)]
        for va, vb, expected in pairs:
            g.push_bar(va, vb)
            assert g.output() == pytest.approx(expected, abs=1e-6)
