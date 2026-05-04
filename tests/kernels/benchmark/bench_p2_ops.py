"""
Benchmark: C++ fe_ops P2 operators vs Python ts_ops.

Covers: Corr, Autocorr, TsMinMaxDiff, TsSkew

Usage:
    cd FactorEngine
    python tests/kernel/benchmark/bench_p2_ops.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_REF_DIR = str(Path(__file__).resolve().parents[1] / "reference")
if _REF_DIR not in sys.path:
    sys.path.insert(0, _REF_DIR)

from ts_ops import Corr, Autocorr, TsMinMaxDiff, TsSkew
import fe_ops

# ── config ───────────────────────────────────────────────────

SIZES = [1_440, 4_320, 10_000, 100_000]
N_REPEAT = 20
N_WARMUP = 3
ROLLING_WINDOWS = [30, 120, 480]


def _make_data(n: int, seed: int = 42):
    rng = np.random.default_rng(seed)
    arr = rng.standard_normal(n).astype(np.float32)
    series = pd.Series(arr)
    return arr, series


def _bench(fn, n_repeat: int = N_REPEAT, n_warmup: int = N_WARMUP) -> float:
    for _ in range(n_warmup):
        fn()
    times = []
    for _ in range(n_repeat):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        times.append(t1 - t0)
    return np.median(times)


def _fmt_time(sec: float) -> str:
    if sec < 1e-3:
        return f"{sec * 1e6:8.1f} µs"
    if sec < 1.0:
        return f"{sec * 1e3:8.2f} ms"
    return f"{sec:8.3f}  s"


def _print_row(name: str, n: int, py_t: float, cpp_t: float):
    speedup = py_t / cpp_t if cpp_t > 0 else float("inf")
    print(f"  {name:25s}  n={n:>10,}  "
          f"py={_fmt_time(py_t)}  cpp={_fmt_time(cpp_t)}  "
          f"speedup={speedup:6.1f}x", flush=True)


# ── Corr benchmark ───────────────────────────────────────────

def bench_corr():
    print("\n" + "=" * 80)
    print("  P2: ROLLING CORRELATION (Corr)")
    print("=" * 80)

    for n in SIZES:
        arr_x, series_x = _make_data(n, seed=42)
        arr_y, series_y = _make_data(n, seed=99)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            py_t = _bench(lambda: Corr(series_x, series_y, w))
            cpp_t = _bench(lambda: fe_ops.rolling_corr(arr_x, arr_y, w))
            _print_row(f"Corr(t={w})", n, py_t, cpp_t)


# ── Autocorr benchmark ───────────────────────────────────────

def bench_autocorr():
    print("\n" + "=" * 80)
    print("  P2: ROLLING AUTOCORRELATION (Autocorr)")
    print("=" * 80)

    lags = [1, 5]
    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            for lag in lags:
                py_t = _bench(lambda: Autocorr(series, w, lag))
                cpp_t = _bench(lambda: fe_ops.autocorr(arr, w, lag))
                _print_row(f"Autocorr(t={w},n={lag})", n, py_t, cpp_t)
        print()


# ── TsMinMaxDiff benchmark ───────────────────────────────────

def bench_minmaxdiff():
    print("\n" + "=" * 80)
    print("  P2: TsMinMaxDiff (min_periods=1)")
    print("=" * 80)

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            py_t = _bench(lambda: TsMinMaxDiff(series, w))
            cpp_t = _bench(lambda: fe_ops.ts_minmax_diff(arr, w))
            _print_row(f"MinMaxDiff(t={w})", n, py_t, cpp_t)


# ── TsSkew benchmark ─────────────────────────────────────────

def bench_skew():
    print("\n" + "=" * 80)
    print("  P2: ROLLING SKEWNESS (TsSkew)")
    print("=" * 80)

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            py_t = _bench(lambda: TsSkew(series, w))
            cpp_t = _bench(lambda: fe_ops.rolling_skew(arr, w))
            _print_row(f"TsSkew(t={w})", n, py_t, cpp_t)


# ── main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import functools
    print = functools.partial(print, flush=True)

    print("=" * 80)
    print(f"  fe_ops P2 Benchmark: C++ vs Python ts_ops")
    print(f"  N_REPEAT={N_REPEAT}, N_WARMUP={N_WARMUP}")
    print(f"  Sizes: {SIZES}")
    print("=" * 80)

    bench_corr()
    bench_autocorr()
    bench_minmaxdiff()
    bench_skew()

    print("\n" + "=" * 80)
    print("  Done.")
    print("=" * 80)
