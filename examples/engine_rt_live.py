"""Real-time factor inference demo using OKX live kline stream.

Shows the streaming architecture:
  - KlineWorker collects OKX WebSocket candles → bar_queue
  - Runtime thread: bar_queue → push_bars (C++ parallel) → signal_deque
  - Main thread polls get_factor_outputs() / get_data() at its own pace
"""
import logging
import time

from factorengine.engine import Engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

engine = Engine(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
    mode="live",
    data_freq="1s",
    factor_group="okx_perp",
    num_threads=4,
)

print(f"Symbols: {len(engine.symbols)}")
print(f"Factors: {len(engine.factor_ids)}")
print(f"Factor IDs: {engine.factor_ids[:5]} ...")

engine.start()

try:
    for cycle in range(1, 200):
        time.sleep(1)

        factors = engine.get_factor_outputs()
        snapshot = engine.get_data()

        # per-symbol summary
        lines = []
        for sym in engine.symbols:
            fvals = factors.get(sym, {})
            bars = snapshot.get(sym)
            n_bars = len(bars) if bars is not None else 0
            n_valid = sum(1 for v in fvals.values() if v == v)
            lines.append(f"  {sym:<16s}  cache={n_bars:4d}  valid={n_valid}/{len(fvals)}")

        print(
            f"\n═══ Cycle {cycle:3d}  bars_pushed={engine.bars_pushed:4d}  "
            f"kline={engine.kline_stats}"
        )
        for ln in lines:
            print(ln)

        # every cycle: show top-5 factor values per symbol
        for sym in engine.symbols:
            fvals = factors.get(sym, {})
            if not fvals:
                continue
            top5 = "  ".join(
                f"{fid}={v:+.4f}" if v == v else f"{fid}=NaN"
                for fid, v in list(sorted(fvals.items()))[:5]
            )
            print(f"  {sym:<16s}  {top5}")

        # every 10 cycles: full factor dump for first symbol
        if cycle % 10 == 0:
            sym0 = engine.symbols[0]
            fvals = factors.get(sym0, {})
            if fvals:
                print(f"\n  ── all {len(fvals)} factors for {sym0} ──")
                for i, (fid, val) in enumerate(sorted(fvals.items())):
                    tag = f"{val:+.6f}" if val == val else "     NaN"
                    end = "\n" if (i + 1) % 6 == 0 else ""
                    print(f"  {fid}={tag:>12s}", end=end)
                print()

finally:
    engine.stop()
    print("Done.")
