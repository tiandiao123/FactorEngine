"""
Single-factor sanity: Cov(close, volume, 60) as a compiled FactorGraph vs pandas."""
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


def make_random_data(n=300, seed=41):
    rng = np.random.RandomState(seed)
    close = 100.0 + np.cumsum(rng.randn(n) * 0.5).astype(np.float32)
    volume = (1e6 + rng.randn(n) * 1e5).astype(np.float32)
    return {"close": close, "volume": volume}


class TestTsCovFactorGraphCloseVolume:
    @pytest.fixture
    def data(self):
        return make_random_data(300, seed=41)

    def python_ref(self, data):
        close = pd.Series(data["close"])
        volume = pd.Series(data["volume"])
        signal = close.rolling(60, min_periods=60).cov(volume)
        return clean_factor(signal).values.astype(np.float32)

    def test_alignment(self, data):
        g = FactorGraph()
        close = g.add_input("close")
        vol = g.add_input("volume")
        g.add_bivariate(Op.TS_COV, close, vol, 60)
        g.compile()
        assert g.warmup_bars() == 60

        cpp = build_and_push(g, data)
        py = self.python_ref(data)
        np.testing.assert_allclose(cpp, py, atol=1e-3, rtol=1e-3)
