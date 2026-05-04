"""
Alignment tests for FactorGraph (DAG push-level runtime).

Verifies that building a factor as a FactorGraph and pushing bars one-by-one
produces the same result as batch pandas/numpy computation.
"""
import numpy as np
import pandas as pd
import pytest

import fe_runtime
from fe_runtime import FactorGraph, Op


# ── helpers ──────────────────────────────────────────────────────

def clean_factor(signal: pd.Series) -> pd.Series:
    return signal.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def build_and_push(graph: FactorGraph, data: dict[str, np.ndarray]):
    """Push all bars through a compiled FactorGraph, return list of outputs."""
    n = len(data["close"])
    results = []
    for i in range(n):
        kwargs = {}
        for key in ("close", "volume", "open", "high", "low", "ret"):
            if key in data:
                kwargs[key] = float(data[key][i])
        graph.push_bar(**kwargs)
        results.append(graph.output())
    return np.array(results, dtype=np.float32)


# ── test data ────────────────────────────────────────────────────

def make_random_data(n=500, seed=42):
    rng = np.random.RandomState(seed)
    close = 100.0 + np.cumsum(rng.randn(n) * 0.5).astype(np.float32)
    volume = (1e6 + rng.randn(n) * 1e5).astype(np.float32)
    return {"close": close, "volume": volume}


# ═══════════════════════════════════════════════════════════════════
#  Factor 0001: (close - Ma(close,120)) / TsStd(close,60)
# ═══════════════════════════════════════════════════════════════════

class TestFactor0001:
    @pytest.fixture
    def data(self):
        return make_random_data(500, seed=42)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        ma = close.rolling(120, min_periods=120).mean()
        std = close.rolling(60, min_periods=60).std(ddof=0)
        signal = (close - ma) / (std + 1e-8)
        return clean_factor(signal).values.astype(np.float32)

    def build_graph(self):
        g = FactorGraph()
        close = g.add_input("close")
        ma120 = g.add_rolling(Op.MA, close, 120)
        dev   = g.add_binary(Op.SUB, close, ma120)
        vol   = g.add_rolling(Op.TS_STD, close, 60)
        sig   = g.add_binary(Op.DIV, dev, vol)
        g.compile()
        return g

    def test_warmup(self):
        g = self.build_graph()
        assert g.warmup_bars() == 120
        assert g.num_nodes() == 5

    def test_alignment(self, data):
        g = self.build_graph()
        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-4, rtol=1e-4)


# ═══════════════════════════════════════════════════════════════════
#  Factor: Neg(TsRank(close, 30))
# ═══════════════════════════════════════════════════════════════════

class TestNegTsRank:
    @pytest.fixture
    def data(self):
        return make_random_data(200, seed=7)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        rank = close.rolling(30, min_periods=30).apply(
            lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False)
        signal = -rank
        return clean_factor(signal).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        rank  = g.add_rolling(Op.TS_RANK, close, 30)
        neg   = g.add_unary(Op.NEG, rank)
        g.compile()
        assert g.warmup_bars() == 30

        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-4, rtol=1e-4)


# ═══════════════════════════════════════════════════════════════════
#  Factor: Ma(Sub(close, Delay(close, 5)), 30)
# ═══════════════════════════════════════════════════════════════════

class TestMaDelay:
    @pytest.fixture
    def data(self):
        return make_random_data(300, seed=99)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        diff = close - close.shift(5)
        ma = diff.rolling(30, min_periods=30).mean()
        return clean_factor(ma).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        delayed = g.add_rolling(Op.DELAY, close, 5)
        diff = g.add_binary(Op.SUB, close, delayed)
        ma = g.add_rolling(Op.MA, diff, 30)
        g.compile()
        assert g.warmup_bars() == 35

        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-4, rtol=1e-4)


# ═══════════════════════════════════════════════════════════════════
#  Factor: TsZscore(Ma(close, 30), 60)
# ═══════════════════════════════════════════════════════════════════

class TestTsZscoreMa:
    @pytest.fixture
    def data(self):
        return make_random_data(400, seed=123)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        ma30 = close.rolling(30, min_periods=30).mean()
        mean60 = ma30.rolling(60, min_periods=60).mean()
        std60 = ma30.rolling(60, min_periods=60).std(ddof=0)
        signal = (ma30 - mean60) / (std60 + 1e-8)
        return clean_factor(signal).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        ma30  = g.add_rolling(Op.MA, close, 30)
        zs    = g.add_rolling(Op.TS_ZSCORE, ma30, 60)
        g.compile()
        assert g.warmup_bars() == 90

        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-4, rtol=1e-4)


# ═══════════════════════════════════════════════════════════════════
#  Factor: Div(Ema(close, 30), Ma(close, 60))
# ═══════════════════════════════════════════════════════════════════

class TestEmaDiv:
    @pytest.fixture
    def data(self):
        return make_random_data(300, seed=55)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        ema30 = close.ewm(span=30, min_periods=30, adjust=False).mean()
        ma60  = close.rolling(60, min_periods=60).mean()
        signal = ema30 / (ma60 + 1e-8)
        return clean_factor(signal).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        ema   = g.add_rolling(Op.EMA, close, 30)
        ma    = g.add_rolling(Op.MA, close, 60)
        div   = g.add_binary(Op.DIV, ema, ma)
        g.compile()
        assert g.warmup_bars() == 60

        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-4, rtol=1e-4)


# ═══════════════════════════════════════════════════════════════════
#  Factor with scalar: Sub(1.0, TsRank(close, 60))
# ═══════════════════════════════════════════════════════════════════

class TestScalarSub:
    @pytest.fixture
    def data(self):
        return make_random_data(200, seed=77)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        rank = close.rolling(60, min_periods=60).apply(
            lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False)
        signal = 1.0 - rank
        return clean_factor(signal).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        rank  = g.add_rolling(Op.TS_RANK, close, 60)
        out   = g.add_scalar_op(Op.SCALAR_SUB, rank, 1.0)
        g.compile()
        assert g.warmup_bars() == 60

        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-4, rtol=1e-4)


# ═══════════════════════════════════════════════════════════════════
#  Factor: Corr(close, volume, 60)
# ═══════════════════════════════════════════════════════════════════

class TestCorr:
    @pytest.fixture
    def data(self):
        return make_random_data(300, seed=33)

    def python_ref(self, data):
        close  = pd.Series(data["close"])
        volume = pd.Series(data["volume"])
        signal = close.rolling(60, min_periods=60).corr(volume)
        return clean_factor(signal).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        vol   = g.add_input("volume")
        corr  = g.add_bivariate(Op.CORR, close, vol, 60)
        g.compile()
        assert g.warmup_bars() == 60

        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-3, rtol=1e-3)


# ═══════════════════════════════════════════════════════════════════
#  Factor: TsMed(close, 30)
# ═══════════════════════════════════════════════════════════════════

class TestTsMed:
    @pytest.fixture
    def data(self):
        return make_random_data(200, seed=11)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        signal = close.rolling(30, min_periods=30).median()
        return clean_factor(signal).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        med   = g.add_rolling(Op.TS_MED, close, 30)
        g.compile()

        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-4, rtol=1e-4)


# ═══════════════════════════════════════════════════════════════════
#  Factor: TsMin(close, 30) + simple chain
# ═══════════════════════════════════════════════════════════════════

class TestTsMinSub:
    @pytest.fixture
    def data(self):
        return make_random_data(200, seed=22)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        ts_min = close.rolling(30, min_periods=30).min()
        signal = close - ts_min
        return clean_factor(signal).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        mn    = g.add_rolling(Op.TS_MIN, close, 30)
        diff  = g.add_binary(Op.SUB, close, mn)
        g.compile()

        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-4, rtol=1e-4)


# ═══════════════════════════════════════════════════════════════════
#  Factor: TsSkew(close, 60)
# ═══════════════════════════════════════════════════════════════════

class TestTsSkew:
    @pytest.fixture
    def data(self):
        return make_random_data(300, seed=88)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        signal = close.rolling(60, min_periods=60).skew()
        return clean_factor(signal).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        skew  = g.add_rolling(Op.TS_SKEW, close, 60)
        g.compile()

        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-3, rtol=1e-3)


# ═══════════════════════════════════════════════════════════════════
#  Complex composite: TsRank(Div(Ma(close,30), TsStd(close,60)), 120)
# ═══════════════════════════════════════════════════════════════════

class TestComplexComposite:
    @pytest.fixture
    def data(self):
        return make_random_data(500, seed=1234)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        ma30  = close.rolling(30, min_periods=30).mean()
        std60 = close.rolling(60, min_periods=60).std(ddof=0)
        ratio = ma30 / (std60 + 1e-8)
        rank  = ratio.rolling(120, min_periods=120).apply(
            lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False)
        return clean_factor(rank).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        ma30  = g.add_rolling(Op.MA, close, 30)
        std60 = g.add_rolling(Op.TS_STD, close, 60)
        ratio = g.add_binary(Op.DIV, ma30, std60)
        rank  = g.add_rolling(Op.TS_RANK, ratio, 120)
        g.compile()
        assert g.warmup_bars() == 180

        cpp = build_and_push(g, data)
        py  = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-3, rtol=1e-3)


# ═══════════════════════════════════════════════════════════════════
#  Test: Log, Sqr, Abs, Sign, Inv unary ops
# ═══════════════════════════════════════════════════════════════════

class TestUnaryOps:
    @pytest.fixture
    def data(self):
        return make_random_data(100, seed=5)

    def test_neg(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        g.add_unary(Op.NEG, close)
        g.compile()
        cpp = build_and_push(g, data)
        py = clean_factor(pd.Series(-data["close"])).values.astype(np.float32)
        np.testing.assert_allclose(cpp, py, atol=1e-6)

    def test_sqr(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        g.add_unary(Op.SQR, close)
        g.compile()
        cpp = build_and_push(g, data)
        py = clean_factor(pd.Series(data["close"] ** 2)).values.astype(np.float32)
        np.testing.assert_allclose(cpp, py, atol=1e-1)  # float32 precision

    def test_abs(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        g.add_unary(Op.ABS, close)
        g.compile()
        cpp = build_and_push(g, data)
        py = clean_factor(pd.Series(np.abs(data["close"]))).values.astype(np.float32)
        np.testing.assert_allclose(cpp, py, atol=1e-6)


# ═══════════════════════════════════════════════════════════════════
#  Test: reset() works correctly
# ═══════════════════════════════════════════════════════════════════

class TestReset:
    def test_reset_produces_same_result(self):
        data = make_random_data(200, seed=42)
        g = FactorGraph()
        close = g.add_input("close")
        g.add_rolling(Op.MA, close, 30)
        g.compile()

        # First run
        r1 = build_and_push(g, data)

        # Reset and run again
        g.reset()
        r2 = build_and_push(g, data)

        np.testing.assert_array_equal(r1, r2)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
