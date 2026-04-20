"""Real-time factor inference demo using simulation dataflow.

Shows the new push-based architecture:
  - SimBarWorker generates bars and directly pushes to InferenceEngine
  - Factor outputs are stored in a signal_deque (maxlen=3)
  - The main thread reads from the deque at its own pace
"""
import time

from factorengine.engine import Engine
from dataflow.simulation.symbols import DEFAULT_SYMBOLS, EXTENDED_SYMBOLS

engine = Engine(
    symbols=EXTENDED_SYMBOLS,
    mode="simulation",
    sim_bar_interval=0.02,
    sim_seed=42,
    bar_window_length=2000,
    factor_group="okx_perp",
    num_threads=4,
    signal_buffer_size=3,
    bar_queue_timeout=0.3,
)

print(f"Symbols: {len(engine.symbols)}")
print(f"Factors: {len(engine.factor_ids)}")
print(f"Factor IDs: {engine.factor_ids}")

engine.start()

try:
    for cycle in range(1, 1000):
        time.sleep(1.0)

        factors = engine.get_factor_outputs()
        snapshot = engine.get_data()

        if cycle == 1:
            print(f"\n--- factors debug ---")
            print(f"  type: {type(factors).__name__}")
            print(f"  num symbols: {len(factors)}")
            if factors:
                first_sym = next(iter(factors))
                fv = factors[first_sym]
                print(f"  first symbol: {first_sym}")
                print(f"  num factors: {len(fv)}")
                print(f"  factor ids (first 5): {list(fv.keys())[:5]}")
                print(f"  values (first 5): {[round(v, 6) if v == v else 'NaN' for v in list(fv.values())[:5]]}")
            print(f"--- snapshot debug ---")
            print(f"  type: {type(snapshot).__name__}")
            print(f"  num symbols: {len(snapshot)}")
            if snapshot:
                first_sym_s = next(iter(snapshot))
                arr = snapshot[first_sym_s]
                print(f"  first symbol: {first_sym_s}")
                print(f"  array shape: {arr.shape}")
                print(f"  dtype: {arr.dtype}")
                print(f"  columns: [ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]")
            print()

        sym = engine.symbols[0]
        bars = snapshot.get(sym)
        n_bars = len(bars) if bars is not None else 0
        fvals = factors.get(sym, {})
        n_valid = sum(1 for v in fvals.values() if v == v)  # non-NaN

        sample = {fid: (f"{v:.6f}" if v == v else "NaN") for fid, v in list(fvals.items())[:10]}
        print(
            f"Cycle {cycle:2d}: bars_pushed={engine.bars_pushed:5d}  "
            f"cache={n_bars:4d}  valid_factors={n_valid}/{len(fvals)}  "
            f"deque_len={len(engine.signal_deque)}"
        )
        print(f"  sample(10): {sample}")

        if n_valid > 0 and cycle % 5 == 0:
            print(f"  ── all 60 factors for {sym} ──")
            for i, (fid, val) in enumerate(sorted(fvals.items())):
                tag = f"{val:.6f}" if val == val else "NaN"
                end = "\n" if (i + 1) % 6 == 0 else ""
                print(f"  {fid}={tag:>12s}", end=end)
            print()
            

finally:
    engine.stop()
    print("Done.")
