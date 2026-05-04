"""Benchmark for TS_CCI (Commodity Channel Index)."""

import time

import numpy as np
import pandas as pd

import fe_ops
import fe_runtime as rt


def _pandas_cci(x, w):
    """pandas reference: manual CCI via rolling mean and MAD."""
    s = pd.Series(x.astype(np.float64))
    sma = s.rolling(w).mean()
    mad = s.rolling(w).apply(lambda v: np.mean(np.abs(v - v.mean())), raw=True)
    return ((s - sma) / (0.015 * mad)).values.astype(np.float32)


def bench_batch(n=10_000, w=14, rounds=50):
    rng = np.random.default_rng(0)
    data = rng.standard_normal(n).astype(np.float32).cumsum() + 100.0

    t0 = time.perf_counter()
    for _ in range(rounds):
        fe_ops.rolling_cci(data, w)
    cpp_ms = (time.perf_counter() - t0) / rounds * 1000

    t0 = time.perf_counter()
    for _ in range(rounds):
        _pandas_cci(data, w)
    pd_ms = (time.perf_counter() - t0) / rounds * 1000

    speedup = pd_ms / cpp_ms
    print(f"[batch n={n} w={w}]  C++ {cpp_ms:.3f} ms  |  pandas {pd_ms:.3f} ms  |  {speedup:.1f}x")


def bench_push(n=10_000, w=14, rounds=5):
    rng = np.random.default_rng(0)
    data = rng.standard_normal(n).astype(np.float32).cumsum() + 100.0

    g = rt.FactorGraph()
    c = g.add_input("close")
    g.add_rolling(rt.Op.TS_CCI, c, w)
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
    bench_batch(n=50_000, w=30)
    bench_push()
