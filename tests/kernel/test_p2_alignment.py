"""
P2 Alignment tests: C++ fe_ops kernels vs Python ts_ops reference.

Covers bivariate / special-rule operators:
  - Corr(x, y, t)       — rolling Pearson correlation
  - Autocorr(x, t, n)   — rolling autocorrelation
  - TsMinMaxDiff(x, t)  — rolling max - min (min_periods=1)
  - TsSkew(x, t)        — rolling skewness

Usage:
    cd FactorEngine
    python tests/kernel/test_p2_alignment.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ts_ops import Corr, Autocorr, TsMinMaxDiff, TsSkew
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
        print(f"  FAIL {op_name:35s} shape mismatch: py={py_arr.shape} cpp={cpp_arr.shape}")
        FAIL += 1
        return

    py_nan = np.isnan(py_arr)
    cpp_nan = np.isnan(cpp_arr)
    nan_mismatch = int((py_nan != cpp_nan).sum())
    if nan_mismatch > 0:
        first_idx = np.where(py_nan != cpp_nan)[0][0]
        print(f"  FAIL {op_name:35s} {nan_mismatch} NaN mismatches (first at idx={first_idx}, "
              f"py={py_arr[first_idx]}, cpp={cpp_arr[first_idx]})")
        FAIL += 1
        return

    valid = ~py_nan
    if valid.sum() == 0:
        print(f"  OK   {op_name:35s} (all NaN)")
        PASS += 1
        return

    max_diff = float(np.max(np.abs(py_arr[valid] - cpp_arr[valid])))
    if max_diff > atol:
        worst_idx = int(np.argmax(np.abs(py_arr[valid] - cpp_arr[valid])))
        print(f"  FAIL {op_name:35s} max_diff={max_diff:.2e} > atol={atol:.0e}  "
              f"(worst valid_idx={worst_idx})")
        FAIL += 1
        return

    print(f"  OK   {op_name:35s} max_diff={max_diff:.2e}  n_valid={int(valid.sum())}")
    PASS += 1


# ── special data generators ──────────────────────────────────

def _all_nan(n: int = 200) -> np.ndarray:
    return np.full(n, np.nan, dtype=np.float32)

def _constant(val: float, n: int = 500) -> np.ndarray:
    return np.full(n, val, dtype=np.float32)

def _monotonic_up(n: int = 1000) -> np.ndarray:
    return np.linspace(-5, 5, n).astype(np.float32)

def _monotonic_down(n: int = 1000) -> np.ndarray:
    return np.linspace(5, -5, n).astype(np.float32)

def _with_extremes(n: int = 3000, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n).astype(np.float32)
    x[50] = np.float32(1e30)
    x[100] = np.float32(-1e30)
    return x


# ═══════════════════════════════════════════════════════════════
#  P2: ROLLING CORRELATION (Corr)
# ═══════════════════════════════════════════════════════════════

def test_corr_basic():
    """Corr(x, y, t) across multiple windows and seeds."""
    for seed_x, seed_y in [(42, 99), (123, 456), (7, 8)]:
        x = _random_series(n=5000, seed=seed_x, nan_ratio=0)
        y = _random_series(n=5000, seed=seed_y, nan_ratio=0)
        tag = f"s{seed_x}"
        for w in [5, 30, 60, 120, 480]:
            py_out = Corr(pd.Series(x), pd.Series(y), w).values
            cpp_out = fe_ops.rolling_corr(x, y, w)
            assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"Corr(t={w},{tag})")


def test_corr_identical():
    """Corr(x, x) should be 1.0 everywhere (after warmup)."""
    x = _random_series(n=2000, seed=42, nan_ratio=0)
    for w in [5, 30]:
        py_out = Corr(pd.Series(x), pd.Series(x), w).values
        cpp_out = fe_ops.rolling_corr(x, x, w)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"Corr(x,x,t={w})")


def test_corr_opposite():
    """Corr(x, -x) should be -1.0 everywhere."""
    x = _random_series(n=2000, seed=42, nan_ratio=0)
    neg_x = -x
    for w in [5, 30]:
        py_out = Corr(pd.Series(x), pd.Series(neg_x), w).values
        cpp_out = fe_ops.rolling_corr(x, neg_x, w)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"Corr(x,-x,t={w})")


def test_corr_constant():
    """Constant input → std=0 → produces ±inf (matching pandas)."""
    global PASS, FAIL
    c = _constant(5.0, 500)
    x = _random_series(n=500, seed=42, nan_ratio=0)
    py_out = Corr(pd.Series(c), pd.Series(x), 30).values
    cpp_out = np.asarray(fe_ops.rolling_corr(c, x, 30), dtype=np.float32)
    py_nan_or_inf = np.isnan(py_out) | np.isinf(py_out)
    cpp_nan_or_inf = np.isnan(cpp_out) | np.isinf(cpp_out)
    mismatch = int((py_nan_or_inf != cpp_nan_or_inf).sum())
    if mismatch > 0:
        print(f"  FAIL {'Corr(const,x)':35s} {mismatch} NaN/inf position mismatches")
        FAIL += 1
    else:
        both_inf = np.isinf(py_out) & np.isinf(cpp_out)
        sign_match = np.sign(py_out[both_inf]) == np.sign(cpp_out[both_inf])
        if not sign_match.all():
            print(f"  FAIL {'Corr(const,x)':35s} inf sign mismatches")
            FAIL += 1
        else:
            print(f"  OK   {'Corr(const,x)':35s} (all NaN/inf match)")
            PASS += 1


def test_corr_large_window():
    """Large windows t=1440, 2880."""
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    y = _random_series(n=5000, seed=99, nan_ratio=0)
    for w in [1440, 2880]:
        py_out = Corr(pd.Series(x), pd.Series(y), w).values
        cpp_out = fe_ops.rolling_corr(x, y, w)
        assert_aligned(py_out, cpp_out, atol=1e-4, op_name=f"Corr(t={w})")


# ═══════════════════════════════════════════════════════════════
#  P2: AUTOCORRELATION (Autocorr)
# ═══════════════════════════════════════════════════════════════

def test_autocorr_basic():
    """Autocorr(x, t, n) across multiple windows and lags."""
    for seed in [42, 123, 999]:
        x = _random_series(n=5000, seed=seed, nan_ratio=0)
        for w in [30, 60, 120]:
            for lag in [1, 5, 10]:
                py_out = Autocorr(pd.Series(x), w, lag).values
                cpp_out = fe_ops.autocorr(x, w, lag)
                assert_aligned(py_out, cpp_out, atol=1e-4,
                               op_name=f"Autocorr(t={w},n={lag},s={seed})")


def test_autocorr_edge():
    """Edge cases: t<2, n<1 → all NaN."""
    x = _random_series(n=500, seed=42, nan_ratio=0)

    py_out = Autocorr(pd.Series(x), 1, 1).values
    cpp_out = fe_ops.autocorr(x, 1, 1)
    assert_aligned(py_out, cpp_out, atol=0, op_name="Autocorr(t=1,n=1)")

    py_out = Autocorr(pd.Series(x), 30, 0).values
    cpp_out = fe_ops.autocorr(x, 30, 0)
    assert_aligned(py_out, cpp_out, atol=0, op_name="Autocorr(t=30,n=0)")


def test_autocorr_constant():
    """Constant input → zero variance → NaN."""
    c = _constant(3.0, 500)
    py_out = Autocorr(pd.Series(c), 30, 1).values
    cpp_out = fe_ops.autocorr(c, 30, 1)
    assert_aligned(py_out, cpp_out, atol=1e-4, op_name="Autocorr(const)")


# ═══════════════════════════════════════════════════════════════
#  P2: TsMinMaxDiff (min_periods=1)
# ═══════════════════════════════════════════════════════════════

def test_minmaxdiff_basic():
    """TsMinMaxDiff across multiple windows and seeds."""
    for seed in [42, 123, 999]:
        x = _random_series(n=5000, seed=seed, nan_ratio=0)
        for w in [5, 30, 60, 120, 480]:
            py_out = TsMinMaxDiff(pd.Series(x), w).values
            cpp_out = fe_ops.ts_minmax_diff(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"MinMaxDiff(t={w},s={seed})")


def test_minmaxdiff_constant():
    """Constant input → diff = 0."""
    c = _constant(5.0, 500)
    py_out = TsMinMaxDiff(pd.Series(c), 30).values
    cpp_out = fe_ops.ts_minmax_diff(c, 30)
    assert_aligned(py_out, cpp_out, atol=0, op_name="MinMaxDiff(const)")


def test_minmaxdiff_monotonic():
    """Monotonic up/down — predictable max-min."""
    for x, tag in [(_monotonic_up(1000), "up"), (_monotonic_down(1000), "down")]:
        for w in [5, 30]:
            py_out = TsMinMaxDiff(pd.Series(x), w).values
            cpp_out = fe_ops.ts_minmax_diff(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"MinMaxDiff(mono_{tag},t={w})")


def test_minmaxdiff_window1():
    """window=1 → always 0 for non-NaN input."""
    x = _random_series(n=1000, seed=42, nan_ratio=0)
    py_out = TsMinMaxDiff(pd.Series(x), 1).values
    cpp_out = fe_ops.ts_minmax_diff(x, 1)
    assert_aligned(py_out, cpp_out, atol=0, op_name="MinMaxDiff(t=1)")


def test_minmaxdiff_all_nan():
    """All NaN → all NaN output."""
    x = _all_nan(200)
    py_out = TsMinMaxDiff(pd.Series(x), 30).values
    cpp_out = fe_ops.ts_minmax_diff(x, 30)
    assert_aligned(py_out, cpp_out, atol=0, op_name="MinMaxDiff(all_nan)")


def test_minmaxdiff_large_window():
    """Large windows t=1440, 2880."""
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for w in [1440, 2880]:
        py_out = TsMinMaxDiff(pd.Series(x), w).values
        cpp_out = fe_ops.ts_minmax_diff(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"MinMaxDiff(t={w})")


# ═══════════════════════════════════════════════════════════════
#  P2: ROLLING SKEWNESS (TsSkew)
# ═══════════════════════════════════════════════════════════════

def test_skew_basic():
    """TsSkew across multiple windows and seeds."""
    for seed in [42, 123, 999]:
        x = _random_series(n=5000, seed=seed, nan_ratio=0)
        for w in [5, 30, 60, 120]:
            py_out = TsSkew(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_skew(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-3, op_name=f"TsSkew(t={w},s={seed})")


def test_skew_constant():
    """Constant input → std=0 → NaN skew."""
    c = _constant(5.0, 500)
    py_out = TsSkew(pd.Series(c), 30).values
    cpp_out = fe_ops.rolling_skew(c, 30)
    assert_aligned(py_out, cpp_out, atol=1e-3, op_name="TsSkew(const)")


def test_skew_small_window():
    """window < 3 → all NaN."""
    x = _random_series(n=1000, seed=42, nan_ratio=0)
    py_out = TsSkew(pd.Series(x), 2).values
    cpp_out = fe_ops.rolling_skew(x, 2)
    assert_aligned(py_out, cpp_out, atol=0, op_name="TsSkew(t=2)")


def test_skew_large_window():
    """Large windows t=1440, 2880."""
    x = _random_series(n=5000, seed=42, nan_ratio=0)
    for w in [1440, 2880]:
        py_out = TsSkew(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_skew(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-3, op_name=f"TsSkew(t={w})")


def test_skew_monotonic():
    """Monotonic input."""
    for x, tag in [(_monotonic_up(1000), "up"), (_monotonic_down(1000), "down")]:
        for w in [30, 120]:
            py_out = TsSkew(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_skew(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-3, op_name=f"TsSkew(mono_{tag},t={w})")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import functools
    print = functools.partial(print, flush=True)

    print("=" * 70)
    print("  P2 ts_ops C++ <-> Python alignment tests")
    print("=" * 70)

    print("\n[P2 Corr: basic]")
    test_corr_basic()
    print("\n[P2 Corr: identical]")
    test_corr_identical()
    print("\n[P2 Corr: opposite]")
    test_corr_opposite()
    print("\n[P2 Corr: constant]")
    test_corr_constant()
    print("\n[P2 Corr: large window]")
    test_corr_large_window()

    print("\n[P2 Autocorr: basic]")
    test_autocorr_basic()
    print("\n[P2 Autocorr: edge]")
    test_autocorr_edge()
    print("\n[P2 Autocorr: constant]")
    test_autocorr_constant()

    print("\n[P2 TsMinMaxDiff: basic]")
    test_minmaxdiff_basic()
    print("\n[P2 TsMinMaxDiff: constant]")
    test_minmaxdiff_constant()
    print("\n[P2 TsMinMaxDiff: monotonic]")
    test_minmaxdiff_monotonic()
    print("\n[P2 TsMinMaxDiff: window=1]")
    test_minmaxdiff_window1()
    print("\n[P2 TsMinMaxDiff: all NaN]")
    test_minmaxdiff_all_nan()
    print("\n[P2 TsMinMaxDiff: large window]")
    test_minmaxdiff_large_window()

    print("\n[P2 TsSkew: basic]")
    test_skew_basic()
    print("\n[P2 TsSkew: constant]")
    test_skew_constant()
    print("\n[P2 TsSkew: small window]")
    test_skew_small_window()
    print("\n[P2 TsSkew: large window]")
    test_skew_large_window()
    print("\n[P2 TsSkew: monotonic]")
    test_skew_monotonic()

    print("\n" + "=" * 70)
    total = PASS + FAIL
    print(f"  Result: {PASS}/{total} passed, {FAIL} failed")
    print("=" * 70)
    sys.exit(1 if FAIL > 0 else 0)
