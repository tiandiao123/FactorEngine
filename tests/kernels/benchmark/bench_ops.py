"""
Benchmark: C++ fe_ops vs Python ts_ops.

Compares wall-clock time for each operator across multiple array sizes.
Runs each op N_REPEAT times and reports median time + speedup ratio.

Usage:
    cd FactorEngine
    python tests/kernel/benchmark/bench_ops.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ── load Python reference ops (test-local) ───────────────────
_REF_DIR = str(Path(__file__).resolve().parents[1] / "reference")
if _REF_DIR not in sys.path:
    sys.path.insert(0, _REF_DIR)

from ts_ops import (
    Neg, Abs, Log, Sqr, Inv, Sign, Tanh, SLog1p,
    Add, Sub, Mul, Div,
    Ma, TsSum, TsStd, Ema, TsMin, TsMax, TsRank, TsZscore,
    Delay, TsDiff, TsPct,
)
import fe_ops

# ── config ───────────────────────────────────────────────────

SIZES = [1_440, 4_320, 10_000, 100_000, 1_000_000]
N_REPEAT = 20
N_WARMUP = 3
ROLLING_WINDOWS = [30, 120, 480, 4320]


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
    print(f"  {name:20s}  n={n:>10,}  "
          f"py={_fmt_time(py_t)}  cpp={_fmt_time(cpp_t)}  "
          f"speedup={speedup:6.1f}x", flush=True)


# ── unary benchmarks ────────────────────────────────────────

def bench_unary():
    print("\n" + "=" * 80)
    print("  UNARY OPERATORS")
    print("=" * 80)

    ops = [
        ("Neg",    Neg,    fe_ops.neg),
        ("Abs",    Abs,    fe_ops.abs_op),
        ("Log",    Log,    fe_ops.log_op),
        ("Sqr",    Sqr,    fe_ops.sqr),
        ("Inv",    Inv,    fe_ops.inv),
        ("Sign",   Sign,   fe_ops.sign),
        ("Tanh",   Tanh,   fe_ops.tanh_op),
        ("SLog1p", SLog1p, fe_ops.slog1p),
    ]

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for name, py_fn, cpp_fn in ops:
            py_t = _bench(lambda: py_fn(series))
            cpp_t = _bench(lambda: cpp_fn(arr))
            _print_row(name, n, py_t, cpp_t)


# ── binary benchmarks ───────────────────────────────────────

def bench_binary():
    print("\n" + "=" * 80)
    print("  BINARY OPERATORS")
    print("=" * 80)

    ops = [
        ("Add", Add, fe_ops.add),
        ("Sub", Sub, fe_ops.sub),
        ("Mul", Mul, fe_ops.mul),
        ("Div", Div, fe_ops.div_op),
    ]

    for n in SIZES:
        arr_x, series_x = _make_data(n, seed=42)
        arr_y, series_y = _make_data(n, seed=99)
        scalar = np.float32(2.5)

        print(f"\n  --- n = {n:,} (arr + arr) ---")
        for name, py_fn, cpp_fn in ops:
            py_t = _bench(lambda: py_fn(series_x, series_y))
            cpp_t = _bench(lambda: cpp_fn(arr_x, arr_y))
            _print_row(f"{name}(arr,arr)", n, py_t, cpp_t)

        print(f"\n  --- n = {n:,} (arr + scalar) ---")
        for name, py_fn, cpp_fn in ops:
            py_t = _bench(lambda: py_fn(series_x, scalar))
            cpp_t = _bench(lambda: cpp_fn(arr_x, 2.5))
            _print_row(f"{name}(arr,scl)", n, py_t, cpp_t)


# ── rolling mean benchmarks ─────────────────────────────────

def bench_rolling_mean():
    print("\n" + "=" * 80)
    print("  ROLLING MEAN (Ma)")
    print("=" * 80)

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            py_t = _bench(lambda: Ma(series, w))
            cpp_t = _bench(lambda: fe_ops.rolling_mean(arr, w))
            _print_row(f"Ma(t={w})", n, py_t, cpp_t)


# ── P1 rolling benchmarks ────────────────────────────────────

def bench_rolling_p1():
    print("\n" + "=" * 80)
    print("  P1 ROLLING OPERATORS")
    print("=" * 80)

    ops = [
        ("TsSum",    TsSum,    fe_ops.rolling_sum),
        ("TsStd",    TsStd,    fe_ops.rolling_std),
        ("Ema",      Ema,      fe_ops.ema),
        ("TsMin",    TsMin,    fe_ops.rolling_min),
        ("TsMax",    TsMax,    fe_ops.rolling_max),
        ("TsZscore", TsZscore, fe_ops.rolling_zscore),
    ]

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in ROLLING_WINDOWS:
            if w > n:
                continue
            for name, py_fn, cpp_fn in ops:
                py_t = _bench(lambda: py_fn(series, w))
                cpp_t = _bench(lambda: cpp_fn(arr, w))
                _print_row(f"{name}(t={w})", n, py_t, cpp_t)
            print()


def bench_rolling_rank():
    print("\n" + "=" * 80)
    print("  P1 ROLLING RANK (most expensive)")
    print("=" * 80)

    rank_windows = [30, 60, 120]

    for n in [1_440, 10_000, 100_000]:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for w in rank_windows:
            if w > n:
                continue
            py_t = _bench(lambda: TsRank(series, w))
            cpp_t = _bench(lambda: fe_ops.rolling_rank(arr, w))
            _print_row(f"TsRank(t={w})", n, py_t, cpp_t)


def bench_shift():
    print("\n" + "=" * 80)
    print("  P1 SHIFT OPERATORS (Delay / TsDiff / TsPct)")
    print("=" * 80)

    shift_lags = [1, 5, 30]

    ops = [
        ("Delay",  Delay,  fe_ops.delay),
        ("TsDiff", TsDiff, fe_ops.ts_diff),
        ("TsPct",  TsPct,  fe_ops.ts_pct),
    ]

    for n in SIZES:
        arr, series = _make_data(n)
        print(f"\n  --- n = {n:,} ---")
        for lag in shift_lags:
            for name, py_fn, cpp_fn in ops:
                py_t = _bench(lambda: py_fn(series, lag))
                cpp_t = _bench(lambda: cpp_fn(arr, lag))
                _print_row(f"{name}(t={lag})", n, py_t, cpp_t)
            print()


# ── main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import functools
    print = functools.partial(print, flush=True)

    print("=" * 80)
    print(f"  fe_ops Benchmark: C++ vs Python ts_ops")
    print(f"  N_REPEAT={N_REPEAT}, N_WARMUP={N_WARMUP}")
    print(f"  Sizes: {SIZES}")
    print("=" * 80)

    bench_unary()
    bench_binary()
    bench_rolling_mean()
    bench_rolling_p1()
    bench_rolling_rank()
    bench_shift()

    print("\n" + "=" * 80)
    print("  Done.")
    print("=" * 80)
