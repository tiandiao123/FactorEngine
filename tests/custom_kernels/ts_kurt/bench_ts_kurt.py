"""
Benchmark: TS_KURT — C++ vs pandas, batch vs push.

Usage:
    cd FactorEngine
    python tests/custom_kernels/ts_kurt/bench_ts_kurt.py
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

import fe_ops
import fe_runtime as rt


SIZES = [1_440, 4_320, 10_000, 100_000]
WINDOWS = [30, 120, 480]
N_REPEAT = 20
N_WARMUP = 3


def _make_data(n: int, seed: int = 42):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(n).astype(np.float32)


def _bench(fn, n_repeat=N_REPEAT, n_warmup=N_WARMUP) -> float:
    for _ in range(n_warmup):
        fn()
    times = []
    for _ in range(n_repeat):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        times.append(t1 - t0)
    return float(np.median(times))


def _fmt(sec: float) -> str:
    if sec < 1e-3:
        return f"{sec * 1e6:8.1f} µs"
    if sec < 1.0:
        return f"{sec * 1e3:8.2f} ms"
    return f"{sec:8.3f}  s"


def bench_batch():
    print("\n" + "=" * 80)
    print("  BATCH: fe_ops.rolling_kurt vs pandas .rolling().kurt()")
    print("=" * 80)

    for n in SIZES:
        arr = _make_data(n)
        series = pd.Series(arr)
        print(f"\n  --- n = {n:,} ---")
        for w in WINDOWS:
            if w >= n:
                continue
            py_t = _bench(lambda: series.rolling(w, min_periods=w).kurt())
            cpp_t = _bench(lambda: fe_ops.rolling_kurt(arr, w))
            speedup = py_t / cpp_t if cpp_t > 0 else float("inf")
            print(f"  TsKurt(w={w:>4d})  "
                  f"pandas={_fmt(py_t)}  cpp={_fmt(cpp_t)}  "
                  f"speedup={speedup:6.1f}x")


def bench_push():
    print("\n" + "=" * 80)
    print("  PUSH: FactorGraph streaming throughput")
    print("=" * 80)

    n = 10_000
    arr = _make_data(n)

    for w in WINDOWS:
        g = rt.FactorGraph()
        c = g.add_input("close")
        g.add_rolling(rt.Op.TS_KURT, c, w)
        g.compile()

        # warmup
        for i in range(min(500, n)):
            g.push_bar(float(arr[i]))
        g.reset()

        t0 = time.perf_counter()
        for i in range(n):
            g.push_bar(float(arr[i]))
        elapsed = time.perf_counter() - t0

        avg_ns = elapsed / n * 1e9
        throughput = n / elapsed
        print(f"  TsKurt push (w={w:>4d}):  "
              f"avg={avg_ns:8.0f} ns/push  "
              f"throughput={throughput:,.0f} push/s")


def bench_push_in_graph():
    """Benchmark TS_KURT as part of a realistic factor graph."""
    print("\n" + "=" * 80)
    print("  PUSH: TS_KURT in a realistic factor graph")
    print("  Factor: TsZscore(TsKurt(ret, 60), 120)")
    print("=" * 80)

    n = 10_000
    arr = _make_data(n)

    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(rt.Op.LOG, c)
    ret = g.add_rolling(rt.Op.TS_DIFF, logc, 1)
    kurt = g.add_rolling(rt.Op.TS_KURT, ret, 60)
    g.add_rolling(rt.Op.TS_ZSCORE, kurt, 120)
    g.compile()

    t0 = time.perf_counter()
    for i in range(n):
        g.push_bar(float(arr[i]))
    elapsed = time.perf_counter() - t0

    avg_ns = elapsed / n * 1e9
    print(f"  avg={avg_ns:8.0f} ns/push  ({n / elapsed:,.0f} push/s)  "
          f"nodes={g.num_nodes()}  warmup={g.warmup_bars()}")


if __name__ == "__main__":
    bench_batch()
    bench_push()
    bench_push_in_graph()
