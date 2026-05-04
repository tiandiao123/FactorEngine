"""
Benchmark: C++ fe_ops P3 operators vs Python ts_ops.

Covers: TsMed, TsMad, TsWMA, TsMaxDiff, TsMinDiff

Usage:
    cd FactorEngine
    python tests/kernel/benchmark/bench_p3_ops.py
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

from ts_ops import TsMed, TsMad, TsWMA, TsMaxDiff, TsMinDiff
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


# ── TsMed benchmark ──────────────────────────────────────────

def bench_med():
    print("\n" + "=" * 80)
    print("  P3: ROLLING MEDIAN (TsMed)")
    print("=" * 80)

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            py_t = _bench(lambda: TsMed(series, w))
            cpp_t = _bench(lambda: fe_ops.rolling_median(arr, w))
            _print_row(f"TsMed(t={w})", n, py_t, cpp_t)


# ── TsMad benchmark ──────────────────────────────────────────

def bench_mad():
    print("\n" + "=" * 80)
    print("  P3: ROLLING MAD (TsMad)")
    print("=" * 80)

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            py_t = _bench(lambda: TsMad(series, w))
            cpp_t = _bench(lambda: fe_ops.rolling_mad(arr, w))
            _print_row(f"TsMad(t={w})", n, py_t, cpp_t)


# ── TsWMA benchmark ─────────────────────────────────────────

def bench_wma():
    print("\n" + "=" * 80)
    print("  P3: LINEAR WEIGHTED MOVING AVERAGE (TsWMA)")
    print("=" * 80)

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            py_t = _bench(lambda: TsWMA(series, w))
            cpp_t = _bench(lambda: fe_ops.rolling_wma(arr, w))
            _print_row(f"TsWMA(t={w})", n, py_t, cpp_t)


# ── TsMaxDiff benchmark ─────────────────────────────────────

def bench_maxdiff():
    print("\n" + "=" * 80)
    print("  P3: TsMaxDiff (x - rolling max, min_periods=1)")
    print("=" * 80)

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            py_t = _bench(lambda: TsMaxDiff(series, w))
            cpp_t = _bench(lambda: fe_ops.ts_max_diff(arr, w))
            _print_row(f"TsMaxDiff(t={w})", n, py_t, cpp_t)


# ── TsMinDiff benchmark ─────────────────────────────────────

def bench_mindiff():
    print("\n" + "=" * 80)
    print("  P3: TsMinDiff (x - rolling min, min_periods=1)")
    print("=" * 80)

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            py_t = _bench(lambda: TsMinDiff(series, w))
            cpp_t = _bench(lambda: fe_ops.ts_min_diff(arr, w))
            _print_row(f"TsMinDiff(t={w})", n, py_t, cpp_t)


# ── main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import functools
    print = functools.partial(print, flush=True)

    print("=" * 80)
    print(f"  fe_ops P3 Benchmark: C++ vs Python ts_ops")
    print(f"  N_REPEAT={N_REPEAT}, N_WARMUP={N_WARMUP}")
    print(f"  Sizes: {SIZES}")
    print("=" * 80)

    bench_med()
    bench_mad()
    bench_wma()
    bench_maxdiff()
    bench_mindiff()

    print("\n" + "=" * 80)
    print("  Done.")
    print("=" * 80)
