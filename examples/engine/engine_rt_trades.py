"""Real-time factor inference demo using OKX kline + trades streams.

Shows the kline_trades architecture:
  - KlineWorker collects OKX WebSocket candles → bar_queue
  - TradesWorker collects OKX trades → per-symbol TradesAccumulators
  - KlinePublisher snapshots accumulators on each flush, producing
    (round_bars, round_trades) tuples
  - Runtime thread: bar_queue → push_bars (C++ parallel) → signal_deque
  - Main thread polls get_factor_outputs() at its own pace

factor_group accepts a single group string or a list of groups:
  - "okx_perp_trades"                   → trades-only factors (T0001–T0006)
  - ["okx_perp", "okx_perp_trades"]     → kline + trades factors combined
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
    data_freq="5s",
    factor_group=["okx_perp", "okx_perp_trades"],
    num_threads=4,
    data_source="kline_trades",
)

print(f"Symbols:     {len(engine.symbols)}")
print(f"Factors:     {len(engine.factor_ids)}")
print(f"Factor IDs:  {engine.factor_ids}")
print(f"Data source: {engine.data_source}")


engine.start()

try:
    for cycle in range(1, 200):
        time.sleep(5)

        factors = engine.get_factor_outputs()

        # per-symbol summary
        lines = []
        for sym in engine.symbols:
            fvals = factors.get(sym, {})
            n_valid = sum(1 for v in fvals.values() if v == v)
            lines.append(f"  {sym:<16s}  valid={n_valid}/{len(fvals)}")

        print(
            f"\n═══ Cycle {cycle:3d}  bars_pushed={engine.bars_pushed:4d}  "
            f"kline={engine.kline_stats}  "
            f"trades={engine.trades_stats}"
        )
        for ln in lines:
            print(ln)

        # every cycle: show all factor values per symbol
        for sym in engine.symbols:
            fvals = factors.get(sym, {})
            if not fvals:
                continue
            items = "  ".join(
                f"{fid}={v:+.4f}" if v == v else f"{fid}=NaN"
                for fid, v in sorted(fvals.items())
            )
            print(f"  {sym:<16s}  {items}")

        # every 10 cycles: detailed breakdown
        if cycle % 10 == 0:
            sym0 = engine.symbols[0]
            fvals = factors.get(sym0, {})
            if fvals:
                print(f"\n  ── all {len(fvals)} factors for {sym0} ──")
                for fid, val in sorted(fvals.items()):
                    tag = f"{val:+.6f}" if val == val else "     NaN"
                    print(f"    {fid:<8s} {tag}")

finally:
    engine.stop()
    print("Done.")
