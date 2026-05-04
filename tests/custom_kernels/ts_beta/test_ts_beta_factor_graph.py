"""
Single-factor sanity: Beta(close, volume, 60) as a compiled FactorGraph vs pandas.

TS_BETA(close, volume, 60) = Cov(close, volume) / Var(close),
i.e. volume regressed on close (close is the independent variable).

Usage:
    cd FactorEngine
    pytest tests/custom_kernels/ts_beta/test_ts_beta_factor_graph.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fe_runtime import FactorGraph, Op


def clean_factor(signal: pd.Series) -> pd.Series:
    return signal.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def build_and_push(graph: FactorGraph, data: dict[str, np.ndarray]) -> np.ndarray:
    n = len(data["close"])
    results = []
    for i in range(n):
        graph.push_bar(close=float(data["close"][i]), volume=float(data["volume"][i]))
        results.append(graph.output())
    return np.array(results, dtype=np.float32)


def make_random_data(n=500, seed=41):
    rng = np.random.RandomState(seed)
    close = 100.0 + np.cumsum(rng.randn(n) * 0.5).astype(np.float32)
    volume = (1e6 + rng.randn(n) * 1e5).astype(np.float32)
    return {"close": close, "volume": volume}


class TestTsBetaFactorGraph:
    @pytest.fixture
    def data(self):
        return make_random_data(500, seed=41)

    def python_ref(self, data):
        close = pd.Series(data["close"].astype(np.float64))
        volume = pd.Series(data["volume"].astype(np.float64))
        cov = close.rolling(60, min_periods=60).cov(volume)
        var = close.rolling(60, min_periods=60).var(ddof=1)
        beta = cov / var
        return clean_factor(beta).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        c = g.add_input("close")
        v = g.add_input("volume")
        g.add_bivariate(Op.TS_BETA, c, v, 60)
        g.compile()
        assert g.warmup_bars() == 60

        cpp = build_and_push(g, data)
        py = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-3, rtol=1e-3)
