"""Benchmark for TS_SUM_POS and TS_POS_RATIO."""

import time

import numpy as np
import pandas as pd

import fe_ops
import fe_runtime as rt


def _pandas_sumpos(x, w):
    s = pd.Series(x.astype(np.float64))
    return s.rolling(w).apply(lambda v: np.sum(v[v > 0]), raw=True).values


def _pandas_posratio(x, w):
    s = pd.Series(x.astype(np.float64))
    return s.rolling(w).apply(lambda v: np.sum(v > 0) / len(v), raw=True).values


def bench_batch(name, cpp_fn, pd_fn, n=10_000, w=14, rounds=50):
    rng = np.random.default_rng(0)
    data = rng.standard_normal(n).astype(np.float32)

    t0 = time.perf_counter()
    for _ in range(rounds):
        cpp_fn(data, w)
    cpp_ms = (time.perf_counter() - t0) / rounds * 1000

    t0 = time.perf_counter()
    for _ in range(rounds):
        pd_fn(data, w)
    pd_ms = (time.perf_counter() - t0) / rounds * 1000

    speedup = pd_ms / cpp_ms
    print(f"[{name} batch n={n} w={w}]  C++ {cpp_ms:.3f} ms  |  pandas {pd_ms:.3f} ms  |  {speedup:.1f}x")


def bench_push(name, op, n=10_000, w=14, rounds=5):
    rng = np.random.default_rng(0)
    data = rng.standard_normal(n).astype(np.float32)

    g = rt.FactorGraph()
    c = g.add_input("close")
    g.add_rolling(op, c, w)
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
    print(f"[{name} push  n={n} w={w}]  avg push_bar: {avg_ns:.0f} ns  ({n*rounds} calls)")


if __name__ == "__main__":
    bench_batch("SumPos", fe_ops.rolling_sumpos, _pandas_sumpos)
    bench_batch("PosRatio", fe_ops.rolling_posratio, _pandas_posratio)
    bench_push("SumPos", rt.Op.TS_SUM_POS)
    bench_push("PosRatio", rt.Op.TS_POS_RATIO)
