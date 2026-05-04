"""Benchmark TS_CV: C++ batch vs pandas, FactorGraph push throughput."""

import time
import numpy as np
import pandas as pd

import fe_ops
import fe_runtime as rt


def _pandas_cv(x, w):
    s = pd.Series(x.astype(np.float64))
    return (s.rolling(w).std(ddof=0) / s.rolling(w).mean().abs()).values


def bench_batch(n=10_000, w=60, rounds=50):
    data = np.random.default_rng(0).standard_normal(n).astype(np.float32) + 5.0
    # warmup
    fe_ops.rolling_cv(data, w)
    _pandas_cv(data, w)

    t0 = time.perf_counter()
    for _ in range(rounds):
        fe_ops.rolling_cv(data, w)
    cpp_ms = (time.perf_counter() - t0) / rounds * 1000

    t0 = time.perf_counter()
    for _ in range(rounds):
        _pandas_cv(data, w)
    pd_ms = (time.perf_counter() - t0) / rounds * 1000

    print(f"[batch n={n} w={w}]  C++: {cpp_ms:.3f} ms  pandas: {pd_ms:.3f} ms  speedup: {pd_ms/cpp_ms:.1f}x")


def bench_push(n=2000, w=60, rounds=5):
    data = np.random.default_rng(1).standard_normal(n).astype(np.float32) + 5.0
    g = rt.FactorGraph()
    c = g.add_input("close")
    g.add_rolling(rt.Op.TS_CV, c, w)
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
