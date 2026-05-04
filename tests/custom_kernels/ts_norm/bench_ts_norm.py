"""Benchmark TS_NORM: C++ batch vs pandas, FactorGraph push throughput."""

import time
import numpy as np
import pandas as pd

import fe_ops
import fe_runtime as rt


def _pandas_norm(x, w):
    s = pd.Series(x.astype(np.float64))
    rmin = s.rolling(w, min_periods=w).min()
    rmax = s.rolling(w, min_periods=w).max()
    return ((s - rmin) / (rmax - rmin)).values


def bench_batch(n=10_000, w=60, rounds=50):
    data = np.random.default_rng(0).standard_normal(n).astype(np.float32)
    # warmup
    fe_ops.rolling_norm(data, w)
    _pandas_norm(data, w)

    t0 = time.perf_counter()
    for _ in range(rounds):
        fe_ops.rolling_norm(data, w)
    cpp_ms = (time.perf_counter() - t0) / rounds * 1000

    t0 = time.perf_counter()
    for _ in range(rounds):
        _pandas_norm(data, w)
    pd_ms = (time.perf_counter() - t0) / rounds * 1000

    print(f"[batch n={n} w={w}]  C++: {cpp_ms:.3f} ms  pandas: {pd_ms:.3f} ms  speedup: {pd_ms/cpp_ms:.1f}x")


def bench_push(n=2000, w=60, rounds=5):
    data = np.random.default_rng(1).standard_normal(n).astype(np.float32)
    g = rt.FactorGraph()
    c = g.add_input("close")
    g.add_rolling(rt.Op.TS_NORM, c, w)
    g.compile()

    total_ns = 0
    for _ in range(rounds):
        g.reset()
        for v in data:
            t0 = time.perf_counter_ns()
            g.push_bar(float(v))
            _out = g.output()
            total_ns += time.perf_counter_ns() - t0

    avg_ns = total_ns / (n * rounds)
    print(f"[push  n={n} w={w}]  avg push_bar: {avg_ns:.0f} ns  ({n*rounds} calls)")


if __name__ == "__main__":
    bench_batch()
    bench_batch(n=50_000, w=120)
    bench_push()
