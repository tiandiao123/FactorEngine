"""
End-to-end factor alignment test for TS_BETA-based trades factors.

Verifies that C++ streaming factor output matches pandas batch computation
for T0009 (Volume-Price Beta) and T0010 (Imbalance-Close Beta).

Usage:
    cd FactorEngine
    pytest tests/custom_kernels/ts_beta/test_ts_beta_trade_factor.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import fe_runtime as rt
from factorengine.factors.okx_perp_trades.factor_bank import (
    build_volume_price_beta,
    build_imbalance_close_beta,
)

Op = rt.Op
EPS = 1e-8
N = 800
SEEDS = [42, 123]
ATOL = 5e-2
RTOL = 5e-2


def make_trades_data(seed: int, n: int = N):
    rng = np.random.RandomState(seed)
    close = 100.0 + np.cumsum(rng.randn(n) * 0.5).astype(np.float32)
    volume = (1000 + rng.rand(n) * 5000).astype(np.float32)
    high = close + np.abs(rng.randn(n) * 0.3).astype(np.float32)
    low = close - np.abs(rng.randn(n) * 0.3).astype(np.float32)
    open_ = low + rng.rand(n).astype(np.float32) * (high - low)
    ret = np.zeros(n, dtype=np.float32)
    ret[1:] = close[1:] / close[:-1] - 1.0
    vwap = close + (rng.randn(n) * 0.1).astype(np.float32)
    buy_vol = (500 + rng.rand(n) * 3000).astype(np.float32)
    sell_vol = (500 + rng.rand(n) * 3000).astype(np.float32)
    return close, volume, open_, high, low, ret, vwap, buy_vol, sell_vol


def push_all(g: rt.FactorGraph, close, volume, open_, high, low, ret,
             vwap, buy_vol, sell_vol):
    n = len(close)
    out = np.full(n, np.nan, dtype=np.float32)
    for i in range(n):
        g.push_bar(float(close[i]), float(volume[i]),
                    float(open_[i]), float(high[i]), float(low[i]),
                    float(ret[i]), float(vwap[i]),
                    float(buy_vol[i]), float(sell_vol[i]))
        out[i] = g.raw_output()
    return out


def slog1p(x):
    return np.sign(x) * np.log1p(np.abs(x))


def ts_zscore(s: pd.Series, w: int) -> pd.Series:
    mu = s.rolling(w, min_periods=w).mean()
    sd = s.rolling(w, min_periods=w).std(ddof=0)
    return (s - mu) / (sd + EPS)


def ts_rank(s: pd.Series, w: int) -> pd.Series:
    return s.rolling(w, min_periods=w).apply(
        lambda a: pd.Series(a).rank(pct=True).iloc[-1], raw=False
    )


def ts_beta(x: pd.Series, y: pd.Series, w: int) -> pd.Series:
    cov = x.rolling(w, min_periods=w).cov(y)
    var = x.rolling(w, min_periods=w).var(ddof=1)
    return cov / var


def clean_factor(x):
    return np.where(np.isfinite(x), x, 0.0).astype(np.float32)


def assert_aligned(cpp_arr, py_arr, label="", atol=ATOL, rtol=RTOL):
    cpp = np.asarray(cpp_arr, dtype=np.float32)
    py = np.asarray(py_arr, dtype=np.float32)
    cpp_nonfinite = ~np.isfinite(cpp)
    py_nonfinite = ~np.isfinite(py)
    np.testing.assert_array_equal(
        cpp_nonfinite, py_nonfinite,
        err_msg=f"Factor {label}: non-finite pattern mismatch",
    )
    cpp_c = clean_factor(cpp)
    py_c = clean_factor(py)
    np.testing.assert_allclose(
        cpp_c, py_c, atol=atol, rtol=rtol,
        err_msg=f"Factor {label}: value mismatch",
    )


# ═══════════════════════════════════════════════════════════════
#  T0009: Volume-Price Beta
# ═══════════════════════════════════════════════════════════════

class TestFactorT0009:
    @staticmethod
    def pandas_ref(volume, ret):
        svol = pd.Series(slog1p(volume))
        r = pd.Series(ret)
        beta = ts_beta(svol, r, 60)
        return ts_zscore(beta, 120).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret, vwap, buy_vol, sell_vol = \
            make_trades_data(seed)
        g = build_volume_price_beta()
        cpp = push_all(g, close, volume, open_, high, low, ret,
                       vwap, buy_vol, sell_vol)
        py = self.pandas_ref(volume, ret)
        assert_aligned(cpp, py, "T0009")


# ═══════════════════════════════════════════════════════════════
#  T0010: Imbalance-Close Beta
# ═══════════════════════════════════════════════════════════════

class TestFactorT0010:
    @staticmethod
    def pandas_ref(buy_vol, sell_vol, close):
        bv = pd.Series(buy_vol)
        sv = pd.Series(sell_vol)
        imbalance = (bv - sv) / (bv + sv)
        pct = pd.Series(close).pct_change(1)
        beta = ts_beta(imbalance, pct, 30)
        ranked = ts_rank(beta, 120)
        return (-ranked).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret, vwap, buy_vol, sell_vol = \
            make_trades_data(seed)
        g = build_imbalance_close_beta()
        cpp = push_all(g, close, volume, open_, high, low, ret,
                       vwap, buy_vol, sell_vol)
        py = self.pandas_ref(buy_vol, sell_vol, close)
        assert_aligned(cpp, py, "T0010")
