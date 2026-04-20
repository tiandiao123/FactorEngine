"""
End-to-end alignment tests for real factor expressions from the factor bank.

Each test:
  1. Generates random OHLCV data (single symbol, N bars)
  2. Computes the factor in Python (pandas) as ground truth
  3. Builds a FactorGraph, pushes the same data bar-by-bar
  4. Compares C++ streaming output vs pandas batch output
"""
import numpy as np
import pandas as pd
import pytest

import fe_runtime as rt

Op = rt.Op
EPS = 1e-8
N = 600
SEEDS = [42, 123, 7]


def make_ohlcv(seed: int, n: int = N):
    """Generate synthetic OHLCV data."""
    rng = np.random.RandomState(seed)
    close = 100.0 + np.cumsum(rng.randn(n) * 0.5).astype(np.float32)
    volume = (1000 + rng.rand(n) * 5000).astype(np.float32)
    noise = rng.rand(n).astype(np.float32)
    high = close + np.abs(rng.randn(n) * 0.3).astype(np.float32)
    low = close - np.abs(rng.randn(n) * 0.3).astype(np.float32)
    open_ = low + noise * (high - low)
    ret = np.zeros(n, dtype=np.float32)
    ret[1:] = close[1:] / close[:-1] - 1.0
    return close, volume, open_, high, low, ret


def push_all(g: rt.FactorGraph, close, volume, open_, high, low, ret):
    """Push all bars and collect raw outputs."""
    n = len(close)
    out = np.full(n, np.nan, dtype=np.float32)
    for i in range(n):
        g.push_bar(close[i], volume[i], open_[i], high[i], low[i], ret[i])
        out[i] = g.raw_output()
    return out


def clean_factor(x):
    """Replicate factorlib clean_factor: inf→NaN, NaN→0."""
    return np.where(np.isfinite(x), x, 0.0).astype(np.float32)


def assert_aligned(cpp_arr, py_arr, label="", atol=1e-3, rtol=1e-3):
    """Compare after applying clean_factor to both sides."""
    cpp_clean = clean_factor(cpp_arr)
    py_clean = clean_factor(py_arr)
    mask = (cpp_clean != 0.0) | (py_clean != 0.0)
    if mask.sum() == 0:
        return
    np.testing.assert_allclose(
        cpp_clean[mask], py_clean[mask],
        atol=atol, rtol=rtol,
        err_msg=f"Factor {label}: mismatch on {mask.sum()} non-zero values",
    )


# ═══════════════════════════════════════════════════════════════
#  Factor 0001: Div(Sub(close, Ma(close, 120)), TsStd(close, 60))
# ═══════════════════════════════════════════════════════════════

class TestFactor0001:
    @staticmethod
    def build_graph():
        g = rt.FactorGraph()
        c = g.add_input("close")
        ma120 = g.add_rolling(Op.MA, c, 120)
        dev = g.add_binary(Op.SUB, c, ma120)
        vol = g.add_rolling(Op.TS_STD, c, 60)
        g.add_binary(Op.DIV, dev, vol)
        g.compile()
        return g

    @staticmethod
    def pandas_ref(close):
        s = pd.Series(close)
        ma = s.rolling(120, min_periods=120).mean()
        dev = s - ma
        vol = s.rolling(60, min_periods=60).std(ddof=0)
        return (dev / vol).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = self.build_graph()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close)
        assert_aligned(cpp, py, "0001")


# ═══════════════════════════════════════════════════════════════
#  Factor 0050: Neg(Corr(TsRank(ret, 30), TsRank(volume, 30), 120))
# ═══════════════════════════════════════════════════════════════

class TestFactor0050:
    @staticmethod
    def build_graph():
        g = rt.FactorGraph()
        c = g.add_input("close")
        v = g.add_input("volume")
        # ret = pct_change(close) — use PCT_CHANGE op
        pct = g.add_rolling(Op.PCT_CHANGE, c, 1)
        rr = g.add_rolling(Op.TS_RANK, pct, 30)
        vr = g.add_rolling(Op.TS_RANK, v, 30)
        corr = g.add_bivariate(Op.CORR, rr, vr, 120)
        g.add_unary(Op.NEG, corr)
        g.compile()
        return g

    @staticmethod
    def pandas_ref(close, volume):
        s_close = pd.Series(close)
        s_vol = pd.Series(volume)
        ret = s_close.pct_change()
        rr = ret.rolling(30, min_periods=30).apply(
            lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False
        )
        vr = s_vol.rolling(30, min_periods=30).apply(
            lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False
        )
        corr = rr.rolling(120, min_periods=120).corr(vr)
        return (-corr).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = self.build_graph()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0050", atol=5e-2, rtol=5e-2)


# ═══════════════════════════════════════════════════════════════
#  Factor 0010: Neg(TsRank(Div(Div(Sub(close, Ma(close,10)),
#               TsStd(close,60)), SLog1p(Div(Ma(vol,5),Ma(vol,120)))), 180))
# ═══════════════════════════════════════════════════════════════

class TestFactor0010:
    @staticmethod
    def build_graph():
        g = rt.FactorGraph()
        c = g.add_input("close")
        v = g.add_input("volume")
        # price convexity: (close - Ma(close,10)) / TsStd(close,60)
        ma10 = g.add_rolling(Op.MA, c, 10)
        dev = g.add_binary(Op.SUB, c, ma10)
        vol60 = g.add_rolling(Op.TS_STD, c, 60)
        price_conv = g.add_binary(Op.DIV, dev, vol60)
        # volume surge: SLog1p(Ma(volume,5) / Ma(volume,120))
        vol_short = g.add_rolling(Op.MA, v, 5)
        vol_long = g.add_rolling(Op.MA, v, 120)
        vol_ratio = g.add_binary(Op.DIV, vol_short, vol_long)
        vol_slog = g.add_unary(Op.SLOG1P, vol_ratio)
        # signal
        raw = g.add_binary(Op.DIV, price_conv, vol_slog)
        ranked = g.add_rolling(Op.TS_RANK, raw, 180)
        g.add_unary(Op.NEG, ranked)
        g.compile()
        return g

    @staticmethod
    def pandas_ref(close, volume):
        sc = pd.Series(close)
        sv = pd.Series(volume)
        ma10 = sc.rolling(10, min_periods=10).mean()
        dev = sc - ma10
        vol60 = sc.rolling(60, min_periods=60).std(ddof=0)
        price_conv = dev / vol60
        vol_short = sv.rolling(5, min_periods=5).mean()
        vol_long = sv.rolling(120, min_periods=120).mean()
        vol_ratio = vol_short / vol_long
        vol_slog = np.sign(vol_ratio) * np.log1p(np.abs(vol_ratio))
        raw = price_conv / vol_slog
        ranked = raw.rolling(180, min_periods=180).apply(
            lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False
        )
        return (-ranked).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = self.build_graph()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0010", atol=5e-2, rtol=5e-2)


# ═══════════════════════════════════════════════════════════════
#  Factor 0020: Neg(TsZscore(Mul(Sub(range_pos, 0.5), vol_ratio), 240))
#  range_pos = Div(Sub(close, TsMin(low,120)),
#                  Sub(TsMax(high,120), TsMin(low,120)))
#  vol_ratio = Div(Ma(volume,15), Ma(volume,120))
# ═══════════════════════════════════════════════════════════════

class TestFactor0020:
    @staticmethod
    def build_graph():
        g = rt.FactorGraph()
        c = g.add_input("close")
        h = g.add_input("high")
        lo = g.add_input("low")
        v = g.add_input("volume")
        # rolling range
        rh = g.add_rolling(Op.TS_MAX, h, 120)
        rl = g.add_rolling(Op.TS_MIN, lo, 120)
        rng = g.add_binary(Op.SUB, rh, rl)
        pos = g.add_binary(Op.DIV, g.add_binary(Op.SUB, c, rl), rng)
        centered = g.add_scalar_op(Op.SUB_SCALAR, pos, 0.5)
        # volume ratio
        vs = g.add_rolling(Op.MA, v, 15)
        vl = g.add_rolling(Op.MA, v, 120)
        vr = g.add_binary(Op.DIV, vs, vl)
        # interaction
        raw = g.add_binary(Op.MUL, centered, vr)
        zs = g.add_rolling(Op.TS_ZSCORE, raw, 240)
        g.add_unary(Op.NEG, zs)
        g.compile()
        return g

    @staticmethod
    def pandas_ref(close, volume, high, low):
        sc = pd.Series(close)
        sh = pd.Series(high)
        sl = pd.Series(low)
        sv = pd.Series(volume)
        rh = sh.rolling(120, min_periods=120).max()
        rl = sl.rolling(120, min_periods=120).min()
        rng = rh - rl
        pos = (sc - rl) / rng
        centered = pos - 0.5
        vs = sv.rolling(15, min_periods=15).mean()
        vl = sv.rolling(120, min_periods=120).mean()
        vr = vs / vl
        raw = centered * vr
        mean_raw = raw.rolling(240, min_periods=240).mean()
        std_raw = raw.rolling(240, min_periods=240).std(ddof=0)
        zs = (raw - mean_raw) / (std_raw + EPS)
        return (-zs).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = self.build_graph()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, high, low)
        assert_aligned(cpp, py, "0020")


# ═══════════════════════════════════════════════════════════════
#  Factor 0100: Neg(Div(Ma(Sub(TsRank(close,180),TsRank(vol,180)),30),
#                       TsStd(Sub(TsRank(close,180),TsRank(vol,180)),360)))
# ═══════════════════════════════════════════════════════════════

class TestFactor0100:
    @staticmethod
    def build_graph():
        g = rt.FactorGraph()
        c = g.add_input("close")
        v = g.add_input("volume")
        pr = g.add_rolling(Op.TS_RANK, c, 180)
        vr = g.add_rolling(Op.TS_RANK, v, 180)
        div_ = g.add_binary(Op.SUB, pr, vr)
        smooth = g.add_rolling(Op.MA, div_, 30)
        std = g.add_rolling(Op.TS_STD, div_, 360)
        norm = g.add_binary(Op.DIV, smooth, std)
        g.add_unary(Op.NEG, norm)
        g.compile()
        return g

    @staticmethod
    def pandas_ref(close, volume):
        sc = pd.Series(close)
        sv = pd.Series(volume)
        pr = sc.rolling(180, min_periods=180).apply(
            lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False
        )
        vr = sv.rolling(180, min_periods=180).apply(
            lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False
        )
        div_ = pr - vr
        smooth = div_.rolling(30, min_periods=30).mean()
        std = div_.rolling(360, min_periods=360).std(ddof=0)
        norm = smooth / std
        return (-norm).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed, n=800)
        g = self.build_graph()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0100", atol=5e-2, rtol=5e-2)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
