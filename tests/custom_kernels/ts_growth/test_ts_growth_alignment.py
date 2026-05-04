"""
Alignment tests for TS_GROWTH (= TS_PCT alias: (x - x[t-n]) / x[t-n]).

TS_GROWTH is semantically identical to TS_PCT. These tests verify:
1. Batch rolling_growth matches fe_ops.ts_pct
2. Push-level TS_GROWTH Op matches batch
3. FactorGraph Op.TS_GROWTH produces same results as Op.TS_PCT
"""

import numpy as np
import pytest

import fe_ops
import fe_runtime as rt


WINDOWS = [1, 2, 5, 10, 30]
N = 500


class TestGrowthBatchAlignment:
    @pytest.fixture(autouse=True)
    def _data(self):
        rng = np.random.default_rng(42)
        self.data = rng.standard_normal(N).astype(np.float32).cumsum() + 100.0

    @pytest.mark.parametrize("w", WINDOWS)
    def test_growth_equals_ts_pct(self, w):
        """rolling_growth should produce identical output to ts_pct."""
        growth = fe_ops.rolling_growth(self.data, w)
        pct = fe_ops.ts_pct(self.data, w)
        np.testing.assert_array_equal(growth, pct)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_push_vs_batch(self, w):
        batch = fe_ops.rolling_growth(self.data, w)
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_GROWTH, c, w)
        g.compile()
        push_out = []
        for v in self.data:
            g.push_bar(float(v))
            push_out.append(g.output())
        push_arr = np.array(push_out, dtype=np.float32)
        valid = ~np.isnan(batch)
        np.testing.assert_allclose(push_arr[valid], batch[valid], atol=1e-5)

    @pytest.mark.parametrize("w", WINDOWS)
    def test_growth_op_equals_pct_op(self, w):
        """TS_GROWTH Op should produce same output as TS_PCT Op."""
        g1 = rt.FactorGraph()
        c1 = g1.add_input("close")
        g1.add_rolling(rt.Op.TS_GROWTH, c1, w)
        g1.compile()

        g2 = rt.FactorGraph()
        c2 = g2.add_input("close")
        g2.add_rolling(rt.Op.TS_PCT, c2, w)
        g2.compile()

        out1, out2 = [], []
        for v in self.data:
            g1.push_bar(float(v))
            g2.push_bar(float(v))
            out1.append(g1.output())
            out2.append(g2.output())

        np.testing.assert_array_equal(np.array(out1), np.array(out2))


class TestGrowthEdgeCases:
    def test_warmup_nan(self):
        data = np.arange(20, dtype=np.float32) + 1.0
        result = fe_ops.rolling_growth(data, 5)
        assert np.isnan(result[:5]).all()
        assert not np.isnan(result[5:]).any()

    def test_growth_formula(self):
        """Verify (x - x[t-n]) / (x[t-n] + eps) formula."""
        data = np.array([100, 110, 121, 133.1, 146.41], dtype=np.float32)
        result = fe_ops.rolling_growth(data, 1)
        np.testing.assert_allclose(result[1:], 0.1, atol=1e-4)
