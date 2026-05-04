"""Benchmark TS_COS: C++ batch vs manual pandas, FactorGraph push throughput."""

import time
import numpy as np
import pandas as pd

import fe_ops
import fe_runtime as rt


def _pandas_cos(x, y, w):
    sx = pd.Series(x.astype(np.float64))
    sy = pd.Series(y.astype(np.float64))
    sxy = (sx * sy).rolling(w, min_periods=w).sum()
    sxx = (sx * sx).rolling(w, min_periods=w).sum()
    syy = (sy * sy).rolling(w, min_periods=w).sum()
    return (sxy / np.sqrt(sxx * syy)).values


def bench_batch(n=10_000, w=60, rounds=50):
    rng = np.random.default_rng(0)
    x = rng.standard_normal(n).astype(np.float32) + 5.0
    y = rng.standard_normal(n).astype(np.float32) + 3.0
    # warmup
    fe_ops.rolling_cos(x, y, w)
    _pandas_cos(x, y, w)

    t0 = time.perf_counter()
    for _ in range(rounds):
        fe_ops.rolling_cos(x, y, w)
    cpp_ms = (time.perf_counter() - t0) / rounds * 1000

    t0 = time.perf_counter()
    for _ in range(rounds):
        _pandas_cos(x, y, w)
    pd_ms = (time.perf_counter() - t0) / rounds * 1000

    print(f"[batch n={n} w={w}]  C++: {cpp_ms:.3f} ms  pandas: {pd_ms:.3f} ms  speedup: {pd_ms/cpp_ms:.1f}x")


def bench_push(n=2000, w=60, rounds=5):
    rng = np.random.default_rng(1)
    x = rng.standard_normal(n).astype(np.float32) + 5.0
    y = rng.standard_normal(n).astype(np.float32) + 3.0
    g = rt.FactorGraph()
    cx = g.add_input("close")
    cy = g.add_input("volume")
    g.add_bivariate(rt.Op.TS_COS, cx, cy, w)
    g.compile()

    total_ns = 0
    for _ in range(rounds):
        g.reset()
        for xv, yv in zip(x, y):
            t0 = time.perf_counter_ns()
            g.push_bar(float(xv), volume=float(yv))
            _out = g.output()
            total_ns += time.perf_counter_ns() - t0

    avg_ns = total_ns / (n * rounds)
    print(f"[push  n={n} w={w}]  avg push_bar: {avg_ns:.0f} ns  ({n*rounds} calls)")


if __name__ == "__main__":
    bench_batch()
    bench_batch(n=50_000, w=120)
    bench_push()
