"""
Benchmark: rolling_cov — C++ batch vs pandas, FactorGraph TS_COV push.

Usage:
    cd FactorEngine
    python tests/custom_kernels/ts_cov/bench_ts_cov.py
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

import fe_ops
import fe_runtime as rt

SIZES = [1440, 4320, 10000]
WINDOWS = [30, 120, 480]
N_REPEAT = 15
N_WARMUP = 3


def _make_pair(n: int, seed: int = 42):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n).astype(np.float32)
    y = rng.standard_normal(n).astype(np.float32)
    return x, y


def _bench(fn, n_repeat=N_REPEAT, n_warmup=N_WARMUP) -> float:
    for _ in range(n_warmup):
        fn()
    times = []
    for _ in range(n_repeat):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return float(np.median(times))


def _fmt(sec: float) -> str:
    if sec < 1e-3:
        return f"{sec * 1e6:8.1f} µs"
    if sec < 1.0:
        return f"{sec * 1e3:8.2f} ms"
    return f"{sec:8.3f}  s"


def bench_batch():
    print("\n" + "=" * 72)
    print("  BATCH: fe_ops.rolling_cov vs pandas .rolling().cov()")
    print("=" * 72)
    for n in SIZES:
        arr_x, arr_y = _make_pair(n)
        sx = pd.Series(arr_x)
        sy = pd.Series(arr_y)
        print(f"\n  --- n = {n:,} ---")
        for w in WINDOWS:
            if w >= n:
                continue
            py_t = _bench(lambda: sx.rolling(w, min_periods=w).cov(sy))
            cpp_t = _bench(lambda: fe_ops.rolling_cov(arr_x, arr_y, w))
            sp = py_t / cpp_t if cpp_t > 0 else float("inf")
            print(f"  Cov(w={w:>4})  pandas={_fmt(py_t)}  cpp={_fmt(cpp_t)}  speedup={sp:5.1f}x")


def bench_push():
    print("\n" + "=" * 72)
    print("  PUSH: FactorGraph TS_COV (close, volume)")
    print("=" * 72)
    n = 10_000
    x, y = _make_pair(n)
    for w in WINDOWS:
        g = rt.FactorGraph()
        c = g.add_input("close")
        v = g.add_input("volume")
        g.add_bivariate(rt.Op.TS_COV, c, v, w)
        g.compile()
        for i in range(min(500, n)):
            g.push_bar(close=float(x[i]), volume=float(y[i]))
        g.reset()

        t0 = time.perf_counter()
        for i in range(n):
            g.push_bar(close=float(x[i]), volume=float(y[i]))
        elapsed = time.perf_counter() - t0
        avg_ns = elapsed / n * 1e9
        print(f"  TS_COV push (w={w:>4}): avg={avg_ns:8.0f} ns/push  ({n / elapsed:,.0f} push/s)")


if __name__ == "__main__":
    bench_batch()
    bench_push()
