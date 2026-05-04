"""
Alignment tests: C++ fe_ops kernels vs Python ts_ops reference.

Comprehensive numerical coverage including:
  - Multi-seed random data
  - NaN-sprinkled inputs
  - All-NaN, all-zero, all-positive, all-negative, constant arrays
  - Extreme values (+/-1e30, +/-inf)
  - Edge windows (window=1, window=n, window>n)
  - Tie-heavy / monotonic / single-element inputs
  - Lag edge cases (lag=0, lag>=n)

Usage:
    cd FactorEngine
    python tests/kernel/test_ops_alignment.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ts_ops import (
    Neg, Abs, Log, Sqr, Inv, Sign, Tanh, SLog1p,
    Add, Sub, Mul, Div,
    Ma, TsSum, TsStd, TsVari, Ema, TsMin, TsMax, TsRank, TsZscore,
    Delay, TsDiff, TsPct,
)
import fe_ops

# ── test utilities ───────────────────────────────────────────

PASS = 0
FAIL = 0


def _random_series(n: int = 5000, seed: int = 42, nan_ratio: float = 0.02) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n).astype(np.float32)
    if nan_ratio > 0:
        nan_idx = rng.choice(n, size=int(n * nan_ratio), replace=False)
        x[nan_idx] = np.nan
    return x


def assert_aligned(py_out, cpp_out, atol: float, op_name: str):
    global PASS, FAIL
    py_arr = np.asarray(py_out, dtype=np.float32)
    cpp_arr = np.asarray(cpp_out, dtype=np.float32)

    if py_arr.shape != cpp_arr.shape:
        print(f"  FAIL {op_name:30s} shape mismatch: py={py_arr.shape} cpp={cpp_arr.shape}")
        FAIL += 1
        return

    py_nan = np.isnan(py_arr)
    cpp_nan = np.isnan(cpp_arr)
    nan_mismatch = int((py_nan != cpp_nan).sum())
    if nan_mismatch > 0:
        first_idx = np.where(py_nan != cpp_nan)[0][0]
        print(f"  FAIL {op_name:30s} {nan_mismatch} NaN mismatches (first at idx={first_idx}, "
              f"py={py_arr[first_idx]}, cpp={cpp_arr[first_idx]})")
        FAIL += 1
        return

    valid = ~py_nan
    if valid.sum() == 0:
        print(f"  OK   {op_name:30s} (all NaN)")
        PASS += 1
        return

    with np.errstate(invalid="ignore"):
        diff = np.abs(py_arr[valid] - cpp_arr[valid])
    max_diff = float(np.max(diff))
    if max_diff > atol:
        worst_idx = int(np.argmax(diff))
        print(f"  FAIL {op_name:30s} max_diff={max_diff:.2e} > atol={atol:.0e}  "
              f"(worst valid_idx={worst_idx})")
        FAIL += 1
        return

    print(f"  OK   {op_name:30s} max_diff={max_diff:.2e}  n_valid={int(valid.sum())}")
    PASS += 1


# ── special data generators ──────────────────────────────────

def _all_nan(n: int = 200) -> np.ndarray:
    return np.full(n, np.nan, dtype=np.float32)

def _all_zeros(n: int = 200) -> np.ndarray:
    return np.zeros(n, dtype=np.float32)

def _all_positive(n: int = 2000, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.random(n) * 10 + 0.01).astype(np.float32)

def _all_negative(n: int = 2000, seed: int = 42) -> np.ndarray:
    return -_all_positive(n, seed)

def _constant(val: float, n: int = 500) -> np.ndarray:
    return np.full(n, val, dtype=np.float32)

def _with_extremes(n: int = 3000, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n).astype(np.float32)
    x[50] = np.float32(1e30)
    x[100] = np.float32(-1e30)
    x[150] = np.float32(1e-38)
    x[200] = np.float32(-1e-38)
    return x

def _monotonic_up(n: int = 1000) -> np.ndarray:
    return np.linspace(-5, 5, n).astype(np.float32)

def _monotonic_down(n: int = 1000) -> np.ndarray:
    return np.linspace(5, -5, n).astype(np.float32)

def _with_ties(n: int = 2000, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.choice(np.array([1.0, 2.0, 3.0, np.nan], dtype=np.float32), size=n)

def _single_element() -> np.ndarray:
    return np.array([3.14], dtype=np.float32)


# ═══════════════════════════════════════════════════════════════
#  P0: UNARY TESTS
# ═══════════════════════════════════════════════════════════════

def test_unary_multi_seed():
    """All 8 unary ops × 3 seeds."""
    ops = [
        ("Neg",    Neg,    fe_ops.neg,     0),
        ("Abs",    Abs,    fe_ops.abs_op,  0),
        ("Log",    Log,    fe_ops.log_op,  1e-6),
        ("Sqr",    Sqr,    fe_ops.sqr,     0),
        ("Inv",    Inv,    fe_ops.inv,     1e-6),
        ("Sign",   Sign,   fe_ops.sign,    0),
        ("Tanh",   Tanh,   fe_ops.tanh_op, 1e-6),
        ("SLog1p", SLog1p, fe_ops.slog1p,  1e-6),
    ]
    for seed in [42, 123, 999]:
        x = _random_series(seed=seed)
        for name, py_fn, cpp_fn, atol in ops:
            py_out = py_fn(pd.Series(x)).values
            cpp_out = cpp_fn(x)
            assert_aligned(py_out, cpp_out, atol=atol, op_name=f"{name}(seed={seed})")


def test_unary_special_values():
    """Unary ops on all-zero, all-positive, all-negative, extreme, single element."""
    ops = [
        ("Neg",    Neg,    fe_ops.neg,     0),
        ("Abs",    Abs,    fe_ops.abs_op,  0),
        ("Log",    Log,    fe_ops.log_op,  1e-6),
        ("Sqr",    Sqr,    fe_ops.sqr,     0),
        ("Inv",    Inv,    fe_ops.inv,     1e-6),
        ("Sign",   Sign,   fe_ops.sign,    0),
        ("Tanh",   Tanh,   fe_ops.tanh_op, 1e-6),
        ("SLog1p", SLog1p, fe_ops.slog1p,  1e-6),
    ]
    datasets = [
        ("zeros",    _all_zeros(500)),
        ("pos",      _all_positive(500)),
        ("neg",      _all_negative(500)),
        ("extreme",  _with_extremes(500)),
        ("single",   _single_element()),
    ]
    for dname, x in datasets:
        for name, py_fn, cpp_fn, atol in ops:
            py_out = py_fn(pd.Series(x)).values
            cpp_out = cpp_fn(x)
            assert_aligned(py_out, cpp_out, atol=atol, op_name=f"{name}({dname})")


def test_unary_nan_input():
    """Unary ops must propagate NaN correctly."""
    ops = [
        ("Neg",    Neg,    fe_ops.neg,     0),
        ("Abs",    Abs,    fe_ops.abs_op,  0),
        ("Log",    Log,    fe_ops.log_op,  1e-6),
        ("Sqr",    Sqr,    fe_ops.sqr,     0),
        ("Inv",    Inv,    fe_ops.inv,     1e-6),
        ("Sign",   Sign,   fe_ops.sign,    0),
        ("Tanh",   Tanh,   fe_ops.tanh_op, 1e-6),
        ("SLog1p", SLog1p, fe_ops.slog1p,  1e-6),
    ]
    for nan_ratio in [0.05, 0.3]:
        x = _random_series(n=3000, seed=77, nan_ratio=nan_ratio)
        tag = f"nan{int(nan_ratio*100)}%"
        for name, py_fn, cpp_fn, atol in ops:
            py_out = py_fn(pd.Series(x)).values
            cpp_out = cpp_fn(x)
            assert_aligned(py_out, cpp_out, atol=atol, op_name=f"{name}({tag})")

    x_all_nan = _all_nan(100)
    for name, py_fn, cpp_fn, atol in ops:
        py_out = py_fn(pd.Series(x_all_nan)).values
        cpp_out = cpp_fn(x_all_nan)
        assert_aligned(py_out, cpp_out, atol=atol, op_name=f"{name}(all_nan)")


# ═══════════════════════════════════════════════════════════════
#  P0: BINARY TESTS
# ═══════════════════════════════════════════════════════════════

def test_binary_basic():
    """Binary ops: arr+arr, arr+scl, scl+arr across 3 seeds."""
    ops = [
        ("Add", Add, fe_ops.add,    0),
        ("Sub", Sub, fe_ops.sub,    0),
        ("Mul", Mul, fe_ops.mul,    0),
        ("Div", Div, fe_ops.div_op, 1e-6),
    ]
    for seed_x, seed_y in [(42, 99), (123, 456), (7, 8)]:
        x = _random_series(seed=seed_x, nan_ratio=0)
        y = _random_series(seed=seed_y, nan_ratio=0)
        tag = f"s{seed_x}"
        for name, py_fn, cpp_fn, atol in ops:
            py_out = py_fn(pd.Series(x), pd.Series(y)).values
            cpp_out = cpp_fn(x, y)
            assert_aligned(py_out, cpp_out, atol=atol, op_name=f"{name}(aa,{tag})")

            py_out_as = py_fn(pd.Series(x), np.float32(2.5)).values
            cpp_out_as = cpp_fn(x, 2.5)
            assert_aligned(py_out_as, cpp_out_as, atol=atol, op_name=f"{name}(as,{tag})")

            py_out_sa = py_fn(np.float32(2.5), pd.Series(y)).values
            cpp_out_sa = cpp_fn(2.5, y)
            assert_aligned(py_out_sa, cpp_out_sa, atol=atol, op_name=f"{name}(sa,{tag})")


def test_binary_nan():
    """Binary ops with NaN in one or both operands."""
    ops = [
        ("Add", Add, fe_ops.add,    0),
        ("Sub", Sub, fe_ops.sub,    0),
        ("Mul", Mul, fe_ops.mul,    0),
        ("Div", Div, fe_ops.div_op, 1e-6),
    ]
    x_nan = _random_series(n=3000, seed=42, nan_ratio=0.05)
    y_nan = _random_series(n=3000, seed=99, nan_ratio=0.05)
    y_clean = _random_series(n=3000, seed=99, nan_ratio=0)

    for name, py_fn, cpp_fn, atol in ops:
        py_out = py_fn(pd.Series(x_nan), pd.Series(y_clean)).values
        cpp_out = cpp_fn(x_nan, y_clean)
        assert_aligned(py_out, cpp_out, atol=atol, op_name=f"{name}(nan_x)")

        py_out2 = py_fn(pd.Series(x_nan), pd.Series(y_nan)).values
        cpp_out2 = cpp_fn(x_nan, y_nan)
        assert_aligned(py_out2, cpp_out2, atol=atol, op_name=f"{name}(nan_both)")


def test_binary_edge_values():
    """Binary ops with zeros, near-zero divisor, negative scalars."""
    x = _random_series(n=2000, seed=42, nan_ratio=0)
    z = _all_zeros(2000)

    for name, py_fn, cpp_fn, atol in [
        ("Add", Add, fe_ops.add, 0),
        ("Mul", Mul, fe_ops.mul, 0),
        ("Div", Div, fe_ops.div_op, 1e-6),
    ]:
        py_out = py_fn(pd.Series(x), pd.Series(z)).values
        cpp_out = cpp_fn(x, z)
        assert_aligned(py_out, cpp_out, atol=atol, op_name=f"{name}(arr,zeros)")

    for scalar in [0.0, -1.0, 1e-8, 1e30]:
        py_out = Div(pd.Series(x), np.float32(scalar)).values
        cpp_out = fe_ops.div_op(x, float(scalar))
        assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"Div(arr,{scalar:.0e})")


# ═══════════════════════════════════════════════════════════════
#  P1: ROLLING MEAN
# ═══════════════════════════════════════════════════════════════

def test_rolling_mean():
    for window in [1, 5, 30, 120, 480]:
        x = _random_series(n=5000, seed=42, nan_ratio=0)
        py_out = Ma(pd.Series(x), window).values
        cpp_out = fe_ops.rolling_mean(x, window)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"Ma(t={window})")


def test_rolling_mean_nan():
    for nan_ratio in [0.02, 0.1, 0.5]:
        x = _random_series(n=5000, seed=42, nan_ratio=nan_ratio)
        tag = f"nan{int(nan_ratio*100)}%"
        for w in [5, 30, 120]:
            py_out = Ma(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_mean(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"Ma(t={w},{tag})")

    x_all_nan = _all_nan(100)
    py_out = Ma(pd.Series(x_all_nan), 10).values
    cpp_out = fe_ops.rolling_mean(x_all_nan, 10)
    assert_aligned(py_out, cpp_out, atol=0, op_name="Ma(all_nan)")


def test_rolling_mean_edge():
    x = _random_series(n=3000, seed=42, nan_ratio=0)
    # window = n
    py_out = Ma(pd.Series(x), len(x)).values
    cpp_out = fe_ops.rolling_mean(x, len(x))
    assert_aligned(py_out, cpp_out, atol=1e-5, op_name="Ma(t=n)")

    # constant input → mean == constant
    c = _constant(7.5, 500)
    py_out = Ma(pd.Series(c), 30).values
    cpp_out = fe_ops.rolling_mean(c, 30)
    assert_aligned(py_out, cpp_out, atol=1e-5, op_name="Ma(const)")

    # extreme values
    x_ext = _with_extremes(3000)
    py_out = Ma(pd.Series(x_ext), 30).values
    cpp_out = fe_ops.rolling_mean(x_ext, 30)
    assert_aligned(py_out, cpp_out, atol=1e20, op_name="Ma(extreme)")


# ═══════════════════════════════════════════════════════════════
#  P1: ROLLING SUM
# ═══════════════════════════════════════════════════════════════

def test_rolling_sum():
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for w in [1, 5, 30, 120, 480]:
        py_out = TsSum(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_sum(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsSum(t={w})")


def test_rolling_sum_nan():
    x = _random_series(n=5000, seed=42, nan_ratio=0.05)
    for w in [5, 30, 120]:
        py_out = TsSum(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_sum(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsSum(t={w},nan)")

    x_all_nan = _all_nan(100)
    py_out = TsSum(pd.Series(x_all_nan), 10).values
    cpp_out = fe_ops.rolling_sum(x_all_nan, 10)
    assert_aligned(py_out, cpp_out, atol=0, op_name="TsSum(all_nan)")


def test_rolling_sum_edge():
    c = _constant(1.0, 500)
    py_out = TsSum(pd.Series(c), 30).values
    cpp_out = fe_ops.rolling_sum(c, 30)
    assert_aligned(py_out, cpp_out, atol=1e-4, op_name="TsSum(const)")

    x = _random_series(n=100, seed=42, nan_ratio=0)
    py_out = TsSum(pd.Series(x), len(x)).values
    cpp_out = fe_ops.rolling_sum(x, len(x))
    assert_aligned(py_out, cpp_out, atol=1e-4, op_name="TsSum(t=n)")


# ═══════════════════════════════════════════════════════════════
#  P1: ROLLING STD
# ═══════════════════════════════════════════════════════════════

def test_rolling_std():
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for w in [2, 5, 30, 120]:
        py_out = TsStd(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_std(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsStd(t={w})")


def test_rolling_std_nan():
    x = _random_series(n=5000, seed=42, nan_ratio=0.05)
    for w in [5, 30, 120]:
        py_out = TsStd(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_std(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsStd(t={w},nan)")


def test_rolling_std_constant():
    """Constant input → std == 0."""
    c = _constant(3.0, 500)
    py_out = TsStd(pd.Series(c), 30).values
    cpp_out = fe_ops.rolling_std(c, 30)
    assert_aligned(py_out, cpp_out, atol=1e-6, op_name="TsStd(const)")


# ═══════════════════════════════════════════════════════════════
#  P1: ROLLING VARIANCE (TsVari)
# ═══════════════════════════════════════════════════════════════

def test_rolling_var():
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for w in [2, 5, 30, 120]:
        py_out = TsVari(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_var(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsVari(t={w})")


def test_rolling_var_nan():
    x = _random_series(n=5000, seed=42, nan_ratio=0.05)
    for w in [5, 30, 120]:
        py_out = TsVari(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_var(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsVari(t={w},nan)")


def test_rolling_var_constant():
    c = _constant(3.0, 500)
    py_out = TsVari(pd.Series(c), 30).values
    cpp_out = fe_ops.rolling_var(c, 30)
    assert_aligned(py_out, cpp_out, atol=1e-6, op_name="TsVari(const)")


# ═══════════════════════════════════════════════════════════════
#  P1: LARGE WINDOW TESTS (doc Section 7: t=1440, 2880)
# ═══════════════════════════════════════════════════════════════

def test_large_window():
    """Doc Section 7 requires testing window=1440 and window=2880."""
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    s = pd.Series(x)

    for w in [1440, 2880]:
        py_out = Ma(s, w).values
        cpp_out = fe_ops.rolling_mean(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"Ma(t={w})")

        py_out = TsSum(s, w).values
        cpp_out = fe_ops.rolling_sum(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsSum(t={w})")

        py_out = TsStd(s, w).values
        cpp_out = fe_ops.rolling_std(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsStd(t={w})")

        py_out = TsVari(s, w).values
        cpp_out = fe_ops.rolling_var(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsVari(t={w})")

        py_out = Ema(s, w).values
        cpp_out = fe_ops.ema(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"Ema(t={w})")

        py_out = TsMin(s, w).values
        cpp_out = fe_ops.rolling_min(x, w)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMin(t={w})")

        py_out = TsMax(s, w).values
        cpp_out = fe_ops.rolling_max(x, w)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMax(t={w})")

        py_out = TsZscore(s, w).values
        cpp_out = fe_ops.rolling_zscore(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsZscore(t={w})")

    x_rank = _random_series(n=5000, seed=42, nan_ratio=0)
    for w in [1440, 2880]:
        py_out = TsRank(pd.Series(x_rank), w).values
        cpp_out = fe_ops.rolling_rank(x_rank, w)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsRank(t={w})")


# ═══════════════════════════════════════════════════════════════
#  P1: EMA
# ═══════════════════════════════════════════════════════════════

def test_ema():
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for w in [1, 5, 30, 120]:
        py_out = Ema(pd.Series(x), w).values
        cpp_out = fe_ops.ema(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"Ema(t={w})")


def test_ema_multi_seed():
    for seed in [42, 77, 256]:
        x = _random_series(n=5000, seed=seed, nan_ratio=0)
        py_out = Ema(pd.Series(x), 30).values
        cpp_out = fe_ops.ema(x, 30)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"Ema(t=30,s={seed})")


def test_ema_constant():
    c = _constant(5.0, 500)
    py_out = Ema(pd.Series(c), 30).values
    cpp_out = fe_ops.ema(c, 30)
    assert_aligned(py_out, cpp_out, atol=1e-5, op_name="Ema(const)")


# ═══════════════════════════════════════════════════════════════
#  P1: ROLLING MIN / MAX
# ═══════════════════════════════════════════════════════════════

def test_rolling_min():
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for w in [1, 5, 30, 120]:
        py_out = TsMin(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_min(x, w)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMin(t={w})")


def test_rolling_max():
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for w in [1, 5, 30, 120]:
        py_out = TsMax(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_max(x, w)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMax(t={w})")


def test_rolling_minmax_nan():
    x = _random_series(n=5000, seed=42, nan_ratio=0.05)
    for w in [5, 30, 120]:
        py_out = TsMin(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_min(x, w)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMin(t={w},nan)")

        py_out = TsMax(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_max(x, w)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMax(t={w},nan)")


def test_rolling_minmax_constant():
    c = _constant(2.0, 500)
    py_out = TsMin(pd.Series(c), 30).values
    cpp_out = fe_ops.rolling_min(c, 30)
    assert_aligned(py_out, cpp_out, atol=0, op_name="TsMin(const)")

    py_out = TsMax(pd.Series(c), 30).values
    cpp_out = fe_ops.rolling_max(c, 30)
    assert_aligned(py_out, cpp_out, atol=0, op_name="TsMax(const)")


def test_rolling_minmax_monotonic():
    for x, tag in [(_monotonic_up(), "up"), (_monotonic_down(), "down")]:
        for w in [5, 30]:
            py_out = TsMin(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_min(x, w)
            assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMin(mono_{tag},t={w})")

            py_out = TsMax(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_max(x, w)
            assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMax(mono_{tag},t={w})")


# ═══════════════════════════════════════════════════════════════
#  P1: ROLLING RANK
# ═══════════════════════════════════════════════════════════════

def test_rolling_rank():
    x = _random_series(n=3000, seed=42, nan_ratio=0)
    for w in [2, 5, 30, 60, 120]:
        py_out = TsRank(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_rank(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsRank(t={w})")


def test_rolling_rank_multi_seed():
    for seed in [42, 77, 256]:
        x = _random_series(n=3000, seed=seed, nan_ratio=0)
        py_out = TsRank(pd.Series(x), 30).values
        cpp_out = fe_ops.rolling_rank(x, 30)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsRank(t=30,s={seed})")


def test_rolling_rank_edge():
    x = _random_series(n=100, seed=42, nan_ratio=0)

    py_out = TsRank(pd.Series(x), 1).values
    cpp_out = fe_ops.rolling_rank(x, 1)
    assert_aligned(py_out, cpp_out, atol=0, op_name="TsRank(t=1)")

    py_out = TsRank(pd.Series(x), len(x)).values
    cpp_out = fe_ops.rolling_rank(x, len(x))
    assert_aligned(py_out, cpp_out, atol=1e-5, op_name="TsRank(t=n)")


def test_rolling_rank_nan():
    x = _random_series(n=3000, seed=42, nan_ratio=0.05)
    for w in [5, 30, 60]:
        py_out = TsRank(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_rank(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsRank(t={w},nan)")


def test_rolling_rank_ties():
    """Heavy ties: only a few distinct values."""
    x = np.array([1, 2, 2, 3, 1, 2, 3, 3, 1, 1, 2, 2, 3, 1, 2, 3, 1, 2, 3, 1],
                 dtype=np.float32)
    for w in [3, 5, 10]:
        py_out = TsRank(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_rank(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsRank(ties,t={w})")


def test_rolling_rank_constant():
    c = _constant(5.0, 200)
    py_out = TsRank(pd.Series(c), 30).values
    cpp_out = fe_ops.rolling_rank(c, 30)
    assert_aligned(py_out, cpp_out, atol=1e-5, op_name="TsRank(const)")


def test_rolling_rank_monotonic():
    for x, tag in [(_monotonic_up(500), "up"), (_monotonic_down(500), "down")]:
        for w in [5, 30]:
            py_out = TsRank(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_rank(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsRank(mono_{tag},t={w})")


# ═══════════════════════════════════════════════════════════════
#  P1: ROLLING ZSCORE
# ═══════════════════════════════════════════════════════════════

def test_rolling_zscore():
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for w in [2, 5, 30, 120]:
        py_out = TsZscore(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_zscore(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsZscore(t={w})")


def test_rolling_zscore_nan():
    x = _random_series(n=5000, seed=42, nan_ratio=0.05)
    for w in [5, 30, 120]:
        py_out = TsZscore(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_zscore(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsZscore(t={w},nan)")


def test_rolling_zscore_constant():
    """Constant input → std~0 → should be NaN."""
    c = _constant(3.0, 500)
    py_out = TsZscore(pd.Series(c), 30).values
    cpp_out = fe_ops.rolling_zscore(c, 30)
    assert_aligned(py_out, cpp_out, atol=1e-4, op_name="TsZscore(const)")


def test_rolling_zscore_multi_seed():
    for seed in [42, 123, 999]:
        x = _random_series(n=5000, seed=seed, nan_ratio=0)
        py_out = TsZscore(pd.Series(x), 30).values
        cpp_out = fe_ops.rolling_zscore(x, 30)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"TsZscore(t=30,s={seed})")


# ═══════════════════════════════════════════════════════════════
#  P1: DELAY / DIFF / PCT
# ═══════════════════════════════════════════════════════════════

def test_delay():
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for t in [0, 1, 5, 30]:
        py_out = Delay(pd.Series(x), t).values
        cpp_out = fe_ops.delay(x, t)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"Delay(t={t})")


def test_delay_large_lag():
    """Lag >= n → all NaN."""
    x = _random_series(n=100, seed=42, nan_ratio=0)
    py_out = Delay(pd.Series(x), 100).values
    cpp_out = fe_ops.delay(x, 100)
    assert_aligned(py_out, cpp_out, atol=0, op_name="Delay(t=n)")

    py_out = Delay(pd.Series(x), 200).values
    cpp_out = fe_ops.delay(x, 200)
    assert_aligned(py_out, cpp_out, atol=0, op_name="Delay(t>n)")


def test_ts_diff():
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for t in [1, 5, 30]:
        py_out = TsDiff(pd.Series(x), t).values
        cpp_out = fe_ops.ts_diff(x, t)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsDiff(t={t})")


def test_ts_diff_multi_seed():
    for seed in [42, 77, 256]:
        x = _random_series(n=5000, seed=seed, nan_ratio=0)
        py_out = TsDiff(pd.Series(x), 5).values
        cpp_out = fe_ops.ts_diff(x, 5)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsDiff(t=5,s={seed})")


def test_ts_pct():
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for t in [1, 5, 30]:
        py_out = TsPct(pd.Series(x), t).values
        cpp_out = fe_ops.ts_pct(x, t)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsPct(t={t})")


def test_ts_pct_multi_seed():
    for seed in [42, 77, 256]:
        x = _random_series(n=5000, seed=seed, nan_ratio=0)
        py_out = TsPct(pd.Series(x), 5).values
        cpp_out = fe_ops.ts_pct(x, 5)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsPct(t=5,s={seed})")


def test_shift_ops_nan():
    """Delay / TsDiff / TsPct with NaN in input."""
    x = _random_series(n=3000, seed=42, nan_ratio=0.05)
    for t in [1, 5, 30]:
        py_out = Delay(pd.Series(x), t).values
        cpp_out = fe_ops.delay(x, t)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"Delay(t={t},nan)")

        py_out = TsDiff(pd.Series(x), t).values
        cpp_out = fe_ops.ts_diff(x, t)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsDiff(t={t},nan)")

        py_out = TsPct(pd.Series(x), t).values
        cpp_out = fe_ops.ts_pct(x, t)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsPct(t={t},nan)")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import functools
    print = functools.partial(print, flush=True)

    print("=" * 70)
    print("  ts_ops C++ <-> Python alignment tests (comprehensive)")
    print("=" * 70)

    print("\n[P0 Unary: multi-seed]")
    test_unary_multi_seed()

    print("\n[P0 Unary: special values]")
    test_unary_special_values()

    print("\n[P0 Unary: NaN propagation]")
    test_unary_nan_input()

    print("\n[P0 Binary: basic]")
    test_binary_basic()

    print("\n[P0 Binary: NaN]")
    test_binary_nan()

    print("\n[P0 Binary: edge values]")
    test_binary_edge_values()

    print("\n[P1 Rolling Mean]")
    test_rolling_mean()
    test_rolling_mean_nan()
    test_rolling_mean_edge()

    print("\n[P1 Rolling Sum]")
    test_rolling_sum()
    test_rolling_sum_nan()
    test_rolling_sum_edge()

    print("\n[P1 Rolling Std]")
    test_rolling_std()
    test_rolling_std_nan()
    test_rolling_std_constant()

    print("\n[P1 Rolling Variance (TsVari)]")
    test_rolling_var()
    test_rolling_var_nan()
    test_rolling_var_constant()

    print("\n[P1 Large Window (t=1440, 2880)]")
    test_large_window()

    print("\n[P1 Ema]")
    test_ema()
    test_ema_multi_seed()
    test_ema_constant()

    print("\n[P1 Rolling Min/Max]")
    test_rolling_min()
    test_rolling_max()
    test_rolling_minmax_nan()
    test_rolling_minmax_constant()
    test_rolling_minmax_monotonic()

    print("\n[P1 Rolling Rank]")
    test_rolling_rank()
    test_rolling_rank_multi_seed()
    test_rolling_rank_edge()
    test_rolling_rank_nan()
    test_rolling_rank_ties()
    test_rolling_rank_constant()
    test_rolling_rank_monotonic()

    print("\n[P1 Rolling Zscore]")
    test_rolling_zscore()
    test_rolling_zscore_nan()
    test_rolling_zscore_constant()
    test_rolling_zscore_multi_seed()

    print("\n[P1 Delay / Diff / Pct]")
    test_delay()
    test_delay_large_lag()
    test_ts_diff()
    test_ts_diff_multi_seed()
    test_ts_pct()
    test_ts_pct_multi_seed()
    test_shift_ops_nan()

    print("\n" + "=" * 70)
    total = PASS + FAIL
    print(f"  Result: {PASS}/{total} passed, {FAIL} failed")
    print("=" * 70)
    sys.exit(1 if FAIL > 0 else 0)
