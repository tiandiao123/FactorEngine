#!/usr/bin/env python3
"""
Latency benchmark: single-threaded vs multi-threaded InferenceEngine.

Measures pure C++ push_bars() latency on a warm engine — the metric
that matters in production (one new bar arrives, how fast do we get
all factor outputs for all symbols).

Configurations:
  - Symbols  : 10, 50, 100, 200
  - Threads  : 1, 2, 4, 8
  - Factors  : 5 per symbol (all registered factors)
  - Warmup   : 499 bars,  Measure: last bar × 100 repeats

Usage:
  python tests/runtime_engine/demo_latency.py
"""
import time
import numpy as np
import fe_runtime as rt
from factorengine.factors import FactorRegistry

N_BARS = 500
N_REPEATS = 100
SYMBOL_COUNTS = [10, 50, 100, 200]
THREAD_COUNTS = [1, 2, 4, 8]


def make_ohlcv(seed: int, n: int):
    rng = np.random.RandomState(seed)
    close = 100.0 + np.cumsum(rng.randn(n) * 0.5).astype(np.float32)
    volume = (1000 + rng.rand(n) * 5000).astype(np.float32)
    noise = rng.rand(n).astype(np.float32)
    high = close + np.abs(rng.randn(n) * 0.3).astype(np.float32)
    low = close - np.abs(rng.randn(n) * 0.3).astype(np.float32)
    open_ = low + noise * (high - low)
    ret = np.zeros(n, dtype=np.float32)
    ret[1:] = close[1:] / close[:-1] - 1.0
    return close, volume, open_, high, low, ret


def build_engine(num_symbols: int, num_threads: int, reg: FactorRegistry):
    engine = rt.InferenceEngine(num_threads=num_threads)
    symbols = [f"SYM-{i:04d}" for i in range(num_symbols)]
    for sym in symbols:
        engine.add_symbol(sym)
        for fid, graph in reg.build_all().items():
            engine.add_factor(sym, fid, graph)
    return engine, symbols


def warmup_engine(engine, symbols, data, n_bars):
    """Feed n_bars into engine to fill all rolling windows."""
    for bar_idx in range(n_bars):
        bars = {}
        for sym in symbols:
            c, v, o, h, lo, r = data[sym]
            bars[sym] = rt.BarData(c[bar_idx], v[bar_idx],
                                   o[bar_idx], h[bar_idx],
                                   lo[bar_idx], r[bar_idx])
        engine.push_bars(bars)


def measure_single_bar_latency(engine, symbols, data, bar_idx):
    """Measure a single push_bars() call — pure C++ latency."""
    bars = {}
    for sym in symbols:
        c, v, o, h, lo, r = data[sym]
        bars[sym] = rt.BarData(c[bar_idx], v[bar_idx],
                               o[bar_idx], h[bar_idx],
                               lo[bar_idx], r[bar_idx])

    t0 = time.perf_counter()
    engine.push_bars(bars)
    return (time.perf_counter() - t0) * 1e6  # microseconds


def main():
    reg = FactorRegistry()
    reg.load_all()
    n_factors = len(reg)

    print("=" * 78)
    print("InferenceEngine — Single-bar Latency Benchmark")
    print(f"  Factors/symbol : {n_factors}")
    print(f"  Warmup bars    : {N_BARS - 1}")
    print(f"  Repeats        : {N_REPEATS} (reset + re-warmup each time)")
    print("=" * 78)

    # Collect all results for summary table
    results = []

    for n_sym in SYMBOL_COUNTS:
        data = {f"SYM-{i:04d}": make_ohlcv(seed=i, n=N_BARS)
                for i in range(n_sym)}

        baseline_mean = None

        for n_thr in THREAD_COUNTS:
            latencies = []
            for rep in range(N_REPEATS):
                engine, symbols = build_engine(n_sym, n_thr, reg)
                warmup_engine(engine, symbols, data, N_BARS - 1)
                lat = measure_single_bar_latency(engine, symbols, data, N_BARS - 1)
                latencies.append(lat)

            lat = np.array(latencies)
            mean = lat.mean()
            p50 = np.percentile(lat, 50)
            p99 = np.percentile(lat, 99)

            if baseline_mean is None:
                baseline_mean = mean
            speedup = baseline_mean / mean

            results.append((n_sym, n_thr, mean, p50, p99, speedup))

    # ── Print table ──
    print(f"\n{'symbols':>8} | {'threads':>8} | {'mean (us)':>10} | "
          f"{'p50 (us)':>10} | {'p99 (us)':>10} | {'speedup':>8}")
    print("-" * 70)

    prev_sym = None
    for n_sym, n_thr, mean, p50, p99, speedup in results:
        if prev_sym is not None and n_sym != prev_sym:
            print("-" * 70)
        sp_str = "baseline" if n_thr == 1 else f"{speedup:.2f}x"
        print(f"{n_sym:>8} | {n_thr:>8} | {mean:>10.0f} | "
              f"{p50:>10.0f} | {p99:>10.0f} | {sp_str:>8}")
        prev_sym = n_sym

    print("-" * 70)

    # ── Analysis ──
    print("\nAnalysis:")
    for n_sym in SYMBOL_COUNTS:
        group = [(n_thr, mean, sp) for s, n_thr, mean, _, _, sp in results if s == n_sym]
        best_thr, best_mean, best_sp = min(group, key=lambda x: x[1])
        base_mean = group[0][1]
        print(f"  {n_sym:>3} symbols: best = {best_thr} threads "
              f"({best_mean:.0f} us, {best_sp:.2f}x vs 1-thread {base_mean:.0f} us)")


if __name__ == "__main__":
    main()
