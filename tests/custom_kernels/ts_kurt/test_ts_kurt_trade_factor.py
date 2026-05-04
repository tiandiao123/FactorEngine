"""
End-to-end factor alignment test for TS_KURT-based trades factors.

Verifies that C++ streaming factor output matches pandas batch computation
for T0007 (Imbalance Kurtosis) and T0008 (Buy Volume Kurtosis).

Usage:
    cd FactorEngine
    pytest tests/custom_kernels/ts_kurt/test_ts_kurt_trade_factor.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import fe_runtime as rt
from factorengine.factors.okx_perp_trades.factor_bank import (
    build_imbalance_kurtosis,
    build_buy_vol_kurtosis,
)

Op = rt.Op
EPS = 1e-8
N = 800
SEEDS = [42, 123]
ATOL = 5e-2
RTOL = 5e-2


# ── data generation ──────────────────────────────────────────

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


def clean_factor(x):
    return np.where(np.isfinite(x), x, 0.0).astype(np.float32)


def assert_aligned(cpp_arr, py_arr, label="", atol=ATOL, rtol=RTOL):
    cpp_c = clean_factor(cpp_arr)
    py_c = clean_factor(py_arr)
    mask = (cpp_c != 0.0) | (py_c != 0.0)
    if mask.sum() == 0:
        return
    np.testing.assert_allclose(
        cpp_c[mask], py_c[mask], atol=atol, rtol=rtol,
        err_msg=f"Factor {label}: mismatch on {mask.sum()} non-zero values",
    )


# ── pandas helpers ───────────────────────────────────────────

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


def ts_kurt(s: pd.Series, w: int) -> pd.Series:
    return s.rolling(w, min_periods=w).kurt()


# ═══════════════════════════════════════════════════════════════
#  T0007: Imbalance Kurtosis
# ═══════════════════════════════════════════════════════════════

class TestFactorT0007:
    @staticmethod
    def pandas_ref(buy_vol, sell_vol):
        bv = pd.Series(buy_vol)
        sv = pd.Series(sell_vol)
        imbalance = (bv - sv) / (bv + sv)
        kurt = ts_kurt(imbalance, 60)
        return ts_zscore(kurt, 120).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret, vwap, buy_vol, sell_vol = \
            make_trades_data(seed)
        g = build_imbalance_kurtosis()
        cpp = push_all(g, close, volume, open_, high, low, ret,
                       vwap, buy_vol, sell_vol)
        py = self.pandas_ref(buy_vol, sell_vol)
        assert_aligned(cpp, py, "T0007")


# ═══════════════════════════════════════════════════════════════
#  T0008: Buy Volume Kurtosis
# ═══════════════════════════════════════════════════════════════

class TestFactorT0008:
    @staticmethod
    def pandas_ref(buy_vol):
        bv = pd.Series(buy_vol)
        sbv = pd.Series(slog1p(bv.values))
        kurt = ts_kurt(sbv, 30)
        ranked = ts_rank(kurt, 120)
        return (-ranked).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret, vwap, buy_vol, sell_vol = \
            make_trades_data(seed)
        g = build_buy_vol_kurtosis()
        cpp = push_all(g, close, volume, open_, high, low, ret,
                       vwap, buy_vol, sell_vol)
        py = self.pandas_ref(buy_vol)
        assert_aligned(cpp, py, "T0008")
