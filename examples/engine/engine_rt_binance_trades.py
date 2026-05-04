"""Real-time factor inference demo — Binance Futures aggTrade stream.

Uses exchange="binance", data_source="trades" so only BinanceTradesWorker
runs.  OHLCV is derived from the aggTrade stream via TradesAccumulator.

Binance Futures does NOT support 1s klines, so trades-only is the
primary mode for high-frequency Binance data.

Set testnet=True to use the Binance Futures testnet instead of production.
"""
import logging
import time

from factorengine.engine import Engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

TESTNET = False

engine = Engine(
    symbols=[
        "BTC-USDT", "ETH-USDT", "SOL-USDT",
        "BNB-USDT", "DOGE-USDT", "XRP-USDT",
        "ADA-USDT", "AVAX-USDT", "LINK-USDT",
        "DOT-USDT", "SUI-USDT", "NEAR-USDT",
        "APT-USDT", "ARB-USDT", "OP-USDT",
        "PEPE-USDT", "FIL-USDT", "LTC-USDT",
        "UNI-USDT", "ATOM-USDT",
    ],
    mode="live",
    exchange="binance",
    data_freq="1s",
    factor_group=["okx_perp_trades"],
    num_threads=4,
    data_source="trades",
    testnet=TESTNET,
)

print(f"Exchange:    {engine.exchange}")
print(f"Symbols:     {len(engine.symbols)}")
print(f"Factors:     {len(engine.factor_ids)}")
print(f"Factor IDs:  {engine.factor_ids}")
print(f"Data source: {engine.data_source}")


engine.start()

try:
    for cycle in range(1, 200):
        time.sleep(1)

        factors = engine.get_factor_outputs()

        lines = []
        for sym in engine.symbols:
            fvals = factors.get(sym, {})
            n_valid = sum(1 for v in fvals.values() if v == v)
            lines.append(f"  {sym:<16s}  valid={n_valid}/{len(fvals)}")

        print(
            f"\n═══ Cycle {cycle:3d}  bars_pushed={engine.bars_pushed:4d}  "
            f"trades={engine.trades_stats}  "
            f"clock={engine.clock_stats}"
        )

        debug = engine.last_bar_debug
        if debug:
            print(f"  ── BarData pushed (bar_index={debug['bar_index']}, ts={debug['ts']}) ──")
            for sym, bd in debug["bars"].items():
                print(
                    f"    {sym:<16s}  "
                    f"O={bd['open']:<10.2f} H={bd['high']:<10.2f} "
                    f"L={bd['low']:<10.2f} C={bd['close']:<10.2f} "
                    f"V={bd['volume']:<10.4f} ret={bd['ret']:+.6f}"
                )
                print(
                    f"    {'':16s}  "
                    f"vwap={bd['vwap']:<12.4f} "
                    f"buy_vol={bd['buy_vol']:<10.4f} "
                    f"sell_vol={bd['sell_vol']:<10.4f}"
                )

        for ln in lines:
            print(ln)

        for sym in engine.symbols:
            fvals = factors.get(sym, {})
            if not fvals:
                continue
            items = "  ".join(
                f"{fid}={v:+.4f}" if v == v else f"{fid}=NaN"
                for fid, v in sorted(fvals.items())
            )
            print(f"  {sym:<16s}  {items}")

finally:
    engine.stop()
    print("Done.")
