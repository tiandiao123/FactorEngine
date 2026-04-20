"""
TimeSeries operators for crypto kbar factors.

All Ts* operators:
  - accept pd.Series (single coin) OR pd.DataFrame (panel: index=time, columns=coin)
  - return the same type as input (Series → Series, DataFrame → DataFrame)
  - use rolling(center=False) to prevent look-ahead
  - NaN-aware, min_periods=t (insufficient data → NaN)
  - DataFrame columns are computed independently (no cross-column leakage)
"""

from __future__ import annotations

from typing import Union

import numpy as np
import pandas as pd

DTYPE = np.float32
EPS: float = 1e-8

PanelLike = Union[pd.Series, pd.DataFrame]
Operand = Union[pd.Series, pd.DataFrame, int, float, np.number]


def _to_int(t, name: str = "t") -> int:
    try:
        return int(t)
    except Exception as e:
        raise TypeError(f"{name} must be integer, got {type(t)} {t!r}") from e


def _ensure(x, name: str = "x") -> PanelLike:
    """Accept pd.Series or pd.DataFrame, cast to DTYPE."""
    if isinstance(x, pd.DataFrame):
        return x.astype(DTYPE, copy=False)
    if isinstance(x, pd.Series):
        return x.astype(DTYPE, copy=False)
    raise TypeError(f"{name} must be pd.Series or pd.DataFrame, got {type(x)}")


def _as_operand(x, name: str = "x"):
    if isinstance(x, (pd.Series, pd.DataFrame)):
        return x.astype(DTYPE, copy=False)
    if isinstance(x, (int, float, np.number)):
        return np.float32(x)
    raise TypeError(f"{name} must be pd.Series/DataFrame or scalar, got {type(x)}")


def _wrap_np(arr, ref):
    """Wrap numpy result back into the same container type as ref."""
    if isinstance(ref, pd.DataFrame):
        return pd.DataFrame(arr, index=ref.index, columns=ref.columns, dtype=DTYPE)
    return pd.Series(arr, index=ref.index, dtype=DTYPE)


# ── utility functions (exported) ─────────────────────────────


def clean_factor(factor: pd.Series) -> pd.Series:
    """Replace +/-inf with NaN, fill NaN with 0."""
    return factor.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def cross_sectional_zscore(factor: pd.Series) -> pd.Series:
    """Cross-sectional z-score for one snapshot (coin → value)."""
    f = factor.astype(np.float64)
    std = f.std()
    if not np.isfinite(std) or std < 1e-10:
        return (factor * 0.0).astype(DTYPE)
    return ((f - f.mean()) / std).astype(DTYPE)


def cross_sectional_rank(factor: pd.Series) -> pd.Series:
    """Cross-sectional percentile rank [0,1] for one snapshot."""
    return factor.rank(pct=True).astype(DTYPE)


# ── elementwise unary ────────────────────────────────────────


def Neg(x: PanelLike) -> PanelLike:
    x = _ensure(x)
    return (-x).astype(DTYPE)


def Abs(x: PanelLike) -> PanelLike:
    x = _ensure(x)
    return x.abs().astype(DTYPE)


def Log(x: PanelLike) -> PanelLike:
    """Natural log. Non-positive values → NaN."""
    x = _ensure(x)
    out = x.astype(np.float64, copy=True)
    out[out <= 0] = np.nan
    return np.log(out).astype(DTYPE)


def Sqr(x: PanelLike) -> PanelLike:
    x = _ensure(x)
    return (x * x).astype(DTYPE)


def Inv(x: PanelLike) -> PanelLike:
    """1 / (x + eps)."""
    x = _ensure(x)
    return (1.0 / (x + EPS)).astype(DTYPE)


def Sign(x: PanelLike) -> PanelLike:
    x = _ensure(x)
    return _wrap_np(np.sign(x.to_numpy()), x)


def Tanh(x: PanelLike) -> PanelLike:
    x = _ensure(x)
    return _wrap_np(np.tanh(x.to_numpy()), x)


def SLog1p(x: PanelLike) -> PanelLike:
    """Signed log transform: sign(x) * log(1 + |x|)."""
    x = _ensure(x)
    v = x.to_numpy().astype(np.float64)
    arr = (np.sign(v) * np.log1p(np.abs(v))).astype(DTYPE)
    return _wrap_np(arr, x)


# ── binary ops (support scalar broadcasting) ─────────────────


def Add(x: Operand, y: Operand) -> Operand:
    a, b = _as_operand(x, "x"), _as_operand(y, "y")
    result = a + b
    if isinstance(result, (pd.Series, pd.DataFrame)):
        return result.astype(DTYPE)
    return np.float32(result)


def Sub(x: Operand, y: Operand) -> Operand:
    a, b = _as_operand(x, "x"), _as_operand(y, "y")
    result = a - b
    if isinstance(result, (pd.Series, pd.DataFrame)):
        return result.astype(DTYPE)
    return np.float32(result)


def Mul(x: Operand, y: Operand) -> Operand:
    a, b = _as_operand(x, "x"), _as_operand(y, "y")
    result = a * b
    if isinstance(result, (pd.Series, pd.DataFrame)):
        return result.astype(DTYPE)
    return np.float32(result)


def Div(x: Operand, y: Operand) -> Operand:
    a, b = _as_operand(x, "x"), _as_operand(y, "y")
    result = a / (b + EPS)
    if isinstance(result, (pd.Series, pd.DataFrame)):
        return result.astype(DTYPE)
    return np.float32(result)


# ── time shift / differences ─────────────────────────────────


def Delay(x: PanelLike, t: int) -> PanelLike:
    """Shift x forward by t steps (access historical value at t bars ago)."""
    t = _to_int(t)
    if t < 0:
        raise ValueError("t must be >= 0")
    x = _ensure(x)
    return x.shift(t).astype(DTYPE)


def TsDiff(x: PanelLike, t: int) -> PanelLike:
    """x - x.shift(t)"""
    x = _ensure(x)
    t = _to_int(t)
    return (x - x.shift(t)).astype(DTYPE)


def TsPct(x: PanelLike, t: int) -> PanelLike:
    """Percent change: x / x.shift(t) - 1."""
    x = _ensure(x)
    t = _to_int(t)
    return (x / (x.shift(t) + EPS) - 1.0).astype(DTYPE)


# ── rolling window statistics ────────────────────────────────


def Ma(x: PanelLike, t: int) -> PanelLike:
    """Rolling mean."""
    x = _ensure(x)
    t = _to_int(t)
    return x.rolling(t, min_periods=t, center=False).mean().astype(DTYPE)


def Ema(x: PanelLike, t: int) -> PanelLike:
    """Exponential weighted moving average (span=t, adjust=False)."""
    x = _ensure(x)
    t = _to_int(t)
    return x.ewm(span=t, min_periods=t, adjust=False).mean().astype(DTYPE)


def TsSum(x: PanelLike, t: int) -> PanelLike:
    """Rolling sum."""
    x = _ensure(x)
    t = _to_int(t)
    return x.rolling(t, min_periods=t, center=False).sum().astype(DTYPE)


def TsStd(x: PanelLike, t: int, ddof: int = 0) -> PanelLike:
    """Rolling standard deviation. Computed in float64 for numerical stability."""
    x = _ensure(x)
    t = _to_int(t)
    x64 = x.replace([np.inf, -np.inf], np.nan).astype(np.float64, copy=False)
    return x64.rolling(t, min_periods=t, center=False).std(ddof=ddof).astype(DTYPE)


def TsMin(x: PanelLike, t: int) -> PanelLike:
    """Rolling minimum."""
    x = _ensure(x)
    t = _to_int(t)
    return x.rolling(t, min_periods=t, center=False).min().astype(DTYPE)


def TsMax(x: PanelLike, t: int) -> PanelLike:
    """Rolling maximum."""
    x = _ensure(x)
    t = _to_int(t)
    return x.rolling(t, min_periods=t, center=False).max().astype(DTYPE)


def TsMed(x: PanelLike, t: int) -> PanelLike:
    """Rolling median."""
    x = _ensure(x)
    t = _to_int(t)
    return x.rolling(t, min_periods=t, center=False).median().astype(DTYPE)


def TsVari(x: PanelLike, t: int, ddof: int = 0) -> PanelLike:
    """Rolling variance."""
    x = _ensure(x)
    t = _to_int(t)
    return x.rolling(t, min_periods=t, center=False).var(ddof=ddof).astype(DTYPE)


def TsSkew(x: PanelLike, t: int) -> PanelLike:
    """Rolling skewness."""
    x = _ensure(x)
    t = _to_int(t)
    r = x.rolling(t, min_periods=t, center=False)
    if hasattr(r, "skew"):
        return r.skew().astype(DTYPE)
    return r.apply(lambda a: float(pd.Series(a).skew()), raw=True).astype(DTYPE)


def TsKurt(x: PanelLike, t: int) -> PanelLike:
    """Rolling excess kurtosis."""
    x = _ensure(x)
    t = _to_int(t)
    r = x.rolling(t, min_periods=t, center=False)
    if hasattr(r, "kurt"):
        return r.kurt().astype(DTYPE)
    return r.apply(lambda a: float(pd.Series(a).kurt()), raw=True).astype(DTYPE)


def TsZscore(x: PanelLike, t: int) -> PanelLike:
    """Rolling z-score: (x - Ma(x,t)) / (TsStd(x,t) + eps)."""
    x = _ensure(x)
    m = Ma(x, t)
    s = TsStd(x, t, ddof=0)
    out = (x - m) / (s + EPS)
    mask = (s.abs() < EPS) | s.isna()
    if isinstance(out, pd.DataFrame):
        out = out.mask(mask, np.nan)
    else:
        out[mask] = np.nan
    return out.astype(DTYPE)


def TsRank(x: PanelLike, t: int) -> PanelLike:
    """Time-series rank of current value within past t values, normalized to [0,1]."""
    x = _ensure(x)
    t = _to_int(t)
    if t <= 0:
        return (x * np.nan).astype(DTYPE)
    if t == 1:
        return (x * 0.0).astype(DTYPE)

    r = x.rolling(window=t, min_periods=t, center=False)
    if hasattr(r, "rank"):
        return r.rank(pct=True).astype(DTYPE)

    def _rank_last(values: np.ndarray) -> float:
        n = values.size
        last_v = values[-1]
        if np.isnan(last_v):
            return np.nan
        m = ~np.isnan(values)
        valid_v = values[m]
        if valid_v.size < n:
            return np.nan
        less_n = np.sum(valid_v < last_v)
        equal_n = np.sum(valid_v == last_v)
        rank = less_n + (equal_n + 1.0) * 0.5
        return (rank - 1.0) / float(n - 1)

    return x.rolling(t, min_periods=t).apply(_rank_last, raw=True).astype(DTYPE)


# ── rolling bivariate ────────────────────────────────────────


def _rolling_cov_df(x: pd.DataFrame, y: pd.DataFrame, t: int) -> pd.DataFrame:
    """Column-wise rolling covariance for DataFrames."""
    cols = x.columns.intersection(y.columns)
    result = pd.DataFrame(index=x.index, columns=cols, dtype=DTYPE)
    for col in cols:
        result[col] = x[col].rolling(t, min_periods=t).cov(y[col]).astype(DTYPE)
    return result


def _rolling_corr_df(x: pd.DataFrame, y: pd.DataFrame, t: int) -> pd.DataFrame:
    """Column-wise rolling correlation for DataFrames."""
    cols = x.columns.intersection(y.columns)
    result = pd.DataFrame(index=x.index, columns=cols, dtype=DTYPE)
    for col in cols:
        result[col] = x[col].rolling(t, min_periods=t).corr(y[col]).astype(DTYPE)
    return result


def Cov(x: PanelLike, y: PanelLike, t: int) -> PanelLike:
    """Rolling covariance between x and y."""
    x = _ensure(x, "x")
    y = _ensure(y, "y")
    t = _to_int(t)
    if isinstance(x, pd.DataFrame) and isinstance(y, pd.DataFrame):
        return _rolling_cov_df(x, y, t)
    if isinstance(x, pd.Series) and isinstance(y, pd.Series):
        return x.rolling(t, min_periods=t).cov(y).astype(DTYPE)
    raise TypeError("Cov: x and y must be both Series or both DataFrame")


def Corr(x: PanelLike, y: PanelLike, t: int) -> PanelLike:
    """Rolling Pearson correlation between x and y."""
    x = _ensure(x, "x")
    y = _ensure(y, "y")
    t = _to_int(t)
    if isinstance(x, pd.DataFrame) and isinstance(y, pd.DataFrame):
        return _rolling_corr_df(x, y, t)
    if isinstance(x, pd.Series) and isinstance(y, pd.Series):
        return x.rolling(t, min_periods=t).corr(y).astype(DTYPE)
    raise TypeError("Corr: x and y must be both Series or both DataFrame")


def Autocorr(x: PanelLike, t: int, n: int) -> PanelLike:
    """Rolling autocorrelation: corr(x, x.shift(n)) over window t."""
    x = _ensure(x, "x")
    t = _to_int(t)
    n = _to_int(n, "n")
    if t < 2 or n < 1:
        return (x * np.nan).astype(DTYPE)
    x0 = x.astype(DTYPE)
    x1 = x0.shift(n)
    mean0 = x0.rolling(t, min_periods=t).mean()
    mean1 = x1.rolling(t, min_periods=t).mean()
    cov = ((x0 - mean0) * (x1 - mean1)).rolling(t, min_periods=t).mean()
    std0 = x0.rolling(t, min_periods=t).std()
    std1 = x1.rolling(t, min_periods=t).std()
    out = cov / (std0 * std1)
    out = out.where((std0 > 0) & (std1 > 0))
    return out.astype(DTYPE)


# ── rolling weighted / derived ───────────────────────────────


def TsWMA(x: PanelLike, t: int) -> PanelLike:
    """Linear weighted moving average with weights [1..t] (latest = highest)."""
    x = _ensure(x, "x")
    t = _to_int(t)
    weights = np.arange(1, t + 1, dtype=np.float64)
    weights = weights / weights.sum()

    if isinstance(x, pd.DataFrame):
        vals = x.to_numpy(dtype=np.float64)
        out = np.full_like(vals, np.nan)
        for i in range(t - 1, vals.shape[0]):
            window = vals[i - t + 1: i + 1, :]
            if np.isnan(window).any(axis=0).all():
                continue
            out[i, :] = (weights[:, None] * window).sum(axis=0)
        return pd.DataFrame(out, index=x.index, columns=x.columns, dtype=DTYPE)

    result = x.rolling(window=t, min_periods=t).apply(
        lambda arr: float(np.dot(arr, weights)), raw=True
    )
    return result.astype(DTYPE)


def TsIr(x: PanelLike, t: int) -> PanelLike:
    """Rolling information ratio: mean / std."""
    x = _ensure(x, "x")
    t = _to_int(t)
    mp = max(2, t // 2)
    rolling = x.rolling(window=t, min_periods=mp)
    mu = rolling.mean()
    sigma = rolling.std()
    return (mu / (sigma + EPS)).astype(DTYPE)


def TsMad(x: PanelLike, t: int) -> PanelLike:
    """Rolling MAD: median(|x - median(x)|)."""
    x = _ensure(x, "x")
    t = _to_int(t)
    mp = max(2, t // 2)
    med = x.rolling(window=t, min_periods=mp).median()
    mad = (x - med).abs().rolling(window=t, min_periods=mp).median()
    return mad.astype(DTYPE)


# ── rolling extremal differences ─────────────────────────────


def TsMinMaxDiff(x: PanelLike, t: int) -> PanelLike:
    """Rolling max - min."""
    x = _ensure(x, "x")
    t = _to_int(t)
    r = x.rolling(window=t, min_periods=1)
    return (r.max() - r.min()).astype(DTYPE)


def TsMaxDiff(x: PanelLike, t: int) -> PanelLike:
    """x - rolling max (always <= 0 for finite x)."""
    x = _ensure(x, "x")
    t = _to_int(t)
    return (x - x.rolling(window=t, min_periods=1).max()).astype(DTYPE)


def TsMinDiff(x: PanelLike, t: int) -> PanelLike:
    """x - rolling min (always >= 0 for finite x)."""
    x = _ensure(x, "x")
    t = _to_int(t)
    return (x - x.rolling(window=t, min_periods=1).min()).astype(DTYPE)
