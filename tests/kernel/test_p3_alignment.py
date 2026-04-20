"""
P3 Alignment tests: C++ fe_ops kernels vs Python ts_ops reference.

Covers low-frequency operators:
  - TsMed(x, t)      — rolling median
  - TsMad(x, t)      — rolling MAD: median(|x - median(x)|)
  - TsWMA(x, t)      — linear weighted moving average
  - TsMaxDiff(x, t)  — x - rolling max (min_periods=1)
  - TsMinDiff(x, t)  — x - rolling min (min_periods=1)

Usage:
    cd FactorEngine
    python tests/kernel/test_p3_alignment.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ts_ops import TsMed, TsMad, TsWMA, TsMaxDiff, TsMinDiff
import fe_ops

# ── test utilities ───────────────────────────────────────────

PASS = 0
FAIL = 0


def _random_series(n: int = 5000, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(n).astype(np.float32)


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
#  P3: ROLLING MEDIAN (TsMed)
# ═══════════════════════════════════════════════════════════════

def test_med_basic():
    """TsMed across multiple windows and seeds."""
    for seed in [42, 123, 999]:
        x = _random_series(n=5000, seed=seed)
        for w in [5, 30, 60, 120, 480]:
            py_out = TsMed(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_median(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"TsMed(t={w},s={seed})")


def test_med_constant():
    """Constant input → median = constant."""
    c = _constant(5.0, 500)
    for w in [5, 30]:
        py_out = TsMed(pd.Series(c), w).values
        cpp_out = fe_ops.rolling_median(c, w)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMed(const,t={w})")


def test_med_monotonic():
    """Monotonic input."""
    for x, tag in [(_monotonic_up(1000), "up"), (_monotonic_down(1000), "down")]:
        for w in [5, 30]:
            py_out = TsMed(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_median(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"TsMed(mono_{tag},t={w})")


def test_med_window1():
    """window=1 → output = input."""
    x = _random_series(n=1000, seed=42)
    py_out = TsMed(pd.Series(x), 1).values
    cpp_out = fe_ops.rolling_median(x, 1)
    assert_aligned(py_out, cpp_out, atol=0, op_name="TsMed(t=1)")


def test_med_large_window():
    """Large windows t=1440, 2880."""
    x = _random_series(n=5000, seed=42)
    for w in [1440, 2880]:
        py_out = TsMed(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_median(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"TsMed(t={w})")


def test_med_extremes():
    """Input with extreme values."""
    x = _with_extremes(3000)
    for w in [30, 120]:
        py_out = TsMed(pd.Series(x), w).values
        cpp_out = fe_ops.rolling_median(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsMed(extreme,t={w})")


# ═══════════════════════════════════════════════════════════════
#  P3: ROLLING MAD (TsMad)
# ═══════════════════════════════════════════════════════════════

def test_mad_basic():
    """TsMad across multiple windows and seeds."""
    for seed in [42, 123, 999]:
        x = _random_series(n=5000, seed=seed)
        for w in [5, 30, 60, 120]:
            py_out = TsMad(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_mad(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsMad(t={w},s={seed})")


def test_mad_constant():
    """Constant input → MAD = 0."""
    c = _constant(5.0, 500)
    for w in [5, 30]:
        py_out = TsMad(pd.Series(c), w).values
        cpp_out = fe_ops.rolling_mad(c, w)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMad(const,t={w})")


def test_mad_monotonic():
    """Monotonic input."""
    for x, tag in [(_monotonic_up(1000), "up"), (_monotonic_down(1000), "down")]:
        for w in [30, 120]:
            py_out = TsMad(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_mad(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsMad(mono_{tag},t={w})")


def test_mad_small_window():
    """window=2 → min_periods=max(2,1)=2."""
    x = _random_series(n=500, seed=42)
    py_out = TsMad(pd.Series(x), 2).values
    cpp_out = fe_ops.rolling_mad(x, 2)
    assert_aligned(py_out, cpp_out, atol=1e-6, op_name="TsMad(t=2)")


# ═══════════════════════════════════════════════════════════════
#  P3: LINEAR WEIGHTED MOVING AVERAGE (TsWMA)
# ═══════════════════════════════════════════════════════════════

def test_wma_basic():
    """TsWMA across multiple windows and seeds."""
    for seed in [42, 123, 999]:
        x = _random_series(n=5000, seed=seed)
        for w in [5, 30, 60, 120]:
            py_out = TsWMA(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_wma(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsWMA(t={w},s={seed})")


def test_wma_constant():
    """Constant input → WMA = constant."""
    c = _constant(7.0, 500)
    for w in [5, 30]:
        py_out = TsWMA(pd.Series(c), w).values
        cpp_out = fe_ops.rolling_wma(c, w)
        assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"TsWMA(const,t={w})")


def test_wma_monotonic():
    """Monotonic input."""
    for x, tag in [(_monotonic_up(1000), "up"), (_monotonic_down(1000), "down")]:
        for w in [5, 30]:
            py_out = TsWMA(pd.Series(x), w).values
            cpp_out = fe_ops.rolling_wma(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"TsWMA(mono_{tag},t={w})")


def test_wma_window1():
    """window=1 → output = input."""
    x = _random_series(n=1000, seed=42)
    py_out = TsWMA(pd.Series(x), 1).values
    cpp_out = fe_ops.rolling_wma(x, 1)
    assert_aligned(py_out, cpp_out, atol=0, op_name="TsWMA(t=1)")


def test_wma_large_window():
    """Large windows t=1440."""
    x = _random_series(n=5000, seed=42)
    py_out = TsWMA(pd.Series(x), 1440).values
    cpp_out = fe_ops.rolling_wma(x, 1440)
    assert_aligned(py_out, cpp_out, atol=1e-4, op_name="TsWMA(t=1440)")


# ═══════════════════════════════════════════════════════════════
#  P3: TsMaxDiff / TsMinDiff (min_periods=1)
# ═══════════════════════════════════════════════════════════════

def test_maxdiff_basic():
    """TsMaxDiff across multiple windows and seeds."""
    for seed in [42, 123, 999]:
        x = _random_series(n=5000, seed=seed)
        for w in [5, 30, 60, 120, 480]:
            py_out = TsMaxDiff(pd.Series(x), w).values
            cpp_out = fe_ops.ts_max_diff(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"TsMaxDiff(t={w},s={seed})")


def test_maxdiff_constant():
    """Constant input → diff = 0."""
    c = _constant(5.0, 500)
    for w in [5, 30]:
        py_out = TsMaxDiff(pd.Series(c), w).values
        cpp_out = fe_ops.ts_max_diff(c, w)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMaxDiff(const,t={w})")


def test_maxdiff_monotonic():
    for x, tag in [(_monotonic_up(1000), "up"), (_monotonic_down(1000), "down")]:
        for w in [5, 30]:
            py_out = TsMaxDiff(pd.Series(x), w).values
            cpp_out = fe_ops.ts_max_diff(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"TsMaxDiff(mono_{tag},t={w})")


def test_maxdiff_window1():
    """window=1 → always 0."""
    x = _random_series(n=1000, seed=42)
    py_out = TsMaxDiff(pd.Series(x), 1).values
    cpp_out = fe_ops.ts_max_diff(x, 1)
    assert_aligned(py_out, cpp_out, atol=0, op_name="TsMaxDiff(t=1)")


def test_mindiff_basic():
    """TsMinDiff across multiple windows and seeds."""
    for seed in [42, 123, 999]:
        x = _random_series(n=5000, seed=seed)
        for w in [5, 30, 60, 120, 480]:
            py_out = TsMinDiff(pd.Series(x), w).values
            cpp_out = fe_ops.ts_min_diff(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"TsMinDiff(t={w},s={seed})")


def test_mindiff_constant():
    """Constant input → diff = 0."""
    c = _constant(5.0, 500)
    for w in [5, 30]:
        py_out = TsMinDiff(pd.Series(c), w).values
        cpp_out = fe_ops.ts_min_diff(c, w)
        assert_aligned(py_out, cpp_out, atol=0, op_name=f"TsMinDiff(const,t={w})")


def test_mindiff_monotonic():
    for x, tag in [(_monotonic_up(1000), "up"), (_monotonic_down(1000), "down")]:
        for w in [5, 30]:
            py_out = TsMinDiff(pd.Series(x), w).values
            cpp_out = fe_ops.ts_min_diff(x, w)
            assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"TsMinDiff(mono_{tag},t={w})")


def test_mindiff_window1():
    """window=1 → always 0."""
    x = _random_series(n=1000, seed=42)
    py_out = TsMinDiff(pd.Series(x), 1).values
    cpp_out = fe_ops.ts_min_diff(x, 1)
    assert_aligned(py_out, cpp_out, atol=0, op_name="TsMinDiff(t=1)")


def test_maxdiff_large_window():
    """Large windows."""
    x = _random_series(n=5000, seed=42)
    for w in [1440, 2880]:
        py_out = TsMaxDiff(pd.Series(x), w).values
        cpp_out = fe_ops.ts_max_diff(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"TsMaxDiff(t={w})")


def test_mindiff_large_window():
    """Large windows."""
    x = _random_series(n=5000, seed=42)
    for w in [1440, 2880]:
        py_out = TsMinDiff(pd.Series(x), w).values
        cpp_out = fe_ops.ts_min_diff(x, w)
        assert_aligned(py_out, cpp_out, atol=1e-6, op_name=f"TsMinDiff(t={w})")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import functools
    print = functools.partial(print, flush=True)

    print("=" * 70)
    print("  P3 ts_ops C++ <-> Python alignment tests")
    print("=" * 70)

    print("\n[P3 TsMed: basic]")
    test_med_basic()
    print("\n[P3 TsMed: constant]")
    test_med_constant()
    print("\n[P3 TsMed: monotonic]")
    test_med_monotonic()
    print("\n[P3 TsMed: window=1]")
    test_med_window1()
    print("\n[P3 TsMed: large window]")
    test_med_large_window()
    print("\n[P3 TsMed: extremes]")
    test_med_extremes()

    print("\n[P3 TsMad: basic]")
    test_mad_basic()
    print("\n[P3 TsMad: constant]")
    test_mad_constant()
    print("\n[P3 TsMad: monotonic]")
    test_mad_monotonic()
    print("\n[P3 TsMad: small window]")
    test_mad_small_window()

    print("\n[P3 TsWMA: basic]")
    test_wma_basic()
    print("\n[P3 TsWMA: constant]")
    test_wma_constant()
    print("\n[P3 TsWMA: monotonic]")
    test_wma_monotonic()
    print("\n[P3 TsWMA: window=1]")
    test_wma_window1()
    print("\n[P3 TsWMA: large window]")
    test_wma_large_window()

    print("\n[P3 TsMaxDiff: basic]")
    test_maxdiff_basic()
    print("\n[P3 TsMaxDiff: constant]")
    test_maxdiff_constant()
    print("\n[P3 TsMaxDiff: monotonic]")
    test_maxdiff_monotonic()
    print("\n[P3 TsMaxDiff: window=1]")
    test_maxdiff_window1()
    print("\n[P3 TsMaxDiff: large window]")
    test_maxdiff_large_window()

    print("\n[P3 TsMinDiff: basic]")
    test_mindiff_basic()
    print("\n[P3 TsMinDiff: constant]")
    test_mindiff_constant()
    print("\n[P3 TsMinDiff: monotonic]")
    test_mindiff_monotonic()
    print("\n[P3 TsMinDiff: window=1]")
    test_mindiff_window1()
    print("\n[P3 TsMinDiff: large window]")
    test_mindiff_large_window()

    print("\n" + "=" * 70)
    total = PASS + FAIL
    print(f"  Result: {PASS}/{total} passed, {FAIL} failed")
    print("=" * 70)
    sys.exit(1 if FAIL > 0 else 0)
