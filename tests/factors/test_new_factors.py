"""
Alignment tests for newly registered factors (0002–0034).

Each test:
  1. Generates random OHLCV data (single symbol, N bars)
  2. Computes the factor in Python (pandas) as ground truth
  3. Builds a FactorGraph via the factor_bank builder
  4. Pushes the same data bar-by-bar
  5. Compares C++ streaming output vs pandas batch output
"""
import numpy as np
import pandas as pd
import pytest

import fe_runtime as rt
from factorengine.factors.okx_perp.factor_bank import (
    build_factor_0002, build_factor_0003, build_factor_0004,
    build_factor_0005, build_factor_0007, build_factor_0009,
    build_factor_0011, build_factor_0012, build_factor_0013,
    build_factor_0014, build_factor_0015, build_factor_0016,
    build_factor_0018, build_factor_0019, build_factor_0022,
    build_factor_0023, build_factor_0024, build_factor_0025,
    build_factor_0026, build_factor_0028, build_factor_0029,
    build_factor_0031, build_factor_0032, build_factor_0033,
    build_factor_0034,
    build_factor_0035, build_factor_0036, build_factor_0037,
    build_factor_0038, build_factor_0039, build_factor_0040,
    build_factor_0041, build_factor_0042, build_factor_0043,
    build_factor_0044, build_factor_0045, build_factor_0046,
    build_factor_0047, build_factor_0048, build_factor_0049,
    build_factor_0051, build_factor_0052, build_factor_0053,
    build_factor_0054, build_factor_0055, build_factor_0056,
    build_factor_0057, build_factor_0058, build_factor_0059,
    build_factor_0060, build_factor_0061, build_factor_0062,
    build_factor_0063, build_factor_0064, build_factor_0065,
)

Op = rt.Op
EPS = 1e-8
N = 800
SEEDS = [42, 123]
ATOL = 5e-2
RTOL = 5e-2

# ─── data generation ──────────────────────────────────────────

def make_ohlcv(seed: int, n: int = N):
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
    n = len(close)
    out = np.full(n, np.nan, dtype=np.float32)
    for i in range(n):
        g.push_bar(close[i], volume[i], open_[i], high[i], low[i], ret[i])
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


# ─── pandas helpers ───────────────────────────────────────────

def slog1p(x):
    return np.sign(x) * np.log1p(np.abs(x))


def ts_rank(s: pd.Series, w: int) -> pd.Series:
    return s.rolling(w, min_periods=w).apply(
        lambda a: pd.Series(a).rank(pct=True).iloc[-1], raw=False
    )


def ts_diff(s: pd.Series, w: int) -> pd.Series:
    return s - s.shift(w)


def ts_std(s: pd.Series, w: int) -> pd.Series:
    return s.rolling(w, min_periods=w).std(ddof=0)


def ts_zscore(s: pd.Series, w: int) -> pd.Series:
    mu = s.rolling(w, min_periods=w).mean()
    sd = ts_std(s, w)
    return (s - mu) / (sd + EPS)


def ts_sum(s: pd.Series, w: int) -> pd.Series:
    return s.rolling(w, min_periods=w).sum()


def ma(s: pd.Series, w: int) -> pd.Series:
    return s.rolling(w, min_periods=w).mean()


def ema(s: pd.Series, w: int) -> pd.Series:
    return s.ewm(span=w, adjust=False, min_periods=w).mean()


def corr(a: pd.Series, b: pd.Series, w: int) -> pd.Series:
    return a.rolling(w, min_periods=w).corr(b)


def ts_max(s: pd.Series, w: int) -> pd.Series:
    return s.rolling(w, min_periods=w).max()


def ts_min(s: pd.Series, w: int) -> pd.Series:
    return s.rolling(w, min_periods=w).min()


def delay(s: pd.Series, w: int) -> pd.Series:
    return s.shift(w)


def autocorr_pd(s: pd.Series, w: int, lag: int) -> pd.Series:
    return s.rolling(w, min_periods=w).corr(s.shift(lag))


# ═══════════════════════════════════════════════════════════════
#  Factor 0002
# ═══════════════════════════════════════════════════════════════

class TestFactor0002:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        ret30 = ts_diff(logc, 30)
        ret1 = ts_diff(logc, 1)
        vol120 = ts_std(ret1, 120)
        zscore_ret = ret30 / vol120
        vol_rank = ts_rank(pd.Series(volume), 240)
        return (zscore_ret / (-vol_rank)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0002()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0002")


# ═══════════════════════════════════════════════════════════════
#  Factor 0003
# ═══════════════════════════════════════════════════════════════

class TestFactor0003:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        velocity = ts_diff(logc, 15)
        rvol = ts_std(ret1, 60)
        move_eff = velocity / rvol
        avg_vol = ma(v, 30)
        slog_vol = slog1p(avg_vol)
        abs_vel = velocity.abs()
        price_impact = abs_vel / slog_vol
        eff_rank = ts_rank(move_eff, 240)
        impact_rank = ts_rank(price_impact, 240)
        return (eff_rank * impact_rank).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0003()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0003")


# ═══════════════════════════════════════════════════════════════
#  Factor 0004
# ═══════════════════════════════════════════════════════════════

class TestFactor0004:
    @staticmethod
    def pandas_ref(close):
        c = pd.Series(close)
        logc = np.log(c)
        ret5 = ts_diff(logc, 5)
        vol60 = ts_std(ret5, 60)
        vol_adj_ret = ret5 / vol60
        disp = ts_diff(logc, 30).abs()
        abs_ret5 = ret5.abs()
        path_len = ts_sum(abs_ret5, 6)
        efficiency = disp / path_len
        weighted_mom = vol_adj_ret * efficiency
        ranked = ts_rank(weighted_mom, 180)
        return (-ranked).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0004()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close)
        assert_aligned(cpp, py, "0004")


# ═══════════════════════════════════════════════════════════════
#  Factor 0005
# ═══════════════════════════════════════════════════════════════

class TestFactor0005:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        ret = ts_diff(logc, 1)
        logv = np.log(v)
        ema_logv = ema(logv, 5)
        vol_change = ts_diff(ema_logv, 1)
        pv_corr = corr(ret, vol_change, 120)
        rvol = ts_std(ret, 30)
        vol_regime = ts_zscore(rvol, 360)
        abs_regime = vol_regime.abs()
        divergence = (-pv_corr) * abs_regime
        return ts_rank(divergence, 240).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0005()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0005")


# ═══════════════════════════════════════════════════════════════
#  Factor 0007
# ═══════════════════════════════════════════════════════════════

class TestFactor0007:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        price_mom = ts_diff(logc, 30)
        vol_short = ma(v, 30)
        vol_med = ma(v, 120)
        vol_ratio = vol_short / vol_med
        mom_rank = ts_rank(price_mom, 240)
        vol_rank = ts_rank(vol_ratio, 240)
        divergence = mom_rank - vol_rank
        ret1 = ts_diff(logc, 1)
        rvol = ts_std(ret1, 60)
        rvol_rank = ts_rank(rvol, 360)
        signal = divergence * rvol_rank
        return (-signal).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0007()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0007")


# ═══════════════════════════════════════════════════════════════
#  Factor 0009
# ═══════════════════════════════════════════════════════════════

class TestFactor0009:
    @staticmethod
    def pandas_ref(close, high, low):
        c = pd.Series(close)
        h = pd.Series(high)
        lo_s = pd.Series(low)
        rh = ts_max(h, 30)
        rl = ts_min(lo_s, 30)
        price_range = rh - rl
        norm_range = price_range / c
        range_rank = ts_rank(norm_range, 360)
        close_pos = (c - rl) / price_range
        centered_pos = close_pos - 0.5
        comp_weight = 1.0 - range_rank
        raw_signal = centered_pos * comp_weight
        signal_z = ts_zscore(raw_signal, 120)
        return np.tanh((signal_z / 2.0).values).astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0009()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, high, low)
        assert_aligned(cpp, py, "0009")


# ═══════════════════════════════════════════════════════════════
#  Factor 0011
# ═══════════════════════════════════════════════════════════════

class TestFactor0011:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        ret5 = ts_diff(logc, 5)
        slogv = pd.Series(slog1p(v.values))
        vol_change5 = ts_diff(slogv, 5)
        pv_corr = corr(ret5, vol_change5, 60)
        divergence = -ts_rank(pv_corr, 120)
        ret1 = ts_diff(logc, 1)
        recent_vol = ts_std(ret1, 15)
        vol_rank = ts_rank(recent_vol, 120)
        return (divergence * vol_rank).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0011()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0011")


# ═══════════════════════════════════════════════════════════════
#  Factor 0012
# ═══════════════════════════════════════════════════════════════

class TestFactor0012:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        std15 = ts_std(c, 15)
        std240 = ts_std(c, 240)
        rel_vol = std15 / std240
        vol15 = ma(v, 15)
        vol240 = ma(v, 240)
        rel_vccy = vol15 / vol240
        slog_vccy = pd.Series(slog1p(rel_vccy.values))
        lac = rel_vol / slog_vccy
        lac_rank = ts_rank(lac, 480)
        logc = np.log(c)
        mom60 = ts_diff(logc, 60)
        signal = mom60 * lac_rank
        return (-signal).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0012()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0012")


# ═══════════════════════════════════════════════════════════════
#  Factor 0013
# ═══════════════════════════════════════════════════════════════

class TestFactor0013:
    @staticmethod
    def pandas_ref(close):
        c = pd.Series(close)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        displacement = ts_diff(logc, 60)
        path_vol = ts_std(ret1, 60)
        eff_ratio = displacement / path_vol
        vol_short = ts_std(ret1, 30)
        vol_long = ts_std(ret1, 240)
        vol_regime = vol_short / vol_long
        vol_regime_rank = ts_rank(vol_regime, 120)
        signal = eff_ratio * vol_regime_rank
        return ts_zscore(signal, 180).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0013()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close)
        assert_aligned(cpp, py, "0013")


# ═══════════════════════════════════════════════════════════════
#  Factor 0014
# ═══════════════════════════════════════════════════════════════

class TestFactor0014:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        volatility = ts_std(ret1, 30)
        slogv = pd.Series(slog1p(v.values))
        smoothed_vol = ma(slogv, 30)
        vol_eff = volatility / smoothed_vol
        eff_rank = ts_rank(vol_eff, 240)
        trend_dir = ts_zscore(ts_diff(c, 60), 120)
        signal = trend_dir * eff_rank
        return (-signal).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0014()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0014")


# ═══════════════════════════════════════════════════════════════
#  Factor 0015
# ═══════════════════════════════════════════════════════════════

class TestFactor0015:
    @staticmethod
    def pandas_ref(close, volume, high, low):
        c = pd.Series(close)
        v = pd.Series(volume)
        h = pd.Series(high)
        lo = pd.Series(low)
        price_range = (h - lo) / c
        log_vol = pd.Series(slog1p(v.values))
        illiq = price_range / log_vol
        illiq_smooth = ma(illiq, 20)
        illiq_rank = ts_rank(illiq_smooth, 240)
        logc = np.log(c)
        ret60 = ts_diff(logc, 60)
        ret_rank = ts_rank(ret60, 360)
        illiq_c = illiq_rank - 0.5
        ret_c = ret_rank - 0.5
        raw = illiq_c * ret_c
        return (-raw).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0015()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, high, low)
        assert_aligned(cpp, py, "0015")


# ═══════════════════════════════════════════════════════════════
#  Factor 0016
# ═══════════════════════════════════════════════════════════════

class TestFactor0016:
    @staticmethod
    def pandas_ref(close, volume, high, low):
        c = pd.Series(close)
        v = pd.Series(volume)
        h = pd.Series(high)
        lo = pd.Series(low)
        range_pct = (h - lo) / c
        vol_comp = ma(range_pct, 30) / ma(range_pct, 240)
        rel_vol = ts_rank(ma(v, 60) / ma(v, 360), 360)
        absorption = rel_vol / pd.Series(slog1p(vol_comp.values))
        logc = np.log(c)
        recent_ret = ts_diff(logc, 60)
        signal = absorption * recent_ret
        return (-signal).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0016()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, high, low)
        assert_aligned(cpp, py, "0016")


# ═══════════════════════════════════════════════════════════════
#  Factor 0018
# ═══════════════════════════════════════════════════════════════

class TestFactor0018:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        log_ret = ts_diff(logc, 1)
        slogv = pd.Series(slog1p(v.values))
        smooth_ret = ema(log_ret, 20)
        smooth_vol = ema(slogv, 20)
        efficiency = smooth_ret / smooth_vol
        eff_ema = ema(efficiency, 15)
        eff_accel = ts_diff(eff_ema, 45)
        signal_z = ts_zscore(eff_accel, 360)
        return (-signal_z).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0018()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0018")


# ═══════════════════════════════════════════════════════════════
#  Factor 0019
# ═══════════════════════════════════════════════════════════════

class TestFactor0019:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        fast_ma = ma(ret1, 10)
        slow_ma = ma(ret1, 60)
        price_accel = fast_ma - slow_ma
        vol_intensity = v / ma(v, 120)
        accel_rank = ts_rank(price_accel, 240)
        vol_rank = ts_rank(vol_intensity, 240)
        overheating = accel_rank * vol_rank
        return (-overheating).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0019()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0019")


# ═══════════════════════════════════════════════════════════════
#  Factor 0022
# ═══════════════════════════════════════════════════════════════

class TestFactor0022:
    @staticmethod
    def pandas_ref(close, volume, high, low):
        c = pd.Series(close)
        v = pd.Series(volume)
        h = pd.Series(high)
        lo = pd.Series(low)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        short_range = ts_max(h, 15) - ts_min(lo, 15)
        med_vol = ts_std(ret1, 120)
        denom = c * med_vol
        vol_ratio = short_range / denom
        ret30 = ts_diff(logc, 30)
        z_ret30 = ts_zscore(ret30, 180)
        rel_v = ma(v, 10) / ma(v, 60)
        rank_exp = ts_rank(vol_ratio, 120)
        rank_dir = ts_rank(z_ret30, 120)
        rank_conv = ts_rank(rel_v, 120)
        return (rank_exp * rank_dir * rank_conv).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0022()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, high, low)
        assert_aligned(cpp, py, "0022")


# ═══════════════════════════════════════════════════════════════
#  Factor 0023
# ═══════════════════════════════════════════════════════════════

class TestFactor0023:
    @staticmethod
    def pandas_ref(volume, ret):
        v = pd.Series(volume)
        r = pd.Series(ret)
        signed_vol = v * r
        sv_short = ts_sum(signed_vol, 30)
        abs_sv = signed_vol.abs()
        sv_norm = ts_sum(abs_sv, 180)
        dv_ratio = sv_short / sv_norm
        dv_ranked = ts_rank(dv_ratio, 360)
        return (-dv_ranked).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0023()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(volume, ret)
        assert_aligned(cpp, py, "0023")


# ═══════════════════════════════════════════════════════════════
#  Factor 0024
# ═══════════════════════════════════════════════════════════════

class TestFactor0024:
    @staticmethod
    def pandas_ref(close):
        c = pd.Series(close)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        disp_short = ts_diff(logc, 20)
        rough_short = ts_std(ret1, 20)
        efficiency = disp_short / rough_short
        long_vol = ts_std(ret1, 120)
        smooth_vol = ma(long_vol, 30)
        signal = efficiency / smooth_vol
        return (-signal).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0024()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close)
        assert_aligned(cpp, py, "0024")


# ═══════════════════════════════════════════════════════════════
#  Factor 0025
# ═══════════════════════════════════════════════════════════════

class TestFactor0025:
    @staticmethod
    def pandas_ref(close, high, low):
        c = pd.Series(close)
        h = pd.Series(high)
        lo = pd.Series(low)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        short_vol = ts_std(ret1, 30)
        long_vol = ts_std(ret1, 240)
        vol_comp = short_vol / long_vol
        wh = ts_max(h, 60)
        wl = ts_min(lo, 60)
        range_pos = (c - wl) / (wh - wl)
        move_str = ts_zscore(ts_diff(logc, 15), 120)
        raw = move_str * range_pos
        final = raw * vol_comp
        return (-final).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0025()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, high, low)
        assert_aligned(cpp, py, "0025")


# ═══════════════════════════════════════════════════════════════
#  Factor 0026
# ═══════════════════════════════════════════════════════════════

class TestFactor0026:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        minute_ret = ts_diff(logc, 1)
        log_vol = pd.Series(slog1p(v.values))
        signed_flow = minute_ret * log_vol
        flow_accum = ts_sum(signed_flow, 60)
        flow_z = ts_zscore(flow_accum, 360)
        compressed = np.tanh(flow_z.values)
        return (-compressed).astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0026()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0026")


# ═══════════════════════════════════════════════════════════════
#  Factor 0028
# ═══════════════════════════════════════════════════════════════

class TestFactor0028:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        disp = ts_diff(c, 120).abs()
        abs_step = ts_diff(c, 1).abs()
        path = ts_sum(abs_step, 120)
        path_eff = disp / path
        ret_sq = ts_diff(logc, 1) ** 2
        vol_w = v / ma(v, 120)
        weighted_vol = ma(ret_sq * vol_w, 30)
        eff_rank = ts_rank(path_eff, 240)
        vol_rank = ts_rank(weighted_vol, 240)
        recent_trend = ts_diff(logc, 60)
        return (recent_trend * (-eff_rank) * vol_rank).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0028()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0028")


# ═══════════════════════════════════════════════════════════════
#  Factor 0029
# ═══════════════════════════════════════════════════════════════

class TestFactor0029:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        mom_short = ts_diff(logc, 15)
        mom_mid = ts_diff(logc, 45)
        avg_mid = mom_mid / 3.0
        accel = mom_short - avg_mid
        vol = ts_std(ret1, 120)
        norm_accel = accel / vol
        vol_regime = ts_rank(v, 120)
        raw = norm_accel * vol_regime
        return (-raw).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0029()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0029")


# ═══════════════════════════════════════════════════════════════
#  Factor 0031
# ═══════════════════════════════════════════════════════════════

class TestFactor0031:
    @staticmethod
    def pandas_ref(volume, ret):
        v = pd.Series(volume)
        r = pd.Series(ret)
        ret30 = ma(r, 30)
        price_z = ts_zscore(ret30, 360)
        abs_ret = r.abs()
        rv_corr = corr(abs_ret, v, 120)
        raw = np.tanh((price_z * rv_corr).values)
        ranked = ts_rank(pd.Series(raw), 240)
        return (-ranked).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0031()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(volume, ret)
        assert_aligned(cpp, py, "0031")


# ═══════════════════════════════════════════════════════════════
#  Factor 0032
# ═══════════════════════════════════════════════════════════════

class TestFactor0032:
    @staticmethod
    def pandas_ref(close, volume, high, low):
        c = pd.Series(close)
        v = pd.Series(volume)
        h = pd.Series(high)
        lo = pd.Series(low)
        range_pct = (h - lo) / c
        log_vol = pd.Series(slog1p(ma(v, 5).values))
        rve = range_pct / log_vol
        rve_rank = ts_rank(rve, 240)
        comp_intensity = 1.0 - rve_rank
        logc = np.log(c)
        short_mom = ts_diff(logc, 30)
        return (short_mom * comp_intensity).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0032()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, high, low)
        assert_aligned(cpp, py, "0032")


# ═══════════════════════════════════════════════════════════════
#  Factor 0033
# ═══════════════════════════════════════════════════════════════

class TestFactor0033:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        disp = ts_diff(logc, 30).abs()
        path_noise = ts_std(ret1, 30)
        efficiency = disp / path_noise
        vol_short = ma(v, 30)
        vol_long = ma(v, 240)
        rel_vol = vol_short / vol_long
        climax = ts_rank(efficiency, 720) * ts_rank(rel_vol, 720)
        direction = np.sign(ts_diff(c, 30).values)
        raw = climax.values * direction
        return (-raw).astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed, n=1000)
        g = build_factor_0033()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0033")


# ═══════════════════════════════════════════════════════════════
#  Factor 0034
# ═══════════════════════════════════════════════════════════════

class TestFactor0034:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        v = pd.Series(volume)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        std_short = ts_std(ret1, 30)
        std_long = ts_std(ret1, 240)
        vol_ratio = std_short / std_long
        comp_str = 1.0 / vol_ratio
        abs_ret = ret1.abs()
        slogv = pd.Series(slog1p(v.values))
        vol_force = corr(abs_ret, slogv, 60)
        z_ret = ts_zscore(ts_diff(logc, 60), 120)
        raw = z_ret * comp_str * vol_force
        return (-raw).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0034()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0034")


# ═══════════════════════════════════════════════════════════════
#  Factor 0035
# ═══════════════════════════════════════════════════════════════

class TestFactor0035:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        e10 = ema(c, 10); e60 = ema(c, 60)
        return ((e10 - e60) / ts_std(c, 60)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0035()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0035")


class TestFactor0036:
    @staticmethod
    def pandas_ref(close, volume):
        v = pd.Series(volume)
        return ts_zscore(pd.Series(slog1p(v.values)), 120).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0036()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0036")


class TestFactor0037:
    @staticmethod
    def pandas_ref(close, volume, open_, high, low):
        c, h_, lo_ = pd.Series(close), pd.Series(high), pd.Series(low)
        rng = h_ - lo_
        norm_rng = rng / ma(c, 30)
        return (-ts_zscore(norm_rng, 120)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0037()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, open_, high, low)
        assert_aligned(cpp, py, "0037")


class TestFactor0038:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        return (-autocorr_pd(ret1, 60, 5)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0038()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0038")


class TestFactor0039:
    @staticmethod
    def pandas_ref(close, volume):
        c, v = pd.Series(close), pd.Series(volume)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        sv = pd.Series(slog1p(v.values))
        vret = ret1 * sv
        return (-ts_rank(vret, 60)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0039()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0039")


class TestFactor0040:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        return (-(ts_std(ret1, 15) / ts_std(ret1, 120))).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0040()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0040")


class TestFactor0041:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        return ((c - ma(c, 60)) / ts_std(c, 60)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0041()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0041")


class TestFactor0042:
    @staticmethod
    def pandas_ref(close, volume):
        c, v = pd.Series(close), pd.Series(volume)
        logc = np.log(c)
        ret5 = ts_diff(logc, 5)
        sv = pd.Series(slog1p(v.values))
        return (-corr(ret5, sv, 60)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0042()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0042")


class TestFactor0043:
    @staticmethod
    def pandas_ref(close, volume, open_, high, low):
        c, h_, lo_ = pd.Series(close), pd.Series(high), pd.Series(low)
        rmin = ts_min(lo_, 60)
        rmax = ts_max(h_, 60)
        pos = (c - rmin) / (rmax - rmin)
        return (pos - 0.5).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0043()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, open_, high, low)
        assert_aligned(cpp, py, "0043")


class TestFactor0044:
    @staticmethod
    def pandas_ref(close, volume, open_, high, low):
        c, h_, lo_ = pd.Series(close), pd.Series(high), pd.Series(low)
        abs_chg = ts_diff(c, 1).abs()
        rng = h_ - lo_
        eff = abs_chg / rng
        return ts_zscore(eff, 60).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0044()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, open_, high, low)
        assert_aligned(cpp, py, "0044")


class TestFactor0045:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        ret30 = ts_diff(logc, 30)
        return (-ts_rank(ret30, 120)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0045()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0045")


class TestFactor0046:
    @staticmethod
    def pandas_ref(close, volume):
        c, v = pd.Series(close), pd.Series(volume)
        e30 = ema(c, 30)
        dev = (c - e30) / e30
        vr = ts_rank(v, 30)
        return (dev * vr).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0046()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0046")


class TestFactor0047:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        path_len = ts_sum(ret1.abs(), 30)
        net_move = ts_diff(logc, 30).abs()
        return (-(path_len / net_move)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0047()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0047")


class TestFactor0048:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        worst = ts_min(ret1, 60)
        best = ts_max(ret1, 60)
        return (worst / (-best)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0048()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0048")


class TestFactor0049:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        d10 = ts_diff(c, 10)
        d1 = ts_diff(c, 1)
        s10 = ts_std(d10, 30)
        s1 = ts_std(d1, 30)
        return (s10 / (s1 * 3.162) - 1.0).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0049()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0049")


class TestFactor0051:
    @staticmethod
    def pandas_ref(close, volume):
        c, v = pd.Series(close), pd.Series(volume)
        return (ts_rank(c, 60) - ts_rank(v, 60)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0051()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0051")


class TestFactor0052:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        smooth = ema(ret1, 10)
        return ts_zscore(smooth, 120).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0052()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0052")


class TestFactor0053:
    @staticmethod
    def pandas_ref(close, volume):
        v = pd.Series(volume)
        sv = pd.Series(slog1p(v.values))
        vstd = ts_std(sv, 30)
        return (-ts_zscore(vstd, 120)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0053()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0053")


class TestFactor0054:
    @staticmethod
    def pandas_ref(close, volume, open_, high, low):
        c, h_ = pd.Series(close), pd.Series(high)
        dist = c - ts_max(h_, 30)
        return (dist / ts_std(c, 30)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0054()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, open_, high, low)
        assert_aligned(cpp, py, "0054")


class TestFactor0055:
    @staticmethod
    def pandas_ref(close, volume):
        c, v = pd.Series(close), pd.Series(volume)
        logc = np.log(c)
        ret5 = ts_diff(logc, 5)
        vratio = ma(v, 5) / ma(v, 60)
        return (-(ret5 * vratio)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0055()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0055")


class TestFactor0056:
    @staticmethod
    def pandas_ref(close, volume, open_, high, low):
        c, h_, lo_ = pd.Series(close), pd.Series(high), pd.Series(low)
        ratio = (c - lo_) / (h_ - lo_)
        return (-ts_rank(ratio, 60)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0056()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, open_, high, low)
        assert_aligned(cpp, py, "0056")


class TestFactor0057:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        ret5 = ts_diff(logc, 5)
        return (ret5 - delay(ret5, 10)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0057()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0057")


class TestFactor0058:
    @staticmethod
    def pandas_ref(close, volume):
        c, v = pd.Series(close), pd.Series(volume)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        abs_ret = ret1.abs()
        rr = ts_rank(abs_ret, 30)
        vr = ts_rank(v, 30)
        return (-corr(rr, vr, 60)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0058()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0058")


class TestFactor0059:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        ratio = ema(c, 10) / ema(c, 120)
        return ts_zscore(ratio, 60).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0059()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0059")


class TestFactor0060:
    @staticmethod
    def pandas_ref(close, volume):
        c, v = pd.Series(close), pd.Series(volume)
        sign_chg = np.sign(ts_diff(c, 1))
        sv = pd.Series(slog1p(v.values))
        signed_vol = sign_chg * sv
        return ts_zscore(signed_vol, 60).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0060()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0060")


class TestFactor0061:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        return (ma(ret1, 30) / ts_std(ret1, 30)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0061()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0061")


class TestFactor0062:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        rvol = ts_std(ret1, 15)
        return (-ts_rank(rvol, 120)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0062()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0062")


class TestFactor0063:
    @staticmethod
    def pandas_ref(close, volume):
        c = pd.Series(close)
        logc = np.log(c)
        mom10 = ts_diff(logc, 10)
        return ts_diff(mom10, 10).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0063()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0063")


class TestFactor0064:
    @staticmethod
    def pandas_ref(close, volume):
        c, v = pd.Series(close), pd.Series(volume)
        return (-corr(ma(c, 10), ma(v, 10), 120)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0064()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume)
        assert_aligned(cpp, py, "0064")


class TestFactor0065:
    @staticmethod
    def pandas_ref(close, volume, open_, high, low):
        c, h_, lo_ = pd.Series(close), pd.Series(high), pd.Series(low)
        logc = np.log(c)
        ret1 = ts_diff(logc, 1)
        rng = (h_ - lo_) / c
        weighted = ret1 * rng
        return (-ts_zscore(weighted, 60)).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = build_factor_0065()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close, volume, open_, high, low)
        assert_aligned(cpp, py, "0065")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
