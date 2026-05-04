"""
Alignment tests for SQRT and SIGN_SQRT unary ops.

SQRT: sqrt(x), NaN for negative inputs.
SIGN_SQRT: sign(x) * sqrt(|x|), symmetric compression.
"""

import math

import numpy as np
import pytest

import fe_ops
import fe_runtime as rt


N = 500


class TestSqrtBatch:

    def test_positive_values(self):
        data = np.array([0.0, 1.0, 4.0, 9.0, 16.0, 100.0], dtype=np.float32)
        result = fe_ops.sqrt_op(data)
        expected = np.sqrt(data)
        np.testing.assert_allclose(result, expected, atol=1e-6)

    def test_negative_returns_nan(self):
        data = np.array([-1.0, -4.0, -0.01], dtype=np.float32)
        result = fe_ops.sqrt_op(data)
        assert np.isnan(result).all()

    def test_nan_passthrough(self):
        data = np.array([np.nan, 4.0, np.nan], dtype=np.float32)
        result = fe_ops.sqrt_op(data)
        assert np.isnan(result[0])
        assert result[1] == pytest.approx(2.0, abs=1e-6)
        assert np.isnan(result[2])

    def test_large_random(self):
        rng = np.random.default_rng(42)
        data = np.abs(rng.standard_normal(N)).astype(np.float32) * 100
        result = fe_ops.sqrt_op(data)
        expected = np.sqrt(data.astype(np.float64)).astype(np.float32)
        np.testing.assert_allclose(result, expected, atol=1e-4, rtol=1e-5)


class TestSqrtPush:

    def test_push_matches_batch(self):
        rng = np.random.default_rng(99)
        data = np.abs(rng.standard_normal(100)).astype(np.float32) * 10

        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_unary(rt.Op.SQRT, c)
        g.compile()

        for i, v in enumerate(data):
            g.push_bar(float(v))
            expected = math.sqrt(v)
            assert g.output() == pytest.approx(expected, abs=1e-4)


class TestSignSqrtBatch:

    def test_positive_values(self):
        data = np.array([4.0, 9.0, 16.0], dtype=np.float32)
        result = fe_ops.sign_sqrt(data)
        expected = np.sqrt(data)
        np.testing.assert_allclose(result, expected, atol=1e-6)

    def test_negative_values(self):
        data = np.array([-4.0, -9.0, -16.0], dtype=np.float32)
        result = fe_ops.sign_sqrt(data)
        expected = -np.sqrt(np.abs(data))
        np.testing.assert_allclose(result, expected, atol=1e-6)

    def test_zero(self):
        result = fe_ops.sign_sqrt(np.array([0.0], dtype=np.float32))
        assert result[0] == pytest.approx(0.0, abs=1e-8)

    def test_symmetry(self):
        """sign_sqrt(-x) == -sign_sqrt(x)"""
        rng = np.random.default_rng(42)
        data = rng.standard_normal(N).astype(np.float32)
        pos = fe_ops.sign_sqrt(np.abs(data))
        neg = fe_ops.sign_sqrt(-np.abs(data))
        np.testing.assert_allclose(neg, -pos, atol=1e-6)

    def test_nan_passthrough(self):
        data = np.array([np.nan, 4.0, np.nan], dtype=np.float32)
        result = fe_ops.sign_sqrt(data)
        assert np.isnan(result[0])
        assert result[1] == pytest.approx(2.0, abs=1e-6)
        assert np.isnan(result[2])


class TestSignSqrtPush:

    def test_push_matches_batch(self):
        rng = np.random.default_rng(77)
        data = rng.standard_normal(100).astype(np.float32)

        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_unary(rt.Op.SIGN_SQRT, c)
        g.compile()

        batch = fe_ops.sign_sqrt(data)
        for i, v in enumerate(data):
            g.push_bar(float(v))
            assert g.output() == pytest.approx(float(batch[i]), abs=1e-5)
